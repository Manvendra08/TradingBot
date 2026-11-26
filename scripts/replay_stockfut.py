"""CLI Runner for Stock Futures Historical Replay."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone

from config.stockfut_loader import load_stockfut_config
from src.fetchers.kite_fetcher import get_kite_data_client
from src.stockfut.replay import ReplayEngine
from src.stockfut.universe import UniverseBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("replay_stockfut")
IST = timezone(timedelta(hours=5, minutes=30))


def run_live_3h_ab(capital: float, days: int, max_symbols: int) -> None:
    """Fetch cash daily + 60m candles from Kite and compare Strategy A (09:30) vs B (12:20 3H)."""
    cfg = load_stockfut_config()
    client = get_kite_data_client()
    now_dt = datetime.now(IST)
    universe = UniverseBuilder(cfg=cfg, kite_client=client).build_universe(asof=now_dt.date(), capital=capital)
    if max_symbols > 0:
        universe = universe[:max_symbols]

    from_day = now_dt - timedelta(days=max(120, days + 90))
    from_60m = now_dt - timedelta(days=days)
    nifty_tok = client.resolve_instrument_token("NIFTY") or 256265
    def _fmt_date(val) -> str:
        return val.strftime("%Y-%m-%d") if hasattr(val, "strftime") else str(val)[:10]

    index_daily = [
        {**b, "date": _fmt_date(b["date"])}
        for b in client.get_historical_candles(nifty_tok, interval="day", from_date=from_day, to_date=now_dt)
    ]

    daily_map = {}
    h60_map = {}
    lot_sizes = {}
    for c in universe:
        tok = client.resolve_instrument_token(c.symbol)
        if not tok:
            continue
        d_bars = client.get_historical_candles(tok, interval="day", from_date=from_day, to_date=now_dt)
        m_bars = client.get_historical_candles(tok, interval="60minute", from_date=from_60m, to_date=now_dt)
        if not d_bars or not m_bars:
            continue
        daily_map[c.symbol] = [{**b, "date": _fmt_date(b["date"])} for b in d_bars]
        h60_map[c.symbol] = m_bars
        lot_sizes[c.symbol] = c.lot_size
        time.sleep(0.05)

    res = ReplayEngine(cfg=cfg, capital=capital).run_replay_3h_ab(
        symbol_daily_data=daily_map,
        symbol_60m_data=h60_map,
        lot_sizes=lot_sizes,
        index_daily=index_daily,
        start_idx=max(30, len(index_daily) - days),
    )
    a, b = res["strategy_a"], res["strategy_b"]
    log.info(
        "Strategy A (09:30): trades=%d, win=%.1f%%, exp=%.3fR, net=₹%.0f, drift_missed=%d",
        a.total_trades, a.net_positive_rate, a.expectancy_r, a.net_pnl, res["drift_missed_by_a"],
    )
    log.info(
        "Strategy B (12:20 3H): trades=%d, win=%.1f%%, exp=%.3fR, net=₹%.0f | b_beats_a=%s (Δ=%.3fR)",
        b.total_trades, b.net_positive_rate, b.expectancy_r, b.net_pnl, res["b_beats_a"], res["delta_expectancy_r"],
    )


def main():
    parser = argparse.ArgumentParser(description="Stock Futures Replay Harness")
    parser.add_argument("--capital", type=float, default=10000000.0, help="Initial capital in INR")
    parser.add_argument("--start", type=str, default="2023-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-11-20", help="End date (YYYY-MM-DD)")
    parser.add_argument("--ab-3h", action="store_true", help="Run 60d Kite cash 60m A/B test (09:30 vs 12:20 3H)")
    parser.add_argument("--days", type=int, default=60, help="Lookback days for --ab-3h")
    parser.add_argument("--max-symbols", type=int, default=40, help="Max symbols for --ab-3h (0=all)")
    args = parser.parse_args()

    if args.ab_3h:
        run_live_3h_ab(capital=args.capital, days=args.days, max_symbols=args.max_symbols)
        return

    log.info("Initializing Stock Futures Replay Harness for capital ₹%.0f...", args.capital)
    engine = ReplayEngine(capital=args.capital)
    # Empty placeholder data run for CLI validation
    report = engine.run_replay(symbol_daily_data={}, lot_sizes={})
    log.info("Replay completed. Total Trades: %d, Net Win Rate: %.1f%%, Expectancy: %.2fR",
             report.total_trades, report.net_positive_rate, report.expectancy_r)


if __name__ == "__main__":
    main()
