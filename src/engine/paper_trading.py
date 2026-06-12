"""Auto paper-trading engine based on bot verdict + scan context."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

import pytz

from src.engine.paper_plan import (
    build_paper_trade_plan,
    is_bearish_verdict,
    is_bullish_verdict,
)
from src.models.schema import (
    close_paper_trade,
    get_open_paper_trade,
    insert_paper_trade,
    get_latest_snapshots_for_symbol,
    get_open_timeframe_trades,
    get_scan_summary_at_least_1h_old,
    get_today_scan_count,
    get_scan_summary_n_scans_ago,
    get_conn,
)
from src.engine.trade_decision import make_trade_decision
from src.engine.risk_engine import check_risk_limits
from config.settings import LOT_SIZES, DEFAULT_LOTS_PER_TRADE, TIMEFRAME_OI_MIN_DIFF_PCT
from config.symbol_classes import market_window, get_strike_step, get_symbol_class
from config.runtime_config import get_scan_frequency_minutes, get_scan_frequency_nse, get_scan_frequency_mcx


IST = pytz.timezone("Asia/Kolkata")


def _is_market_open(symbol: str) -> bool:
    """Return True only if current IST time is within the symbol's market window."""
    now = datetime.now(IST)
    open_t, close_t, days = market_window(symbol)
    if now.weekday() not in days:
        return False
    from config.holidays import is_market_holiday
    if is_market_holiday(symbol, now):
        return False
    t = now.strftime("%H:%M")
    return open_t <= t <= close_t


def _get_option_premium(
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    option_rows: list[dict] | None = None,
) -> float | None:
    """Fetch current option premium (LTP) from latest snapshot."""
    for row in option_rows or []:
        try:
            if (
                abs(float(row.get("strike") or 0) - strike) < 0.01
                and str(row.get("option_type") or "").upper() == option_type
            ):
                premium = float(row.get("ltp") or 0.0)
                return premium if premium > 0 else None
        except Exception:
            continue
    try:
        snapshots = get_latest_snapshots_for_symbol(symbol, expiry)
        for snap in snapshots:
            if (abs(snap.get("strike", 0) - strike) < 0.01 and
                snap.get("option_type") == option_type):
                return float(snap.get("ltp") or 0.0)
    except Exception:
        pass
    return None


def _calculate_buy_sl_target(entry_premium: float) -> tuple[float, float]:
    """
    Calculate SL and Target in premium terms for BUY option trades.
      SL     = entry * 0.70  (exit if premium drops 30%)
      Target = entry * 1.50  (exit when premium rises 50%)
    """
    if entry_premium <= 0:
        return 0.0, 0.0
    sl     = round(entry_premium * 0.70, 2)
    target = round(entry_premium * 1.50, 2)
    return sl, target


def _calculate_sell_sl_target(entry_premium: float) -> tuple[float, float]:
    """
    Calculate SL and Target in premium terms for SELL option trades.
      SL     = entry * 1.50  (exit if premium rises 50% against us)
      Target = entry * 0.60  (exit when premium decays 40%)
    """
    if entry_premium <= 0:
        return 0.0, 0.0
    sl     = round(entry_premium * 1.50, 2)
    target = round(entry_premium * 0.60, 2)
    return sl, target


def _parse_verdict_and_confidence(intel_text: str) -> tuple[str, int]:
    verdict = ""
    confidence = 0
    m_v = re.search(r"\*Verdict:\s*([^\*]+)\*", intel_text or "")
    if m_v:
        verdict = m_v.group(1).strip()
    m_c = re.search(r"Confidence:\s*(\d+)%", intel_text or "")
    if m_c:
        confidence = int(m_c.group(1))
    return verdict, confidence


def _is_reversal_against_open_trade(open_trade: dict, verdict: str, confidence: int) -> bool:
    if confidence < 70:
        return False
    ot = str(open_trade.get("option_type") or "").upper()
    side = open_trade.get("side") or "BUY"
    if ot == "CE" and side == "BUY" and is_bearish_verdict(verdict):
        return True
    if ot == "PE" and side == "BUY" and is_bullish_verdict(verdict):
        return True
    if ot == "CE" and side == "SELL" and is_bullish_verdict(verdict):
        return True
    if ot == "PE" and side == "SELL" and is_bearish_verdict(verdict):
        return True
    return False


