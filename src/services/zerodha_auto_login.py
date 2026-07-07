"""
Headless Zerodha Kite auto-login via Playwright.

Flow:
  1. Check if existing token is still valid for today (skip if so, unless forced)
  2. Read broker config (api_key, api_secret, user_id, encrypted password, encrypted totp_secret)
  3. Generate TOTP from stored secret
  4. Launch headless Chromium via Playwright
  5. Navigate to https://kite.zerodha.com/connect/login?api_key=XXXX
  6. Enter user_id → Continue, password → Login, TOTP → Verify
  7. Capture request_token from the OAuth redirect URL
  8. Exchange request_token for access_token via kiteconnect.generate_session()
  9. Persist access_token to DB, invalidate cached Kite client
  10. Return success/failure dict

Dependencies:
  - playwright (pip install playwright && playwright install chromium)
  - kiteconnect
  - pyotp
"""

import logging
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Max attempts per stage before giving up
_MAX_RETRIES = 2
_TOTP_RETRIES = 2
_PAGE_TIMEOUT_MS = 45_000
_NAV_TIMEOUT_MS = 30_000

# Combined Playwright selector for TOTP input on Kite's 2FA page
_TOTP_SELECTOR_STRING = ", ".join(
    [
        "#pin",
        "#totp",
        "input[name='pin']",
        "input[name='totp']",
        "input[name='twofa']",
        "input[name='2fa']",
        "input[type='tel']",
        "input[type='number']",
        "input[placeholder='••••••']",
        "input[placeholder*='OTP']",
        "input[placeholder*='2FA']",
        "input[placeholder*='TOTP']",
        "input[placeholder*='code']",
        "input[placeholder*='authenticator']",
    ]
)


