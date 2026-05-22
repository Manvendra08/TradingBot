import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

from src.fetchers.chart_fetcher import get_chart_fetcher

print("Testing ChartFetcher for NATURALGAS...")
try:
    result = get_chart_fetcher().fetch("NATURALGAS")
    print(f"SUCCESS! Result: {result}")
except Exception as e:
    print(f"CRASHED: {e}")
print("Done!")