def _trade_plan_from_verdict(verdict: str, confidence: int, ctx: dict) -> dict | None:
    plan = build_paper_trade_plan(verdict, confidence, ctx)
    if not plan:
        return None

    expiry = ctx.get("expiry", "")
    symbol = ctx.get("symbol", "")
    option_rows = ctx.get("option_rows") or []
    strike = float(plan["strike"])
    option_type = str(plan["option_type"])
    side = plan.get("side", "BUY")

    if option_type == "FUT":
        entry_premium = float(plan["entry_underlying"])
        sl_premium = float(plan["sl_underlying"])
        target_premium = float(plan["target_underlying"])
    else:
        entry_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows)

        if not entry_premium or entry_premium <= 0:
            log.warning("%s: paper trade plan aborted — entry option premium unavailable for %s %s strike %s",
                        symbol, option_type, expiry, strike)
            return None

        if side == "SELL":
            sl_premium, target_premium = _calculate_sell_sl_target(entry_premium)
        else:
            sl_premium, target_premium = _calculate_buy_sl_target(entry_premium)

    return {
        **plan,
        "entry_premium": entry_premium,
        "sl_premium": sl_premium,
        "target_premium": target_premium,
    }


def _maybe_close_open_trade(
    symbol: str,
    underlying: float,
    expiry: str,
    now_iso: str,
    option_rows: list[dict] | None = None,
) -> None:
    """
    Check open trade for SL/target hit and close if triggered.

    P0 fix: Underlying and premium checks are now both evaluated independently
    for non-FUT options. Previously the underlying block returned early before
    premium SL/target could fire on the same poll — meaning options that had
    decayed/exploded past their premium SL were not closed until the spot price
    itself crossed the underlying SL level. Both checks now run unless the
    underlying check definitively closes the trade (in which case return is
    correct and premium check is moot).
    """
    open_trade = get_open_paper_trade(symbol)
    if not open_trade:
        return

    option_type = open_trade.get("option_type")
    strike = float(open_trade.get("strike") or 0.0)
    side = open_trade.get("side") or "BUY"

    target_underlying = float(open_trade.get("target_underlying") or 0.0)
    sl_underlying = float(open_trade.get("sl_underlying") or 0.0)

    exit_premium = None
    if option_type != "FUT":
        exit_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows)
    else:
        exit_premium = underlying

    verdict_label = open_trade.get("verdict_label", "")
    from src.engine.paper_plan import is_bullish_verdict
    if option_type == "CE":
        is_bull = (side == "BUY")
    elif option_type == "PE":
        is_bull = (side == "SELL")
    elif option_type == "FUT":
        is_bull = (side == "BUY")
    else:
        is_bull = is_bullish_verdict(verdict_label)

    # Underlying-based exit — evaluated only when both levels are set.
    # For FUT: this is the only exit mechanism (exit_premium == underlying).
    # For options: only closes if underlying level is definitively hit;
    #              if not hit, fall through to premium check below.
    underlying_closed = False
    if target_underlying > 0 and sl_underlying > 0:
        if is_bull:
            if underlying >= target_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                underlying_closed = True
            elif underlying <= sl_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                underlying_closed = True
        else:
            if underlying <= target_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                underlying_closed = True
            elif underlying >= sl_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                underlying_closed = True

    if underlying_closed:
        return

    # Premium-based exit for options — always evaluated if underlying did not close the trade.
    # This ensures options that have decayed/spiked past their premium SL/target are closed
    # even when spot price has not yet reached the underlying SL/target level.
    if option_type != "FUT" and exit_premium is not None:
        sl_premium = open_trade.get("sl_premium")
        target_premium = open_trade.get("target_premium")
        if sl_premium is not None and target_premium is not None:
            if side == "SELL":
                if exit_premium <= float(target_premium):
                    close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                    return
                elif exit_premium >= float(sl_premium):
                    close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                    return
            else:
                if exit_premium >= float(target_premium):
                    close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                    return
                elif exit_premium <= float(sl_premium):
                    close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                    return


