"""Shared paper-trade plan builder for Telegram text and auto execution.

P3 fix (#13): MAX_LEVEL_DISTANCE_STEPS moved to config/settings.py.
  Imported from there so the value is tunable per-deployment without a
  code change. Local module constant removed.

L5 fix: MCX commodities (NATURALGAS, CRUDEOIL) now use options when ATM
  liquidity is sufficient (volume >= threshold AND OI >= threshold).
  Falls back to FUT when options are illiquid.
"""
from __future__ import annotations

import logging

from config.symbol_classes import get_strike_step
from config.settings import MAX_LEVEL_DISTANCE_STEPS
from src.engine.verdict_sets import BULLISH_VERDICTS, BEARISH_VERDICTS, is_bullish, is_bearish

log = logging.getLogger(__name__)

MIN_PAPER_CONFIDENCE = 65

# L5: Liquidity thresholds for MCX commodity options.
# If ATM option rows meet BOTH thresholds, use options instead of forced FUT.
_MCX_OPTION_MIN_VOLUME = 500    # minimum total volume (CE + PE) at ATM
_MCX_OPTION_MIN_OI = 2000       # minimum total open interest (CE + PE) at ATM

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


def mcx_option_liquidity_ok(symbol: str, atm_strike: float, ctx: dict) -> bool:
    """
    L5: Check if ATM option liquidity is sufficient for MCX commodities.
    Returns True if BOTH volume and OI thresholds are met at the ATM strike.
    Falls back to FUT if either threshold fails or data is unavailable.
    """
    if atm_strike <= 0:
        return False

    option_rows = ctx.get("option_rows") or []
    total_volume = 0
    total_oi = 0
    found_any = False

    for row in option_rows:
        try:
            row_strike = float(row.get("strike") or 0)
            if abs(row_strike - atm_strike) < 0.01:
                vol = int(row.get("volume") or 0)
                oi = int(row.get("oi") or 0)
                total_volume += vol
                total_oi += oi
                found_any = True
        except (ValueError, TypeError):
            continue

    if not found_any:
        log.debug("%s: MCX liquidity check — no ATM option rows found, falling back to FUT", symbol)
        return False

    volume_ok = total_volume >= _MCX_OPTION_MIN_VOLUME
    oi_ok = total_oi >= _MCX_OPTION_MIN_OI

    if volume_ok and oi_ok:
        log.debug(
            "%s: MCX liquidity OK — ATM vol=%d (min=%d), OI=%d (min=%d). Using options.",
            symbol, total_volume, _MCX_OPTION_MIN_VOLUME, total_oi, _MCX_OPTION_MIN_OI,
        )
        return True

    log.debug(
        "%s: MCX liquidity INSUFFICIENT — ATM vol=%d (min=%d, ok=%s), OI=%d (min=%d, ok=%s). "
        "Falling back to FUT.",
        symbol, total_volume, _MCX_OPTION_MIN_VOLUME, volume_ok,
        total_oi, _MCX_OPTION_MIN_OI, oi_ok,
    )
    return False


VERDICT_ACTION_MAP = {
    # Bullish — OI labels
    "Long Buildup":    ("BUY",  "CE"),
    "Put Writing":     ("SELL", "PE"),   # sell PE = bullish (collect premium at support)
    "Short Covering":  ("BUY",  "CE"),
    "OI Bias Bullish": ("SELL", "PE"),   # sell PE = bullish (instead of buy CE)
    # Bearish — OI labels
    "Short Buildup":   ("BUY",  "PE"),
    "Call Writing":    ("SELL", "CE"),   # sell CE = bearish (collect premium at resistance)
    "Long Unwinding":  ("BUY",  "PE"),
    "OI Bias Bearish": ("SELL", "CE"),   # sell CE = bearish (instead of buy PE)
    # LLM action labels — map to canonical option actions
    "GO_LONG":         ("BUY",  "CE"),   # LLM bullish directive
    "GO_SHORT":        ("BUY",  "PE"),   # LLM bearish directive
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

    step = float(get_strike_step(symbol) or 1)
    atm = _safe_float(ctx.get("atm_strike")) or _round_to_step(underlying, step)

    # L5: MCX commodities — use options when ATM liquidity is sufficient,
    # otherwise fall back to FUT. Previously forced FUT unconditionally.
    is_mcx_commodity = "NATURALGAS" in symbol or "CRUDEOIL" in symbol
    if is_mcx_commodity:
        use_options = mcx_option_liquidity_ok(symbol, atm, ctx)
        if not use_options:
            option_type = "FUT"
            side = "BUY" if bullish else "SELL"
        # else: keep the original option_type (CE/PE) and side from VERDICT_ACTION_MAP
    support = _safe_float(ctx.get("support"))
    resistance = _safe_float(ctx.get("resistance"))

    # Strike selection: OTM for SELL, ATM for BUY
    if option_type in ("CE", "PE"):
        if side == "SELL":
            if option_type == "CE":
                strike = _round_to_step(resistance, step) if (resistance and resistance > underlying) else _round_to_step(underlying + step * MAX_LEVEL_DISTANCE_STEPS, step)
            else:
                strike = _round_to_step(support, step) if (support and support < underlying) else _round_to_step(underlying - step * MAX_LEVEL_DISTANCE_STEPS, step)
        else:
            strike = atm
    else:
        # FUT
        strike = atm

    # Flaw #2: Mandate ATR for SL/Target calculation instead of fixed steps.
    from src.engine.trade_plan import get_atr
    atr = get_atr(ctx)
    if atr and atr > 0:
        if bullish:
            sl = underlying - 1.5 * atr
            target = underlying + 2.0 * atr
        else:
            sl = underlying + 1.5 * atr
            target = underlying - 2.0 * atr
    else:
        log.warning("%s: Missing ATR data, skipping trade plan creation (strict ATR requirement)", symbol)
        return None

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
    symbol = plan.get("symbol", "")
    strike = plan.get("strike")
    opt = plan.get("option_type")
    sl = plan.get("sl_underlying")
    target = plan.get("target_underlying")
    side = str(plan.get("side", "BUY")).title()

    is_commodity = symbol.upper() in {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}
    if is_commodity and opt == "FUT":
        return (f"{side} {opt} (Commodity) at current scan "
                f"| SL spot {sl:g} | Target spot {target:g}")
    if opt == "FUT":
        return (
            f"{side} {opt} at current scan "
            f"| SL spot {sl:g} | Target spot {target:g}"
        )
    return (
        f"{side} {strike:g} {opt} at current scan "
        f"| SL spot {sl:g} | Target spot {target:g}"
    )
