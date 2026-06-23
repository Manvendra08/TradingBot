# autopsy fix #10: _is_reversal_against_open_trade now requires
# entry_quality >= MIN_ENTRY_QUALITY_CORE AND trend_alignment check
# before closing/flipping an open trade, matching the guard strength
# of the initial entry path. Previously fired on confidence >= 70 alone.
#
# All other logic in this file is unchanged from the prior patch commit.
# The marker comment below is intentionally minimal — the real change is
# the guard added inside _is_reversal_against_open_trade in the body below.
#
# NOTE: Because paper_trading.py is ~40KB, only the patched function is
# replaced here. The rest of the file content is preserved via the
# full-file push below.
#
# This file is regenerated in full on each push — SHA c66b5d5 replaced.
from __future__ import annotations

import logging
import time
import re
import pytz
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

from src.engine.trade_decision import _extract_ai_bias

from config.settings import (
    LOT_SIZES,
    MIN_ENTRY_QUALITY_CORE,
    REVERSAL_MIN_CONFIDENCE,
    DEFAULT_LOTS_PER_TRADE,
    TIMEFRAME_OI_MIN_DIFF_PCT,
)
from config.runtime_config import (
    load_runtime_config,
    get_scan_frequency_nse,
    get_scan_frequency_mcx,
    get_scan_frequency_minutes,
)
from src.models.schema import (
    get_conn,
    get_open_paper_trade,
    insert_paper_trade,
    close_paper_trade,
    get_latest_snapshots_for_symbol,
    get_open_timeframe_trades,
    get_scan_summary_at_least_1h_old,
    get_today_scan_count,
    get_scan_summary_n_scans_ago,
)
from src.engine.capital_allocator import calculate_trade_lots
from src.engine.risk_engine import check_risk_limits
from src.engine.entry_quality import calculate_entry_quality
from src.engine.trend_analysis import get_trend_alignment_score
from src.engine.verdict_sets import is_bullish, is_bearish
from config.symbol_classes import get_symbol_class, get_strike_step, market_window
from src.engine.trade_plan import convert_underlying_sl_to_premium

IST = pytz.timezone("Asia/Kolkata")


def _is_market_open(symbol: str) -> bool:
    now = datetime.now(IST)
    open_t, close_t, days = market_window(symbol)
    if now.weekday() not in days:
        return False
    from config.holidays import is_market_holiday
    if is_market_holiday(symbol, now):
        return False
    t = now.strftime("%H:%M")
    return open_t <= t <= close_t




# ---------------------------------------------------------------------------
# SL / Target calculation — imported from unified trade_plan.py
# ---------------------------------------------------------------------------
from src.engine.trade_plan import (
    get_atr as _get_atr,
    calculate_buy_sl_target as _calculate_buy_sl_target,
    calculate_sell_sl_target as _calculate_sell_sl_target,
    get_option_premium as _get_option_premium,
    parse_verdict_and_confidence as _parse_verdict_and_confidence,
)


# Helpers now imported from src.engine.trade_plan (see top of file)


# ---------------------------------------------------------------------------
# Reversal guard — fix #10
# ---------------------------------------------------------------------------

def _is_reversal_against_open_trade(
    open_trade: dict,
    verdict: str,
    confidence: int,
    symbol: str,
    option_type: str,
    strike: float,
    ctx: dict,
) -> bool:
    """
    Return True only when a genuinely strong reversal signal contradicts the
    open trade direction.

    Fix #10: Added entry_quality and trend_alignment guards matching the
    initial-entry path.  Previously fired on confidence >= 70 alone, which
    meant a 70-confidence counter-signal during a strong trend day would
    close a profitable position that a fresh entry in the same direction
    would have been blocked from opening (those require entry_quality AND
    regime checks that were absent here).

    Guards (all must pass):
      1. confidence >= REVERSAL_MIN_CONFIDENCE (default 75)
      2. entry_quality >= MIN_ENTRY_QUALITY_CORE (default 60)
      3. trend_alignment score <= 40 (trend no longer supports open direction)
    """
    # Guard 1: confidence threshold — must be a strong signal
    if confidence < REVERSAL_MIN_CONFIDENCE:
        log.debug(
            "%s: reversal guard — confidence %d < REVERSAL_MIN_CONFIDENCE %d, ignoring.",
            symbol, confidence, REVERSAL_MIN_CONFIDENCE,
        )
        return False

    # Guard 2: entry quality — requires a genuine setup, not noise
    entry_quality, entry_reasons = calculate_entry_quality(symbol, option_type, strike, ctx)
    if entry_quality < MIN_ENTRY_QUALITY_CORE:
        log.debug(
            "%s: reversal guard — entry_quality %d < MIN_ENTRY_QUALITY_CORE %d (%s), ignoring.",
            symbol, entry_quality, MIN_ENTRY_QUALITY_CORE, entry_reasons,
        )
        return False

    # Guard 3: trend alignment — ensure trend has actually shifted
    trend_alignment = get_trend_alignment_score(symbol, verdict)
    if trend_alignment > 40:
        log.debug(
            "%s: reversal guard — trend_alignment %d > 40, trend still supports open direction.",
            symbol, trend_alignment,
        )
        return False

    # Directional check: reversal must be against open trade
    open_side = str(open_trade.get("side", "")).upper()
    is_open_bullish = open_side == "BUY" and open_trade.get("option_type") == "CE"
    is_open_bullish = is_open_bullish or (open_side == "SELL" and open_trade.get("option_type") == "PE")
    is_open_bearish = open_side == "BUY" and open_trade.get("option_type") == "PE"
    is_open_bearish = is_open_bearish or (open_side == "SELL" and open_trade.get("option_type") == "CE")

    new_is_bullish = is_bullish(verdict)
    new_is_bearish = is_bearish(verdict)

    if is_open_bullish and new_is_bearish:
        log.info("%s: valid reversal — closing bullish trade on bearish signal (conf=%d eq=%d ta=%d).",
                 symbol, confidence, entry_quality, trend_alignment)
        return True
    if is_open_bearish and new_is_bullish:
        log.info("%s: valid reversal — closing bearish trade on bullish signal (conf=%d eq=%d ta=%d).",
                 symbol, confidence, entry_quality, trend_alignment)
        return True

    return False


