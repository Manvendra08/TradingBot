"""
Scheduler loop — runs pipeline on runtime-configured interval.
Per-symbol market-hours guard: NSE 09:15–15:30, MCX 09:00–23:30.
Force-scan (--now flag) always bypasses the guard.

Phase 2: Weekly ML training job added (Sunday 2 AM IST fallback).
Event-driven triggers (20+ trades, edge health < 60) are wired in pipeline.py.
"""

import logging
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz

# Set global socket timeout to prevent indefinite hangs in third-party libraries (e.g. tvDatafeed, urllib)
socket.setdefaulttimeout(15.0)

from config.runtime_config import (
    get_scan_frequency_mcx,
    get_scan_frequency_minutes,
    get_scan_frequency_nse,
)
from config.settings import FETCH_INTERVAL_MINUTES, WATCH_SYMBOLS
from config.symbol_classes import get_symbol_class, is_market_open, market_window, MARKET_WINDOWS
from src.engine.pipeline import run_pipeline
from src.models.schema import has_recent_scan_summary

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SCRAPE_RUNNER = ROOT / "tools" / "scrape_dhan_naturalgas.py"

IST = pytz.timezone("Asia/Kolkata")


def exit_all_positions_friday(market_class: str) -> None:
    """Exit all open paper and live trades for symbols matching the given market class."""
    from datetime import datetime, timezone

    from config.runtime_config import load_runtime_config
    from config.settings import WATCH_SYMBOLS
    from src.engine.live_trading import _exit_open_live_trade, get_kite_client
    from src.engine.trade_plan import get_option_premium
    from src.fetchers.router import fetch_option_chain
    from src.models.schema import close_paper_trade, get_conn

    log.info(
        "[Friday Exit] Weekend Risk auto-exit triggered for class: %s", market_class
    )
    config = load_runtime_config()
    shadow_mode = config.get("live_shadow_mode", True)
    kite = get_kite_client()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Determine symbols matching class
    symbols = [s for s in WATCH_SYMBOLS if get_symbol_class(s) == market_class]

    for symbol in symbols:
        try:
            # 1. Fetch open paper trades
            with get_conn() as conn:
                open_paper = conn.execute(
                    "SELECT * FROM paper_trades WHERE symbol=? AND status='OPEN'",
                    (symbol,),
                ).fetchall()

            # 2. Fetch open live trades
            with get_conn() as conn:
                open_live = conn.execute(
                    "SELECT * FROM live_trades WHERE symbol=? AND status='OPEN'",
                    (symbol,),
                ).fetchall()

            if not open_paper and not open_live:
                continue

            log.info(
                "[Friday Exit] Found open trades for %s. Fetching latest CMP data to square off...",
                symbol,
            )
            # Fetch options chain to get latest premiums
            oc_data = fetch_option_chain(symbol)
            if not oc_data or not oc_data.get("underlying_price"):
                log.warning(
                    "[Friday Exit] Could not fetch latest prices for %s. Skipping Friday exit.",
                    symbol,
                )
                continue

            underlying = oc_data["underlying_price"]
            option_rows = oc_data.get("strikes") or []

            # Exit Paper Trades
            for row in open_paper:
                trade = dict(row)
                exit_premium = None
                if trade.get("option_type") == "FUT":
                    exit_premium = underlying
                else:
                    exit_premium = get_option_premium(
                        symbol,
                        trade.get("expiry"),
                        trade.get("strike"),
                        trade.get("option_type"),
                        option_rows,
                    )

                # Fallback to intrinsic value if premium is missing / zero
                if exit_premium is None or exit_premium <= 0:
                    strike = float(trade.get("strike") or 0.0)
                    if trade.get("option_type") == "CE":
                        exit_premium = max(0.0, underlying - strike)
                    else:
                        exit_premium = max(0.0, strike - underlying)

                close_paper_trade(
                    trade["id"],
                    now_iso,
                    underlying,
                    exit_premium,
                    "CLOSED_WEEKEND",
                    "Friday auto-exit to avoid weekend risk",
                )
                log.info(
                    "[Friday Exit] Successfully closed paper trade #%d for %s at premium %.2f",
                    trade["id"],
                    symbol,
                    exit_premium,
                )

            # Exit Live Trades
            for row in open_live:
                trade = dict(row)
                exit_premium = None
                if trade.get("option_type") == "FUT":
                    exit_premium = underlying
                else:
                    exit_premium = get_option_premium(
                        symbol,
                        trade.get("expiry"),
                        trade.get("strike"),
                        trade.get("option_type"),
                        option_rows,
                    )

                # Fallback to intrinsic value
                if exit_premium is None or exit_premium <= 0:
                    strike = float(trade.get("strike") or 0.0)
                    if trade.get("option_type") == "CE":
                        exit_premium = max(0.0, underlying - strike)
                    else:
                        exit_premium = max(0.0, strike - underlying)

                try:
                    _exit_open_live_trade(
                        kite=kite,
                        symbol=symbol,
                        trade=trade,
                        underlying=underlying,
                        exit_premium=exit_premium,
                        status="CLOSED_WEEKEND",
                        reason="Friday auto-exit to avoid weekend risk",
                        shadow_mode=shadow_mode,
                        now_iso=now_iso,
                    )
                    log.info(
                        "[Friday Exit] Successfully closed live trade #%d for %s",
                        trade["id"],
                        symbol,
                    )
                    from src.alerts.telegram_dispatcher import send_text

                    prefix = "[SHADOW]" if shadow_mode else "🚨 [LIVE]"
                    send_text(
                        f"{prefix} **Friday Auto-Exit** | Closed `{symbol}` `{trade.get('option_type')}` position at underlying `{underlying}` to avoid weekend risk."
                    )
                except Exception as live_exc:
                    log.error(
                        "[Friday Exit] Failed to close live trade #%d for %s: %s",
                        trade["id"],
                        symbol,
                        live_exc,
                    )

        except Exception as sym_exc:
            log.error(
                "[Friday Exit] Error executing Friday exit for %s: %s", symbol, sym_exc
            )


