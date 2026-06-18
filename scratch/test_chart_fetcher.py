import os
from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

import sys
sys.path.insert(0, ".")
from src.fetchers.chart_fetcher import get_chart_fetcher

print("=== Testing NATURALGAS via ChartFetcher.fetch() ===")
cf = get_chart_fetcher()
result = cf.fetch("NATURALGAS", timeframes=["1h", "3h"])
if result:
    for sym, tfs in result.items():
        for tf, data in tfs.items():
            sentiment = data.get("sentiment")
            close = (data.get("ohlc") or {}).get("close")
            print(f"  {sym} {tf}: sentiment={sentiment}, close={close}")
    print("\nSUCCESS: NATURALGAS charts fetched via ChartFetcher!")
else:
    print("FAILED: ChartFetcher returned empty result")