# ---------------------------------------------------------------------------
# Core paper trade execution
# ---------------------------------------------------------------------------

def execute_paper_trade(
    symbol: str,
    verdict: str,
    confidence: int,
    ctx: dict,
    plan: dict,
    ai_verdict=None,
) -> dict:
    """
    Execute or update a paper trade.
    Returns action dict with keys: action, trade_id, reason.
    """
    rconf = load_runtime_config()
    open_trade = get_open_paper_trade(symbol)

    option_type = plan["option_type"]
    strike      = plan["strike"]
    side        = plan["side"]
    underlying  = float(plan.get("entry_underlying") or ctx.get("underlying") or 0)
    from config.symbol_classes import get_strike_step
    step = float(get_strike_step(symbol) or 50)

    # Check reversal against open trade
    if open_trade:
        is_reversal = _is_reversal_against_open_trade(
            open_trade, verdict, confidence, symbol, option_type, strike, ctx
        )
        if is_reversal:
            exit_premium = None
            if open_trade["option_type"] != "FUT":
                exit_premium = _get_option_premium(
                    symbol,
                    open_trade.get("expiry"),
                    open_trade.get("strike"),
                    open_trade.get("option_type"),
                    ctx.get("option_rows")
                )
            close_paper_trade(
                open_trade["id"],
                datetime.now(timezone.utc).isoformat(),
                underlying,
                exit_premium,
                "CLOSED_REVERSAL",
                f"reversal: verdict={verdict} conf={confidence}",
            )
            log.info("%s: closed trade #%s on reversal signal.", symbol, open_trade["id"])
            open_trade = None  # fall through to open new trade
        else:
            return {"action": "HOLD", "trade_id": open_trade["id"],
                    "reason": "Open trade exists, no valid reversal"}

    # Risk limits
    risk_ok, risk_reason = check_risk_limits(symbol)
    if not risk_ok:
        return {"action": "BLOCKED_RISK", "trade_id": None, "reason": risk_reason}

    # Lot sizing
    lots = calculate_trade_lots(symbol, plan.get("entry_premium", 0), side, is_paper=True)
    if lots <= 0:
        return {"action": "BLOCKED_LOTS", "trade_id": None, "reason": "Zero lots calculated"}

    # SL / Target — M2 fix: prefer plan values (already computed by build_paper_trade_plan)
    # so the Telegram digest matches what is actually stored. Only recalculate
    # if plan values are missing or invalid.
    entry_premium = float(plan.get("entry_premium") or 0)
    plan_sl = plan.get("sl_underlying")
    plan_tgt = plan.get("target_underlying")
    if (plan_sl is not None and plan_tgt is not None
            and float(plan_sl) > 0 and float(plan_tgt) > 0):
        sl_ul = float(plan_sl)
        tgt_ul = float(plan_tgt)
    else:
        # Fallback: recalculate if plan values are missing/invalid
        if side == "BUY":
            sl_ul, tgt_ul = _calculate_buy_sl_target(entry_premium, underlying, ctx, step)
        else:
            sl_ul, tgt_ul = _calculate_sell_sl_target(entry_premium, underlying, ctx, step)

    now_iso = datetime.now(timezone.utc).isoformat()
    today_date = datetime.now(IST).strftime("%Y%m%d")
    signal_key = f"{symbol}:{option_type}:{int(strike)}:{today_date}:paper"

    trade_data = {
        "opened_at":            now_iso,
        "symbol":               symbol,
        "expiry":               ctx.get("expiry"),
        "verdict_label":        verdict,
        "side":                 side,
        "option_type":          option_type,
        "strike":               strike,
        "entry_underlying":     underlying,
        "entry_premium":        entry_premium,
        "sl_underlying":        sl_ul,
        "sl_premium":           plan.get("sl_premium"),
        "target_underlying":    tgt_ul,
        "target_premium":       plan.get("target_premium"),
        "lots":                 lots,
        "status":               "OPEN",
        "reason":               f"auto | verdict={verdict} conf={confidence}",
        "digest_id":            plan.get("digest_id") or ctx.get("digest_id"),
        "trade_status":         "TRIGGERED_CORE",
        "setup_type":           plan.get("setup_type") or "CORE",
        "decision_reason":      "Signal filters passed",
        "confidence_score":     confidence,
        "entry_quality_score":  plan.get("entry_quality_score"),
        "trend_alignment_score": plan.get("trend_alignment_score"),
        "regime_score":         plan.get("regime_score"),
        "signal_key":           signal_key,
        "pyramid_level":        plan.get("pyramid_level", 1),
        "max_favorable_r":      0.0,
    }

    trade_id = insert_paper_trade(trade_data)
    if not trade_id:
        log.warning("%s: paper trade INSERT skipped - duplicate signal_key=%s", symbol, signal_key)
        return {"action": "BLOCKED_PLAN", "trade_id": None, "reason": "duplicate signal key"}

    log.info("%s: paper trade #%s opened — %s %s %g | SL %g | Tgt %g",
             symbol, trade_id, side, option_type, strike, sl_ul, tgt_ul)
    return {"action": "OPENED", "trade_id": trade_id, "reason": "New paper trade placed"}



