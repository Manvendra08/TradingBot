"""
Dhan Authentication CLI Utility.

Launches a HEADED Playwright browser with the persistent profile so the user
can complete QR/OTP login on Dhan's options-trader site.  After successful
login the browser session is persisted to DHAN_PROFILE_DIR.

Usage:
    python tools/authenticate_dhan.py

The script waits until the user closes the browser window or presses Ctrl+C.
Subsequent headless runs (DhanHeadlessFetcher) reuse the saved session.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Resolve project root so imports work from any cwd
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_DHAN_OC_URL = "https://options-trader.dhan.co/advanceoptionchain"
_PROFILE_DIR = Path(
    os.getenv("DHAN_PROFILE_DIR", Path.home() / ".nsebot" / "dhan_profile")
)


async def _run():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: playwright not installed.\n  pip install playwright && playwright install chromium")
        sys.exit(1)

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[auth] Profile directory: {_PROFILE_DIR}")
    print("[auth] Launching HEADED browser — complete login, then close the browser window.")
    print("[auth] Once you see the option chain data, login is complete.\n")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=False,          # headed — user must interact
            slow_mo=100,
            args=["--no-sandbox"],
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(_DHAN_OC_URL, timeout=60_000)

        print("[auth] Browser open. Log in via QR code / OTP.")
        print("[auth] Close the browser window when done to save session.")

        # Wait until all pages are closed
        while ctx.pages:
            await asyncio.sleep(1)

        await ctx.close()

    print("\n[auth] Session saved. DhanHeadlessFetcher will now use this profile.")


def main():
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n[auth] Interrupted. Partial session state may be saved.")


if __name__ == "__main__":
    main()