def auto_login_kite(force: bool = False) -> dict:
    """
    Attempt to login to Zerodha Kite headlessly.

    Args:
        force: If True, re-login even if an existing token is still valid today.

    Returns:
        dict with keys:
          - success (bool)
          - message (str)
          - action (str): "SKIPPED_ALREADY_LOGGED_IN" | "SKIPPED_NO_CREDENTIALS"
                          | "SKIPPED_WEEKEND" | "SKIPPED_HOLIDAY"
                          | "LOGIN_SUCCESS" | "FAILED"
    """
    # ── 1. Check if already logged in today ──────────────────────────────
    from src.services.zerodha_auth import is_token_valid

    if is_token_valid() and not force:
        log.info("[auto_login] Kite token is still valid for today — skipping")
        return {
            "success": True,
            "message": "Kite token is still valid for today",
            "action": "SKIPPED_ALREADY_LOGGED_IN",
        }

    # ── 2. Read broker config ────────────────────────────────────────────
    from src.models.schema import get_broker_config

    config = get_broker_config()
    if not config:
        return {
            "success": False,
            "message": "No broker config found in database",
            "action": "SKIPPED_NO_CREDENTIALS",
        }

    api_key = config.get("api_key", "")
    api_secret = config.get("api_secret", "")
    user_id = config.get("user_id", "")
    password_enc = config.get("password", "")
    totp_enc = config.get("totp_secret", "")

    if not api_key or not api_secret:
        return {
            "success": False,
            "message": "Zerodha API key or secret not configured",
            "action": "SKIPPED_NO_CREDENTIALS",
        }
    if not user_id:
        return {
            "success": False,
            "message": "Zerodha user ID (client ID) not configured",
            "action": "SKIPPED_NO_CREDENTIALS",
        }
    if not password_enc:
        return {
            "success": False,
            "message": "Zerodha password not configured",
            "action": "SKIPPED_NO_CREDENTIALS",
        }
    if not totp_enc:
        return {
            "success": False,
            "message": "Zerodha TOTP secret not configured",
            "action": "SKIPPED_NO_CREDENTIALS",
        }

    # ── 3. Decrypt password and TOTP secret ──────────────────────────────
    from src.services.zerodha_auth import decrypt_secret

    password = decrypt_secret(password_enc)
    if not password:
        return {
            "success": False,
            "message": "Failed to decrypt stored password — Fernet key may have changed",
            "action": "FAILED",
        }
    totp_secret = decrypt_secret(totp_enc)
    if not totp_secret:
        return {
            "success": False,
            "message": "Failed to decrypt stored TOTP secret — Fernet key may have changed",
            "action": "FAILED",
        }

    # ── 4. Check if Kite is reachable ────────────────────────────────────
    from src.engine.live_trading import _kite_host_reachable

    if not _kite_host_reachable():
        log.warning("[auto_login] api.kite.trade is not reachable — skipping login")
        return {
            "success": False,
            "message": "api.kite.trade DNS resolution failed — network unreachable",
            "action": "FAILED",
        }

    # ── 5. Execute headless login ────────────────────────────────────────
    last_error = ""
    for attempt in range(_MAX_RETRIES):
        if attempt > 0:
            wait = attempt * 10
            log.info(
                "[auto_login] Retry %d/%d in %ds...", attempt + 1, _MAX_RETRIES, wait
            )
            time.sleep(wait)

        # Generate fresh TOTP for each attempt
        import pyotp

        try:
            totp_code = pyotp.TOTP(totp_secret.strip().replace(" ", "")).now()
        except Exception as e:
            log.error("[auto_login] TOTP generation failed: %s", e)
            return {
                "success": False,
                "message": f"TOTP generation failed: {e}",
                "action": "FAILED",
            }

        request_token = _extract_request_token_via_playwright(
            user_id=user_id,
            password=password,
            totp=totp_code,
            api_key=api_key,
        )
        if request_token:
            # ── 6. Check if dashboard callback already exchanged the token ──
            from src.services.zerodha_auth import is_token_valid

            if is_token_valid():
                log.info(
                    "[auto_login] Access token already valid — dashboard callback handled exchange"
                )
                return {
                    "success": True,
                    "message": "Kite auto-login successful (token exchanged by dashboard callback)",
                    "action": "LOGIN_SUCCESS",
                }

            # ── 7. Exchange request_token for access_token ──────────────────
            result = _exchange_request_token(request_token, api_key, api_secret)
            if result["success"]:
                return result
            last_error = result["message"]
        else:
            last_error = "Failed to extract request_token from browser redirect"

    return {
        "success": False,
        "message": f"Auto-login failed after {_MAX_RETRIES} attempts: {last_error}",
        "action": "FAILED",
    }