def run_paper_trading(symbol: str, scan_context: dict, digest_id: str, intel: dict) -> dict | None:
    """
    intel: structured dict from generate_intelligence_structured().
    Accepts legacy str for backward-compat (extension_bridge etc.).
    """
    if isinstance(intel, str):
        verdict, confidence = _parse_verdict_and_confidence(intel)
        intel = {"verdict_label": verdict, "confidence": confidence}

    now_iso = datetime.now(timezone.utc).isoformat()
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    expiry = (scan_context or {}).get("expiry", "")
    verdict = intel.get("verdict_label", "")
    confidence = int(intel.get("confidence") or 0)

    if underlying <= 0:
        return None

    if not _is_market_open(symbol):
        log.debug("%s: paper-trading skipped — outside market hours", symbol)
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    option_rows = list((scan_context or {}).get("option_rows") or [])
    prev_open_trade = get_open_paper_trade(symbol)
    _maybe_close_open_trade(symbol, underlying, expiry, now_iso, option_rows)
    current_open_trade = get_open_paper_trade(symbol)

    if prev_open_trade and not current_open_trade:
        with get_conn() as conn:
            closed_trade = conn.execute("SELECT * FROM paper_trades WHERE id=?", (prev_open_trade["id"],)).fetchone()
            if closed_trade:
                closed_trade = dict(closed_trade)
                return {
                    "action": "CLOSED",
                    "trade": closed_trade,
                    "reason": f"Closed via exit logic: {closed_trade.get('reason') or 'SL/Target hit'} (P&L: \u20b9{closed_trade.get('pnl_rupees', 0.0):,.2f})"
                }

    if current_open_trade and _is_reversal_against_open_trade(current_open_trade, verdict, confidence):
        strike = float(current_open_trade.get("strike") or 0.0)
        option_type = str(current_open_trade.get("option_type") or "")
        exit_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows) if strike > 0 else None
        trade_id = current_open_trade["id"]
        close_paper_trade(
            trade_id, now_iso, underlying, exit_premium, "CLOSED_MANUAL",
            f"reversal: verdict={verdict} conf={confidence}",
        )
        current_open_trade = None
        with get_conn() as conn:
            closed_trade = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            if closed_trade:
                closed_trade = dict(closed_trade)
                return {
                    "action": "CLOSED",
                    "trade": closed_trade,
                    "reason": f"Closed on opposite reversal signal: verdict={verdict} (P&L: \u20b9{closed_trade.get('pnl_rupees', 0.0):,.2f})"
                }

    if current_open_trade:
        return {"action": "HELD", "trade": current_open_trade}

    ctx = {**(scan_context or {}), "symbol": symbol, "expiry": expiry, "option_rows": option_rows}

    decision = make_trade_decision(symbol, intel, ctx)
    if decision["status"] == "BLOCKED":
        log.info("%s: paper trade blocked by decision engine — %s", symbol, decision["reason"])
        return {"action": "BLOCKED_DECISION", "reason": decision["reason"]}

    risk_ok, risk_reason = check_risk_limits(symbol, decision.get("setup_type"))
    if not risk_ok:
        log.info("%s: paper trade blocked by risk engine — %s", symbol, risk_reason)
        return {"action": "BLOCKED_RISK", "reason": risk_reason}

    plan = _trade_plan_from_verdict(verdict, confidence, ctx)
    if not plan:
        return {"action": "BLOCKED_PLAN", "reason": "No valid trade plan (e.g. missing option premium/strikes)"}

    lots = DEFAULT_LOTS_PER_TRADE
    scores = decision.get("scores") or {}

    # Deterministic signal_key for INSERT OR IGNORE deduplication on retry.
    today_date = datetime.now(IST).strftime("%Y%m%d")
    option_type_key = plan.get("option_type", "")
    strike_key = int(plan.get("strike") or 0)
    signal_key = f"{symbol}:{option_type_key}:{strike_key}:{verdict}:{today_date}"

    trade_data = {
        "opened_at":             now_iso,
        "symbol":                symbol,
        "verdict_label":         plan["verdict_label"],
        "side":                  plan.get("side", "BUY"),
        "option_type":           plan["option_type"],
        "strike":                plan["strike"],
        "entry_underlying":      plan["entry_underlying"],
        "entry_premium":         plan.get("entry_premium"),
        "sl_underlying":         plan["sl_underlying"],
        "sl_premium":            plan.get("sl_premium"),
        "target_underlying":     plan["target_underlying"],
        "target_premium":        plan.get("target_premium"),
        "lots":                  lots,
        "status":                "OPEN",
        "reason":                f"auto | {decision['reason']}",
        "digest_id":             digest_id,
        "trade_status":          decision["status"],
        "setup_type":            decision["setup_type"],
        "decision_reason":       decision["reason"],
        "confidence_score":      scores.get("confidence"),
        "entry_quality_score":   scores.get("entry_quality"),
        "trend_alignment_score": scores.get("trend_alignment"),
        "regime_score":          scores.get("regime_score"),
        "signal_key":            signal_key,
    }

    # P1 fix: INSERT OR IGNORE silently drops duplicate rows and still returns
    # lastrowid=0 (or the prior rowid). Check lastrowid explicitly so the caller
    # can distinguish a real insert from a dedup-blocked one.
    inserted_id = insert_paper_trade(trade_data)
    if not inserted_id:
        log.warning("%s: paper trade INSERT skipped — duplicate signal_key=%s", symbol, signal_key)
        return {
            "action":     "DEDUP_SKIPPED",
            "reason":     f"Duplicate signal already recorded today (signal_key={signal_key})",
            "signal_key": signal_key,
        }

    return {
        "action":     "EXECUTED",
        "trade":      trade_data,
        "setup_type": decision["setup_type"],
        "reason":     decision["reason"],
        "lots":       lots,
    }


