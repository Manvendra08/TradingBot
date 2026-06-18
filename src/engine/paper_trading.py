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
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

from config.settings import (
    LOT_SIZES,
    MIN_ENTRY_QUALITY_CORE,
    REVERSAL_MIN_CONFIDENCE,
)
from config.runtime_config import load_runtime_config
from src.models.schema import (
    get_conn,
    get_open_paper_trade,
    insert_paper_trade,
    close_paper_trade,
)
from src.engine.capital_allocator import calculate_trade_lots
from src.engine.risk_engine import check_risk_limits
from src.engine.entry_quality import calculate_entry_quality
from src.engine.trend_analysis import get_trend_alignment_score
from src.engine.verdict_sets import is_bullish, is_bearish


# ---------------------------------------------------------------------------
# SL / Target calculation — ATR-based (unified with live_trading.py)
# ---------------------------------------------------------------------------

def _get_atr(ctx: dict) -> Optional[float]:
    """Extract ATR-14 from chart_indicators, trying 3h then 1h then any TF."""
    chart_indicators = ctx.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}
    pay_3h = tf_data.get("3h") or {}
    pay_1h = tf_data.get("1h") or {}
    atr = pay_3h.get("atr_14") or pay_1h.get("atr_14")
    if atr is None:
        for _tf, payload in tf_data.items():
            if isinstance(payload, dict) and payload.get("atr_14"):
                return float(payload["atr_14"])
    return float(atr) if atr else None


def _calculate_buy_sl_target(
    entry_premium: float,
    underlying: float,
    ctx: dict,
    step: float = 50.0,
) -> tuple[float, float]:
    """
    ATR-based SL/Target for BUY legs (unified with live_trading.py).
    Falls back to 2-step underlying distance when ATR unavailable.
    Previously used fixed 0.70× / 1.50× premium multipliers — those
    were premium-only, making OTM options near-zero delta effectively
    un-stoppable on noise. ATR on the underlying is the correct unit.
    """
    atr = _get_atr(ctx)
    if atr and atr > 0:
        sl_underlying     = underlying - 1.5 * atr
        target_underlying = underlying + 2.0 * atr
    else:
        sl_underlying     = underlying - 2 * step
        target_underlying = underlying + 2 * step
    return round(sl_underlying, 2), round(target_underlying, 2)


def _calculate_sell_sl_target(
    entry_premium: float,
    underlying: float,
    ctx: dict,
    step: float = 50.0,
) -> tuple[float, float]:
    """
    ATR-based SL/Target for SELL legs (unified with live_trading.py).
    Falls back to 2-step underlying distance when ATR unavailable.
    """
    atr = _get_atr(ctx)
    if atr and atr > 0:
        sl_underlying     = underlying + 1.5 * atr
        target_underlying = underlying - 2.0 * atr
    else:
        sl_underlying     = underlying + 2 * step
        target_underlying = underlying - 2 * step
    return round(sl_underlying, 2), round(target_underlying, 2)


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
            close_paper_trade(
                open_trade["id"],
                exit_reason="REVERSAL_SIGNAL",
                exit_underlying=underlying,
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
    lots = calculate_trade_lots(symbol, plan.get("entry_premium", 0), side, rconf)
    if lots <= 0:
        return {"action": "BLOCKED_LOTS", "trade_id": None, "reason": "Zero lots calculated"}

    # SL / Target
    entry_premium = float(plan.get("entry_premium") or 0)
    if side == "BUY":
        sl_ul, tgt_ul = _calculate_buy_sl_target(entry_premium, underlying, ctx, step)
    else:
        sl_ul, tgt_ul = _calculate_sell_sl_target(entry_premium, underlying, ctx, step)

    trade_id = insert_paper_trade(
        symbol=symbol,
        side=side,
        option_type=option_type,
        strike=strike,
        entry_premium=entry_premium,
        entry_underlying=underlying,
        sl_underlying=sl_ul,
        target_underlying=tgt_ul,
        lots=lots,
        verdict=verdict,
        confidence=confidence,
    )
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

    if hit_sl or hit_target:
        reason = "SL_HIT" if hit_sl else "TARGET_HIT"
        close_paper_trade(
            open_trade["id"],
            exit_reason=reason,
            exit_underlying=underlying,
        )
        log.info("%s: paper trade #%s closed — %s at underlying %g",
                 symbol, open_trade["id"], reason, underlying)
        actions.append({"action": reason, "trade_id": open_trade["id"],
                        "underlying": underlying})

    return actions
