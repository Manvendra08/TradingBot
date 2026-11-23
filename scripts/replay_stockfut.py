"""CLI Runner for Stock Futures Historical Replay."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from src.stockfut.replay import ReplayEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("replay_stockfut")


def main():
    parser = argparse.ArgumentParser(description="Stock Futures 3-Year Replay Harness")
    parser.add_argument("--capital", type=float, default=10000000.0, help="Initial capital in INR")
    parser.add_argument("--start", type=str, default="2023-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-11-20", help="End date (YYYY-MM-DD)")
    args = parser.parse_args()

    log.info("Initializing Stock Futures Replay Harness for capital ₹%.0f...", args.capital)
    engine = ReplayEngine(capital=args.capital)
    # Empty placeholder data run for CLI validation
    report = engine.run_replay(symbol_daily_data={}, lot_sizes={})
    log.info("Replay completed. Total Trades: %d, Net Win Rate: %.1f%%, Expectancy: %.2fR",
             report.total_trades, report.net_positive_rate, report.expectancy_r)


if __name__ == "__main__":
    main()
