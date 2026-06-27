#!/usr/bin/env python
"""
Paytm Money headless authentication utility.

Automatically obtains fresh JWT token using Playwright headless browser.

Usage:
  python tools/authenticate_paytm.py

Prerequisites:
  1. Set in .env:
     PAYTM_EMAIL=your_email@example.com
     PAYTM_PASSWORD=your_password
     PAYTM_API_KEY=your_api_key
     PAYTM_API_SECRET=your_api_secret

  2. Install playwright: pip install playwright && playwright install chromium

The tool will:
  1. Launch headless browser
  2. Auto-fill credentials and authorize
  3. Capture request_token from redirect
  4. Exchange for JWT
  5. Update .env with new JWT
"""

import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Load .env
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("paytm_auth")


def main():
    from config.settings import _optional_env

    email = _optional_env("PAYTM_EMAIL")
    password = _optional_env("PAYTM_PASSWORD")
    api_key = _optional_env("PAYTM_API_KEY")
    api_secret = _optional_env("PAYTM_API_SECRET")

    missing = []
    if not email:
        missing.append("PAYTM_EMAIL")
    if not password:
        missing.append("PAYTM_PASSWORD")
    if not api_key:
        missing.append("PAYTM_API_KEY")
    if not api_secret:
        missing.append("PAYTM_API_SECRET")

    if missing:
        log.error("Missing credentials in .env: %s", ", ".join(missing))
        print("\nAdd these to your .env file:")
        print("PAYTM_EMAIL=your_email@example.com")
        print("PAYTM_PASSWORD=your_password")
        print("PAYTM_API_KEY=your_api_key")
        print("PAYTM_API_SECRET=your_api_secret")
        return

    print("=== Paytm Money Headless Authentication ===")
    print(f"Email: {email}")
    print(f"API Key: {api_key[:10]}...")

    from src.fetchers.paytm_headless_auth import _get_paytm_jwt_headless

    jwt = _get_paytm_jwt_headless(api_key, api_secret, email, password)

    if jwt:
        print("\n" + "=" * 50)
        print("SUCCESS! Paytm Money JWT Token obtained:")
        print("=" * 50)
        print(jwt)
        print("=" * 50)

        # Auto-update .env
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            try:
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
                log.info("✅ Automatically updated PAYTM_JWT_TOKEN in .env!")
            except Exception as e:
                log.error("Failed to auto-update .env: %s", e)
        else:
            print(f"\nAdd to your .env file:")
            print(f"PAYTM_JWT_TOKEN={jwt}")
    else:
        log.error("Failed to obtain JWT token. Check credentials and try again.")
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
