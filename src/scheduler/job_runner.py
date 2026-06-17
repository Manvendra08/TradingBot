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
from config.runtime_config import get_scan_frequency_minutes, get_scan_frequency_nse, get_scan_frequency_mcx
from config.symbol_classes import market_window, get_symbol_class
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


def _guarded_run(class_key: str | None = None):
    symbols_to_check = [s for s in WATCH_SYMBOLS if (class_key is None or get_symbol_class(s) == class_key)]
    open_symbols = [s for s in symbols_to_check if _is_open_for(s)]
    if not open_symbols:
        log.debug("All symbols outside market hours%s — skipping", f" for {class_key}" if class_key else "")
        return
    closed = set(symbols_to_check) - set(open_symbols)
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
        paper_rows = conn.execute("SELECT DISTINCT symbol FROM paper_trades WHERE status='OPEN'").fetchall()
        live_rows = conn.execute("SELECT DISTINCT symbol FROM live_trades WHERE status='OPEN'").fetchall()
        open_symbols = list(set([r["symbol"] for r in paper_rows] + [r["symbol"] for r in live_rows]))
    
    if not open_symbols:
        return
        
    log.debug("Running live CMP refresh for active symbols: %s", open_symbols)
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
                log.debug("Live CMP refresh completed for %s (%d strikes)", symbol, len(rows_to_insert))
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


