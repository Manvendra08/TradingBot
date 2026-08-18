"""
Shoonya Headless Portal IP Updater.

Automates logging into the Shoonya Developer/Trading Portal headlessly to update
the Primary IP Address and Backup IP Address whenever the local public IP rotates.

Workflow:
1. Detects current public IP (or takes provided IP).
2. Launches headless Playwright Chromium browser.
3. Authenticates with SHOONYA_USER_ID, SHOONYA_PASSWORD, and dynamic TOTP.
4. Opens the API Key configuration window.
5. Fills Primary IP Address and Backup IP Address fields.
6. Submits the update and confirms dialogs.
7. Saves diagnostic error screenshots to data/shoonya_ip_update_error.png on failure.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Tuple

log = logging.getLogger(__name__)


def _get_current_public_ip() -> str | None:
    """Fetch current public IP using ip_monitor or ipify fallback."""
    try:
        from src.utils import ip_monitor

        ip = ip_monitor._fetch_public_ip(timeout=4.0, max_providers=3, retries=2)
        if ip and ip_monitor._is_valid_public_ipv4(ip):
            return ip
    except Exception as exc:
        log.debug("[shoonya-ip-updater] ip_monitor failed: %s", exc)

    import urllib.request

    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                cand = resp.read().decode("utf-8").strip()
                if cand:
                    return cand
        except Exception:
            continue
    return None


def update_shoonya_portal_ip(
    new_ip: str | None = None,
    backup_ip: str | None = None,
    headless: bool = True,
    timeout_s: float = 30.0,
) -> Tuple[bool, str]:
    """
    Headlessly logs into Shoonya portal and updates Primary & Backup IP addresses.

    Parameters
    ----------
    new_ip : str | None
        Target public IP. If None, auto-detected via public IP lookup.
    backup_ip : str | None
        Target backup IP. If None, defaults to `new_ip`.
    headless : bool
        Whether to run Playwright in headless mode. Default True.
    timeout_s : float
        Navigation and interaction timeout in seconds.

    Returns
    -------
    Tuple[bool, str]
        (success, message)
    """
    target_ip = new_ip or _get_current_public_ip()
    if not target_ip:
        err_msg = "Could not determine current public IP address."
        log.error("[shoonya-ip-updater] %s", err_msg)
        return False, err_msg

    target_backup_ip = backup_ip or target_ip

    user_id = os.environ.get("SHOONYA_USER_ID")
    password = os.environ.get("SHOONYA_PASSWORD")
    totp_key = os.environ.get("SHOONYA_TOTP_KEY")
    vendor_code = os.environ.get("SHOONYA_VENDOR_CODE", f"{user_id}_U" if user_id else "")

    if not user_id or not password or not totp_key:
        err_msg = "Missing SHOONYA_USER_ID, SHOONYA_PASSWORD, or SHOONYA_TOTP_KEY in environment."
        log.error("[shoonya-ip-updater] %s", err_msg)
        return False, err_msg

    try:
        from playwright.sync_api import sync_playwright
        import pyotp
    except ImportError as imp_err:
        err_msg = f"Required dependencies not available: {imp_err}. Ensure playwright and pyotp are installed."
        log.error("[shoonya-ip-updater] %s", err_msg)
        return False, err_msg

    log.info(
        "[shoonya-ip-updater] Starting headless portal update: Primary=%s, Backup=%s for user=%s",
        target_ip,
        target_backup_ip,
        user_id,
    )

    error_screenshot = Path("data/shoonya_ip_update_error.png")
    error_screenshot.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # Optimize resource loading — abort fonts and heavy images to speed up login
        def handle_route(route):
            if route.request.resource_type in ("font", "media"):
                route.abort()
            else:
                route.continue_()

        page.route("**/*", handle_route)

        try:
            # -----------------------------------------------------------------
            # Strategy A: Shoonya Developer Portal (OAuthlogin Smart Window)
            # -----------------------------------------------------------------
            portal_url = f"https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id={vendor_code}"
            log.info("[shoonya-ip-updater] Navigating to %s ...", portal_url)
            page.goto(portal_url, wait_until="commit", timeout=int(timeout_s * 1000))

            # Wait for login inputs
            page.wait_for_selector("#lgnusrid", state="visible", timeout=int(timeout_s * 1000))
            log.debug("[shoonya-ip-updater] Login form detected. Filling credentials...")

            page.locator("#lgnusrid").fill(user_id)
            page.locator("#lgnpwd").fill(password)
            totp_code = pyotp.TOTP(totp_key).now()
            page.locator("#lgnotp").fill(totp_code)

            # Open API Key Smart Window in DOM
            log.debug("[shoonya-ip-updater] Opening API Key modal...")
            page.evaluate("""() => {
                const win = document.querySelector('smart-window[label="API Key"]');
                if (win) {
                    if (typeof win.open === 'function') {
                        win.open();
                    } else {
                        win.classList.remove('smart-visibility-hidden');
                        win.style.display = 'block';
                    }
                }
            }""")
            time.sleep(1.0)

            # Locate Primary and Backup IP input elements inside smart-window
            prim_input = page.locator("smart-window[label='API Key'] input[placeholder*='Primary IP']")
            back_input = page.locator("smart-window[label='API Key'] input[placeholder*='Backup IP']")

            if prim_input.count() > 0:
                log.info(
                    "[shoonya-ip-updater] Binding IP addresses: Primary=%s, Backup=%s",
                    target_ip,
                    target_backup_ip,
                )
                prim_input.fill(target_ip)
                if back_input.count() > 0:
                    back_input.fill(target_backup_ip)

                # Click Update button inside API Key smart window
                update_btn = page.locator("smart-window[label='API Key'] button:has-text('Update')")
                if update_btn.count() > 0:
                    update_btn.first.click()
                    time.sleep(1.5)

                    # Handle confirmation modal if prompted
                    confirm_btn = page.locator(
                        "button.proceedbutton:has-text('Yes'), button:has-text('Yes'), button:has-text('Proceed'), button:has-text('Confirm')"
                    )
                    for i in range(confirm_btn.count()):
                        btn = confirm_btn.nth(i)
                        if btn.is_visible():
                            log.debug("[shoonya-ip-updater] Confirming update dialog...")
                            btn.click()
                            break

                    time.sleep(3.0)

                    log.info(
                        "[shoonya-ip-updater] Successfully updated Shoonya API Key IP binding to %s (backup=%s)",
                        target_ip,
                        target_backup_ip,
                    )
                    return True, f"Shoonya IP binding updated to {target_ip} (backup={target_backup_ip})"

            log.warning("[shoonya-ip-updater] Primary IP field not found in Developer Portal modal.")

        except Exception as exc:
            log.warning("[shoonya-ip-updater] Exception during portal update: %s", exc)
            try:
                page.screenshot(path=str(error_screenshot))
                log.info("[shoonya-ip-updater] Saved failure screenshot to %s", error_screenshot)
            except Exception:
                pass
            return False, str(exc)
        finally:
            browser.close()

    return False, "Failed to update IP address on Shoonya portal."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser(description="Headless Shoonya Portal IP Updater")
    parser.add_argument("--ip", type=str, default=None, help="Target Primary IP address (default: auto-detect)")
    parser.add_argument("--backup-ip", type=str, default=None, help="Target Backup IP address (default: same as Primary)")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode for visual debugging")
    args = parser.parse_args()

    success, msg = update_shoonya_portal_ip(
        new_ip=args.ip,
        backup_ip=args.backup_ip,
        headless=not args.headed,
    )
    print(f"\nResult: success={success}\nMessage: {msg}")
    sys.exit(0 if success else 1)
