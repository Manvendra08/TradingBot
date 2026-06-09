"""
Scheduler loop — runs pipeline on runtime-configured interval.
Per-symbol market-hours guard: NSE 09:15–15:30, MCX 09:00–23:30.
Force-scan (--now flag) always bypasses the guard.
"""
import logging
from datetime import datetime
import subprocess
import sys
import time
from pathlib import Path
import pytz

from config.settings import FETCH_INTERVAL_MINUTES, WATCH_SYMBOLS
from config.runtime_config import get_scan_frequency_minutes
from config.symbol_classes import market_window
from src.engine.pipeline import run_pipeline

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SCRAPE_RUNNER = ROOT / "tools" / "scrape_dhan_naturalgas.py"

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


def _run_dhan_naturalgas_scrape():
    """Refresh the latest NATURALGAS snapshot JSON from the public Dhan page."""
    if not SCRAPE_RUNNER.exists():
        log.warning("Dhan scrape runner missing: %s", SCRAPE_RUNNER)
        return

    try:
        result = subprocess.run(
            [sys.executable, str(SCRAPE_RUNNER)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        if result.stdout:
            log.info("Dhan scrape runner output: %s", result.stdout.strip())
    except subprocess.CalledProcessError as exc:
        log.warning("Dhan scrape runner failed: %s", exc.stderr.strip() if exc.stderr else exc)
    except Exception as exc:
        log.warning("Dhan scrape runner error: %s", exc)


def start_scheduler():
    from src.models.schema import delete_expired_contracts
    
    log.info(
        "Scheduler started — default interval: %d min | runtime interval: %d min | symbols: %s",
        FETCH_INTERVAL_MINUTES,
        get_scan_frequency_minutes(),
        WATCH_SYMBOLS,
    )
    # Run a cleanup of expired data on startup
    delete_expired_contracts()
    
    try:
        while True:
            interval = get_scan_frequency_minutes()
            cycle_start = time.time()
            _guarded_run()
            _run_dhan_naturalgas_scrape()
            elapsed = time.time() - cycle_start
            sleep_for = max(1, int(interval * 60 - elapsed))
            log.debug("Scheduler cycle done in %.1fs; next run in %ds", elapsed, sleep_for)
            time.sleep(sleep_for)
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")