def _is_open_for(symbol: str) -> bool:
    """Check if the market is currently open for the given symbol (used for scheduling guards)."""
    now = datetime.now(IST)
    open_t, close_t, days = market_window(symbol)
    if now.weekday() not in days:
        return False
    from config.holidays import is_market_holiday

    if is_market_holiday(symbol, now):
        return False
    t = now.strftime("%H:%M")
    return open_t <= t <= close_t


def _latest_interval_data_available(class_key: str, current_interval_idx: int, interval_min: int, market_open_time_ist: datetime) -> bool:
    """Check if scan summaries are present in the DB for the current interval's timestamp."""
    from datetime import timedelta, timezone
    from src.models.schema import get_conn

    interval_start_ist = market_open_time_ist + timedelta(minutes=current_interval_idx * interval_min)
    interval_start_utc = interval_start_ist.astimezone(timezone.utc)
    interval_start_utc_str = interval_start_utc.isoformat()

    symbols = [s for s in WATCH_SYMBOLS if get_symbol_class(s) == class_key]
    if not symbols:
        return True

    with get_conn() as conn:
        for symbol in symbols:
            if not _is_open_for(symbol):
                continue
            row = conn.execute(
                "SELECT 1 FROM scan_summaries WHERE symbol=? AND fetched_at >= ? LIMIT 1",
                (symbol, interval_start_utc_str)
            ).fetchone()
            if not row:
                return False
    return True


def _guarded_run(class_key: str | None = None):
    symbols_to_check = [
        s
        for s in WATCH_SYMBOLS
        if (class_key is None or get_symbol_class(s) == class_key)
    ]
    open_symbols = [s for s in symbols_to_check if _is_open_for(s)]
    if not open_symbols:
        log.info("[%s] Market is closed. Skipping scan.", class_key or "ALL")
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
        log.warning(
            "Dhan scrape runner failed: %s", exc.stderr.strip() if exc.stderr else exc
        )
    except Exception as exc:
        log.warning("Dhan scrape runner error: %s", exc)