def monitor_paper_trades(symbol: str, current_ctx: dict) -> list[dict]:
    """
    Check all open paper trades for SL/Target hit.
    Returns list of action dicts.
    """
    actions = []
    open_trade = get_open_paper_trade(symbol)
    if not open_trade:
        return actions

    underlying = float(current_ctx.get("underlying") or 0)
    if underlying <= 0:
        return actions

    sl_ul  = float(open_trade.get("sl_underlying") or 0)
    tgt_ul = float(open_trade.get("target_underlying") or 0)
    side   = str(open_trade.get("side", "")).upper()

    hit_sl     = (side == "BUY"  and sl_ul  > 0 and underlying <= sl_ul)
    hit_sl     = hit_sl or (side == "SELL" and sl_ul  > 0 and underlying >= sl_ul)
    hit_target = (side == "BUY"  and tgt_ul > 0 and underlying >= tgt_ul)
    hit_target = hit_target or (side == "SELL" and tgt_ul > 0 and underlying <= tgt_ul)

    # C5: Also check premium-based SL/Target for options
    if not hit_sl and not hit_target and open_trade.get("option_type") in ("CE", "PE"):
        exit_premium_check = _get_option_premium(
            symbol,
            open_trade.get("expiry"),
            open_trade.get("strike"),
            open_trade.get("option_type"),
            current_ctx.get("option_rows"),
        )
        if exit_premium_check and exit_premium_check > 0:
            sl_prem = float(open_trade.get("sl_premium") or 0)
            tgt_prem = float(open_trade.get("target_premium") or 0)
            if side == "BUY":
                if sl_prem > 0 and exit_premium_check <= sl_prem:
                    hit_sl = True
                if tgt_prem > 0 and exit_premium_check >= tgt_prem:
                    hit_target = True
            elif side == "SELL":
                if sl_prem > 0 and exit_premium_check >= sl_prem:
                    hit_sl = True
                if tgt_prem > 0 and exit_premium_check <= tgt_prem:
                    hit_target = True

    if hit_sl or hit_target:
        reason = "SL_HIT" if hit_sl else "TARGET_HIT"
        exit_premium = None
        if open_trade["option_type"] != "FUT":
            exit_premium = _get_option_premium(
                symbol,
                open_trade.get("expiry"),
                open_trade.get("strike"),
                open_trade.get("option_type"),
                current_ctx.get("option_rows")
            )
        close_paper_trade(
            open_trade["id"],
            datetime.now(timezone.utc).isoformat(),
            underlying,
            exit_premium,
            "CLOSED_SL" if hit_sl else "CLOSED_TARGET",
            reason,
        )
        log.info("%s: paper trade #%s closed — %s at underlying %g",
                 symbol, open_trade["id"], reason, underlying)
        actions.append({"action": reason, "trade_id": open_trade["id"],
                        "underlying": underlying})

    return actions


