"""
Paytm Money headless OAuth2 authentication using Playwright.

Flow:
  1. Navigate to OAuth authorize URL with api_key
  2. Auto-fill credentials (email + password)
  3. Capture request_token from redirect URL
  4. Exchange request_token for JWT using API key + secret
  5. Return JWT token
"""

import logging
import os
import re
from config.settings import _optional_env

log = logging.getLogger(__name__)


def _get_paytm_jwt_headless(api_key: str, api_secret: str, email: str, password: str) -> str | None:
    """
    Headless browser Paytm OAuth login.
    Returns JWT token or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(
            "[paytm_auth] playwright not installed. Run: pip install playwright && playwright install chromium"
        )
        return None

    authorize_url = f"https://developer.paytmmoney.com/accounts/v2/authorize?api_key={api_key}"
    log.info("[paytm_auth] Launching headless browser for OAuth login...")
    request_token: str | None = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            captured_urls: list[str] = []

            # Block images/fonts for speed
            def handle_route(route):
                if route.request.resource_type in ("image", "font"):
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", handle_route)

            # Capture URLs with request_token
            page.on(
                "request",
                lambda r: captured_urls.append(r.url) if "request_token=" in r.url else None,
            )
            page.on(
                "response",
                lambda r: captured_urls.append(r.url) if "request_token=" in r.url else None,
            )

            # Navigate to OAuth URL
            page.goto(authorize_url, wait_until="commit")
            log.debug("[paytm_auth] Landed on: %s", page.url)

            # Paytm Money login accepts phone-first, email, or generic text inputs.
            # The selector covers all known variants of their OAuth page.
            _INPUT_SELECTOR = (
                'input[type="email"], '
                'input[type="tel"], '
                'input[type="text"], '
                'input[name*="phone"], '
                'input[name*="mobile"], '
                'input[name*="email"], '
                'input[placeholder*="phone" i], '
                'input[placeholder*="mobile" i], '
                'input[placeholder*="email" i]'
            )
            try:
                page.wait_for_selector(_INPUT_SELECTOR, state="visible", timeout=30000)
            except Exception as sel_err:
                # Dump page source for diagnosis and bail
                try:
                    html_snippet = page.content()[:2000]
                except Exception:
                    html_snippet = "<unavailable>"
                log.error(
                    "[paytm_auth] Login input not found after 30s. URL: %s\nPage HTML (first 2000 chars):\n%s",
                    page.url, html_snippet,
                )
                browser.close()
                return None

            # Fill credentials — try email first, then tel/text (phone-first flow)
            email_inputs = page.locator('input[type="email"]')
            tel_inputs   = page.locator('input[type="tel"]')
            if email_inputs.count() > 0:
                email_inputs.first.fill(email)
            elif tel_inputs.count() > 0:
                # Phone-first flow: fill phone number (use email field value which
                # may be a phone number depending on user config)
                tel_inputs.first.fill(email)
            else:
                page.locator('input[type="text"]').first.fill(email)

            password_inputs = page.locator('input[type="password"]')
            if password_inputs.count() > 0:
                password_inputs.first.fill(password)

            # Click login button
            try:
                login_button = page.locator('button:has-text("Login"), button:has-text("SIGN IN"), button:has-text("Sign In"), [type="submit"]')
                if login_button.count() > 0:
                    login_button.first.click()
                    # Wait for redirect
                    page.wait_for_url("*request_token=*", timeout=45000)
            except Exception as click_err:
                log.debug("[paytm_auth] Click/redirect error (may still have redirected): %s", click_err)

            final_url = page.url
            log.debug("[paytm_auth] Post-login URL: %s", final_url)
            browser.close()

            # Extract request_token from URL candidates
            for candidate in [final_url] + captured_urls:
                m = re.search(r"[?&]request_token=([A-Za-z0-9_\-]+)", candidate)
                if m:
                    request_token = m.group(1)
                    log.info("[paytm_auth] request_token captured successfully")
                    break

            if not request_token:
                log.error(
                    "[paytm_auth] request_token not found. Final URL: %s, Captured: %s",
                    final_url,
                    captured_urls,
                )
                return None

    except Exception as exc:
        log.exception("[paytm_auth] Playwright OAuth login failed: %s", exc)
        return None

    # Exchange request_token for JWT
    if request_token:
        log.info("[paytm_auth] Exchanging request_token for JWT...")
        from src.fetchers.paytm_fetcher import PaytmFetcher

        fetcher = PaytmFetcher()
        if fetcher._refresh_token(request_token):
            log.info("[paytm_auth] JWT obtained successfully")
            return fetcher._jwt_token

    return None


def get_paytm_jwt_auto(force_fresh: bool = False) -> str | None:
    """
    Get Paytm JWT token with auto-refresh if headless auth credentials available.
    
    Args:
        force_fresh: If True, always re-authenticate instead of using cached token
        
    Returns:
        JWT token or None
    """
    from src.fetchers.paytm_fetcher import PaytmFetcher

    fetcher = PaytmFetcher()

    # If token exists and not forced, return it
    if fetcher._jwt_token and not force_fresh:
        log.debug("[paytm_auth] Using existing JWT token from .env")
        return fetcher._jwt_token

    # Try headless auth
    paytm_email = _optional_env("PAYTM_EMAIL")
    paytm_password = _optional_env("PAYTM_PASSWORD")

    if not paytm_email or not paytm_password:
        log.warning(
            "[paytm_auth] PAYTM_EMAIL and PAYTM_PASSWORD not set — cannot auto-refresh JWT"
        )
        return fetcher._jwt_token

    jwt = _get_paytm_jwt_headless(
        fetcher._api_key, fetcher._api_secret, paytm_email, paytm_password
    )

    if jwt:
        fetcher._jwt_token = jwt
        # Auto-update .env if possible
        try:
            from pathlib import Path

            env_path = Path(__file__).resolve().parent.parent.parent / ".env"
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
                lines = content.splitlines()
                updated = False
                for idx, line in enumerate(lines):
                    if line.strip().startswith("PAYTM_JWT_TOKEN="):
                        lines[idx] = f"PAYTM_JWT_TOKEN={jwt}"
                        updated = True
                        break
                if not updated:
                    lines.append(f"PAYTM_JWT_TOKEN={jwt}")
                env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                log.info("[paytm_auth] Auto-updated PAYTM_JWT_TOKEN in .env")
        except Exception as e:
            log.warning("[paytm_auth] Failed to auto-update .env: %s", e)

        return jwt

    return fetcher._jwt_token