def _extract_request_token_via_playwright(
    user_id: str,
    password: str,
    totp: str,
    api_key: str,
) -> str | None:
    """
    Launch headless Chromium, navigate to Kite Connect login, fill credentials
    and TOTP, then capture the request_token from the OAuth redirect URL.

    Returns:
        request_token string, or None on failure.
    """
    try:
        from playwright.sync_api import TimeoutError as PwTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(
            "[auto_login] playwright is not installed. "
            "Install it with: pip install playwright && playwright install chromium"
        )
        return None

    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"

    log.info("[auto_login] Launching headless Chromium for Kite login...")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                timeout=_PAGE_TIMEOUT_MS,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            # ── Navigate to login page ────────────────────────────────────
            log.info("[auto_login] Navigating to %s", login_url)
            try:
                page.goto(
                    login_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
                )
            except PwTimeout:
                log.error("[auto_login] Timeout loading login page")
                browser.close()
                return None
            except Exception as e:
                log.error("[auto_login] Failed to load login page: %s", e)
                browser.close()
                return None

            # Small wait for JS rendering
            page.wait_for_timeout(2000)

            # ── Kite Connect login flow (current: userid + password on same page, TOTP on 2FA page) ──
            # Step 1: Fill user_id
            if not _fill_field(page, "user_id", user_id, "User ID"):
                log.error("[auto_login] Could not find user_id field")
                browser.close()
                return None

            # Step 2: Fill password (now on the same page as userid in Kite's current UI)
            if not _fill_field(page, "password", password, "Password"):
                log.error("[auto_login] Could not find password field")
                browser.close()
                return None

            # Step 3: Click the Login button (submits userid + password together)
            _click_submit_button(page, "password")

            # Step 4: Wait for navigation to 2FA page (URL changes to include sess_id)
            try:
                page.wait_for_timeout(2000)
                page.wait_for_load_state("networkidle", timeout=15000)
            except PwTimeout:
                pass

            # Check if we already got redirected (no TOTP needed in some flows)
            if "request_token" in page.url:
                token = _parse_request_token(page.url)
                if token:
                    log.info(
                        "[auto_login] Got request_token after password step (no TOTP)"
                    )
                    browser.close()
                    return token

            # Step 5: Wait for TOTP field to appear on the 2FA page
            try:
                page.wait_for_selector(
                    _TOTP_SELECTOR_STRING,
                    timeout=_NAV_TIMEOUT_MS,
                )
            except PwTimeout:
                # Try detecting credential errors on the login page
                try:
                    body_text = page.inner_text("body", timeout=3000)
                    if (
                        "invalid" in body_text.lower()
                        or "incorrect" in body_text.lower()
                    ):
                        log.error(
                            "[auto_login] Login page shows credential error — check user_id/password"
                        )
                except Exception:
                    pass
                log.error("[auto_login] Could not find TOTP field on 2FA page")
                browser.close()
                return None

            # Step 6: Fill TOTP code
            if not _fill_field(page, "totp", totp, "TOTP"):
                log.error("[auto_login] Could not find TOTP field on 2FA page")
                browser.close()
                return None

            # Step 7: Click submit and intercept the callback redirect
            # Try clicking "Continue" button first, fall back to Enter
            token = None
            try:
                with page.expect_navigation(
                    url="**request_token**",
                    wait_until="commit",
                    timeout=_NAV_TIMEOUT_MS,
                ) as nav_info:
                    _click_submit_button(page, "totp")
                # Navigation committed — extract URL before follow-through
                nav_url = nav_info.value.url
                log.info("[auto_login] Callback navigation URL: %s", nav_url)
                token = _parse_request_token(nav_url)
                if token:
                    log.info("[auto_login] Got request_token from callback redirect")
                    browser.close()
                    return token
            except PwTimeout:
                log.warning(
                    "[auto_login] No callback redirect detected after TOTP submit"
                )
            except Exception as e:
                log.warning("[auto_login] Navigation error: %s", e)

            # Fallback: check current URL
            final_url = page.url
            log.info("[auto_login] Final URL (fallback): %s", final_url)
            token = _parse_request_token(final_url)
            if token:
                browser.close()
                return token

            # ── Last resort: check for error messages ────────────────────
            try:
                body_text = page.inner_text("body", timeout=3000)
                if "invalid" in body_text.lower() or "incorrect" in body_text.lower():
                    log.error("[auto_login] 2FA page shows error — check TOTP secret")
            except Exception:
                pass

            browser.close()
            return None

    except Exception as e:
        log.error("[auto_login] Playwright automation failed: %s", e, exc_info=True)
        return None


def _fill_field(page, field_type: str, value: str, label: str) -> bool:
    """
    Try multiple CSS/text selectors to find and fill a form field.
    Returns True if the field was found and filled.
    """
    selector = _selector_for(field_type)
    for sel in selector:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.click()
                page.wait_for_timeout(200)
                el.fill(value)
                log.info("[auto_login] Filled %s using selector '%s'", label, sel)
                return True
        except Exception:
            continue

    # Try placeholder-based detection as last resort
    try:
        el = page.locator(f"input[placeholder*='{label.lower()}']").first
        if el.is_visible(timeout=1000):
            el.fill(value)
            log.info("[auto_login] Filled %s via placeholder match", label)
            return True
    except Exception:
        pass

    return False


