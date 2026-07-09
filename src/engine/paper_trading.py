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
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz

log = logging.getLogger(__name__)

from config.runtime_config import (
    get_scan_frequency_mcx,
    get_scan_frequency_minutes,
    get_scan_frequency_nse,
    load_runtime_config,
)
from config.settings import (
    DEFAULT_LOTS_PER_TRADE,
    LOT_SIZES,
    MIN_ENTRY_QUALITY_CORE,
    REVERSAL_MIN_CONFIDENCE,
    TIMEFRAME_OI_MIN_DIFF_PCT,
)
from config.symbol_classes import get_strike_step, get_symbol_class, market_window
from src.engine.capital_allocator import calculate_trade_lots
from src.engine.entry_quality import calculate_entry_quality
from src.engine.risk_engine import check_risk_limits
from src.engine.trade_decision import _extract_ai_bias
from src.engine.trade_plan import convert_underlying_sl_to_premium
from src.engine.trend_analysis import get_trend_alignment_score
from src.engine.verdict_sets import is_bearish, is_bullish
from src.models.schema import (
    close_paper_trade,
    get_conn,
    get_latest_snapshots_for_symbol,
    get_open_paper_trade,
    get_open_timeframe_trades,
    get_scan_summary_at_least_1h_old,
    get_scan_summary_n_scans_ago,
    get_today_scan_count,
    insert_paper_trade,
)


# Phase 1: Cache invalidation on trade close (AI_INTELLIGENCE_ROADMAP_v3.0)
def _invalidate_pattern_cache():
    """Invalidate the TradeHistoryAnalyzer cache after a trade closes."""
    try:
        from src.intelligence.history_analyzer import get_analyzer

        get_analyzer().invalidate_cache()
    except Exception:
        pass  # Non-critical — cache will expire via TTL anyway


def _trigger_ml_retraining():
    """Phase 2: Increment the ML retraining counter after a trade closes."""
    try:
        from src.scheduler.ml_training_job import on_trade_closed

        on_trade_closed()
    except Exception:
        pass  # Non-critical — counter will be incremented on next trade close


IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Phase 0: ML Feature Snapshot Builder
# AI_INTELLIGENCE_ROADMAP_v3.0 — captures scan context at trade OPEN time
# so Phase 2 ML model trains on real features instead of zeros.
# Features are captured at OPEN time (not close) to prevent feature leakage.
# ---------------------------------------------------------------------------