def start_scheduler(immediate: bool = False):
    from src.models.schema import delete_expired_contracts
    
    log.info(
        "Scheduler started (immediate_scan=%s) — default interval: %d min | NSE interval: %d min | MCX interval: %d min | symbols: %s",
        immediate,
        FETCH_INTERVAL_MINUTES,
        get_scan_frequency_nse(),
        get_scan_frequency_mcx(),
        WATCH_SYMBOLS,
    )
    # Run a cleanup of expired data on startup
    delete_expired_contracts()
    
    # ── Instrument cache warm-up at scheduler start ────────────────────────
    def _warmup_instrument_cache():
        try:
            from src.engine.symbol_resolver import _instrument_cache_is_ready, fetch_and_cache_instruments
            from src.engine.live_trading import get_kite_client
            if not _instrument_cache_is_ready():
                kite = get_kite_client()
                if kite:
                    log.info("[scheduler] Warming up instrument cache...")
                    fetch_and_cache_instruments(kite)
                else:
                    log.info("[scheduler] Kite not connected; instrument cache warm-up skipped.")
        except Exception as exc:
            log.warning("[scheduler] Instrument cache warm-up failed: %s", exc)

    threading.Thread(target=_warmup_instrument_cache, daemon=True, name="instrument-cache-startup").start()

    current_date = datetime.now(IST).date()
    last_scanned_interval: dict[str, int] = {}
    has_done_startup_scan: dict[str, bool] = {}
    
    # If immediate scan is NOT requested, skip the first scan for the current interval
    if not immediate:
        import math
        from config.symbol_classes import get_symbol_class, MARKET_WINDOWS
        now_ist = datetime.now(IST)
        for class_key in MARKET_WINDOWS:
            open_t, _, _ = MARKET_WINDOWS[class_key]
            open_h, open_m = map(int, open_t.split(":"))
            market_open_time = now_ist.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
            delta_minutes = (now_ist - market_open_time).total_seconds() / 60.0
            if delta_minutes >= 0:
                if class_key == "MCX_COMMODITY":
                    interval_min = get_scan_frequency_mcx()
                else:
                    interval_min = get_scan_frequency_nse()
                current_interval_idx = math.floor(delta_minutes / interval_min)
                
                has_done_startup_scan[class_key] = True
                last_scanned_interval[class_key] = current_interval_idx
                log.info("Bypassing immediate startup scan for %s. Next scan will trigger at interval index %d.", class_key, current_interval_idx + 1)
    
    last_cmp_refresh = 0.0
    last_instrument_cache_refresh = time.time()  # mark as refreshed now (warmup thread handles first)
    _INSTRUMENT_CACHE_REFRESH_INTERVAL = 4 * 60 * 60  # 4 hours

    try:
        while True:
            now_ts = time.time()
            now_ist = datetime.fromtimestamp(now_ts, IST)
            
            if now_ist.date() > current_date:
                current_date = now_ist.date()
                last_scanned_interval.clear()
                has_done_startup_scan.clear()
            
            # 1. Full Scan Loop per market class
            import math
            from config.symbol_classes import get_symbol_class, MARKET_WINDOWS
            
            for class_key in MARKET_WINDOWS:
                class_symbols = [s for s in WATCH_SYMBOLS if get_symbol_class(s) == class_key]
                if not class_symbols:
                    continue
                    
                open_t, close_t, days = MARKET_WINDOWS[class_key]
                open_h, open_m = map(int, open_t.split(":"))
                market_open_time = now_ist.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
                
                delta_minutes = (now_ist - market_open_time).total_seconds() / 60.0
                if delta_minutes < 0:
                    continue
                
                if class_key == "MCX_COMMODITY":
                    interval_min = get_scan_frequency_mcx()
                else:
                    interval_min = get_scan_frequency_nse()

                current_interval_idx = math.floor(delta_minutes / interval_min)
                should_scan = False
                
                if current_interval_idx == 0:
                    if not has_done_startup_scan.get(class_key, False):
                        should_scan = True
                        has_done_startup_scan[class_key] = True
                        last_scanned_interval[class_key] = 0
                else:
                    last_scanned = last_scanned_interval.get(class_key, -1)
                    if current_interval_idx > last_scanned:
                        should_scan = True
                        has_done_startup_scan[class_key] = True
                        last_scanned_interval[class_key] = current_interval_idx
                        
                if should_scan:
                    cycle_start = time.time()
                    log.info("Triggering scan for %s (interval idx: %d, time since open: %.1f min)", class_key, current_interval_idx, delta_minutes)
                    def run_all():
                        _guarded_run(class_key)
                        if class_key == "MCX_COMMODITY":
                            _run_dhan_naturalgas_scrape()
                    
                    success = run_with_timeout(run_all, timeout=300)
                    if not success:
                        try:
                            from src.alerts.telegram_dispatcher import send_text
                            send_text(f"⚠️ **NSEBOT ALERT**: Scheduler scan loop timed out/hung after 5 minutes for {class_key}. Watchdog bypassed it.")
                        except Exception:
                            pass
                    elapsed = time.time() - cycle_start
                    log.debug("Full scan cycle completed for %s in %.1fs (success=%s)", class_key, elapsed, success)
                
            # 2. Live CMP Refresh Loop (every 2 minutes)
            if time.time() - last_cmp_refresh >= 120:
                cycle_start = time.time()
                success = run_with_timeout(_update_live_cmps, timeout=90)
                elapsed = time.time() - cycle_start
                last_cmp_refresh = time.time()
                if elapsed > 0.5:
                    log.debug("Live CMP refresh completed in %.1fs (success=%s)", elapsed, success)

            # 3. Instrument cache periodic refresh (every 4 hours)
            if time.time() - last_instrument_cache_refresh >= _INSTRUMENT_CACHE_REFRESH_INTERVAL:
                last_instrument_cache_refresh = time.time()
                def _refresh_cache():
                    try:
                        from src.engine.symbol_resolver import fetch_and_cache_instruments
                        from src.engine.live_trading import get_kite_client
                        kite = get_kite_client()
                        if kite:
                            log.info("[scheduler] Periodic instrument cache refresh...")
                            fetch_and_cache_instruments(kite)
                    except Exception as exc:
                        log.warning("[scheduler] Periodic instrument cache refresh failed: %s", exc)
                threading.Thread(target=_refresh_cache, daemon=True, name="instrument-cache-periodic").start()

            # Sleep in short increments to remain responsive to intervals and changes
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")