def run_paper_trading(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict=None,
) -> dict | None:
    """
    Standard paper-trading entry point. Monitors open trades and triggers new ones.
    """
    if not _is_market_open(symbol):
        log.debug("%s: paper-trading skipped — outside market hours", symbol)
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    now_iso = datetime.now(timezone.utc).isoformat()
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    expiry = (scan_context or {}).get("expiry", "")
    option_rows = list((scan_context or {}).get("option_rows") or [])

    if underlying <= 0:
        return None

    # 1. Check open trades exits first
    closed_actions = monitor_paper_trades(symbol, scan_context)
    if closed_actions:
        with get_conn() as conn:
            closed_trade = conn.execute(
                "SELECT * FROM paper_trades WHERE symbol=? AND status != 'OPEN' ORDER BY closed_at DESC LIMIT 1",
                (symbol,)
            ).fetchone()
            if closed_trade:
                closed_trade = dict(closed_trade)
                return {
                    "action": "CLOSED",
                    "trade": closed_trade,
                    "reason": f"Closed via exit logic: {closed_trade.get('exit_reason') or 'SL/Target hit'}"
                }

    # 2. Check if we already have an open trade
    current_open_trade = get_open_paper_trade(symbol)

    # 3. Parse verdict and confidence from intel
    verdict = intel.get("verdict_label", "")
    confidence = int(intel.get("confidence") or 0)
    if not verdict:
        verdict, confidence = _parse_verdict_and_confidence(intel.get("telegram_text") or "")

    # Build paper plan
    from src.engine.paper_plan import build_paper_trade_plan
    plan = build_paper_trade_plan(verdict, confidence, scan_context)
    if not plan:
        return {"action": "HOLD", "trade": current_open_trade, "reason": "No valid plan"}

    # Add null target guard:
    if plan.get("target_underlying") is None and plan.get("option_type") != "FUT":
        log.warning("%s: plan has null target_underlying — rejecting to prevent phantom exit", symbol)
        return {"action": "BLOCKED_PLAN", "reason": "Null target — plan incomplete"}

    # Add extra fields to plan for execute_paper_trade
    option_type = plan["option_type"]
    strike = plan["strike"]
    
    if option_type == "FUT":
        entry_premium = underlying
    else:
        entry_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows)
        if not entry_premium or entry_premium <= 0:
            log.warning("%s: paper trade plan aborted — entry option premium unavailable for %s %s strike %s",
                        symbol, option_type, expiry, strike)
            return {"action": "BLOCKED_PLAN", "reason": "Option premium unavailable"}

    plan["entry_premium"] = entry_premium
    plan["digest_id"] = digest_id

    # Execute paper trade (reversal check is handled inside)
    report = execute_paper_trade(
        symbol=symbol,
        verdict=verdict,
        confidence=confidence,
        ctx=scan_context,
        plan=plan,
        ai_verdict=ai_verdict,
    )

    if report.get("action") == "OPENED":
        with get_conn() as conn:
            opened_trade = conn.execute(
                "SELECT * FROM paper_trades WHERE id=?", (report["trade_id"],)
            ).fetchone()
            if opened_trade:
                return {
                    "action": "EXECUTED",
                    "trade": dict(opened_trade),
                    "setup_type": "CORE",
                    "reason": report.get("reason", "")
                }

    if current_open_trade and not report.get("action") == "CLOSED":
        return {"action": "HELD", "trade": current_open_trade}

    return report


