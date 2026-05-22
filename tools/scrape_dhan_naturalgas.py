"""
Refresh the latest NATURALGAS option-chain snapshot from Dhan's public commodity page.

This runner reuses the same commodity fetcher the pipeline uses, so it does not
depend on Playwright or BeautifulSoup being installed in the runtime venv.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher


OUT_PATH = ROOT / "scratch" / "naturalgas_option_chain_latest.json"


def main() -> int:
    fetcher = DhanCommodityFetcher()
    result = fetcher.fetch_option_chain("NATURALGAS")
    if not result or not result.get("strikes"):
        print("failed to refresh NATURALGAS option chain")
        return 1

    payload = {
        "source": "https://dhan.co/commodity/natural-gas-option-chain/",
        "symbol": result.get("symbol", "NATURALGAS"),
        "underlying_price": result.get("underlying_price"),
        "expiry": result.get("expiry"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "rows": result.get("strikes", []),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())