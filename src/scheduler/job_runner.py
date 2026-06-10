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
    from config.holidays import is_market_holiday
    if is_market_holiday(symbol, now):
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


def _update_live_cmps() -> None:
    """Lightweight live CMP refresh for symbols with OPEN trades."""
    from src.models.schema import get_conn, insert_snapshots, insert_underlying_price
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT symbol FROM paper_trades WHERE status='OPEN'").fetchall()
        open_symbols = [r["symbol"] for r in rows]
    
    if not open_symbols:
        return
        
    log.info("Running live CMP refresh for active symbols: %s", open_symbols)
    from src.fetchers.router import fetch_option_chain
    from datetime import datetime, timezone
    
    for symbol in open_symbols:
        # Check if market is open for this symbol
        if not _is_open_for(symbol):
            continue
        try:
            oc_data = fetch_option_chain(symbol)
            if oc_data and oc_data.get("strikes"):
                fetched_at = datetime.now(timezone.utc).isoformat()
                underlying = oc_data["underlying_price"]
                if underlying:
                    insert_underlying_price(symbol, underlying, oc_data.get("pct_change"), fetched_at)
                
                rows_to_insert = []
                for s in oc_data["strikes"]:
                    rows_to_insert.append({
                        "fetched_at": fetched_at,
                        "symbol": symbol,
                        "expiry": oc_data["expiry"],
                        "strike": s["strike"],
                        "option_type": s["option_type"],
                        "ltp": s["ltp"],
                        "ltp_change_pct": s.get("ltp_change_pct"),
                        "oi": s.get("oi"),
                        "oi_change_pct": s.get("oi_change_pct"),
                        "oi_change": s.get("oi_change"),
                        "volume": s.get("volume"),
                        "iv": s.get("iv"),
                        "bid": s.get("bid"),
                        "ask": s.get("ask"),
                        "delta": s.get("delta"),
                        "underlying_price": underlying,
                        "fetcher_source": oc_data.get("source", "unknown")
                    })
                insert_snapshots(rows_to_insert)
                log.info("Live CMP refresh completed for %s (%d strikes)", symbol, len(rows_to_insert))
        except Exception as e:
            log.warning("Live CMP refresh failed for %s: %s", symbol, e)


import threading

def run_with_timeout(func, timeout, *args, **kwargs) -> bool:
    """Run a function in a daemon thread with a timeout watchdog."""
    t = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.error("Watchdog: function '%s' timed out after %ds and might be hung", func.__name__, timeout)
        return False
    return True


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
    
    last_full_scan = 0.0
    last_cmp_refresh = 0.0
    
    try:
        while True:
            now = time.time()
            interval_min = get_scan_frequency_minutes()
            
            # 1. Full Scan Loop (strategy execution, alerts)
            if now - last_full_scan >= interval_min * 60:
                cycle_start = time.time()
                def run_all():
                    _guarded_run()
                    _run_dhan_naturalgas_scrape()
                
                success = run_with_timeout(run_all, timeout=300)
                if not success:
                    try:
                        from src.alerts.telegram_dispatcher import send_text
                        send_text("⚠️ **NSEBOT ALERT**: Scheduler scan loop timed out/hung after 5 minutes. Watchdog bypassed it to keep scheduler active.")
                    except Exception:
                        pass
                elapsed = time.time() - cycle_start
                last_full_scan = time.time()
                log.debug("Full scan cycle completed in %.1fs (success=%s)", elapsed, success)
                
            # 2. Live CMP Refresh Loop (every 2 minutes)
            if time.time() - last_cmp_refresh >= 120:
                cycle_start = time.time()
                success = run_with_timeout(_update_live_cmps, timeout=90)
                elapsed = time.time() - cycle_start
                last_cmp_refresh = time.time()
                if elapsed > 0.5:
                    log.debug("Live CMP refresh completed in %.1fs (success=%s)", elapsed, success)
            
            # Sleep in short increments to remain responsive to intervals and changes
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")

