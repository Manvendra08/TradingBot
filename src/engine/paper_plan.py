"""Shared paper-trade plan builder for Telegram text and auto execution.

P3 fix (#13): MAX_LEVEL_DISTANCE_STEPS moved to config/settings.py.
Autopsy fix #1: SELL OTM fallback uses SELL_FALLBACK_OTM_STEPS (2 steps).
Autopsy fix #2: Underlying SL/Target fallback uses ATR (1.5×/2.0×) when
  available; hard-minimum floor of 2 steps when ATR absent (was 1 step).
  Prevents noise-level SL on first intraday candle.
Autopsy fix #9: MCX commodity FUT routing checks option OI context before
  forcing FUT. ATR multiplier for FUT SL on NATGAS/CRUDEOIL raised to 2.0×
  to survive normal ±5-8% intraday range.
"""
from __future__ import annotations

import logging

from config.symbol_classes import get_strike_step
from config.settings import MAX_LEVEL_DISTANCE_STEPS
from src.engine.verdict_sets import BULLISH_VERDICTS, BEARISH_VERDICTS, is_bullish, is_bearish

log = logging.getLogger(__name__)

MIN_PAPER_CONFIDENCE = 65

# Minimum OTM distance (in steps) for SELL option legs when no S/R level is
# available. 2 steps keeps the short strike meaningfully OTM on Nifty (100pts)
# and reduces delta to ~0.30 range.
SELL_FALLBACK_OTM_STEPS = 2

# Minimum underlying SL distance (in steps) when ATR is unavailable.
# 2 steps = 100pts on NIFTY (~0.4%) — just above normal 5-min noise floor.
# Previously 1 step (50pts) triggered SL on first candle in low-signal env.
SL_FALLBACK_MIN_STEPS = 2

# ATR multipliers for SL and Target on FUT legs
# NATGAS/CRUDEOIL raised to 2.0× SL (vs 1.5× equity) — instruments move
# ±5-8% intraday vs ±1-2% for equity index futures.
FUT_SL_ATR_MULT_EQUITY   = 1.5
FUT_TGT_ATR_MULT_EQUITY  = 2.0
FUT_SL_ATR_MULT_COMMODITY = 2.0   # fix #9
FUT_TGT_ATR_MULT_COMMODITY = 3.0  # fix #9

LONG_CE_VERDICTS = BULLISH_VERDICTS   # backward-compat alias
LONG_PE_VERDICTS = BEARISH_VERDICTS   # backward-compat alias
WRITING_VERDICTS = {"Put Writing", "Call Writing"}


def is_bullish_verdict(verdict: str) -> bool:
    return is_bullish(verdict)


def is_bearish_verdict(verdict: str) -> bool:
    return is_bearish(verdict)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 2)
    return round(round(value / step) * step, 2)


def _near_level(level: float, underlying: float, step: float, direction: str) -> float | None:
    if level <= 0 or underlying <= 0:
        return None
    distance = abs(level - underlying)
    if distance > step * MAX_LEVEL_DISTANCE_STEPS:
        return None
    if direction == "above" and level > underlying:
        return level
    if direction == "below" and level < underlying:
        return level
    return None


def _is_mcx_commodity(symbol: str) -> bool:
    """Return True for MCX commodity symbols routed as futures."""
    return "NATURALGAS" in symbol or "CRUDEOIL" in symbol


def _should_force_fut(symbol: str, ctx: dict) -> bool:
    """Fix #9: force FUT routing only when option OI data is absent/thin.

    Checks ctx for option_chain or pcr data; if present and non-trivial,
    allows option routing even for MCX symbols. Logs decision.
    """
    if not _is_mcx_commodity(symbol):
        return False
    # If the scan context contains meaningful option chain / PCR data, allow
    # options routing instead of always forcing FUT.
    pcr = _safe_float(ctx.get("pcr"))
    oi_data = ctx.get("option_chain") or ctx.get("oi_data")
    has_option_data = (pcr > 0) or bool(oi_data)
    if has_option_data:
        log.info("%s: option OI data present — skipping forced FUT routing.", symbol)
        return False
    log.info("%s: no option OI data — forcing FUT routing.", symbol)
    return True


VERDICT_ACTION_MAP = {
    # Bullish
    "Long Buildup":    ("BUY",  "CE"),
    "Put Writing":     ("SELL", "PE"),
    "Short Covering":  ("BUY",  "CE"),
    "OI Bias Bullish": ("BUY",  "CE"),
    # Bearish
    "Short Buildup":   ("BUY",  "PE"),
    "Call Writing":    ("SELL", "CE"),
    "Long Unwinding":  ("BUY",  "PE"),
    "OI Bias Bearish": ("BUY",  "PE"),
}