def _build_ml_feature_snapshot(ctx: dict, intel=None) -> dict:
    """
    Extract ML feature columns from scan_context and intelligence for
    persistence at trade-open time.

    Returns a dict with keys matching the Phase 0 ML feature columns:
      price_change_pct, pcr, ce_oi_change, pe_oi_change, underlying,
      support, resistance, max_pain, days_to_expiry, chart_conflict,
      rsi_1h, rsi_3h, regime

    All values are nullable — None is acceptable and will be stored as NULL.
    """
    ctx = ctx or {}

    # ── Days to expiry ────────────────────────────────────────────────
    days_to_expiry = None
    expiry_str = ctx.get("expiry") or ""
    if expiry_str:
        try:
            from datetime import datetime as _dt

            exp_date = _dt.strptime(expiry_str, "%Y-%m-%d").date()
            today_ist = _dt.now(IST).date()
            days_to_expiry = (exp_date - today_ist).days
        except Exception:
            days_to_expiry = None

    # ── RSI from chart indicators ─────────────────────────────────────
    rsi_1h = None
    rsi_3h = None
    chart_data = ctx.get("chart_indicators") or {}
    if chart_data:
        # chart_indicators may be keyed by symbol ({"NIFTY": {"1h":..., "3h":...}})
        # or directly by timeframe ({"1h":..., "3h":...})
        if any(k in chart_data for k in ("1h", "3h")):
            tf_data = chart_data
        else:
            # Symbol-keyed: look up the current symbol to get correct tf data
            sym = ctx.get("symbol", "")
            if sym and sym in chart_data:
                tf_data = chart_data[sym]
            else:
                # Fallback: grab first entry (may be wrong for multi-symbol ctx)
                tf_data = next(iter(chart_data.values()), {}) if chart_data else {}
        # BUG-011 FIX: RSI exactly 0.0 was collapsing to None due to the
        # truthy-check pattern `float(...) or None`. Use explicit None check
        # instead. RSI=0.0 is mathematically rare but valid and should not
        # be treated as missing data.
        try:
            raw_rsi_1h = (tf_data.get("1h") or {}).get("rsi")
            if raw_rsi_1h is not None:
                rsi_1h = float(raw_rsi_1h)
        except (TypeError, ValueError):
            rsi_1h = None
        try:
            raw_rsi_3h = (tf_data.get("3h") or {}).get("rsi")
            if raw_rsi_3h is not None:
                rsi_3h = float(raw_rsi_3h)
        except (TypeError, ValueError):
            rsi_3h = None

    # ── Chart conflict from intelligence ──────────────────────────────
    chart_conflict = None
    if intel is not None:
        try:
            chart_conflict = 1 if intel.get("chart_conflict") else 0
        except Exception:
            chart_conflict = None

    # ── Price change percentage ───────────────────────────────────────
    price_change_pct = ctx.get("price_change_pct")
    if price_change_pct is None:
        # Compute from underlying and prev_price if available
        underlying = ctx.get("underlying")
        prev_price = ctx.get("prev_price")
        if underlying and prev_price and prev_price != 0:
            try:
                price_change_pct = round(
                    (float(underlying) - float(prev_price))
                    / abs(float(prev_price))
                    * 100,
                    4,
                )
            except (TypeError, ValueError, ZeroDivisionError):
                price_change_pct = None

    return {
        "price_change_pct": price_change_pct,
        "pcr": ctx.get("pcr"),
        "ce_oi_change": ctx.get("ce_oi_change"),
        "pe_oi_change": ctx.get("pe_oi_change"),
        "underlying": ctx.get("underlying"),
        "support": ctx.get("support"),
        "resistance": ctx.get("resistance"),
        "max_pain": ctx.get("max_pain"),
        "days_to_expiry": days_to_expiry,
        "chart_conflict": chart_conflict,
        "rsi_1h": rsi_1h,
        "rsi_3h": rsi_3h,
        "regime": ctx.get("market_regime"),
    }


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
    calculate_buy_sl_target as _calculate_buy_sl_target,
)
from src.engine.trade_plan import (
    calculate_sell_sl_target as _calculate_sell_sl_target,
)
from src.engine.trade_plan import (
    get_atr as _get_atr,
)
from src.engine.trade_plan import (
    get_option_premium as _get_option_premium,
)
from src.engine.trade_plan import (
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
            symbol,
            confidence,
            REVERSAL_MIN_CONFIDENCE,
        )
        return False

    # Guard 2: entry quality — requires a genuine setup, not noise
    if ctx and option_type and strike:
        entry_quality, entry_reasons = calculate_entry_quality(
            symbol, option_type, strike, ctx
        )
        if entry_quality < MIN_ENTRY_QUALITY_CORE:
            log.debug(
                "%s: reversal guard — entry_quality %d < MIN_ENTRY_QUALITY_CORE %d (%s), ignoring.",
                symbol,
                entry_quality,
                MIN_ENTRY_QUALITY_CORE,
                entry_reasons,
            )
            return False

    # Guard 3: trend alignment — ensure trend has actually shifted
    trend_alignment = get_trend_alignment_score(symbol, verdict)
    if trend_alignment > 40:
        log.debug(
            "%s: reversal guard — trend_alignment %d > 40, trend still supports open direction.",
            symbol,
            trend_alignment,
        )
        return False

    # Directional check: reversal must be against open trade
    open_side = str(open_trade.get("side", "")).upper()
    is_open_bullish = open_side == "BUY" and open_trade.get("option_type") == "CE"
    is_open_bullish = is_open_bullish or (
        open_side == "SELL" and open_trade.get("option_type") == "PE"
    )
    is_open_bearish = open_side == "BUY" and open_trade.get("option_type") == "PE"
    is_open_bearish = is_open_bearish or (
        open_side == "SELL" and open_trade.get("option_type") == "CE"
    )

    new_is_bullish = is_bullish(verdict)
    new_is_bearish = is_bearish(verdict)

    if is_open_bullish and new_is_bearish:
        log.info(
            "%s: valid reversal — closing bullish trade on bearish signal (conf=%d eq=%d ta=%d).",
            symbol,
            confidence,
            entry_quality,
            trend_alignment,
        )
        return True
    if is_open_bearish and new_is_bullish:
        log.info(
            "%s: valid reversal — closing bearish trade on bullish signal (conf=%d eq=%d ta=%d).",
            symbol,
            confidence,
            entry_quality,
            trend_alignment,
        )
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
    strike = plan["strike"]
    side = plan["side"]
    underlying = float(plan.get("entry_underlying") or ctx.get("underlying") or 0)
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
                    ctx.get("option_rows"),
                )
            close_paper_trade(
                open_trade["id"],
                datetime.now(timezone.utc).isoformat(),
                underlying,
                exit_premium,
                "CLOSED_REVERSAL",
                f"reversal: verdict={verdict} conf={confidence}",
            )
            log.info(
                "%s: closed trade #%s on reversal signal.", symbol, open_trade["id"]
            )
            _invalidate_pattern_cache()  # Phase 1: refresh patterns after close
            _trigger_ml_retraining()  # Phase 2: increment ML retraining counter
            open_trade = None  # fall through to open new trade
        else:
            return {
                "action": "HOLD",
                "trade_id": open_trade["id"],
                "reason": "Open trade exists, no valid reversal",
            }

    # Risk limits
    risk_ok, risk_reason = check_risk_limits(symbol)
    if not risk_ok:
        from src.engine.decision_audit import update_decision_audit

        update_decision_audit(
            plan.get("audit_row_id"),
            action="SKIP",
            block_step="risk",
            block_reason=risk_reason,
        )
        return {"action": "BLOCKED_RISK", "trade_id": None, "reason": risk_reason}

    # Lot sizing
    lots = calculate_trade_lots(
        symbol,
        plan.get("entry_premium", 0),
        side,
        is_paper=True,
        pyramid_level=plan.get("pyramid_level", 1),
    )
    if lots <= 0:
        from src.engine.decision_audit import update_decision_audit

        update_decision_audit(
            plan.get("audit_row_id"),
            action="SKIP",
            block_step="risk",
            block_reason="Zero lots calculated",
        )
        return {
            "action": "BLOCKED_LOTS",
            "trade_id": None,
            "reason": "Zero lots calculated",
        }

    # SL / Target — M2 fix: prefer plan values (already computed by build_paper_trade_plan)
    # so the Telegram digest matches what is actually stored. Only recalculate
    # if plan values are missing or invalid.
    # Slippage Model (Flaw #9): Apply 0.5% slippage to options
    entry_premium = float(plan.get("entry_premium") or 0)
    if entry_premium > 0 and option_type != "FUT":
        if side == "BUY":
            entry_premium = entry_premium * 1.005  # buy higher
        else:
            entry_premium = entry_premium * 0.995  # sell lower

    plan_sl = plan.get("sl_underlying")
    plan_tgt = plan.get("target_underlying")
    if (
        plan_sl is not None
        and plan_tgt is not None
        and float(plan_sl) > 0
        and float(plan_tgt) > 0
    ):
        sl_ul = float(plan_sl)
        tgt_ul = float(plan_tgt)
    else:
        # Fallback: recalculate if plan values are missing/invalid
        option_type = str(plan.get("option_type", "CE")).upper()
        if side == "BUY":
            sl_ul, tgt_ul = _calculate_buy_sl_target(
                entry_premium, underlying, ctx, step, option_type=option_type
            )
        else:
            sl_ul, tgt_ul = _calculate_sell_sl_target(
                entry_premium, underlying, ctx, step, option_type=option_type
            )

    if sl_ul is None or tgt_ul is None:
        from src.engine.decision_audit import update_decision_audit

        update_decision_audit(
            plan.get("audit_row_id"),
            action="SKIP",
            block_step="risk",
            block_reason="Missing ATR data for SL/Target",
        )
        return {
            "action": "BLOCKED_PLAN",
            "trade_id": None,
            "reason": "Missing ATR data for SL/Target",
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    today_date = datetime.now(IST).strftime("%Y%m%d")
    signal_key = f"{symbol}:{option_type}:{int(strike)}:{today_date}:paper"

    # Phase 0: Capture ML feature snapshot at trade-open time
    ml_features = _build_ml_feature_snapshot(ctx, ai_verdict)

    trade_data = {
        "opened_at": now_iso,
        "symbol": symbol,
        "expiry": ctx.get("expiry"),
        "verdict_label": verdict,
        "side": side,
        "option_type": option_type,
        "strike": strike,
        "entry_underlying": underlying,
        "entry_premium": entry_premium,
        "sl_underlying": sl_ul,
        "sl_premium": plan.get("sl_premium"),
        "target_underlying": tgt_ul,
        "target_premium": plan.get("target_premium"),
        "lots": lots,
        "status": "OPEN",
        "reason": f"auto | verdict={verdict} conf={confidence}",
        "digest_id": plan.get("digest_id") or ctx.get("digest_id"),
        "trade_status": "TRIGGERED_CORE",
        "setup_type": plan.get("setup_type") or "CORE",
        "decision_reason": "Signal filters passed",
        "confidence_score": confidence,
        "entry_quality_score": plan.get("entry_quality_score"),
        "trend_alignment_score": plan.get("trend_alignment_score"),
        "regime_score": plan.get("regime_score"),
        "signal_key": signal_key,
        "pyramid_level": plan.get("pyramid_level", 1),
        "max_favorable_r": 0.0,
        # Phase 0: ML feature columns (captured at trade open time)
        **ml_features,
    }

    trade_id = insert_paper_trade(trade_data)
    from src.engine.decision_audit import update_decision_audit

    if not trade_id:
        log.warning(
            "%s: paper trade INSERT skipped - duplicate signal_key=%s",
            symbol,
            signal_key,
        )
        update_decision_audit(
            plan.get("audit_row_id"),
            action="SKIP",
            block_step="signal",
            block_reason="duplicate signal key",
        )
        return {
            "action": "BLOCKED_PLAN",
            "trade_id": None,
            "reason": "duplicate signal key",
        }
    update_decision_audit(plan.get("audit_row_id"), action="TRADE", trade_id=trade_id)

    log.info(
        "%s: paper trade #%s opened — %s %s %g | SL %g | Tgt %g",
        symbol,
        trade_id,
        side,
        option_type,
        strike,
        sl_ul,
        tgt_ul,
    )
    return {
        "action": "OPENED",
        "trade_id": trade_id,
        "reason": "New paper trade placed",
    }


def monitor_paper_trades(symbol: str, current_ctx: dict) -> list[dict]:
    """
    Check all open paper trades for SL/Target hit and Dead Trade conditions.
    Returns list of action dicts.
    """
    actions = []
    open_trade = get_open_paper_trade(symbol)
    if not open_trade:
        return actions

    underlying = float(current_ctx.get("underlying") or 0)
    if underlying <= 0:
        return actions

    sl_ul = float(open_trade.get("sl_underlying") or 0)
    tgt_ul = float(open_trade.get("target_underlying") or 0)
    side = str(open_trade.get("side", "")).upper()
    entry_und = float(open_trade.get("entry_underlying") or 0)
    option_type = str(open_trade.get("option_type", "")).upper()

    # ── Underlying-based SL/Target hit logic ────────────────────────────
    # Direction depends on option_type, not just side:
    #   BUY  CE / SELL PE → profit when underlying RISES
    #   BUY  PE / SELL CE → profit when underlying FALLS
    #   FUT follows CE logic (profit when underlying moves in trade direction)
    is_long = option_type in ("CE", "FUT")
    if side == "BUY":
        if option_type == "PE":
            # Long put: profit when underlying FALLS
            hit_sl = sl_ul > 0 and underlying >= sl_ul
            hit_target = tgt_ul > 0 and underlying <= tgt_ul
        else:
            # Long call / FUT: profit when underlying RISES
            hit_sl = sl_ul > 0 and underlying <= sl_ul
            hit_target = tgt_ul > 0 and underlying >= tgt_ul
    else:  # SELL
        if option_type == "PE":
            # Short put: profit when underlying RISES
            hit_sl = sl_ul > 0 and underlying <= sl_ul
            hit_target = tgt_ul > 0 and underlying >= tgt_ul
        else:
            # Short call / FUT: profit when underlying FALLS
            hit_sl = sl_ul > 0 and underlying >= sl_ul
            hit_target = tgt_ul > 0 and underlying <= tgt_ul

    # Compute current R multiple and update max_favorable_r
    r_current = 0.0
    if entry_und > 0 and sl_ul > 0 and sl_ul != entry_und:
        if option_type == "PE":
            # For puts: profit direction is inverted relative to underlying
            if side == "BUY":
                # Long put: R = (entry - underlying) / (sl - entry)
                r_current = (entry_und - underlying) / (sl_ul - entry_und)
            else:
                # Short put: R = (underlying - entry) / (entry - sl)
                r_current = (underlying - entry_und) / (entry_und - sl_ul)
        else:
            # CE / FUT: profit direction follows underlying
            if side == "SELL":
                r_current = (entry_und - underlying) / (sl_ul - entry_und)
            else:
                r_current = (underlying - entry_und) / (entry_und - sl_ul)

    stored_mfr = float(open_trade.get("max_favorable_r") or 0.0)
    max_fav = max(stored_mfr, r_current)

    # ── Trailing Stop Check (Flaw #1) ────────────────────────────────────────
    trailing_sl_hit = False
    # BUG-014 FIX: The original guard `sl_ul != entry_und` only checked for
    # exact equality, not distance. A near-zero SL gap (possible with degenerate
    # ATR output) produces an extreme R-multiple and fires the trailing stop
    # instantly. Added minimum distance check: R-distance must be at least
    # 0.1% of entry to prevent division-by-near-zero artifacts.
    min_r_distance = entry_und * 0.001  # 0.1% minimum R-distance
    r_distance = abs(entry_und - sl_ul) if sl_ul != entry_und else 0.0
    if max_fav >= 1.0 and entry_und > 0 and sl_ul > 0 and r_distance >= min_r_distance:
        orig_r_dist = r_distance
        trailed_r = int(max_fav) - 1  # 1R max_fav -> 0R (breakeven), 2R -> 1R, etc.
        if option_type == "PE":
            # For puts: trailing stop trails opposite to calls
            if side == "BUY":
                # Long put: profit when underlying FALLS, trail DOWN
                trailing_sl = entry_und - trailed_r * orig_r_dist
                if underlying >= trailing_sl:
                    trailing_sl_hit = True
            else:
                # Short put: profit when underlying RISES, trail UP
                trailing_sl = entry_und + trailed_r * orig_r_dist
                if underlying <= trailing_sl:
                    trailing_sl_hit = True
        else:
            # CE / FUT trailing stop (standard direction)
            if side == "BUY":
                trailing_sl = entry_und + trailed_r * orig_r_dist
                if underlying <= trailing_sl:
                    trailing_sl_hit = True
            else:
                trailing_sl = entry_und - trailed_r * orig_r_dist
                if underlying >= trailing_sl:
                    trailing_sl_hit = True

    if trailing_sl_hit:
        hit_sl = True

    # C5: Also check premium-based SL/Target for options
    if not hit_sl and not hit_target and option_type in ("CE", "PE"):
        exit_premium_check = _get_option_premium(
            symbol,
            open_trade.get("expiry"),
            open_trade.get("strike"),
            option_type,
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

    # ── Dead Trade check for CORE trades ─────────────────────────────────────
    # CORE trades never got Dead Trade assessment because the logic only lived
    # in run_timeframe_strategy() (TIMEFRAME-only). This adds the same check
    # for CORE trades: if open > threshold hours with no meaningful favorable
    # movement, close as Dead Trade to free up capital.
    dead_trade_close = False
    hours_open = 0.0
    if not hit_sl and not hit_target:
        try:
            opened_dt = datetime.fromisoformat(
                open_trade["opened_at"].replace("Z", "+00:00")
            )
            hours_open = (
                datetime.now(timezone.utc) - opened_dt
            ).total_seconds() / 3600.0

            if max_fav > stored_mfr:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE paper_trades SET max_favorable_r=? WHERE id=?",
                        (max_fav, open_trade["id"]),
                    )

            # Dead Trade threshold (Flaw #7): 24h for FUT, 3h for options
            dead_trade_hours = 24.0 if option_type == "FUT" else 3.0
            if hours_open >= dead_trade_hours and max_fav < 0.5:
                dead_trade_close = True
                log.info(
                    "%s: paper trade #%s DEAD TRADE — open %.1fh, max favorable R %.2f < 0.5",
                    symbol,
                    open_trade["id"],
                    hours_open,
                    max_fav,
                )
        except Exception:
            log.debug("%s: Dead Trade check failed gracefully", symbol)

    if hit_sl or hit_target:
        if hit_sl:
            if trailing_sl_hit:
                reason = "CLOSED_TRAILING_SL"
            else:
                reason = "SL_HIT"
        else:
            reason = "TARGET_HIT"

        exit_premium = None
        if option_type != "FUT":
            exit_premium = _get_option_premium(
                symbol,
                open_trade.get("expiry"),
                open_trade.get("strike"),
                option_type,
                current_ctx.get("option_rows"),
            )
            # Apply 0.5% slippage on exit for options (Flaw #9)
            if exit_premium and exit_premium > 0:
                if side == "BUY":
                    # closing a BUY means SELL -> lower price
                    exit_premium *= 0.995
                else:
                    # closing a SELL means BUY -> higher price
                    exit_premium *= 1.005

        close_paper_trade(
            open_trade["id"],
            datetime.now(timezone.utc).isoformat(),
            underlying,
            exit_premium,
            "CLOSED_SL" if hit_sl else "CLOSED_TARGET",
            reason,
        )
        log.info(
            "%s: paper trade #%s closed — %s at underlying %g",
            symbol,
            open_trade["id"],
            reason,
            underlying,
        )
        _invalidate_pattern_cache()
        _trigger_ml_retraining()
        actions.append(
            {"action": reason, "trade_id": open_trade["id"], "underlying": underlying}
        )
    elif dead_trade_close:
        exit_premium = None
        if option_type != "FUT":
            exit_premium = _get_option_premium(
                symbol,
                open_trade.get("expiry"),
                open_trade.get("strike"),
                option_type,
                current_ctx.get("option_rows"),
            )
        close_paper_trade(
            open_trade["id"],
            datetime.now(timezone.utc).isoformat(),
            underlying,
            exit_premium if option_type in ("CE", "PE") else underlying,
            "Dead Trade",
            f"Dead trade exit: {hours_open:.1f}h passed, max favorable R {max_fav:.2f} < 0.5",
        )
        _invalidate_pattern_cache()
        _trigger_ml_retraining()
        actions.append(
            {
                "action": "DEAD_TRADE",
                "trade_id": open_trade["id"],
                "underlying": underlying,
            }
        )

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
                (symbol,),
            ).fetchone()
            if closed_trade:
                closed_trade = dict(closed_trade)
                return {
                    "action": "CLOSED",
                    "trade": closed_trade,
                    "reason": f"Closed via exit logic: {closed_trade.get('exit_reason') or 'SL/Target hit'}",
                }
            return {
                "action": "CLOSED",
                "trade": None,
                "reason": "Closed via exit logic (details unavailable)",
            }

    # 2. Check if we already have an open trade
    current_open_trade = get_open_paper_trade(symbol)

    # 3. Parse verdict and confidence from intel
    verdict = intel.get("verdict_label", "")
    confidence = int(intel.get("confidence") or 0)
    if not verdict:
        verdict, confidence = _parse_verdict_and_confidence(
            intel.get("telegram_text") or ""
        )

    # Build paper plan
    from src.engine.paper_plan import build_paper_trade_plan

    plan = build_paper_trade_plan(verdict, confidence, scan_context)
    if not plan:
        return {
            "action": "HOLD",
            "trade": current_open_trade,
            "reason": "No valid plan",
        }

    if plan and intel:
        td = None
        if hasattr(intel, "trade_decision"):
            td = intel.trade_decision
        elif isinstance(intel, dict):
            td = intel.get("trade_decision")
        if td and isinstance(td, dict):
            if td.get("audit_row_id"):
                plan["audit_row_id"] = td["audit_row_id"]
            if td.get("status") == "BLOCKED":
                log.info(
                    "%s: paper trade blocked by decision engine — %s",
                    symbol,
                    td.get("reason"),
                )
                return {
                    "action": "BLOCKED_DECISION",
                    "reason": td.get("reason", "Blocked by trade decision engine"),
                }

    # Add null target guard:
    if plan.get("target_underlying") is None and plan.get("option_type") != "FUT":
        log.warning(
            "%s: plan has null target_underlying — rejecting to prevent phantom exit",
            symbol,
        )
        return {"action": "BLOCKED_PLAN", "reason": "Null target — plan incomplete"}

    # Add extra fields to plan for execute_paper_trade
    option_type = plan["option_type"]
    strike = plan["strike"]

    if option_type == "FUT":
        entry_premium = underlying
    else:
        entry_premium = _get_option_premium(
            symbol, expiry, strike, option_type, option_rows
        )
        if not entry_premium or entry_premium <= 0:
            log.warning(
                "%s: paper trade plan aborted — entry option premium unavailable for %s %s strike %s",
                symbol,
                option_type,
                expiry,
                strike,
            )
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
                    "reason": report.get("reason", ""),
                }

    if current_open_trade and not report.get("action") == "CLOSED":
        return {"action": "HELD", "trade": current_open_trade}

    return report


