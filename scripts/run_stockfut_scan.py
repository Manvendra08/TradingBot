"""CLI Runner to execute an on-demand Stock Futures scan across all F&O universe stocks."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("run_stockfut_scan")


def main():
    log.info("Starting on-demand Stock Futures scan across universe...")
    from src.stockfut.jobs import job_stockfut_eod_scan
    job_stockfut_eod_scan()
    log.info("Stock Futures scan process completed.")


if __name__ == "__main__":
    main()
