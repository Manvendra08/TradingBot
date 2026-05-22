import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO)

from src.fetchers.moneycontrol_fetcher import MoneycontrolFetcher

print("--- Starting MoneycontrolFetcher fetch_option_chain ---")
fetcher = MoneycontrolFetcher()
res = fetcher.fetch_option_chain("NATURALGAS")
if res:
    print("SUCCESS!")
    print(f"Expiry: {res.get('expiry')}")
    print(f"Strikes count: {len(res.get('strikes', []))}")
    print("Sample strikes (first 10):")
    for s in res.get('strikes', [])[:10]:
        print(s)
else:
    print("FAILED! MoneycontrolFetcher returned None")