def run_timeframe_strategy(
    symbol: str, scan_context: dict, digest_id: str, intel: dict, ai_verdict=None
) -> dict | None:
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

    is_hourly_boundary = True
    current_scan_idx = 1
    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        today_scans = get_today_scan_count(symbol, fetched_at)
        current_scan_idx = today_scans + 1
        if current_scan_idx % scans_needed != 0:
            is_hourly_boundary = False

    open_trades_before = get_open_timeframe_trades(symbol)
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── 1. EXIT LOGIC (SL / Target / Dead Trade checked on EVERY scan cycle for safety) ──
    if open_trades_before:
        # Pre-fetch completed candle crossovers exit requirements only if it is an hourly boundary
        has_crossover_data = False
        c_1h_close = 0.0
        p_1h_low = 0.0
        p_1h_high = 0.0
        breakout_buffer = 0.0
        long_oi_support = False
        short_oi_support = False
        bar_end_1h = None

        if is_hourly_boundary:
            chart_indicators = ctx.get("chart_indicators") or {}
            tf_data = chart_indicators
            if not any(k in chart_indicators for k in ("1h", "3h")):
                tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}
            pay_1h = tf_data.get("1h")
            pay_3h = tf_data.get("3h")
            if pay_1h and pay_3h:
                ohlc_1h = pay_1h.get("ohlc")
                prev_1h = pay_1h.get("prev_ohlc") or pay_1h.get("last_closed_ohlc")
                ohlc_3h = pay_3h.get("ohlc")
                prev_3h = pay_3h.get("prev_ohlc") or pay_3h.get("last_closed_ohlc")
                if ohlc_1h and prev_1h and ohlc_3h and prev_3h:
                    c_1h_close = float(ohlc_1h["close"])
                    p_1h_high = float(prev_1h["high"])
                    p_1h_low = float(prev_1h["low"])
                    bar_end_1h = pay_1h.get("bar_end_utc")
                    
                    # Compute breakout buffer (0.5x ATR with 0.3% minimum floor)
                    atr_val = _get_atr(ctx)
                    breakout_buffer = max((atr_val or 0) * 0.5, underlying * 0.003)
                    
                    # Check scan history for OI support
                    current_ce = ctx.get("total_ce_oi")
                    current_pe = ctx.get("total_pe_oi")
                    if current_ce is not None and current_pe is not None:
                        scans_needed = 60 // scan_freq if scan_freq in (15, 30) else 1
                        older = get_scan_summary_n_scans_ago(symbol, scans_needed - 1) if scan_freq in (15, 30) else get_scan_summary_at_least_1h_old(symbol, fetched_at)
                        if older:
                            prev_ce = older["total_ce_oi"]
                            prev_pe = older["total_pe_oi"]
                            ce_diff = current_ce - prev_ce
                            pe_diff = current_pe - prev_pe
                            min_diff_pct = TIMEFRAME_OI_MIN_DIFF_PCT
                            long_oi_support = (pe_diff - ce_diff) > (prev_pe * min_diff_pct)
                            short_oi_support = (ce_diff - pe_diff) > (prev_ce * min_diff_pct)
                            has_crossover_data = True

        for trade in open_trades_before:
            exit_premium = None
            if trade["option_type"] in ("CE", "PE"):
                exit_premium = _get_option_premium(
                    symbol,
                    ctx.get("expiry", ""),
                    trade["strike"],
                    trade["option_type"],
                    ctx.get("option_rows"),
                )

            # A. LLM Reversal Exit
            if ai_verdict is not None:
                ai_bias = _extract_ai_bias(ai_verdict) or "NEUTRAL"
                ai_conf = float(
                    ai_verdict.get("confidence", 50)
                    if isinstance(ai_verdict, dict)
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

            # B. Standard SL/Target update & checks
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
                conn.execute(
                    "UPDATE paper_trades SET max_favorable_r=? WHERE id=?",
                    (max_fav, trade["id"]),
                )

            # SL/Target hit checks
            is_sl_hit = False
            is_tgt_hit = False

            if trade["option_type"] in ("CE", "PE"):
                sl_prem = trade.get("sl_premium")
                tgt_prem = trade.get("target_premium")
                side = trade.get("side") or "BUY"

                if exit_premium:
                    if sl_prem is not None and str(sl_prem).strip() not in ("", "None", "NULL"):
                        try:
                            sl_val = float(sl_prem)
                            if sl_val > 0:
                                if side == "SELL":
                                    is_sl_hit = exit_premium >= sl_val
                                else:
                                    is_sl_hit = exit_premium <= sl_val
                        except ValueError:
                            pass
                    if tgt_prem is not None and str(tgt_prem).strip() not in ("", "None", "NULL"):
                        try:
                            tgt_val = float(tgt_prem)
                            if tgt_val > 0:
                                if side == "SELL":
                                    is_tgt_hit = exit_premium <= tgt_val
                                else:
                                    is_tgt_hit = exit_premium >= tgt_val
                        except ValueError:
                            pass
            else:
                sl_und = trade.get("sl_underlying")
                tgt_und = trade.get("target_underlying")
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
                    exit_premium if trade["option_type"] in ("CE", "PE") else underlying,
                    "CLOSED_SL",
                    f"Options SL hit: premium {exit_premium} vs SL {sl_prem} ({side})" if trade["option_type"] in ("CE", "PE") else f"Futures SL hit: underlying {underlying} <= SL {sl_und}",
                )
                continue
            elif is_tgt_hit:
                close_paper_trade(
                    trade["id"],
                    now_iso,
                    underlying,
                    exit_premium if trade["option_type"] in ("CE", "PE") else underlying,
                    "CLOSED_TARGET",
                    f"Options Target hit: premium {exit_premium} vs Target {tgt_prem} ({side})" if trade["option_type"] in ("CE", "PE") else f"Futures Target hit: underlying {underlying} >= Target {tgt_und}",
                )
                continue

            # C. Dead Trade check
            opened_dt = datetime.fromisoformat(trade["opened_at"].replace("Z", "+00:00"))
            time_diff = (datetime.now(timezone.utc) - opened_dt).total_seconds()
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

            # D. 1H Crossover exit checks (only run on completed hour boundary)
            if has_crossover_data:
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
                                continue
                            elif short_oi_support:
                                close_paper_trade(
                                    trade["id"],
                                    now_iso,
                                    underlying,
                                    exit_premium if trade["option_type"] == "CE" else underlying,
                                    "TF-1H-Cross",
                                    f"timeframe exit | 1H close {c_1h_close:.2f} < p1H_low {p_1h_low:.2f} + Short OI bias",
                                )
                                continue
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
                                continue
                            elif long_oi_support:
                                close_paper_trade(
                                    trade["id"],
                                    now_iso,
                                    underlying,
                                    exit_premium if trade["option_type"] == "PE" else underlying,
                                    "TF-1H-Cross",
                                    f"timeframe exit | 1H close {c_1h_close:.2f} > p1H_high {p_1h_high:.2f} + Long OI bias",
                                )
                                continue

    open_trades_after = get_open_timeframe_trades(symbol)
    closed_trade_id = None
    for pt in open_trades_before:
        if pt["id"] not in [ct["id"] for ct in open_trades_after]:
            closed_trade_id = pt["id"]
            break

    if closed_trade_id:
        with get_conn() as conn:
            closed = conn.execute(
                "SELECT * FROM paper_trades WHERE id=?", (closed_trade_id,)
            ).fetchone()
            if closed:
                closed = dict(closed)
                return {
                    "action": "CLOSED",
                    "trade": closed,
                    "reason": f"Timeframe exit: {closed.get('exit_reason') or 'SL/Target hit'} (P&L: ₹{closed.get('pnl_rupees', 0.0):,.2f})",
                }

    # If it is not a 1-hour boundary, skip checking for new entries
    if not is_hourly_boundary:
        log.info(
            "%s: Timeframe strategy skipped — scan %d is not a 1-hour boundary",
            symbol,
            current_scan_idx,
        )
        return {
            "action": "SKIPPED_TIMEFRAME_BOUNDARY",
            "reason": f"Skipped scan {current_scan_idx}",
        }

    # Pre-fetch entry indicators (we are on a 1-hour boundary here)
    chart_indicators = ctx.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}

    pay_3h = tf_data.get("3h")
    pay_1h = tf_data.get("1h")
    if not pay_3h or not pay_1h:
        log.warning("%s: Timeframe strategy skipped — missing 3h/1h chart data", symbol)
        return None

    ohlc_3h = pay_3h.get("ohlc")
    prev_3h = pay_3h.get("prev_ohlc") or pay_3h.get("last_closed_ohlc")
    ohlc_1h = pay_1h.get("ohlc")
    prev_1h = pay_1h.get("prev_ohlc") or pay_1h.get("last_closed_ohlc")

    if not ohlc_3h or not prev_3h or not ohlc_1h or not prev_1h:
        log.warning(
            "%s: Timeframe strategy skipped — incomplete 3h/1h candle data", symbol
        )
        return None

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
        return None

    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        older = get_scan_summary_n_scans_ago(symbol, scans_needed - 1)
        if not older:
            log.warning(
                "%s: Timeframe strategy skipped — insufficient scan history", symbol
            )
            return None
    else:
        older = get_scan_summary_at_least_1h_old(symbol, fetched_at)
        if not older:
            log.warning(
                "%s: Timeframe strategy skipped — insufficient scan history", symbol
            )
            return None

    prev_ce = older["total_ce_oi"]
    prev_pe = older["total_pe_oi"]
    ce_diff = current_ce - prev_ce
    pe_diff = current_pe - prev_pe

    min_diff_pct = TIMEFRAME_OI_MIN_DIFF_PCT
    long_oi_support = (pe_diff - ce_diff) > (prev_pe * min_diff_pct)
    short_oi_support = (ce_diff - pe_diff) > (prev_ce * min_diff_pct)

    atr_val = _get_atr(ctx)
    breakout_buffer = max((atr_val or 0) * 0.5, underlying * 0.003)

    # ── 2. ENTRY LOGIC ──
    from src.engine.decision_audit import log_decision
    from src.engine.decision_pipeline import PipelineContext, run_entry_pipeline

    # Merge intel into scan_context so the pipeline can access verdict/confidence
    ctx = dict(ctx)
    if intel:
        ctx.setdefault("intel", {}).update(intel)

    # Initialise pipeline context
    pipeline_ctx = PipelineContext(
        engine="TIMEFRAME",
        symbol=symbol,
        direction=None,
        underlying=underlying,
        scan_context=ctx,
        ai_verdict=ai_verdict,
        steps=[],
    )

    # Run entry pipeline
    run_entry_pipeline(pipeline_ctx)

    # Log to decision_audit SQLite table
    audit_row_id = log_decision(
        pipeline_ctx, action="TRADE" if pipeline_ctx.passed else "SKIP"
    )

    if not pipeline_ctx.passed:
        # Check if the signal itself was missing/failed to return None
        signal_step = next((s for s in pipeline_ctx.steps if s.name == "signal"), None)
        if (
            signal_step
            and not signal_step.passed
            and "No 3H breakout detected" in signal_step.reason
        ):
            return None
        if (
            signal_step
            and not signal_step.passed
            and "Missing or incomplete 3H candle data" in signal_step.reason
        ):
            return None

        return {
            "action": "BLOCKED_PLAN",
            "reason": f"Timeframe entry skipped: {pipeline_ctx.block_reason}",
        }

    direction = pipeline_ctx.direction
    signal_key = pipeline_ctx.scan_context.get("_signal_key")
    pyramid_level = pipeline_ctx.scan_context.get("_pyramid_level")
    lot_multiplier = 1.0
    if pyramid_level == 2:
        lot_multiplier = 0.75
    elif pyramid_level == 3:
        lot_multiplier = 0.50

    # Use runtime_config-aware lot sizing, not static DEFAULT_LOTS_PER_TRADE.
    # Check per-symbol overrides saved by the user in the UI Cockpit.
    # Fall back to module-level DEFAULT_LOTS_PER_TRADE so test patches work.
    import json
    from pathlib import Path

    from config.runtime_config import RUNTIME_CONFIG_PATH

    base_sym = symbol.upper().strip().split()[0]
    base_lots = DEFAULT_LOTS_PER_TRADE
    try:
        if RUNTIME_CONFIG_PATH.exists():
            raw = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
            saved_lots = raw.get("paper_symbol_lots") or {}
            if base_sym in saved_lots:
                base_lots = max(1, int(saved_lots[base_sym]))
    except Exception:
        pass
    lots = max(1, round(base_lots * lot_multiplier))

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
            entry_premium = _get_option_premium(
                symbol, expiry, strike, "CE", ctx.get("option_rows")
            )
            if not entry_premium or entry_premium <= 0:
                from src.engine.decision_audit import update_decision_audit

                update_decision_audit(
                    audit_row_id,
                    action="SKIP",
                    block_step="signal",
                    block_reason="Option premium unavailable",
                )
                return {
                    "action": "BLOCKED_PLAN",
                    "reason": f"Timeframe entry skipped: option premium unavailable for CE strike {strike}",
                }
            sl_underlying = float(ohlc_3h["low"])
            tgt_underlying = underlying + 2 * (underlying - sl_underlying)
    else:  # SHORT
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
            entry_premium = _get_option_premium(
                symbol, expiry, strike, "PE", ctx.get("option_rows")
            )
            if not entry_premium or entry_premium <= 0:
                from src.engine.decision_audit import update_decision_audit

                update_decision_audit(
                    audit_row_id,
                    action="SKIP",
                    block_step="signal",
                    block_reason="Option premium unavailable",
                )
                return {
                    "action": "BLOCKED_PLAN",
                    "reason": f"Timeframe entry skipped: option premium unavailable for PE strike {strike}",
                }
            sl_underlying = float(ohlc_3h["high"])
            tgt_underlying = underlying - 2 * (sl_underlying - underlying)

    # Convert underlying SL/Target to premium equivalents (unified via trade_plan.py)
    side = "BUY" if direction == "LONG" else ("SELL" if opt_type == "FUT" else "BUY")
    sl_premium, target_premium = convert_underlying_sl_to_premium(
        underlying,
        sl_underlying,
        tgt_underlying,
        entry_premium,
        side,
        opt_type,
        strike,
        ctx.get("option_rows"),
    )

    reason_str = (
        f"timeframe entry | 3H close {c_3h_close:.2f} > p3H_high {p_3h_high:.2f} | level {pyramid_level}"
        if direction == "LONG"
        else f"timeframe entry | 3H close {c_3h_close:.2f} < p3H_low {p_3h_low:.2f} | level {pyramid_level}"
    )

    # Phase 0: Capture ML feature snapshot at trade-open time
    ml_features = _build_ml_feature_snapshot(ctx, ai_verdict)

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
        # Phase 0: ML feature columns (captured at trade open time)
        **ml_features,
    }

    if ai_verdict is not None:
        if isinstance(ai_verdict, dict):
            exit_advice = ai_verdict.get("exit_advice", "")
        else:
            exit_advice = getattr(ai_verdict, "exit_advice", "")
        if exit_advice and "sl" in str(exit_advice).lower():
            try:
                m = re.search(
                    r"sl[^\d]*?(\d+(?:\.\d+)?)", str(exit_advice), re.IGNORECASE
                )
                if m:
                    sl_underlying = float(m.group(1))
                    # recalculate premium SL with new underlying SL
                    sl_premium, _ = convert_underlying_sl_to_premium(
                        underlying,
                        sl_underlying,
                        tgt_underlying,
                        entry_premium,
                        side,
                        opt_type,
                        strike,
                        ctx.get("option_rows"),
                    )
                    trade_data["sl_underlying"] = sl_underlying
                    trade_data["sl_premium"] = sl_premium
            except Exception:
                pass

    trade_id = insert_paper_trade(trade_data)
    from src.engine.decision_audit import update_decision_audit

    if not trade_id:
        log.warning(
            "%s: paper trade INSERT skipped - duplicate signal_key=%s",
            symbol,
            signal_key,
        )
        update_decision_audit(
            audit_row_id,
            action="SKIP",
            block_step="signal",
            block_reason="duplicate signal key",
        )
        return {
            "action": "BLOCKED_PLAN",
            "trade_id": None,
            "reason": "duplicate signal key",
        }
    update_decision_audit(audit_row_id, action="TRADE", trade_id=trade_id)
    log.info(
        "%s: Timeframe Strategy %s entry triggered! Strike %g Premium %g Lots %d (Level %d)",
        symbol,
        direction,
        strike,
        entry_premium,
        lots,
        pyramid_level,
    )
    return {
        "action": "EXECUTED",
        "trade": trade_data,
        "setup_type": "TIMEFRAME",
        "reason": f"timeframe entry | level {pyramid_level}",
        "lots": lots,
    }
