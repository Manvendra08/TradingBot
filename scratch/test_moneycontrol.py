import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

try:
    from src.fetchers.moneycontrol_fetcher import MoneycontrolFetcher
    fetcher = MoneycontrolFetcher()
    print("Testing MoneycontrolFetcher for NATURALGAS...")
    result = fetcher.fetch_option_chain("NATURALGAS")
    if result:
        print(f"SUCCESS! Strikes parsed: {len(result.get('strikes', []))}, underlying: {result.get('underlying_price')}, expiry: {result.get('expiry')}")
        # print first few strikes to verify
        print("Example strikes:")
        for s in result.get('strikes', [])[:5]:
            print(s)
    else:
        print("FAILED (returned None)")
except Exception as e:
    print(f"CRASHED: {e}")