def run_timeframe_strategy(symbol: str, scan_context: dict, digest_id: str, intel: dict) -> dict | None:
    """Run secondary timeframe trading strategy (3h Entry / 1h Exit) based on completed candle crossovers."""
    if not _is_market_open(symbol):
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    ctx = scan_context or {}
    underlying = float(ctx.get("underlying") or 0.0)
    if underlying <= 0:
        return None

    if get_symbol_class(symbol) == "MCX_COMMODITY":
        scan_freq = get_scan_frequency_mcx()
    else:
        scan_freq = get_scan_frequency_nse()
    fetched_at = ctx.get("fetched_at") or datetime.now(timezone.utc).isoformat()
    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        today_scans = get_today_scan_count(symbol, fetched_at)
        current_scan_idx = today_scans + 1
        if current_scan_idx % scans_needed != 0:
            log.info("%s: Timeframe strategy skipped — scan %d is not a 1-hour boundary (run every %d scans for %d-min scan freq)",
                     symbol, current_scan_idx, scans_needed, scan_freq)
            return {"action": "SKIPPED_TIMEFRAME_BOUNDARY", "reason": f"Skipped scan {current_scan_idx} (run every {scans_needed} scans)"}

    open_trades_before = get_open_timeframe_trades(symbol)

    chart_indicators = ctx.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}

    pay_3h = tf_data.get("3h")
    pay_1h = tf_data.get("1h")
    if not pay_3h or not pay_1h:
        log.warning("%s: Timeframe strategy skipped — missing 3h/1h chart data", symbol)
        return {"action": "SKIPPED_NO_CHART_DATA", "reason": "Missing 3h/1h chart data"}

    ohlc_3h = pay_3h.get("ohlc")
    prev_3h = pay_3h.get("prev_ohlc") or pay_3h.get("last_closed_ohlc")
    ohlc_1h = pay_1h.get("ohlc")
    prev_1h = pay_1h.get("prev_ohlc") or pay_1h.get("last_closed_ohlc")

    if not ohlc_3h or not prev_3h or not ohlc_1h or not prev_1h:
        log.warning("%s: Timeframe strategy skipped — incomplete 3h/1h candle data", symbol)
        return {"action": "SKIPPED_INCOMPLETE_CANDLES", "reason": "Incomplete 3h/1h candle data"}

    c_3h_close = float(ohlc_3h["close"])
    p_3h_high = float(prev_3h["high"])
    p_3h_low = float(prev_3h["low"])

    c_1h_close = float(ohlc_1h["close"])
    p_1h_high = float(prev_1h["high"])
    p_1h_low = float(prev_1h["low"])

    current_ce = ctx.get("total_ce_oi")
    current_pe = ctx.get("total_pe_oi")

    if current_ce is None or current_pe is None:
        log.warning("%s: Timeframe strategy skipped — missing total OI data", symbol)
        return {"action": "SKIPPED_NO_OI", "reason": "Missing total OI data"}

    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        older = get_scan_summary_n_scans_ago(symbol, scans_needed)
        if not older:
            log.warning("%s: Timeframe strategy skipped — insufficient scan history (%d scans ago)", symbol, scans_needed)
            return {"action": "SKIPPED_INSUFFICIENT_HISTORY", "reason": f"Insufficient scan history ({scans_needed} scans ago)"}
    else:
        older = get_scan_summary_at_least_1h_old(symbol, fetched_at)
        if not older:
            log.warning("%s: Timeframe strategy skipped — insufficient 1h scan history", symbol)
            return {"action": "SKIPPED_INSUFFICIENT_HISTORY", "reason": "Insufficient 1h scan history"}