def _click_submit_button(page, after_field: str):
    """Click the submit/continue button after filling a field."""
    # Try common button text patterns
    texts = {
        "user_id": ["Continue", "Next", "Submit"],
        "password": ["Login", "Sign in", "Submit", "Continue"],
        "totp": ["Verify", "Submit", "Continue", "Login"],
    }
    for text in texts.get(after_field, ["Submit", "Continue", "Login", "Verify"]):
        try:
            btn = page.locator(f"button:has-text('{text}')").first
            if btn.is_visible(timeout=1000):
                btn.click()
                log.info("[auto_login] Clicked '%s' button after %s", text, after_field)
                return
        except Exception:
            continue

    # Try input[type=submit]
    try:
        btn = page.locator("input[type='submit']").first
        if btn.is_visible(timeout=1000):
            btn.click()
            log.info("[auto_login] Clicked input[type=submit] after %s", after_field)
            return
    except Exception:
        pass

    # Try pressing Enter
    try:
        page.keyboard.press("Enter")
        log.info("[auto_login] Pressed Enter after %s", after_field)
    except Exception:
        pass


def _selector_for(field_type: str) -> list[str]:
    """Return an ordered list of CSS/text selectors to try for a given field."""
    selectors = {
        "user_id": [
            "#userid",
            "input[name='user_id']",
            "input[name='userid']",
            "input[data-test='user_id']",
            "input[autocomplete='username']",
            "input[type='text']:not([disabled])",
        ],
        "password": [
            "#password",
            "input[name='password']",
            "input[type='password']",
            "input[autocomplete='current-password']",
        ],
        "totp": [
            "#pin",
            "#totp",
            "input[name='pin']",
            "input[name='totp']",
            "input[name='twofa']",
            "input[name='2fa']",
            "input[data-test='totp']",
            "input[type='tel']",
            "input[type='number']",
            "input[placeholder='••••••']",
            "input[placeholder*='OTP']",
            "input[placeholder*='2FA']",
            "input[placeholder*='TOTP']",
            "input[placeholder*='code']",
            "input[placeholder*='authenticator']",
            "input[type='text']:not([disabled])",
        ],
    }
    return selectors.get(field_type, ["input:not([type='hidden'])"].copy())


def _parse_request_token(url: str) -> str | None:
    """Extract request_token from a URL query string."""
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    tokens = params.get("request_token", [])
    if tokens:
        return tokens[0]
    return None


def _exchange_request_token(request_token: str, api_key: str, api_secret: str) -> dict:
    """
    Exchange a request_token for an access_token via KiteConnect.generate_session().

    Returns:
        dict with success (bool), message (str), possibly action (str).
    """
    from src.models.schema import update_broker_config

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        return {
            "success": False,
            "message": "kiteconnect is not installed",
        }

    try:
        kite = KiteConnect(api_key=api_key)
        from src.utils.tls_adapter import mount_resilient_tls

        try:
            mount_resilient_tls(kite.reqsession)
        except Exception as e:
            log.warning("[auto_login] TLS adapter mount failed: %s", e)

        # Retry generate_session (handles transient SSL EOFs)
        session = None
        last_err = None
        for attempt in range(1, 4):
            try:
                session = kite.generate_session(request_token, api_secret=api_secret)
                break
            except Exception as err:
                last_err = err
                err_msg = str(err).lower()
                if "token is invalid" in err_msg or "token" in err_msg:
                    break
                log.warning(
                    "[auto_login] generate_session attempt %d/3 failed: %s",
                    attempt,
                    err,
                )
                time.sleep(1)

        if not session:
            raise last_err or Exception("generate_session returned no session")

        access_token = session["access_token"]
        today = datetime.now(IST).strftime("%Y-%m-%d")

        update_broker_config(
            access_token=access_token,
            request_token=request_token,
            last_login_date=today,
        )

        # Invalidate cached Kite client so live_trading picks up the new token
        try:
            from src.engine.live_trading import clear_kite_client_cache

            clear_kite_client_cache()
        except Exception:
            pass

        log.info(
            "[auto_login] Kite auto-login successful — token updated for %s", today
        )
        return {
            "success": True,
            "message": f"Kite auto-login successful for {today}",
            "action": "LOGIN_SUCCESS",
        }

    except Exception as e:
        log.error("[auto_login] Token exchange failed: %s", e)
        return {
            "success": False,
            "message": f"Token exchange failed: {e}",
        }