def _check_live_exits(symbol: str, underlying: float, strikes: list[dict]) -> None:
    """
    H4 fix: Check open live trades for SL/Target hits using freshly fetched
    option premiums.  Runs every 2 minutes inside _update_live_cmps() so exits
    are detected between the 5-minute full pipeline scans.

    Only handles premium-poll exits (shadow mode, FUT, or POLL exit_mode).
    GTT-managed exits are handled by the broker itself.
    """
    from datetime import datetime, timezone

    from config.runtime_config import load_runtime_config
    from src.alerts.telegram_dispatcher import send_text
    from src.engine.live_trading import (
        _exit_open_live_trade,
        _get_exchange,
        _resolve_trade_quantity,
        cancel_kite_gtt,
        get_kite_client,
        place_kite_order,
    )
    from src.engine.symbol_resolver import resolve_instrument
    from src.models.schema import get_conn

    config = load_runtime_config()
    shadow_mode = config.get("live_shadow_mode", True)
    kite = get_kite_client()

    with get_conn() as conn:
        open_trades = conn.execute(
            "SELECT * FROM live_trades WHERE symbol=? AND status='OPEN'",
            (symbol,),
        ).fetchall()

    if not open_trades:
        return

    now_iso = datetime.now(timezone.utc).isoformat()

    for trade_row in open_trades:
        trade = dict(trade_row)
        if trade.get("setup_type") == "DIRECT_KITE" and not config.get(
            "manage_direct_kite_positions", False
        ):
            continue

        exit_mode = trade.get("exit_mode") or "GTT"

        # Only poll-exit if: shadow mode, FUT, or explicit POLL fallback
        if not (
            shadow_mode or exit_mode == "POLL" or trade.get("option_type") == "FUT"
        ):
            continue

        # Resolve current premium
        exit_premium = None
        if trade.get("option_type") == "FUT":
            exit_premium = underlying
        else:
            strike = float(trade.get("strike") or 0)
            option_type = str(trade.get("option_type") or "")
            for row in strikes:
                try:
                    if (
                        abs(float(row.get("strike") or 0) - strike) < 0.01
                        and str(row.get("option_type") or "").upper()
                        == option_type.upper()
                    ):
                        ltp = float(row.get("ltp") or 0.0)
                        if ltp > 0:
                            exit_premium = ltp
                        break
                except Exception:
                    continue

        if exit_premium is None:
            continue

        sl_premium = float(trade.get("sl_premium") or 0.0)
        target_premium = float(trade.get("target_premium") or 0.0)
        is_sell = trade.get("side") == "SELL"
        close_status = ""
        close_reason = ""

        if is_sell and sl_premium > 0 and exit_premium >= sl_premium:
            close_status, close_reason = "CLOSED_SL", "stop loss hit (CMP poll)"
        elif is_sell and target_premium > 0 and exit_premium <= target_premium:
            close_status, close_reason = "CLOSED_TARGET", "target hit (CMP poll)"
        elif not is_sell and sl_premium > 0 and exit_premium <= sl_premium:
            close_status, close_reason = "CLOSED_SL", "stop loss hit (CMP poll)"
        elif not is_sell and target_premium > 0 and exit_premium >= target_premium:
            close_status, close_reason = "CLOSED_TARGET", "target hit (CMP poll)"

        if not close_status:
            continue

        try:
            if kite:
                closed = _exit_open_live_trade(
                    kite=kite,
                    symbol=symbol,
                    trade=trade,
                    underlying=underlying,
                    exit_premium=exit_premium,
                    status=close_status,
                    reason=close_reason,
                    shadow_mode=shadow_mode,
                    now_iso=now_iso,
                )
                prefix = "[SHADOW]" if shadow_mode else "🚨 [LIVE]"
                send_text(
                    f"{prefix} **CMP Poll Exit** | Closed `{symbol}` "
                    f"`{trade.get('option_type')}` — `{close_reason}` at premium "
                    f"`{exit_premium}`."
                )
                log.info(
                    "%s: CMP poll exit — %s %s at premium %.2f (%s)",
                    symbol,
                    trade.get("option_type"),
                    close_reason,
                    exit_premium,
                    close_status,
                )
        except Exception as e:
            log.error("%s: CMP poll exit square-off failed: %s", symbol, e)


def _update_live_cmps() -> None:
    """Lightweight live CMP refresh for symbols with OPEN trades."""
    from src.models.schema import get_conn, insert_snapshots, insert_underlying_price

    with get_conn() as conn:
        paper_rows = conn.execute(
            "SELECT DISTINCT symbol FROM paper_trades WHERE status='OPEN'"
        ).fetchall()
        live_rows = conn.execute(
            "SELECT DISTINCT symbol FROM live_trades WHERE status='OPEN'"
        ).fetchall()
        open_symbols = list(
            set([r["symbol"] for r in paper_rows] + [r["symbol"] for r in live_rows])
        )

    if not open_symbols:
        return

    log.debug("Running live CMP refresh for active symbols: %s", open_symbols)
    import concurrent.futures
    from datetime import datetime, timezone

    from src.fetchers.router import fetch_option_chain

    def _update_single_symbol(symbol: str) -> None:
        if not _is_open_for(symbol):
            return
        try:
            oc_data = fetch_option_chain(symbol)
            if oc_data and oc_data.get("strikes"):
                fetched_at = datetime.now(timezone.utc).isoformat()
                underlying = oc_data["underlying_price"]
                if underlying:
                    insert_underlying_price(
                        symbol, underlying, oc_data.get("pct_change"), fetched_at
                    )

                rows_to_insert = []
                for s in oc_data["strikes"]:
                    rows_to_insert.append(
                        {
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
                            "fetcher_source": oc_data.get("source", "unknown"),
                        }
                    )
                insert_snapshots(rows_to_insert)
                log.debug(
                    "Live CMP refresh completed for %s (%d strikes)",
                    symbol,
                    len(rows_to_insert),
                )

                # H4 fix: check live trade SL/Target exits between full scans
                _check_live_exits(symbol, underlying, oc_data.get("strikes") or [])
        except Exception as e:
            log.warning("Live CMP refresh failed for %s: %s", symbol, e)

    # Run all symbol refreshes in parallel. Timeout after 60 seconds to prevent
    # blockages or watchdog timeouts.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(open_symbols)
    ) as executor:
        futures = {
            executor.submit(_update_single_symbol, sym): sym for sym in open_symbols
        }
        done, not_done = concurrent.futures.wait(futures.keys(), timeout=90)
        for f in not_done:
            sym = futures[f]
            log.warning(
                "[scheduler] Live CMP refresh for %s timed out inside thread pool", sym
            )


