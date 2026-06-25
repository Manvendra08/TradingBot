"""Generate Paytm Money JWT token from request_token.

Usage:
  python scratch_get_paytm_jwt.py <request_token>

Steps:
  1. Go to https://developer.paytmmoney.com → create app → get API_KEY + API_SECRET
  2. Set PAYTM_API_KEY and PAYTM_API_SECRET in .env
  3. Open this URL in browser (replace YOUR_API_KEY):
     https://developer.paytmmoney.com/accounts/v2/authorize?api_key=YOUR_API_KEY
  4. Authorize → you'll get redirected with ?request_token=xxx in the URL
  5. Run this script with that request_token
  6. Copy the JWT output to PAYTM_JWT_TOKEN in .env
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.fetchers.paytm_fetcher import PaytmFetcher

if len(sys.argv) < 2:
    print("Usage: python scratch_get_paytm_jwt.py <request_token>")
    print("\n1. Set PAYTM_API_KEY and PAYTM_API_SECRET in .env")
    print("2. Open this URL in browser:")
    key = os.environ.get("PAYTM_API_KEY") or "(set PAYTM_API_KEY in .env first)"
    print(f"   https://developer.paytmmoney.com/accounts/v2/authorize?api_key={key}")
    print("3. After authorizing, copy request_token from the redirect URL")
    print("4. Run: python scratch_get_paytm_jwt.py <that_token>")
    sys.exit(1)

request_token = sys.argv[1].strip()
fetcher = PaytmFetcher()
if fetcher._refresh_token(request_token):
    jwt = fetcher._jwt_token
    print(f"\n✅ JWT Token obtained successfully!")
    print(f"\nAdd this to your .env file:")
    print(f'PAYTM_JWT_TOKEN="{jwt}"')
else:
    print("\n❌ Failed to obtain JWT token. Check API_KEY and API_SECRET in .env")
