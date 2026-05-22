import logging
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

from src.fetchers.dhan_fetcher import DhanFetcher

def test_dhan():
    symbol = "NATURALGAS"
    print(f"\n--- Testing Fetcher: dhan ---")
    try:
        fetcher = DhanFetcher()
        result = fetcher.fetch_option_chain(symbol)
        if result:
            print(f"SUCCESS! Strikes parsed: {len(result.get('strikes', []))}, underlying: {result.get('underlying_price')}, expiry: {result.get('expiry')}")
        else:
            print(f"FAILED (returned None)")
    except Exception as e:
        print(f"CRASHED: {e}")

if __name__ == "__main__":
    test_dhan()