def run_timeframe_strategy(symbol: str, scan_context: dict, digest_id: str, intel: dict, ai_verdict=None) -> dict | None:
    """Run secondary timeframe trading strategy (3h Entry / 1h Exit) based on completed candle crossovers."""
    if not _is_market_open(symbol):
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    ctx = scan_context or {}
    underlying = float(ctx.get("underlying") or 0.0)
    if underlying <= 0:
        return None

    # Gating checks for scan frequency
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
            log.info("%s: Timeframe strategy skipped — scan %d is not a 1-hour boundary", symbol, current_scan_idx)
            return {"action": "SKIPPED_TIMEFRAME_BOUNDARY", "reason": f"Skipped scan {current_scan_idx}"}

    open_trades_before = get_open_timeframe_trades(symbol)

    chart_indicators = ctx.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}
    
    pay_3h = tf_data.get("3h")
    pay_1h = tf_data.get("1h")
    if not pay_3h or not pay_1h:
        log.warning("%s: Timeframe strategy skipped — missing 3h/1h chart data", symbol)
        return

    ohlc_3h = pay_3h.get("ohlc")
    prev_3h = pay_3h.get("prev_ohlc") or pay_3h.get("last_closed_ohlc")
    ohlc_1h = pay_1h.get("ohlc")
    prev_1h = pay_1h.get("prev_ohlc") or pay_1h.get("last_closed_ohlc")

    if not ohlc_3h or not prev_3h or not ohlc_1h or not prev_1h:
        log.warning("%s: Timeframe strategy skipped — incomplete 3h/1h candle data", symbol)
        return

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
        return

    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        older = get_scan_summary_n_scans_ago(symbol, scans_needed)
        if not older:
            log.warning("%s: Timeframe strategy skipped — insufficient scan history", symbol)
            return
    else:
        older = get_scan_summary_at_least_1h_old(symbol, fetched_at)
        if not older:
            log.warning("%s: Timeframe strategy skipped — insufficient scan history", symbol)
            return

    prev_ce = older["total_ce_oi"]
    prev_pe = older["total_pe_oi"]
    ce_diff = current_ce - prev_ce
    pe_diff = current_pe - prev_pe

    min_diff_pct = TIMEFRAME_OI_MIN_DIFF_PCT
    long_oi_support = (pe_diff - ce_diff) > (prev_pe * min_diff_pct)
    short_oi_support = (ce_diff - pe_diff) > (prev_ce * min_diff_pct)

    # M1 fix: ATR-based breakout buffer (0.5x ATR) with 0.3% minimum floor.
    # Old 0.1% floor (e.g. 24pts on NIFTY, 0.3pts on NATURALGAS) was noise-level.
    atr_val = _get_atr(ctx)
    breakout_buffer = max((atr_val or 0) * 0.5, underlying * 0.003)

    # ── 1. EXIT LOGIC ──
    open_trades = get_open_timeframe_trades(symbol)
    now_iso = datetime.now(timezone.utc).isoformat()
    bar_end_1h = pay_1h.get("bar_end_utc")

    for trade in open_trades:
        exit_premium = None
        if trade["option_type"] in ("CE", "PE"):
            exit_premium = _get_option_premium(symbol, ctx.get("expiry", ""), trade["strike"], trade["option_type"], ctx.get("option_rows"))

        # LLM Reversal Exit
        if ai_verdict is not None:
            ai_bias = _extract_ai_bias(ai_verdict) or "NEUTRAL"
            ai_conf = float(
                ai_verdict.get("confidence", 50) if isinstance(ai_verdict, dict)
                else getattr(ai_verdict, "confidence", 50)
            )
            
            is_reversal = False
            if trade["verdict_label"] == "LONG" and ai_bias == "BEARISH":
                is_reversal = True
            elif trade["verdict_label"] == "SHORT" and ai_bias == "BULLISH":
                is_reversal = True
            
            if is_reversal and ai_conf >= 70:
                close_paper_trade(
                    trade["id"],
                    now_iso,
                    underlying,
                    exit_premium,
                    "LLM_REVERSAL",
                    f"LLM sentiment reversal: bias {ai_bias} (confidence {ai_conf}%)",
                )
                continue

        r_current = 0.0
        if trade["option_type"] in ("CE", "PE"):
            entry_prem = float(trade.get("entry_premium") or 0.0)
            sl_prem = float(trade.get("sl_premium") or 0.0)
            side = trade.get("side") or "BUY"
            if side == "SELL":
                if sl_prem > entry_prem and exit_premium:
                    r_current = (entry_prem - float(exit_premium)) / (sl_prem - entry_prem)
            else:
                if entry_prem > sl_prem and exit_premium:
                    r_current = (float(exit_premium) - entry_prem) / (entry_prem - sl_prem)
        else:
            entry_und = float(trade.get("entry_underlying") or 0.0)
            sl_und = float(trade.get("sl_underlying") or 0.0)
            if trade["verdict_label"] == "LONG":
                if entry_und > sl_und:
                    r_current = (underlying - entry_und) / (entry_und - sl_und)
            else:
                if sl_und > entry_und:
                    r_current = (entry_und - underlying) / (sl_und - entry_und)
        
        max_fav = max(float(trade.get("max_favorable_r") or 0.0), r_current)
        with get_conn() as conn:
            conn.execute("UPDATE paper_trades SET max_favorable_r=? WHERE id=?", (max_fav, trade["id"]))

        # SL/Target checks
        if trade["option_type"] in ("CE", "PE"):
            sl_prem = trade.get("sl_premium")
            tgt_prem = trade.get("target_premium")
            side = trade.get("side") or "BUY"
            
            is_sl_hit = False
            is_tgt_hit = False
            
            if exit_premium:
                if sl_prem is not None and str(sl_prem).strip() not in ("", "None", "NULL"):
                    try:
                        sl_val = float(sl_prem)
                        if sl_val > 0:
                            if side == "SELL":
                                is_sl_hit = (exit_premium >= sl_val)
                            else:
                                is_sl_hit = (exit_premium <= sl_val)
                    except ValueError:
                        pass
                if tgt_prem is not None and str(tgt_prem).strip() not in ("", "None", "NULL"):
                    try:
                        tgt_val = float(tgt_prem)
                        if tgt_val > 0:
                            if side == "SELL":
                                is_tgt_hit = (exit_premium <= tgt_val)
                            else:
                                is_tgt_hit = (exit_premium >= tgt_val)
                    except ValueError:
                        pass
                        
            if is_sl_hit:
                close_paper_trade(
                    trade["id"],
                    now_iso,
                    underlying,
                    exit_premium,
                    "CLOSED_SL",
                    f"Options SL hit: premium {exit_premium} vs SL {sl_prem} ({side})",
                )
                continue
            elif is_tgt_hit:
                close_paper_trade(
                    trade["id"],
                    now_iso,
                    underlying,
                    exit_premium,
                    "CLOSED_TARGET",
                    f"Options Target hit: premium {exit_premium} vs Target {tgt_prem} ({side})",
                )
                continue
        else:
            sl_und = trade.get("sl_underlying")
            tgt_und = trade.get("target_underlying")
            
            is_sl_hit = False
            is_tgt_hit = False
            
            if sl_und:
                sl_und_val = float(sl_und)
                if trade["verdict_label"] == "LONG" and underlying <= sl_und_val:
                    is_sl_hit = True
                elif trade["verdict_label"] == "SHORT" and underlying >= sl_und_val:
                    is_sl_hit = True
                    
            if tgt_und:
                tgt_und_val = float(tgt_und)
                if trade["verdict_label"] == "LONG" and underlying >= tgt_und_val:
                    is_tgt_hit = True
                elif trade["verdict_label"] == "SHORT" and underlying <= tgt_und_val:
                    is_tgt_hit = True
                    
            if is_sl_hit:
                close_paper_trade(
                    trade["id"],
                    now_iso,
                    underlying,
                    underlying,
                    "CLOSED_SL",
                    f"Futures SL hit: underlying {underlying} <= SL {sl_und}",
                )
                continue
            elif is_tgt_hit:
                close_paper_trade(
                    trade["id"],
                    now_iso,
                    underlying,
                    underlying,
                    "CLOSED_TARGET",
                    f"Futures Target hit: underlying {underlying} >= Target {tgt_und}",
                )
                continue

        # Dead Trade exit
        opened_dt = datetime.fromisoformat(trade["opened_at"].replace("Z", "+00:00"))
        if bar_end_1h:
            bar_end_dt = datetime.fromisoformat(bar_end_1h.replace("Z", "+00:00"))
            time_diff = (bar_end_dt - opened_dt).total_seconds()
            if time_diff >= 3.0 * 3600 - 60:
                if max_fav < 0.5:
                    close_paper_trade(
                        trade["id"],
                        now_iso,
                        underlying,
                        exit_premium if trade["option_type"] in ("CE", "PE") else underlying,
                        "Dead Trade",
                        f"Dead trade exit: 3 hours passed, max favorable R {max_fav:.2f} < 0.5",
                    )
                    continue

        # Exit Long trade (Crossover)
        if trade["option_type"] in ("CE", "FUT") and trade["verdict_label"] == "LONG":
            if bar_end_1h and trade["opened_at"] < bar_end_1h:
                if c_1h_close < p_1h_low:
                    crossover_size = p_1h_low - c_1h_close
                    if crossover_size > 2 * breakout_buffer:
                        close_paper_trade(
                            trade["id"],
                            now_iso,
                            underlying,
                            exit_premium if trade["option_type"] == "CE" else underlying,
                            "TF-1H-Cross",
                            f"timeframe exit | Large reversal move ({crossover_size:.2f} > 2x buffer {2 * breakout_buffer:.2f})",
                        )
                    elif short_oi_support:
                        close_paper_trade(
                            trade["id"],
                            now_iso,
                            underlying,
                            exit_premium if trade["option_type"] == "CE" else underlying,
                            "TF-1H-Cross",
                            f"timeframe exit | 1H close {c_1h_close:.2f} < p1H_low {p_1h_low:.2f} + Short OI bias",
                        )

        # Exit Short trade (Crossover)
        elif trade["option_type"] in ("PE", "FUT") and trade["verdict_label"] == "SHORT":
            if bar_end_1h and trade["opened_at"] < bar_end_1h:
                if c_1h_close > p_1h_high:
                    crossover_size = c_1h_close - p_1h_high
                    if crossover_size > 2 * breakout_buffer:
                        close_paper_trade(
                            trade["id"],
                            now_iso,
                            underlying,
                            exit_premium if trade["option_type"] == "PE" else underlying,
                            "TF-1H-Cross",
                            f"timeframe exit | Large reversal move ({crossover_size:.2f} > 2x buffer {2 * breakout_buffer:.2f})",
                        )
                    elif long_oi_support:
                        close_paper_trade(
                            trade["id"],
                            now_iso,
                            underlying,
                            exit_premium if trade["option_type"] == "PE" else underlying,
                            "TF-1H-Cross",
                            f"timeframe exit | 1H close {c_1h_close:.2f} > p1H_high {p_1h_high:.2f} + Long OI bias",
                        )

    open_trades_after = get_open_timeframe_trades(symbol)
    closed_trade_id = None
    for pt in open_trades_before:
        if pt["id"] not in [ct["id"] for ct in open_trades_after]:
            closed_trade_id = pt["id"]
            break

    if closed_trade_id:
        with get_conn() as conn:
            closed = conn.execute("SELECT * FROM paper_trades WHERE id=?", (closed_trade_id,)).fetchone()
            if closed:
                closed = dict(closed)
                return {
                    "action": "CLOSED",
                    "trade": closed,
                    "reason": f"Timeframe exit: {closed.get('reason')} (P&L: ₹{closed.get('pnl_rupees', 0.0):,.2f})"
                }

    # ── 2. ENTRY LOGIC ──
    bar_end_3h = pay_3h.get("bar_end_utc")
    if not bar_end_3h:
        return None

    is_long_trigger = c_3h_close > p_3h_high + breakout_buffer and long_oi_support
    is_short_trigger = c_3h_close < p_3h_low - breakout_buffer and short_oi_support

    if not is_long_trigger and not is_short_trigger:
        return None

    direction = "LONG" if is_long_trigger else "SHORT"
    signal_key = f"{symbol}:TIMEFRAME:3H:{direction}:{bar_end_3h}"

    with get_conn() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM paper_trades WHERE signal_key=?",
            (signal_key,)
        ).fetchone()["c"]
        if cnt > 0:
            return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry skipped: duplicate signal key {signal_key}"}

    risk_ok, risk_reason = check_risk_limits(symbol, "TIMEFRAME")
    if not risk_ok:
        return {"action": "BLOCKED_RISK", "reason": f"Timeframe entry skipped: {risk_reason}"}

    if ai_verdict is not None:
        ai_bias = _extract_ai_bias(ai_verdict) or "NEUTRAL"
        ai_risk = str(
            ai_verdict.get("risk_rating", "LOW") if isinstance(ai_verdict, dict)
            else getattr(ai_verdict, "risk_rating", "LOW")
        ).upper()
        
        if direction == "LONG" and ai_bias == "BEARISH":
            return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry skipped: LLM bias alignment mismatch ({ai_bias} vs {direction})"}
        if direction == "SHORT" and ai_bias == "BULLISH":
            return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry skipped: LLM bias alignment mismatch ({ai_bias} vs {direction})"}
        if ai_risk == "HIGH":
            return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry skipped: LLM risk rating is HIGH"}

    open_trades = get_open_timeframe_trades(symbol)
    if len(open_trades) >= 3:
        return {"action": "BLOCKED_PLAN", "reason": "Timeframe entry skipped: maximum pyramid level (3) reached"}

    if len(open_trades) > 0:
        if any(t["verdict_label"] != direction for t in open_trades):
            return {"action": "BLOCKED_PLAN", "reason": "Timeframe entry skipped: cannot pyramid in opposite direction"}

        any_profitable = False
        for t in open_trades:
            if t["option_type"] in ("CE", "PE"):
                t_exit = _get_option_premium(symbol, ctx.get("expiry", ""), t["strike"], t["option_type"], ctx.get("option_rows"))
                t_side = t.get("side") or "BUY"
                if t_exit:
                    if t_side == "SELL":
                        is_profitable = t_exit < float(t.get("entry_premium") or 0.0)
                    else:
                        is_profitable = t_exit > float(t.get("entry_premium") or 0.0)
                    if is_profitable:
                        any_profitable = True
                        break
            else:
                if t["verdict_label"] == "LONG" and underlying > float(t["entry_underlying"]):
                    any_profitable = True
                    break
                elif t["verdict_label"] == "SHORT" and underlying < float(t["entry_underlying"]):
                    any_profitable = True
                    break
        if not any_profitable:
            return {"action": "BLOCKED_PLAN", "reason": "Timeframe entry skipped: no profitable open trades to pyramid"}

    pyramid_level = len(open_trades) + 1
    lot_multiplier = 1.0
    if pyramid_level == 2:
        lot_multiplier = 0.75
    elif pyramid_level == 3:
        lot_multiplier = 0.50

    lots = max(1, round(DEFAULT_LOTS_PER_TRADE * lot_multiplier))

    expiry = ctx.get("expiry", "")
    step = float(get_strike_step(symbol) or 1)
    atm = ctx.get("atm_strike") or round(underlying / step) * step
    from src.engine.paper_plan import mcx_option_liquidity_ok
    is_mcx_commodity = "NATURALGAS" in symbol or "CRUDEOIL" in symbol
    use_mcx_options = is_mcx_commodity and mcx_option_liquidity_ok(symbol, atm, ctx)

    if direction == "LONG":
        if is_mcx_commodity and not use_mcx_options:
            opt_type = "FUT"
            strike = atm
            entry_premium = underlying
            sl_underlying = float(ohlc_3h["low"])
            if underlying - sl_underlying < underlying * 0.003:
                sl_underlying = underlying - underlying * 0.003
            tgt_underlying = underlying + 2 * (underlying - sl_underlying)
        else:
            opt_type = "CE"
            strike = atm if is_mcx_commodity else (atm - 4 * step)
            entry_premium = _get_option_premium(symbol, expiry, strike, "CE", ctx.get("option_rows"))
            if not entry_premium or entry_premium <= 0:
                return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry skipped: option premium unavailable for CE strike {strike}"}
            sl_underlying = float(ohlc_3h["low"])
            tgt_underlying = underlying + 2 * (underlying - sl_underlying)
    else: # SHORT
        if is_mcx_commodity and not use_mcx_options:
            opt_type = "FUT"
            strike = atm
            entry_premium = underlying
            sl_underlying = float(ohlc_3h["high"])
            if sl_underlying - underlying < underlying * 0.003:
                sl_underlying = underlying + underlying * 0.003
            tgt_underlying = underlying - 2 * (sl_underlying - underlying)
        else:
            opt_type = "PE"
            strike = atm if is_mcx_commodity else (atm + 4 * step)
            entry_premium = _get_option_premium(symbol, expiry, strike, "PE", ctx.get("option_rows"))
            if not entry_premium or entry_premium <= 0:
                return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry skipped: option premium unavailable for PE strike {strike}"}
            sl_underlying = float(ohlc_3h["high"])
            tgt_underlying = underlying - 2 * (sl_underlying - underlying)

    # Convert underlying SL/Target to premium equivalents (unified via trade_plan.py)
    side = "BUY" if direction == "LONG" else ("SELL" if opt_type == "FUT" else "BUY")
    sl_premium, target_premium = convert_underlying_sl_to_premium(
        underlying, sl_underlying, tgt_underlying, entry_premium, side, opt_type, strike, ctx.get("option_rows")
    )

    reason_str = f"timeframe entry | 3H close {c_3h_close:.2f} > p3H_high {p_3h_high:.2f} | level {pyramid_level}" if direction == "LONG" else f"timeframe entry | 3H close {c_3h_close:.2f} < p3H_low {p_3h_low:.2f} | level {pyramid_level}"
    
    trade_data = {
        "opened_at": now_iso,
        "symbol": symbol,
        "expiry": expiry,
        "verdict_label": direction,
        "side": side,
        "option_type": opt_type,
        "strike": strike,
        "entry_underlying": underlying,
        "entry_premium": entry_premium,
        "sl_underlying": sl_underlying,
        "sl_premium": sl_premium,
        "target_underlying": tgt_underlying,
        "target_premium": target_premium,
        "lots": lots,
        "status": "OPEN",
        "reason": reason_str,
        "digest_id": digest_id,
        "trade_status": "TRIGGERED_TIMEFRAME",
        "setup_type": "TIMEFRAME",
        "signal_key": signal_key,
        "pyramid_level": pyramid_level,
        "max_favorable_r": 0.0,
    }
    
    if ai_verdict is not None:
        if isinstance(ai_verdict, dict):
            exit_advice = ai_verdict.get("exit_advice", "")
        else:
            exit_advice = getattr(ai_verdict, "exit_advice", "")
        if exit_advice and "sl" in str(exit_advice).lower():
            try:
                m = re.search(r"sl[^\d]*?(\d+(?:\.\d+)?)", str(exit_advice), re.IGNORECASE)
                if m:
                    sl_underlying = float(m.group(1))
                    # recalculate premium SL with new underlying SL
                    sl_premium, _ = convert_underlying_sl_to_premium(
                        underlying, sl_underlying, tgt_underlying, entry_premium, side, opt_type, strike, ctx.get("option_rows")
                    )
                    trade_data["sl_underlying"] = sl_underlying
                    trade_data["sl_premium"] = sl_premium
            except Exception:
                pass

    insert_paper_trade(trade_data)
    log.info("%s: Timeframe Strategy %s entry triggered! Strike %g Premium %g Lots %d (Level %d)", symbol, direction, strike, entry_premium, lots, pyramid_level)
    return {
        "action": "EXECUTED",
        "trade": trade_data,
        "setup_type": "TIMEFRAME",
        "reason": f"timeframe entry | level {pyramid_level}",
        "lots": lots
    }

