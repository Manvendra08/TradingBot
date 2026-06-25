"""Quick test of DhanSensexFetcher"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__) or ".")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

os.environ["DHAN_CLIENT_ID"] = "1104024279"
os.environ["DHAN_API_KEY"] = "cee624fc"
os.environ["DHAN_API_SECRET"] = "b1806f8f-6be7-417a-928b-a294964b32db"

from src.fetchers.dhan_sensex_fetcher import DhanSensexFetcher

f = DhanSensexFetcher()
result = f.fetch_option_chain("SENSEX")
if result:
    strikes = result.get("strikes", [])
    print(f"Symbol: {result.get('symbol')}")
    print(f"Spot: {result.get('underlying_price')}")
    print(f"Expiry: {result.get('expiry')}")
    print(f"Strikes: {len(strikes)}")
    if strikes:
        print(f"First: {strikes[0]}")
        print(f"Last: {strikes[-1]}")
    print("SUCCESS")
else:
    print("FAILED - no result")
