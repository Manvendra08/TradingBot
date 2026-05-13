"""
APScheduler — runs pipeline every N minutes.
Per-symbol market-hours guard: NSE 09:15–15:30, MCX 09:00–23:30.
Force-scan (--now flag) always bypasses the guard.
"""
import logging
from datetime import datetime
import pytz

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import FETCH_INTERVAL_MINUTES, WATCH_SYMBOLS
from config.symbol_classes import market_window
from src.engine.pipeline import run_pipeline

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def _is_open_for(symbol: str) -> bool:
    now = datetime.now(IST)
    open_t, close_t, days = market_window(symbol)
    if now.weekday() not in days:
        return False
    t = now.strftime("%H:%M")
    return open_t <= t <= close_t


def _guarded_run():
    open_symbols = [s for s in WATCH_SYMBOLS if _is_open_for(s)]
    if not open_symbols:
        log.debug("All symbols outside market hours — skipping")
        return
    closed = set(WATCH_SYMBOLS) - set(open_symbols)
    if closed:
        log.debug("Skipping closed symbols: %s", sorted(closed))
    run_pipeline(symbols=open_symbols)


def start_scheduler():
    scheduler = BlockingScheduler(timezone=IST)
    trigger = IntervalTrigger(minutes=FETCH_INTERVAL_MINUTES, timezone=IST)
    scheduler.add_job(
        _guarded_run,
        trigger=trigger,
        id="option_chain_fetch",
        name="NSE/MCX Option Chain Fetch",
        max_instances=1,
        misfire_grace_time=60,
    )
    log.info("Scheduler started — interval: %d min | symbols: %s",
             FETCH_INTERVAL_MINUTES, WATCH_SYMBOLS)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")