import threading


def run_with_timeout(func, timeout, *args, **kwargs) -> bool:
    """Run a function in a daemon thread with a timeout watchdog."""
    t = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.error(
            "Watchdog: function '%s' timed out after %ds and might be hung",
            func.__name__,
            timeout,
        )
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

    # Trigger Index Weights refresh check on startup
    try:
        from src.engine.index_weights import refresh_index_weights_async
        refresh_index_weights_async(force=False)
    except Exception as e:
        log.error("[scheduler] Failed to trigger startup index weights refresh: %s", e)

    # ── Phase 2: Weekly ML Training Job ──────────────────────────────────────
    # AI_INTELLIGENCE_ROADMAP_v3.0 — Weekly fallback retraining (Sunday 2 AM IST)
    # Event-driven triggers (20+ trades, edge health < 60) are wired separately
    # in pipeline.py via on_trade_closed() and on_edge_health_alert().
    _last_ml_training_week: int | None = None  # ISO week number
    _last_eia_run_date = None
    _last_backup_date = None
    _last_fii_fetch_date = None
    _last_autopsy_date = None

    # ── Instrument cache warm-up at scheduler start ────────────────────────
    cache_warmed_event = threading.Event()

    def _warmup_instrument_cache():
        try:
            from src.engine.live_trading import get_kite_client
            from src.engine.symbol_resolver import (
                _instrument_cache_is_ready,
                fetch_and_cache_instruments,
            )

            if not _instrument_cache_is_ready():
                kite = get_kite_client()
                if kite:
                    log.info("[scheduler] Warming up instrument cache...")
                    fetch_and_cache_instruments(kite)
                else:
                    log.info(
                        "[scheduler] Kite not connected; instrument cache warm-up skipped."
                    )
        except Exception as exc:
            log.warning("[scheduler] Instrument cache warm-up failed: %s", exc)
        finally:
            cache_warmed_event.set()

    threading.Thread(
        target=_warmup_instrument_cache, daemon=True, name="instrument-cache-startup"
    ).start()

    current_date = datetime.now(IST).date()
    last_scanned_interval: dict[str, int] = {}
    has_done_startup_scan: dict[str, bool] = {}
    has_logged_closed_pre_open: dict[str, bool] = {}

    # If immediate scan is requested, run it once now (bypassing time/day guards)
    if immediate:
        log.info(
            "[scheduler] --now flag detected: waiting for instrument cache to warm up..."
        )
        cache_warmed_event.wait(timeout=60)

        # ── Check data freshness per symbol ────────────────────────────────
        # If recent snapshots exist within the scan frequency interval, run a
        # lightweight dry run (is_test=True). Otherwise fall through to a full
        # scan with decision engine and DB writes.
        all_fresh = True
        for sym in WATCH_SYMBOLS:
            sym_class = get_symbol_class(sym)
            max_age = (
                get_scan_frequency_mcx()
                if sym_class == "MCX_COMMODITY"
                else get_scan_frequency_nse()
            )
            if not has_recent_scan_summary(sym, max_age):
                log.info(
                    "[scheduler] No recent snapshot for %s (max_age=%d min) — full scan needed",
                    sym,
                    max_age,
                )
                all_fresh = False

        if all_fresh:
            log.info(
                "[scheduler] Recent data snapshots available for all symbols — running dry run"
            )
            try:
                run_pipeline(symbols=WATCH_SYMBOLS, is_test=True)
                log.info("[scheduler] Dry run completed successfully.")
            except Exception as e:
                log.error("[scheduler] Dry run failed: %s", e)
        else:
            log.info("[scheduler] Stale/missing data snapshots — running full scan")
            try:
                run_pipeline(symbols=WATCH_SYMBOLS)
                log.info("[scheduler] Full scan completed successfully.")
            except Exception as e:
                log.error("[scheduler] Full scan failed: %s", e)

        # Initialize scheduling state so it doesn't double-scan inside market hours
        import datetime as dt_mod
        import math

        now_ist = datetime.now(IST)
        now_time = now_ist.time()
        for class_key in MARKET_WINDOWS:
            open_t, _, _ = MARKET_WINDOWS[class_key]
            open_h, open_m = map(int, open_t.split(":"))
            market_open_time = now_ist.replace(
                hour=open_h, minute=open_m, second=0, microsecond=0
            )
            delta_minutes = (now_ist - market_open_time).total_seconds() / 60.0

            has_done_startup_scan[class_key] = True
            if delta_minutes >= 0:
                if class_key == "MCX_COMMODITY":
                    interval_min = get_scan_frequency_mcx()
                else:
                    interval_min = get_scan_frequency_nse()
                current_interval_idx = math.floor(delta_minutes / interval_min)
                last_scanned_interval[class_key] = current_interval_idx
            else:
                last_scanned_interval[class_key] = -1
    else:
        # If immediate scan is NOT requested, skip the first scan for the current interval
        import datetime as dt_mod
        import math

        now_ist = datetime.now(IST)
        now_time = now_ist.time()
        for class_key in MARKET_WINDOWS:
            # Enforce custom scan start times: 9:15 am for MCX, 9:30 am for NSE
            if class_key in ("MCX_COMMODITY", "MCX_AGRI"):
                if now_time < dt_mod.time(9, 15):
                    continue
            else:
                if now_time < dt_mod.time(9, 30):
                    continue

            open_t, _, _ = MARKET_WINDOWS[class_key]
            open_h, open_m = map(int, open_t.split(":"))
            market_open_time = now_ist.replace(
                hour=open_h, minute=open_m, second=0, microsecond=0
            )
            delta_minutes = (now_ist - market_open_time).total_seconds() / 60.0
            if delta_minutes >= 0:
                if class_key == "MCX_COMMODITY":
                    interval_min = get_scan_frequency_mcx()
                else:
                    interval_min = get_scan_frequency_nse()
                current_interval_idx = math.floor(delta_minutes / interval_min)

                has_done_startup_scan[class_key] = True
                last_scanned_interval[class_key] = current_interval_idx
                log.info(
                    "Bypassing immediate startup scan for %s. Next scan will trigger at interval index %d.",
                    class_key,
                    current_interval_idx + 1,
                )

    last_cmp_refresh = 0.0
    last_instrument_cache_refresh = (
        time.time()
    )  # mark as refreshed now (warmup thread handles first)
    _INSTRUMENT_CACHE_REFRESH_INTERVAL = 4 * 60 * 60  # 4 hours
    last_kite_sync_refresh = 0.0
    _KITE_POSITION_SYNC_INTERVAL = 5 * 60  # L3: sync Kite positions every 5 minutes
    _scan_attempts: dict[tuple[str, int], int] = {}
    _last_scan_attempt_time: dict[str, float] = {}

    _last_friday_nse_exit_date = None
    _last_friday_mcx_exit_date = None
    _last_auto_login_date = None
    _last_fii_fetch_date = None
    _last_weights_refresh_date = None
    last_ng_eia_pre_print_close_date = None
    last_ng_eia_consensus_fetch_date = None
    last_ng_exit_check = 0.0

    try:
        while True:
            now_ts = time.time()

            now_ist = datetime.fromtimestamp(now_ts, IST)

            if now_ist.date() > current_date:
                current_date = now_ist.date()
                last_scanned_interval.clear()
                has_done_startup_scan.clear()
                has_logged_closed_pre_open.clear()

            # ── Monday Weightage Refresh ──
            if now_ist.weekday() == 0 and _last_weights_refresh_date != current_date:
                _last_weights_refresh_date = current_date
                log.info("[scheduler] Triggering weekly index weights refresh (Monday)")
                try:
                    from src.engine.index_weights import refresh_index_weights_async
                    refresh_index_weights_async(force=False)
                except Exception as exc:
                    log.error("[scheduler] Index weights refresh trigger exception: %s", exc)

            # ── Pre-market: headless Kite auto-login at ~08:45 IST Mon-Fri ──
            # Runs once per day for NSE indices (NSE opens at 09:15).
            # Skips weekends and market holidays automatically.
            if now_ist.weekday() < 5:  # Monday=0 … Friday=4
                now_time_str = now_ist.strftime("%H:%M")
                if now_time_str == "08:45" and _last_auto_login_date != current_date:
                    _last_auto_login_date = current_date
                    log.info(
                        "[scheduler] Pre-market: triggering headless Kite auto-login (08:45 IST)"
                    )
                    try:
                        from src.services.zerodha_auto_login import auto_login_kite

                        result = auto_login_kite(force=False)
                        if result.get("success"):
                            log.info(
                                "[scheduler] Kite auto-login: %s",
                                result.get("message", "OK"),
                            )
                        else:
                            log.warning(
                                "[scheduler] Kite auto-login failed: %s",
                                result.get("message", "unknown error"),
                            )
                    except Exception as exc:
                        log.error("[scheduler] Kite auto-login exception: %s", exc)

            # ── Post-market: FII/DII Data Fetch at 19:15 IST (Mon-Fri) ──
            if now_ist.weekday() < 5:
                now_time_str = now_ist.strftime("%H:%M")
                if now_time_str == "19:15" and _last_fii_fetch_date != current_date:
                    _last_fii_fetch_date = current_date
                    log.info(
                        "[scheduler] Triggering FII/DII positioning fetch (19:15 IST)"
                    )
                    try:
                        from src.fetchers.nse_archive_fetcher import (
                            fetch_and_store_fii_positioning,
                        )

                        threading.Thread(
                            target=fetch_and_store_fii_positioning,
                            daemon=True,
                            name="fii-fetcher",
                        ).start()
                    except Exception as exc:
                        log.error("[scheduler] FII/DII fetcher exception: %s", exc)

            # Friday Weekend Risk Auto-Exits
            if now_ist.weekday() == 4:  # Friday
                current_time_str = now_ist.strftime("%H:%M")
                if (
                    current_time_str == "15:28"
                    and _last_friday_nse_exit_date != current_date
                ):
                    _last_friday_nse_exit_date = current_date
                    try:
                        exit_all_positions_friday("NSE_INDEX")
                        exit_all_positions_friday("BSE_INDEX")
                    except Exception as e:
                        log.error("Friday auto-exit failed for NSE/BSE: %s", e)
                elif (
                    current_time_str == "23:28"
                    and _last_friday_mcx_exit_date != current_date
                ):
                    _last_friday_mcx_exit_date = current_date
                    try:
                        exit_all_positions_friday("MCX_COMMODITY")
                    except Exception as e:
                        log.error("Friday auto-exit failed for MCX: %s", e)

            # 1. Full Scan Loop per market class
            import math

            for class_key in MARKET_WINDOWS:
                class_symbols = [
                    s for s in WATCH_SYMBOLS if get_symbol_class(s) == class_key
                ]
                if not class_symbols:
                    continue

                open_t, close_t, days = MARKET_WINDOWS[class_key]
                open_h, open_m = map(int, open_t.split(":"))
                market_open_time = now_ist.replace(
                    hour=open_h, minute=open_m, second=0, microsecond=0
                )

                delta_minutes = (now_ist - market_open_time).total_seconds() / 60.0
                if delta_minutes < 0:
                    if not has_logged_closed_pre_open.get(class_key, False):
                        log.info(
                            "[%s] Market is closed (opens at %s). Scheduler will sleep until open.",
                            class_key,
                            open_t,
                        )
                        has_logged_closed_pre_open[class_key] = True
                    continue

                import datetime as dt_mod

                now_time = now_ist.time()
                if class_key in ("MCX_COMMODITY", "MCX_AGRI"):
                    if now_time < dt_mod.time(9, 15):
                        if not has_logged_closed_pre_open.get(class_key, False):
                            log.info(
                                "[%s] Market is closed (NSEBOT waits until 09:15 for MCX). Scheduler will sleep until open.",
                                class_key,
                            )
                            has_logged_closed_pre_open[class_key] = True
                        continue
                else:
                    if now_time < dt_mod.time(9, 30):
                        if not has_logged_closed_pre_open.get(class_key, False):
                            log.info(
                                "[%s] Market is closed (NSEBOT waits until 09:30 for NSE). Scheduler will sleep until open.",
                                class_key,
                            )
                            has_logged_closed_pre_open[class_key] = True
                        continue

                if class_key == "MCX_COMMODITY":
                    interval_min = get_scan_frequency_mcx()
                else:
                    interval_min = get_scan_frequency_nse()

                current_interval_idx = math.floor(delta_minutes / interval_min)
                should_scan = False

                data_available = _latest_interval_data_available(class_key, current_interval_idx, interval_min, market_open_time)

                if not data_available:
                    attempts = _scan_attempts.get((class_key, current_interval_idx), 0)
                    last_attempt = _last_scan_attempt_time.get(class_key, 0.0)
                    if attempts < 3 and (time.time() - last_attempt >= 60.0):
                        should_scan = True
                        _scan_attempts[(class_key, current_interval_idx)] = attempts + 1
                        _last_scan_attempt_time[class_key] = time.time()
                else:
                    last_scanned_interval[class_key] = max(
                        last_scanned_interval.get(class_key, -1),
                        current_interval_idx
                    )

                if should_scan:
                    cycle_start = time.time()
                    log.info(
                        "Triggering scan for %s (interval idx: %d, time since open: %.1f min)",
                        class_key,
                        current_interval_idx,
                        delta_minutes,
                    )

                    def run_all():
                        _guarded_run(class_key)
                        if class_key == "MCX_COMMODITY":
                            _run_dhan_naturalgas_scrape()

                    success = run_with_timeout(run_all, timeout=600)
                    if not success:
                        try:
                            from src.alerts.telegram_dispatcher import send_text

                            send_text(
                                f"⚠️ **NSEBOT ALERT**: Scheduler scan loop timed out/hung after 5 minutes for {class_key}. Watchdog bypassed it."
                            )
                        except Exception:
                            pass
                    elapsed = time.time() - cycle_start
                    log.debug(
                        "Full scan cycle completed for %s in %.1fs (success=%s)",
                        class_key,
                        elapsed,
                        success,
                    )

            # 2a. L3: Kite Position Sync Loop (every 5 minutes)
            if time.time() - last_kite_sync_refresh >= _KITE_POSITION_SYNC_INTERVAL:
                last_kite_sync_refresh = time.time()

                def _sync_kite_positions():
                    try:
                        from src.engine.live_trading import sync_direct_kite_positions

                        sync_direct_kite_positions()
                    except Exception as exc:
                        log.warning("[scheduler] Kite position sync failed: %s", exc)

                threading.Thread(
                    target=_sync_kite_positions, daemon=True, name="kite-position-sync"
                ).start()

            # 2b. Live CMP Refresh Loop (every 15 minutes)
            if time.time() - last_cmp_refresh >= 900:
                cycle_start = time.time()
                success = run_with_timeout(_update_live_cmps, timeout=120)
                elapsed = time.time() - cycle_start
                last_cmp_refresh = time.time()
                if elapsed > 0.5:
                    log.debug(
                        "Live CMP refresh completed in %.1fs (success=%s)",
                        elapsed,
                        success,
                    )

            # 3. Instrument cache periodic refresh (every 4 hours)
            if (
                time.time() - last_instrument_cache_refresh
                >= _INSTRUMENT_CACHE_REFRESH_INTERVAL
            ):
                last_instrument_cache_refresh = time.time()

                def _refresh_cache():
                    try:
                        from src.engine.live_trading import get_kite_client
                        from src.engine.symbol_resolver import (
                            fetch_and_cache_instruments,
                        )

                        kite = get_kite_client()
                        if kite:
                            log.info("[scheduler] Periodic instrument cache refresh...")
                            fetch_and_cache_instruments(kite)
                    except Exception as exc:
                        log.warning(
                            "[scheduler] Periodic instrument cache refresh failed: %s",
                            exc,
                        )

                threading.Thread(
                    target=_refresh_cache, daemon=True, name="instrument-cache-periodic"
                ).start()

            # 4. Phase 2: Weekly ML Training Job (Sunday 2 AM IST)
            # AI_INTELLIGENCE_ROADMAP_v3.0 — Event-driven triggers are handled
            # separately in pipeline.py via on_trade_closed(). This is the weekly
            # fallback as a safety net.
            current_week = now_ist.isocalendar()[1]  # ISO week number
            is_sunday = now_ist.weekday() == 6  # 0=Monday, 6=Sunday
            is_2am = 2 <= now_ist.hour < 3  # 2:00-2:59 AM IST
            if is_sunday and is_2am and _last_ml_training_week != current_week:
                _last_ml_training_week = current_week
                log.info(
                    "[scheduler] Phase 2: Weekly ML training job triggered (Sunday 2 AM IST)"
                )

                def _run_weekly_ml_training():
                    try:
                        from src.scheduler.ml_training_job import run_weekly_training

                        run_weekly_training()
                    except Exception as exc:
                        log.warning("[scheduler] Weekly ML training failed: %s", exc)

                threading.Thread(
                    target=_run_weekly_ml_training,
                    daemon=True,
                    name="ml-training-weekly",
                ).start()

            # 5. EIA Report Job (Thursday 8:00 PM IST)
            is_thursday = now_ist.weekday() == 3  # 0=Monday, 3=Thursday
            is_8pm = now_ist.hour == 20 and now_ist.minute == 0
            if is_thursday and is_8pm and _last_eia_run_date != current_date:
                _last_eia_run_date = current_date
                log.info("[scheduler] EIA Report Job triggered (Thursday 8:00 PM IST)")

                def _run_eia_analyzer():
                    try:
                        from src.engine.eia_analyzer import analyze_eia_report

                        analyze_eia_report()
                    except Exception as exc:
                        log.warning("[scheduler] EIA Report analyzer failed: %s", exc)

                threading.Thread(
                    target=_run_eia_analyzer, daemon=True, name="eia-analyzer"
                ).start()

            # 6. Daily Telegram Backup (Runs at 23:56 PM IST, after last MCX scan)
            is_1156pm = now_ist.hour == 23 and now_ist.minute == 56
            if is_1156pm and _last_backup_date != current_date:
                _last_backup_date = current_date
                log.info(
                    "[scheduler] Daily Telegram database backup triggered (23:56 PM IST)"
                )

                def _run_telegram_backup():
                    try:
                        from src.utils.gdrive_backup import backup_db_to_telegram

                        backup_db_to_telegram()
                    except Exception as exc:
                        log.warning(
                            "[scheduler] Daily Telegram database backup failed: %s", exc
                        )

                threading.Thread(
                    target=_run_telegram_backup, daemon=True, name="telegram-backup"
                ).start()

            # 6b. Nightly Trade Autopsy (Runs at autopsy_time_ist, default 23:45 IST)
            from config.runtime_config import load_runtime_config
            rconf = load_runtime_config()
            autopsy_time = rconf.get("autopsy_time_ist", "23:45")
            try:
                autopsy_hour, autopsy_minute = map(int, autopsy_time.split(":"))
                is_autopsy_time = now_ist.hour == autopsy_hour and now_ist.minute == autopsy_minute
            except Exception:
                is_autopsy_time = now_ist.hour == 23 and now_ist.minute == 45

            if is_autopsy_time and _last_autopsy_date != current_date:
                _last_autopsy_date = current_date
                log.info(
                    "[scheduler] Nightly trade autopsy triggered (%s IST)", autopsy_time
                )

                def _run_autopsy():
                    try:
                        from src.engine.autopsy_writer import run_nightly_autopsy

                        run_nightly_autopsy()
                    except Exception as exc:
                        log.warning(
                            "[scheduler] Nightly trade autopsy failed: %s", exc
                        )

                threading.Thread(
                    target=_run_autopsy, daemon=True, name="autopsy-writer"
                ).start()

            # 7. Natural Gas Exit Check Loop (Runs every 120 seconds)
            if time.time() - last_ng_exit_check >= 120:
                last_ng_exit_check = time.time()

                def _run_ng_exits():
                    try:
                        from src.engine.ng_parity_strategy import (
                            check_ng_parity_exits_every_2_min,
                        )

                        check_ng_parity_exits_every_2_min()

                        from src.engine.ng_eia_strategy import (
                            check_ng_eia_exits_every_2_min,
                        )

                        check_ng_eia_exits_every_2_min()

                        from src.engine.ng_momentum_strategy import (
                            check_ng_weekend_flat,
                        )

                        check_ng_weekend_flat()
                    except Exception as exc:
                        log.warning("[scheduler] NG exits check failed: %s", exc)

                threading.Thread(
                    target=_run_ng_exits, daemon=True, name="ng-exits-check"
                ).start()

            # 8. EIA Pre-Print Force Close (Thursday 19:40 IST)
            if now_ist.weekday() == 3:  # Thursday
                now_time_str = now_ist.strftime("%H:%M")
                if (
                    now_time_str == "19:40"
                    and last_ng_eia_pre_print_close_date != current_date
                ):
                    last_ng_eia_pre_print_close_date = current_date
                    try:
                        from src.engine.ng_eia_strategy import force_close_eia_pre_print

                        force_close_eia_pre_print()
                    except Exception as e:
                        log.error("[scheduler] EIA pre-print force close failed: %s", e)

            # 9. EIA Consensus Scraper (Wednesday 20:00 IST)
            if now_ist.weekday() == 2:  # Wednesday
                now_time_str = now_ist.strftime("%H:%M")
                if (
                    now_time_str == "20:00"
                    and last_ng_eia_consensus_fetch_date != current_date
                ):
                    last_ng_eia_consensus_fetch_date = current_date
                    log.info(
                        "[scheduler] EIA Consensus Scraper Job triggered (Wednesday 8:00 PM IST)"
                    )

                    def _run_eia_fetch():
                        try:
                            from src.fetchers.eia_consensus_fetcher import (
                                fetch_and_store_eia_consensus,
                            )

                            fetch_and_store_eia_consensus()
                        except Exception as exc:
                            log.warning(
                                "[scheduler] EIA consensus fetch failed: %s", exc
                            )

                    threading.Thread(
                        target=_run_eia_fetch, daemon=True, name="eia-consensus-fetch"
                    ).start()

            # ── OPS Agent: heartbeat + health stamps ────────────────────────
            try:
                from pathlib import Path as _P
                _hb = _P("/tmp/nsebot.heartbeat")
                _hb.write_text(str(int(time.time())))
                from src.models.schema import stamp_health, stamp_open_positions
                stamp_health("scheduler_loop", "OK", f"interval_idx={current_interval_idx if 'current_interval_idx' in dir() else '?'}")
                stamp_open_positions()
                # Auto-heal transient telegram_send errors if idle > 30m without new failures
                try:
                    from src.models.schema import read_health_state
                    for h in read_health_state():
                        if h.get("key") == "telegram_send" and h.get("status") in ("DOWN", "DEGRADED"):
                            ts_str = h.get("updated_at", "")
                            if ts_str:
                                dt = datetime.fromisoformat(ts_str)
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=IST)
                                if (datetime.now(IST) - dt).total_seconds() > 1800:
                                    stamp_health("telegram_send", "OK", "Idle (subsequent scans OK)")
                except Exception:
                    pass
            except Exception:
                pass

            # Sleep in short increments to remain responsive to intervals and changes

            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")
