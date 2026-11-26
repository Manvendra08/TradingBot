"""Stock Futures Scheduled Jobs.
Implements Section 3 & Task 15:
- 08:45: Ban list & event refresh
- 09:30: Sequential morning routine:
  1. execute_daily_exits()
  2. await/confirm exit fills
  3. refresh_broker_positions_and_margins()
  4. execute_armed_entries()
- 19:30: EOD scan on completed daily bars -> ARMED watchlist
- Every 15 min: Periodic reconciliation + portfolio risk monitor
- 1-minute GTT watcher (when positions open)
Weekday-only and holiday-aware.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from config.holidays import get_nse_holidays
from config.runtime_config import (
    is_stockfut_live_enabled,
    is_stockfut_paper_enabled,
    load_runtime_config,
)
from config.stockfut_loader import load_stockfut_config, load_stockfut_sectors
from src.fetchers.kite_fetcher import get_kite_data_client
from src.fetchers.nse_eod_fetcher import fetch_ban_list, fetch_events, fetch_mwpl
from src.alerts.futures_digest import (
    send_eod_watchlist_alert,
    send_morning_execution_alert,
    send_trade_event_alert,
)
from src.models.schema import (
    get_latest_stockfut_decision,
    get_latest_stockfut_watchlist,
    get_open_stockfut_trades,
    get_stockfut_trade_by_id,
    log_stockfut_decision,
    update_stockfut_watchlist_status,
)
from src.stockfut.executor import PaperExecutor
from src.stockfut.exits import evaluate_future_exit
from src.stockfut.features import compute_features, market_regime
from src.stockfut.levels import build_zones
from src.stockfut.portfolio_risk import check_entry
from src.stockfut.reconcile import reconcile_periodic, reconcile_startup
from src.stockfut.setups import (
    bias_label,
    build_watchlist,
    is_daily_reversal,
    is_retryable_skip,
    matrix_permission,
    midday_recheck,
    open_check,
    stock_bias,
    WatchRow,
)
from src.stockfut.universe import UniverseBuilder

log = logging.getLogger("stockfut.jobs")
IST = timezone(timedelta(hours=5, minutes=30))


def is_weekday(dt: Optional[datetime] = None) -> bool:
    """Return True if Monday through Friday."""
    now_dt = dt or datetime.now(IST)
    return now_dt.weekday() < 5


def is_trading_day(d: Optional[date] = None) -> bool:
    """Return True if date is a weekday and not an NSE trading holiday."""
    target = d or datetime.now(IST).date()
    if target.weekday() >= 5:
        return False
    return target not in get_nse_holidays(target.year)


def prev_trading_date(ref_date: date) -> date:
    """Return the most recent NSE trading session date strictly before ref_date."""
    cur = ref_date - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return ref_date - timedelta(days=1)


_LAST_MORNING_REFRESH_STATUS: Dict[str, Any] = {}


def get_last_morning_refresh_status() -> Dict[str, Any]:
    """Return the latest 08:45 IST StockFut morning refresh telemetry."""
    return dict(_LAST_MORNING_REFRESH_STATUS)


def job_stockfut_morning_refresh() -> None:
    """08:45 IST: Refresh ban list, MWPL, and corporate events."""
    if not is_trading_day():
        return
    log.info("[STOCKFUT 08:45] Refreshing NSE ban list, MWPL, and event calendar...")
    try:
        ban = fetch_ban_list()
        mwpl = fetch_mwpl()
        events = fetch_events(days=7)
        _LAST_MORNING_REFRESH_STATUS.clear()
        _LAST_MORNING_REFRESH_STATUS.update(
            {
                "date": datetime.now(IST).strftime("%Y-%m-%d"),
                "ok": True,
                "banned": len(ban),
                "mwpl": len(mwpl),
                "events": len(events),
            }
        )
        log.info("[STOCKFUT 08:45] Refreshed: %d banned, %d MWPL entries, %d events", len(ban), len(mwpl), len(events))
    except Exception as e:
        _LAST_MORNING_REFRESH_STATUS.clear()
        _LAST_MORNING_REFRESH_STATUS.update(
            {
                "date": datetime.now(IST).strftime("%Y-%m-%d"),
                "ok": False,
                "error": str(e),
            }
        )
        log.error("[STOCKFUT 08:45] Morning refresh failed: %s", e)


def job_stockfut_morning_execution() -> None:
    """
    09:30 IST: Sequential Morning Routine:
    1. execute_daily_exits()
    2. await/confirm exit fills
    3. refresh positions and margin
    4. execute_armed_entries()
    """
    if not is_trading_day():
        return
    log.info("[STOCKFUT 09:30] Starting morning execution routine...")

    cfg = load_stockfut_config()
    client = get_kite_data_client()
    executor = PaperExecutor(cfg=cfg)

    # Step 1: Execute Daily Exits first
    open_trades = get_open_stockfut_trades()
    if open_trades:
        log.info("[STOCKFUT 09:30] Evaluating daily exits for %d open trades...", len(open_trades))
        # Batch quote for open symbols
        symbols = [t["tradingsymbol"] for t in open_trades]
        quotes = client.get_futures_quotes(symbols)

        for trade in open_trades:
            tsym = trade["tradingsymbol"]
            q = quotes.get(tsym)
            if not q:
                continue
            dec = evaluate_future_exit(trade=trade, market=q)
            if dec:
                log.info("[STOCKFUT 09:30] Trade #%d (%s) triggered %s (%s)", trade["id"], tsym, dec.rule, dec.action)
                executor.apply_exit(trade["id"], decision=dec, quote=q)

    # Step 2: Short buffer to allow fills to settle in position book
    time.sleep(2)

    # Step 3: Refresh open positions and execute ARMED entries
    rt_cfg = load_runtime_config()
    if rt_cfg.get("trading_paused", False):
        log.info("[STOCKFUT 09:30] Trading paused in runtime_config; skipping new entries")
        return

    paper_on = is_stockfut_paper_enabled()
    live_on = is_stockfut_live_enabled()
    if not paper_on and not live_on:
        log.info("[STOCKFUT 09:30] Stock Futures paper and live execution disabled; skipping entries")
        return

    refreshed_open = get_open_stockfut_trades()
    open_symbols: Set[str] = {str(t["symbol"]).upper() for t in refreshed_open}
    sector_map = load_stockfut_sectors()
    equity = float(cfg.get("capital_reference", 10000000.0))

    # EOD scans run on Day T (or last trading session); morning execution runs at Day T+1 09:30 IST.
    today_date = datetime.now(IST).date()
    today_str = today_date.strftime("%Y-%m-%d")
    prev_session_str = prev_trading_date(today_date).strftime("%Y-%m-%d")

    watchlist = get_latest_stockfut_watchlist(scan_date=None)
    eligible_armed: List[Dict[str, Any]] = []
    for r in watchlist:
        if r.get("status") != "ARMED":
            continue
        r_scan_date = str(r.get("scan_date") or "")
        if r_scan_date >= prev_session_str:
            eligible_armed.append(r)
        else:
            update_stockfut_watchlist_status(r["id"], status="EXPIRED", skip_reason=f"STALE_SCAN_{r_scan_date}")

    armed_rows = eligible_armed

    if not armed_rows:
        log.info("[STOCKFUT 09:30] No ARMED setups to execute for today (%s, min scan_date=%s)", today_str, prev_session_str)
        return

    log.info("[STOCKFUT 09:30] Evaluating %d ARMED setups...", len(armed_rows))
    armed_contracts = [r["tradingsymbol"] for r in armed_rows]
    live_quotes = client.get_futures_quotes(armed_contracts)

    fills: List[Dict[str, Any]] = []
    rejections: List[Dict[str, Any]] = []

    for r_dict in armed_rows:
        row = _watchlist_dict_to_row(r_dict)
        if row.symbol.upper() in open_symbols:
            update_stockfut_watchlist_status(r_dict["id"], status="SKIPPED", skip_reason="ALREADY_OPEN")
            rejections.append({"symbol": row.symbol, "reason": "ALREADY_OPEN"})
            continue

        quote = live_quotes.get(row.tradingsymbol)
        if not quote:
            update_stockfut_watchlist_status(r_dict["id"], status="RETRY", skip_reason="MISSING_QUOTE")
            rejections.append({"symbol": row.symbol, "reason": "MISSING_QUOTE (RETRY @ 12:20)"})
            continue

        # Open check — use stored ATR from EOD scan; fall back to SL distance
        stored_atr = float(r_dict.get("atr") or 0.0)
        atr = stored_atr if stored_atr > 0 else abs(row.planned_entry - row.sl)
        passed_open, open_reason = open_check(
            row,
            quote=quote,
            atr=atr,
            max_drift_atr=float(cfg.get("entry_max_drift_atr", 0.6)),
            max_spread_pct=float(cfg.get("max_spread_pct", 0.10)),
        )
        if not passed_open:
            next_status = "RETRY" if is_retryable_skip(open_reason) else "SKIPPED"
            update_stockfut_watchlist_status(r_dict["id"], status=next_status, skip_reason=open_reason)
            suffix = " (RETRY @ 12:20)" if next_status == "RETRY" else ""
            rejections.append({"symbol": row.symbol, "reason": f"{open_reason}{suffix}"})
            continue

        # Portfolio risk check (with sector map)
        c_cand = {
            "symbol": row.symbol,
            "side": row.side,
            "lots": row.lots,
            "lot_size": row.lot_size,
            "entry_price": float(quote["price"]),
        }
        passed_risk, risk_reason = check_entry(
            c_cand, refreshed_open, equity=equity, cfg=cfg, sector_map=sector_map
        )
        if not passed_risk:
            next_status = "RETRY" if is_retryable_skip(risk_reason) else "SKIPPED"
            update_stockfut_watchlist_status(r_dict["id"], status=next_status, skip_reason=risk_reason)
            suffix = " (RETRY @ 12:20)" if next_status == "RETRY" else ""
            rejections.append({"symbol": row.symbol, "reason": f"{risk_reason}{suffix}"})
            continue

        # Execute
        trade_id = executor.enter(row, quote=quote, order_tag=f"SF_PAPER_0930_{row.symbol}")
        if trade_id:
            update_stockfut_watchlist_status(r_dict["id"], status="TRIGGERED")
            fills.append({
                "tradingsymbol": row.tradingsymbol,
                "symbol": row.symbol,
                "direction": row.side,
                "lots": row.lots,
                "entry_price": float(quote["price"]),
                "sl_price": row.sl,
                "t1_price": row.t1,
            })
            refreshed_open = get_open_stockfut_trades()
            open_symbols.add(row.symbol.upper())

    try:
        send_morning_execution_alert(today_str, fills, rejections, refreshed_open)
    except Exception as e:
        log.warning("[STOCKFUT 09:30] Morning alert failed: %s", e)


def _watchlist_dict_to_row(r_dict: Dict[str, Any]) -> WatchRow:
    """Convert a stockfut_watchlist database row dict to WatchRow."""
    return WatchRow(
        scan_date=r_dict["scan_date"],
        scan_time=r_dict["scan_time"],
        symbol=r_dict["symbol"],
        tradingsymbol=r_dict["tradingsymbol"],
        expiry=r_dict["expiry"],
        side=r_dict["side"],
        bias=r_dict["bias"],
        score=int(r_dict["score"]),
        planned_entry=float(r_dict["planned_entry"]),
        sl=float(r_dict["sl"]),
        t1=float(r_dict["t1"]),
        t2=float(r_dict["t2"]),
        lots=int(r_dict["lots"]),
        lot_size=int(r_dict["lot_size"]),
        r_multiple=float(r_dict["r_multiple"]),
        status=r_dict["status"],
        snapshot_id=r_dict.get("snapshot_id"),
        atr=float(r_dict["atr"]) if r_dict.get("atr") else None,
        regime=str(r_dict.get("regime") or "NEUTRAL"),
    )


def _compute_live_regime(
    client: Any,
    nifty_daily: Optional[List[Dict[str, Any]]] = None,
    features_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """Compute current RISK_ON | NEUTRAL | RISK_OFF regime from Nifty, breadth, and India VIX."""
    now_dt = datetime.now(IST)
    if nifty_daily is None:
        try:
            nifty_tok = client.resolve_instrument_token("NIFTY") or 256265
            nifty_daily = client.get_historical_candles(
                nifty_tok, interval="day", from_date=now_dt - timedelta(days=100), to_date=now_dt
            )
        except Exception as e:
            log.debug("Regime Nifty fetch error: %s", e)
            nifty_daily = []

    breadth: Optional[float] = None
    if features_map:
        valid = [f for f in features_map.values() if float(f.get("ema50") or 0.0) > 0]
        if valid:
            above = sum(1 for f in valid if float(f.get("close") or 0.0) > float(f["ema50"]))
            breadth = (above / len(valid)) * 100.0

    vix_val: Optional[float] = None
    vix_chg: Optional[float] = None
    try:
        vix_tok = client.resolve_instrument_token("INDIAVIX") or 264969
        vix_daily = client.get_historical_candles(
            vix_tok, interval="day", from_date=now_dt - timedelta(days=10), to_date=now_dt
        )
        if vix_daily:
            vix_val = float(vix_daily[-1]["close"])
            if len(vix_daily) >= 2 and float(vix_daily[-2]["close"]) > 0:
                prev_vix = float(vix_daily[-2]["close"])
                vix_chg = ((vix_val - prev_vix) / prev_vix) * 100.0
    except Exception as e:
        log.debug("Regime VIX fetch error: %s", e)

    return market_regime(
        index_daily=nifty_daily,
        universe_breadth_pct_above_ema50=breadth,
        vix_value=vix_val,
        vix_change_pct=vix_chg,
    )


def job_stockfut_midday_retry() -> None:
    """
    12:20 IST: Re-evaluate transient 09:30 RETRY setups.
    Validates intraday high/low SL integrity, ban list, regime, SL cushion, and live R >= min_r.
    Unfilled RETRY rows expire after 12:20 to avoid late-day chasing.
    """
    if not is_trading_day():
        return

    rt_cfg = load_runtime_config()
    if rt_cfg.get("trading_paused", False):
        return
    if not is_stockfut_paper_enabled() and not is_stockfut_live_enabled():
        return

    today_date = datetime.now(IST).date()
    today_str = today_date.strftime("%Y-%m-%d")
    prev_session_str = prev_trading_date(today_date).strftime("%Y-%m-%d")

    watchlist = get_latest_stockfut_watchlist(scan_date=None)
    retry_rows: List[Dict[str, Any]] = []
    for r in watchlist:
        if r.get("status") != "RETRY":
            continue
        r_scan_date = str(r.get("scan_date") or "")
        if r_scan_date >= prev_session_str:
            retry_rows.append(r)
        else:
            update_stockfut_watchlist_status(r["id"], status="EXPIRED", skip_reason=f"STALE_RETRY_{r_scan_date}")

    if not retry_rows:
        log.info("[STOCKFUT 12:20] No RETRY setups pending for %s", today_str)
        return

    log.info("[STOCKFUT 12:20] Evaluating %d RETRY setups...", len(retry_rows))
    cfg = load_stockfut_config()
    client = get_kite_data_client()
    executor = PaperExecutor(cfg=cfg)
    sector_map = load_stockfut_sectors()
    equity = float(cfg.get("capital_reference", 10000000.0))

    live_quotes = client.get_futures_quotes([r["tradingsymbol"] for r in retry_rows])
    try:
        ban_list = fetch_ban_list()
    except Exception:
        ban_list = set()
    try:
        events_dict = fetch_events(days=int(cfg.get("events_blackout_sessions", 7)))
    except Exception:
        events_dict = {}
    current_regime = _compute_live_regime(client)

    refreshed_open = get_open_stockfut_trades()
    open_symbols: Set[str] = {str(t["symbol"]).upper() for t in refreshed_open}
    fills: List[Dict[str, Any]] = []
    rejections: List[Dict[str, Any]] = []

    for r_dict in retry_rows:
        row = _watchlist_dict_to_row(r_dict)
        if row.symbol.upper() in open_symbols:
            update_stockfut_watchlist_status(r_dict["id"], status="EXPIRED", skip_reason="ALREADY_OPEN")
            rejections.append({"symbol": row.symbol, "reason": "ALREADY_OPEN"})
            continue

        quote = live_quotes.get(row.tradingsymbol)
        stored_atr = float(r_dict.get("atr") or 0.0)
        atr = stored_atr if stored_atr > 0 else abs(row.planned_entry - row.sl)
        passed_mid, mid_reason = midday_recheck(
            row,
            quote=quote or {},
            atr=atr,
            max_spread_pct=float(cfg.get("max_spread_pct", 0.10)),
            min_r_multiple=float(cfg.get("midday_min_r_multiple", 1.25)),
            min_sl_atr=float(cfg.get("midday_min_sl_atr", 0.4)),
            ban_list=ban_list,
            events_dict=events_dict,
            current_regime=current_regime,
            cfg=cfg,
        )
        if not passed_mid:
            update_stockfut_watchlist_status(r_dict["id"], status="EXPIRED", skip_reason=mid_reason)
            rejections.append({"symbol": row.symbol, "reason": mid_reason})
            continue

        c_cand = {
            "symbol": row.symbol,
            "side": row.side,
            "lots": row.lots,
            "lot_size": row.lot_size,
            "entry_price": float(quote["price"]),
        }
        passed_risk, risk_reason = check_entry(
            c_cand, refreshed_open, equity=equity, cfg=cfg, sector_map=sector_map
        )
        if not passed_risk:
            update_stockfut_watchlist_status(r_dict["id"], status="EXPIRED", skip_reason=risk_reason)
            rejections.append({"symbol": row.symbol, "reason": risk_reason})
            continue

        trade_id = executor.enter(row, quote=quote, order_tag=f"SF_PAPER_1220_RETRY_{row.symbol}")
        if trade_id:
            update_stockfut_watchlist_status(r_dict["id"], status="TRIGGERED")
            fills.append({
                "tradingsymbol": row.tradingsymbol,
                "symbol": row.symbol,
                "direction": row.side,
                "lots": row.lots,
                "entry_price": float(quote["price"]),
                "sl_price": row.sl,
                "t1_price": row.t1,
            })
            refreshed_open = get_open_stockfut_trades()
            open_symbols.add(row.symbol.upper())

    try:
        send_morning_execution_alert(
            today_str, fills, rejections, refreshed_open, window_label="12:20 IST RETRY"
        )
    except Exception as e:
        log.warning("[STOCKFUT 12:20] Midday retry alert failed: %s", e)


def _run_eod_scan_pipeline(persist: bool = True, asof_time: str = "19:30:00") -> Tuple[List[WatchRow], str, str]:
    """Shared universe scan pipeline used by both 15:10 provisional logger and 19:30 EOD scan."""
    cfg = load_stockfut_config()
    client = get_kite_data_client()
    builder = UniverseBuilder(cfg=cfg, kite_client=client)

    now_dt = datetime.now(IST)
    asof = now_dt.date()
    asof_str = asof.strftime("%Y-%m-%d")
    equity = float(cfg.get("capital_reference", 10000000.0))

    universe = builder.build_universe(asof=asof, capital=equity)
    if not universe:
        return [], "NEUTRAL", asof_str

    features_map: Dict[str, Dict[str, Any]] = {}
    zones_map: Dict[str, List[Any]] = {}
    candles_map: Dict[str, List[Dict[str, Any]]] = {}
    from_date = now_dt - timedelta(days=120)

    # Benchmark: Nifty 50 daily candles for relative strength & regime
    nifty_tok = client.resolve_instrument_token("NIFTY") or 256265
    nifty_daily = client.get_historical_candles(nifty_tok, interval="day", from_date=from_date, to_date=now_dt)

    # Pass 1: Collect cash candles, futures OI series, and 20-day returns across universe
    raw_stock_data: Dict[str, Dict[str, Any]] = {}
    universe_20d_returns: List[float] = []

    for contract in universe:
        sym = contract.symbol
        try:
            token = client.resolve_instrument_token(sym)
            if not token:
                continue
            daily = client.get_historical_candles(
                instrument_token=token,
                interval="day",
                from_date=from_date,
                to_date=now_dt,
            )
            if not daily or len(daily) < 25:
                continue

            f_candles = client.get_historical_candles(
                instrument_token=contract.token,
                interval="day",
                from_date=from_date,
                to_date=now_dt,
            ) if contract.token else []
            oi_series = [b.get("oi", 0) for b in f_candles] if f_candles else []

            closes = [float(b["close"]) for b in daily]
            ret_20d = (closes[-1] - closes[-20]) / closes[-20] * 100.0 if len(closes) >= 20 and closes[-20] > 0 else 0.0
            universe_20d_returns.append(ret_20d)

            raw_stock_data[sym] = {
                "daily": daily,
                "oi_series": oi_series,
                "contract": contract,
            }
            time.sleep(0.03)
        except Exception as e:
            log.debug("[%s] Scan candle fetch error: %s", sym, e)

    # Pass 2: Compute full features with cross-universe relative strength & futures OI
    for sym, d in raw_stock_data.items():
        daily = d["daily"]
        oi_series = d["oi_series"]
        candles_map[sym] = daily
        feat = compute_features(
            sym,
            daily,
            index_daily=nifty_daily,
            universe_20d_returns=universe_20d_returns,
            combined_oi_daily=oi_series,
        )
        if not feat:
            continue
        features_map[sym] = feat
        atr = float(feat.get("atr14", 10.0))
        zones_map[sym] = build_zones(daily, atr=atr)

    regime = _compute_live_regime(client, nifty_daily=nifty_daily, features_map=features_map)
    open_symbols: Set[str] = {str(t["symbol"]).upper() for t in get_open_stockfut_trades()}

    top_watchlist = build_watchlist(
        universe=universe,
        features_map=features_map,
        zones_map=zones_map,
        regime=regime,
        asof_date=asof_str,
        asof_time=asof_time,
        cfg=cfg,
        capital=equity,
        daily_candles_map=candles_map,
        exclude_symbols=open_symbols,
        persist=persist,
    )
    return top_watchlist, regime, asof_str


def job_stockfut_provisional_scan() -> None:
    """15:10 IST: Read-only provisional scan logged for 19:30 confirmation diff telemetry."""
    if not is_trading_day():
        return
    log.info("[STOCKFUT 15:10] Running read-only provisional scan for 19:30 diff telemetry...")
    try:
        picks, regime, asof_str = _run_eod_scan_pipeline(persist=False, asof_time="15:10:00")
        payload = {
            "scan_date": asof_str,
            "regime": regime,
            "count": len(picks),
            "picks": [
                {
                    "symbol": w.symbol,
                    "side": w.side,
                    "score": w.score,
                    "planned_entry": w.planned_entry,
                    "sl": w.sl,
                    "t1": w.t1,
                    "atr": w.atr,
                }
                for w in picks
            ],
        }
        log_stockfut_decision(kind="PROVISIONAL_1510", symbol="UNIVERSE", payload=payload)
        log.info(
            "[STOCKFUT 15:10] Provisional scan logged: %d candidates (regime=%s): %s",
            len(picks), regime, [w.symbol for w in picks],
        )
    except Exception as e:
        log.error("[STOCKFUT 15:10] Provisional scan failed: %s", e)


def job_stockfut_eod_scan() -> None:
    """19:30 IST: Run EOD scan across universe, generate ARMED watchlist, and diff 15:10 telemetry."""
    if not is_trading_day():
        return
    log.info("[STOCKFUT 19:30] Running EOD Stock Futures Scan...")
    try:
        top_watchlist, regime, asof_str = _run_eod_scan_pipeline(persist=True, asof_time="19:30:00")
        log.info(
            "[STOCKFUT 19:30] Scan complete: %d ARMED setups armed for tomorrow (regime=%s)",
            len(top_watchlist), regime,
        )

        # Log 15:10 provisional vs 19:30 bhavcopy agreement telemetry if a 15:10 log ran today
        try:
            prov = get_latest_stockfut_decision("PROVISIONAL_1510", symbol="UNIVERSE")
            prov_payload = (prov or {}).get("payload") or {}
            if prov_payload.get("scan_date") == asof_str:
                prov_map = {p["symbol"]: p.get("side") for p in prov_payload.get("picks", [])}
                eod_map = {w.symbol: w.side for w in top_watchlist}
                confirmed = sorted(s for s, sd in prov_map.items() if eod_map.get(s) == sd)
                invalidated = sorted(s for s in prov_map if s not in confirmed)
                new_at_eod = sorted(s for s in eod_map if s not in prov_map)
                fail_pct = round((len(invalidated) / len(prov_map)) * 100.0, 1) if prov_map else 0.0
                log_stockfut_decision(
                    kind="PROVISIONAL_DIFF_1930",
                    symbol="UNIVERSE",
                    payload={
                        "scan_date": asof_str,
                        "provisional_regime": prov_payload.get("regime"),
                        "eod_regime": regime,
                        "provisional_count": len(prov_map),
                        "confirmed_count": len(confirmed),
                        "invalidated_count": len(invalidated),
                        "invalidated_pct": fail_pct,
                        "confirmed_symbols": confirmed,
                        "invalidated_symbols": invalidated,
                        "new_at_eod_symbols": new_at_eod,
                    },
                )
                log.info(
                    "[STOCKFUT 19:30] Provisional 15:10 vs 19:30 diff: %d/%d confirmed, %d invalidated (%.1f%%)",
                    len(confirmed), len(prov_map), len(invalidated), fail_pct,
                )
        except Exception as e:
            log.debug("[STOCKFUT 19:30] Provisional diff logging error: %s", e)

        try:
            wl_dicts = [
                {
                    "symbol": w.symbol,
                    "direction": w.side,
                    "setup": w.bias,
                    "score": w.score,
                    "trigger_price": w.planned_entry,
                    "sl_price": w.sl,
                    "t1_price": w.t1,
                    "t2_price": w.t2,
                    "risk_per_lot": (w.planned_entry - w.sl if w.side == "LONG" else w.sl - w.planned_entry) * w.lot_size,
                    "lot_size": w.lot_size,
                    "lots": w.lots,
                    "status": w.status,
                }
                for w in top_watchlist
            ]
            send_eod_watchlist_alert(asof_str, regime, wl_dicts)
        except Exception as e:
            log.warning("[STOCKFUT 19:30] EOD watchlist alert failed: %s", e)
    except Exception as e:
        log.error("[STOCKFUT 19:30] EOD scan failed: %s", e)


def _evaluate_intraday_paper_exits(now_dt: Optional[datetime] = None) -> None:
    """
    Evaluate quote-only intraday exits (HARD_STOP, T1_HIT, T2_HIT, EXPIRY)
    for open PAPER positions during NSE market hours (09:15-15:30 IST).
    Daily-bar exits (THESIS_BREAK, TRAIL, REGIME_TURN, TIME_STOP) stay at 09:30 IST.
    """
    now_ist = now_dt or datetime.now(IST)
    t_now = now_ist.time()
    if t_now < dt_time(9, 15) or t_now > dt_time(15, 30):
        return

    open_paper = get_open_stockfut_trades(mode="PAPER")
    if not open_paper:
        return

    symbols = sorted({str(t["tradingsymbol"]) for t in open_paper if t.get("tradingsymbol")})
    if not symbols:
        return

    try:
        quotes = get_kite_data_client().get_futures_quotes(symbols)
    except Exception as e:
        log.warning("[STOCKFUT INTRADAY] Quote fetch failed: %s", e)
        return

    if not quotes:
        return

    cfg = load_stockfut_config()
    executor = PaperExecutor(cfg=cfg)
    allowed_rules = {"HARD_STOP", "T1_HIT", "T2_HIT", "EXPIRY"}

    for trade in open_paper:
        tsym = str(trade.get("tradingsymbol", ""))
        q = quotes.get(tsym)
        if not q or float(q.get("price") or 0.0) <= 0.0:
            continue

        ltp = float(q["price"])
        est_atr = abs(float(trade["entry_price"]) - float(trade.get("initial_sl", trade["sl"]))) or 10.0
        intraday_market = {
            "price": ltp,
            "bid": float(q.get("bid") or ltp),
            "ask": float(q.get("ask") or ltp),
            "atr": est_atr,
        }
        dec = evaluate_future_exit(trade=trade, market=intraday_market, now=now_ist, cfg=cfg)
        if not dec or dec.rule not in allowed_rules:
            continue

        log.info(
            "[STOCKFUT INTRADAY] Trade #%d (%s) triggered %s (%s) @ ₹%.2f",
            trade["id"], tsym, dec.rule, dec.action, ltp,
        )
        executor.apply_exit(trade["id"], decision=dec, quote=q, asof=now_ist)

        try:
            updated = get_stockfut_trade_by_id(trade["id"]) or trade
            ev_type = "SL_HIT" if dec.rule == "HARD_STOP" else dec.rule
            payload = {
                **updated,
                "realized_pnl": float(updated.get("pnl", 0.0) or 0.0),
                "exit_price": float(updated.get("exit_price") or ltp),
            }
            send_trade_event_alert(ev_type, payload, detail=dec.reason)
        except Exception as e:
            log.warning("[STOCKFUT INTRADAY] Exit alert failed for #%d: %s", trade["id"], e)


def job_stockfut_reconcile() -> None:
    """15-minute periodic intraday exit monitor + broker reconciliation."""
    if not is_trading_day():
        return
    _evaluate_intraday_paper_exits()
    reconcile_periodic()


def register_stockfut_jobs(scheduler: Any) -> None:
    """
    Register all stock futures cron/interval jobs with APScheduler.
    Called once during startup in job_runner.py.
    """
    if scheduler is None:
        log.warning("Scheduler not provided to register_stockfut_jobs")
        return

    # 1. 08:45 Morning Refresh (Mon-Fri)
    scheduler.add_job(
        job_stockfut_morning_refresh,
        "cron",
        hour=8,
        minute=45,
        day_of_week="mon-fri",
        id="stockfut_morning_refresh",
        replace_existing=True,
    )

    # 2. 09:30 Morning Execution Routine (Mon-Fri)
    scheduler.add_job(
        job_stockfut_morning_execution,
        "cron",
        hour=9,
        minute=30,
        day_of_week="mon-fri",
        id="stockfut_morning_execution",
        replace_existing=True,
    )

    # 3. 12:20 Midday Retry Routine for transient 09:30 RETRY rows (Mon-Fri)
    scheduler.add_job(
        job_stockfut_midday_retry,
        "cron",
        hour=12,
        minute=20,
        day_of_week="mon-fri",
        id="stockfut_midday_retry_1220",
        replace_existing=True,
    )

    # 4. 15:10 Provisional Scan (read-only telemetry for 19:30 agreement tracking)
    scheduler.add_job(
        job_stockfut_provisional_scan,
        "cron",
        hour=15,
        minute=10,
        day_of_week="mon-fri",
        id="stockfut_provisional_scan_1510",
        replace_existing=True,
    )

    # 5. Evening EOD Scan on final settlement bhavcopy (Mon-Fri at 19:30 IST)
    scheduler.add_job(
        job_stockfut_eod_scan,
        "cron",
        hour=19,
        minute=30,
        day_of_week="mon-fri",
        id="stockfut_eod_scan_evening",
        replace_existing=True,
    )

    # 5. Every 15 minutes reconciliation between 09:15 and 15:30 (Mon-Fri)
    scheduler.add_job(
        job_stockfut_reconcile,
        "cron",
        minute="*/15",
        hour="9-15",
        day_of_week="mon-fri",
        id="stockfut_reconcile_15m",
        replace_existing=True,
    )

    log.info("Registered all stock futures scheduled jobs with APScheduler")
