"""Shared paper-trade plan builder for Telegram text and auto execution.

P3 fix (#13): MAX_LEVEL_DISTANCE_STEPS moved to config/settings.py.
  Imported from there so the value is tunable per-deployment without a
  code change. Local module constant removed.

Autopsy fix 1: SELL OTM fallback strike now defaults to ATM ± 2 steps
  (properly OTM) when support/resistance level is absent or too distant.
  Previously defaulted to ATM ± 1 step which could produce near-ATM or
  ITM credit legs with high delta and unlimited loss potential.
"""
from __future__ import annotations

from config.symbol_classes import get_strike_step
from config.settings import MAX_LEVEL_DISTANCE_STEPS
from src.engine.verdict_sets import BULLISH_VERDICTS, BEARISH_VERDICTS, is_bullish, is_bearish

MIN_PAPER_CONFIDENCE = 65

# Minimum OTM distance (in steps) for SELL option legs when no S/R level is
# available. 2 steps keeps the short strike meaningfully OTM on Nifty (100pts)
# and reduces delta to ~0.30 range, which is a reasonable credit-spread entry.
SELL_FALLBACK_OTM_STEPS = 2

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


VERDICT_ACTION_MAP = {
    # Bullish
    "Long Buildup":    ("BUY",  "CE"),
    "Put Writing":     ("SELL", "PE"),   # sell PE = bullish (collect premium at support)
    "Short Covering":  ("BUY",  "CE"),
    "OI Bias Bullish": ("BUY",  "CE"),
    # Bearish
    "Short Buildup":   ("BUY",  "PE"),
    "Call Writing":    ("SELL", "CE"),   # sell CE = bearish (collect premium at resistance)
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

    # Futures fallback/preference for Natural Gas and Crude Oil (MCX commodities with poor option liquidity)
    is_natgas = "NATURALGAS" in symbol or "CRUDEOIL" in symbol
    if is_natgas:
        option_type = "FUT"
        side = "BUY" if bullish else "SELL"

    step = float(get_strike_step(symbol) or 1)
    atm = _safe_float(ctx.get("atm_strike")) or _round_to_step(underlying, step)
    support = _safe_float(ctx.get("support"))
    resistance = _safe_float(ctx.get("resistance"))

    # Strike selection: OTM for SELL, ATM for BUY
    # Autopsy fix 1: SELL fallback uses SELL_FALLBACK_OTM_STEPS (2) instead
    # of 1 step, ensuring the short strike remains genuinely OTM when no
    # support/resistance level is available from the scan context.
    if option_type in ("CE", "PE"):
        if side == "SELL":
            if option_type == "CE":
                # Prefer resistance level if it's meaningfully above spot;
                # fall back to ATM + 2 steps (OTM) so we avoid writing ITM calls.
                strike = (
                    _round_to_step(resistance, step)
                    if (resistance and resistance > underlying)
                    else _round_to_step(underlying + SELL_FALLBACK_OTM_STEPS * step, step)
                )
            else:
                # Prefer support level if it's meaningfully below spot;
                # fall back to ATM - 2 steps (OTM) so we avoid writing ITM puts.
                strike = (
                    _round_to_step(support, step)
                    if (support and support < underlying)
                    else _round_to_step(underlying - SELL_FALLBACK_OTM_STEPS * step, step)
                )
        else:
            strike = atm
    else:
        # FUT
        strike = atm

    atr_used = False
    setup_type = ctx.get("setup_type")
    if option_type == "FUT" and setup_type != "TIMEFRAME":
        chart_indicators = ctx.get("chart_indicators") or {}
        tf_data = chart_indicators
        if not any(k in chart_indicators for k in ("1h", "3h")):
            tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}
        pay_3h = tf_data.get("3h") or {}
        pay_1h = tf_data.get("1h") or {}
        atr = pay_3h.get("atr_14") or pay_1h.get("atr_14")
        if atr is None:
            for tf, payload in tf_data.items():
                if isinstance(payload, dict) and payload.get("atr_14"):
                    atr = payload["atr_14"]
                    break
        if atr and atr > 0:
            atr_used = True
            if bullish:
                sl = underlying - 1.5 * atr
                target = underlying + 2.0 * atr
            else:
                sl = underlying + 1.5 * atr
                target = underlying - 2.0 * atr

    if not atr_used:
        if bullish:
            sl = _near_level(support, underlying, step, "below")
            target = _near_level(resistance, underlying, step, "above")
            sl = sl if sl is not None else _round_to_step(underlying - step, step)
            target = target if target is not None else _round_to_step(underlying + step, step)
            if sl >= underlying:
                sl = _round_to_step(underlying - step, step)
            if target <= underlying:
                target = _round_to_step(underlying + step, step)
        else:
            sl = _near_level(resistance, underlying, step, "above")
            target = _near_level(support, underlying, step, "below")
            sl = sl if sl is not None else _round_to_step(underlying + step, step)
            target = target if target is not None else _round_to_step(underlying - step, step)
            if sl <= underlying:
                sl = _round_to_step(underlying + step, step)
            if target >= underlying:
                target = _round_to_step(underlying - step, step)

    return {
        "verdict_label": verdict,
        "side": side,
        "option_type": option_type,
        "strike": strike,
        "entry_underlying": underlying,
        "sl_underlying": round(sl, 4),
        "target_underlying": round(target, 4),
        "confidence": int(confidence or 0),
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
