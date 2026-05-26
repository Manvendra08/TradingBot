"""Shared paper-trade plan builder for Telegram text and auto execution."""
from __future__ import annotations

from config.symbol_classes import get_strike_step

MIN_PAPER_CONFIDENCE = 65
MAX_LEVEL_DISTANCE_STEPS = 3

LONG_CE_VERDICTS = {"Long Buildup", "OI Bias Bullish"}
LONG_PE_VERDICTS = {"Short Buildup", "OI Bias Bearish"}
WRITING_VERDICTS = {"Put Writing", "Call Writing"}


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


def is_bullish_verdict(verdict: str) -> bool:
    return str(verdict or "") in LONG_CE_VERDICTS


def is_bearish_verdict(verdict: str) -> bool:
    return str(verdict or "") in LONG_PE_VERDICTS


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


def build_paper_trade_plan(verdict: str, confidence: int, ctx: dict) -> dict | None:
    """Return the executable long-option paper plan, or None when no clean auto entry exists."""
    if int(confidence or 0) < MIN_PAPER_CONFIDENCE:
        return None

    symbol = str(ctx.get("symbol") or "").upper()
    underlying = _safe_float(ctx.get("underlying"))
    if underlying <= 0:
        return None

    bullish = str(verdict or "") in LONG_CE_VERDICTS
    bearish = str(verdict or "") in LONG_PE_VERDICTS
    if str(verdict or "") in WRITING_VERDICTS or (not bullish and not bearish):
        return None

    step = float(get_strike_step(symbol) or 1)
    atm = _safe_float(ctx.get("atm_strike")) or _round_to_step(underlying, step)
    support = _safe_float(ctx.get("support"))
    resistance = _safe_float(ctx.get("resistance"))
    option_type = "CE" if bullish else "PE"

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
        "side": "BUY",
        "option_type": option_type,
        "strike": atm,
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
    return (
        f"Buy {strike:g} {opt} at current scan "
        f"| SL spot {sl:g} | Target spot {target:g}"
    )