def build_paper_trade_plan(verdict: str, confidence: int, ctx: dict) -> dict | None:
    """Return the executable paper plan, or None when no clean auto entry exists."""
    if int(confidence or 0) < MIN_PAPER_CONFIDENCE:
        return None

    symbol = str(ctx.get("symbol") or "").upper()
    underlying = _safe_float(ctx.get("underlying"))
    if underlying <= 0:
        return None

    verdict_str = str(verdict or "")
    if verdict_str not in VERDICT_ACTION_MAP:
        return None

    side, option_type = VERDICT_ACTION_MAP[verdict_str]
    bullish = is_bullish(verdict_str)
    is_commodity = _is_mcx_commodity(symbol)

    # Fix #9: only force FUT when option OI data is genuinely absent
    if _should_force_fut(symbol, ctx):
        option_type = "FUT"
        side = "BUY" if bullish else "SELL"

    step = float(get_strike_step(symbol) or 1)
    atm = _safe_float(ctx.get("atm_strike")) or _round_to_step(underlying, step)
    support = _safe_float(ctx.get("support"))
    resistance = _safe_float(ctx.get("resistance"))

    # Strike selection: OTM for SELL, ATM for BUY
    if option_type in ("CE", "PE"):
        if side == "SELL":
            if option_type == "CE":
                strike = (
                    _round_to_step(resistance, step)
                    if (resistance and resistance > underlying)
                    else _round_to_step(underlying + SELL_FALLBACK_OTM_STEPS * step, step)
                )
            else:
                strike = (
                    _round_to_step(support, step)
                    if (support and support < underlying)
                    else _round_to_step(underlying - SELL_FALLBACK_OTM_STEPS * step, step)
                )
        else:
            strike = atm
    else:
        strike = atm

    # ── SL / Target calculation ───────────────────────────────────────────
    # Fix #2 + #9: ATR-based SL/Target for ALL option_type paths (not just
    # setup_type!=TIMEFRAME). Commodity futures use wider ATR multipliers.
    # When ATR is unavailable, fall back to SL_FALLBACK_MIN_STEPS (2) instead
    # of the old 1-step floor that caused noise-triggered SLs.

    sl_atr_mult  = FUT_SL_ATR_MULT_COMMODITY  if is_commodity else FUT_SL_ATR_MULT_EQUITY
    tgt_atr_mult = FUT_TGT_ATR_MULT_COMMODITY if is_commodity else FUT_TGT_ATR_MULT_EQUITY

    atr_used = False
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
                atr = payload["atr_14"]
                break

    if atr and atr > 0:
        atr_used = True
        if bullish:
            sl     = underlying - sl_atr_mult * atr
            target = underlying + tgt_atr_mult * atr
        else:
            sl     = underlying + sl_atr_mult * atr
            target = underlying - tgt_atr_mult * atr

    # Fix #2: fallback uses SL_FALLBACK_MIN_STEPS (2) not 1
    if not atr_used:
        min_sl_dist = SL_FALLBACK_MIN_STEPS * step
        if bullish:
            sl = _near_level(support, underlying, step, "below")
            target = _near_level(resistance, underlying, step, "above")
            sl = sl if sl is not None else _round_to_step(underlying - min_sl_dist, step)
            target = target if target is not None else _round_to_step(underlying + min_sl_dist, step)
            if sl >= underlying:
                sl = _round_to_step(underlying - min_sl_dist, step)
            if target <= underlying:
                target = _round_to_step(underlying + min_sl_dist, step)
        else:
            sl = _near_level(resistance, underlying, step, "above")
            target = _near_level(support, underlying, step, "below")
            sl = sl if sl is not None else _round_to_step(underlying + min_sl_dist, step)
            target = target if target is not None else _round_to_step(underlying - min_sl_dist, step)
            if sl <= underlying:
                sl = _round_to_step(underlying + min_sl_dist, step)
            if target >= underlying:
                target = _round_to_step(underlying - min_sl_dist, step)

    return {
        "verdict_label":    verdict,
        "side":             side,
        "option_type":      option_type,
        "strike":           strike,
        "entry_underlying": underlying,
        "sl_underlying":    round(sl, 4),
        "target_underlying": round(target, 4),
        "confidence":       int(confidence or 0),
    }


def format_paper_plan(plan: dict | None) -> str:
    if not plan:
        return "No auto paper trade: wait for cleaner alignment"
    strike = plan.get("strike")
    opt = plan.get("option_type")
    sl = plan.get("sl_underlying")
    target = plan.get("target_underlying")
    side = str(plan.get("side", "BUY")).title()
    if opt == "FUT":
        return (
            f"{side} {opt} at current scan "
            f"| SL spot {sl:g} | Target spot {target:g}"
        )
    return (
        f"{side} {strike:g} {opt} at current scan "
        f"| SL spot {sl:g} | Target spot {target:g}"
    )
