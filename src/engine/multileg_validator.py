"""Multi-Leg Strategy Pre-Flight & Engine Alignment Validator.

Prevents LLM direction flips and enforces multi-leg risk rules (margin cap, net delta cap, leg limits).
"""
from dataclasses import dataclass
from src.engine.execution_parser import ParsedExecution

@dataclass
class ValidationResult:
    is_valid: bool
    rejection_reason: str = ""
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


def _estimate_margin(legs: list[dict], underlying: float, symbol: str = "NIFTY") -> float:
    """Accurate estimation of broker margin requirement based on structure and lot size."""
    try:
        from src.engine.multileg_strategy import calculate_combined_margin
        norm_legs = []
        for leg in legs:
            l_copy = dict(leg)
            if "side" not in l_copy and "action" in l_copy:
                l_copy["side"] = l_copy["action"]
            if "lots" not in l_copy and "ratio" in l_copy:
                l_copy["lots"] = l_copy["ratio"]
            if "premium" not in l_copy and "entry_premium" in l_copy:
                l_copy["premium"] = l_copy["entry_premium"]
            norm_legs.append(l_copy)
        margin = calculate_combined_margin(norm_legs, symbol, underlying=underlying)
        if margin > 0:
            return float(margin)
    except Exception:
        pass

    from config.settings import LOT_SIZES
    base = symbol.upper().split()[0] if symbol else "NIFTY"
    lot_size = LOT_SIZES.get(base, LOT_SIZES.get(symbol, 75))
    margin = 0.0
    for leg in legs:
        if str(leg.get("action") or leg.get("side") or "").upper() == "SELL":
            ratio = float(leg.get("ratio") or leg.get("lots") or 1)
            margin += (underlying * ratio * lot_size * 0.15)
    return margin


from src.engine.verdict_sets import is_bullish as _is_bullish_canonical, is_bearish as _is_bearish_canonical


def _check_engine_direction(engine_verdict: str) -> tuple[bool, bool]:
    """Return (is_bullish, is_bearish) tuple respecting canonical verdict sets."""
    v = str(engine_verdict or "").strip()
    if _is_bullish_canonical(v):
        return True, False
    if _is_bearish_canonical(v):
        return False, True
    v_upper = v.upper()
    if "SHORT COVERING" in v_upper:
        return True, False
    if "LONG UNWINDING" in v_upper:
        return False, True
    is_bull = "BULLISH" in v_upper or "LONG" in v_upper
    is_bear = "BEARISH" in v_upper or "SHORT" in v_upper
    return is_bull, is_bear


def validate_multileg_trade(
    proposal: ParsedExecution,
    engine_verdict: str,
    underlying: float,
    max_margin_inr: float = 500000.0,
    max_net_delta: float = 0.60,
    symbol: str = "NIFTY",
) -> ValidationResult:
    """Validate multileg proposal against engine verdict and risk limits."""
    if not proposal.is_valid:
        return ValidationResult(is_valid=False, rejection_reason=proposal.rejection_reason)

    is_bullish_engine, is_bearish_engine = _check_engine_direction(engine_verdict)

    # Check direction conflict
    if is_bearish_engine and proposal.action == "GO_LONG":
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Direction conflict: Engine is {engine_verdict}, but proposal is GO_LONG."
        )
    if is_bullish_engine and proposal.action == "GO_SHORT":
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Direction conflict: Engine is {engine_verdict}, but proposal is GO_SHORT."
        )

    # Check max legs limit
    if len(proposal.legs) > 6:
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Risk limit exceeded: Proposal has {len(proposal.legs)} legs (max 6)."
        )

    # Approximate margin check
    estimated_margin = _estimate_margin(proposal.legs, underlying, symbol=symbol)
    if estimated_margin > max_margin_inr:
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Margin requirement exceeded: Estimated margin ₹{estimated_margin:.2f} > max ₹{max_margin_inr:.2f}."
        )

    # Net delta cap check
    net_delta = 0.0
    has_delta = False
    for leg in proposal.legs:
        if "delta" in leg and leg["delta"] is not None:
            has_delta = True
            leg_delta = float(leg["delta"])
            action = str(leg.get("action") or leg.get("side") or "SELL").upper()
            ratio = float(leg.get("ratio") or 1)
            sign = -1.0 if action == "SELL" else 1.0
            net_delta += sign * leg_delta * ratio

    if has_delta and abs(net_delta) > max_net_delta:
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Risk limit exceeded: Proposal net delta {net_delta:+.2f} exceeds cap ±{max_net_delta:.2f}."
        )

    # Defined-risk wing width check
    strategy_type = getattr(proposal, "strategy_type", None) or getattr(proposal, "strategy", None) or getattr(proposal, "structure", None) or ""
    if str(strategy_type).upper() in ("IRON_CONDOR", "BEAR_CALL_SPREAD", "BULL_PUT_SPREAD"):
        from config.multileg_strategies import MIN_WING_WIDTH_PCT, MIN_WING_WIDTH_POINTS
        sym_key = symbol.upper().split()[0] if symbol else "DEFAULT"
        min_width_pts = MIN_WING_WIDTH_POINTS.get(sym_key, MIN_WING_WIDTH_POINTS.get("DEFAULT", 50.0))
        min_width_pct = MIN_WING_WIDTH_PCT.get(sym_key, MIN_WING_WIDTH_PCT.get("DEFAULT", 0.005))
        effective_min_width = max(min_width_pts, underlying * min_width_pct) if underlying > 0 else min_width_pts

        ce_sell = [float(l["strike"]) for l in proposal.legs if str(l.get("option_type") or "").upper() == "CE" and str(l.get("action") or l.get("side") or "").upper() == "SELL" and "strike" in l]
        ce_buy = [float(l["strike"]) for l in proposal.legs if str(l.get("option_type") or "").upper() == "CE" and str(l.get("action") or l.get("side") or "").upper() == "BUY" and "strike" in l]
        pe_sell = [float(l["strike"]) for l in proposal.legs if str(l.get("option_type") or "").upper() == "PE" and str(l.get("action") or l.get("side") or "").upper() == "SELL" and "strike" in l]
        pe_buy = [float(l["strike"]) for l in proposal.legs if str(l.get("option_type") or "").upper() == "PE" and str(l.get("action") or l.get("side") or "").upper() == "BUY" and "strike" in l]

        if ce_sell and ce_buy:
            call_width = max(ce_buy) - min(ce_sell)
            if call_width < effective_min_width:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"Call wing width {call_width:.0f} pts is too narrow for {symbol} (min required: {effective_min_width:.0f} pts). Buy leg is too close."
                )

        if pe_sell and pe_buy:
            put_width = max(pe_sell) - min(pe_buy)
            if put_width < effective_min_width:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"Put wing width {put_width:.0f} pts is too narrow for {symbol} (min required: {effective_min_width:.0f} pts). Buy leg is too close."
                )

    return ValidationResult(is_valid=True)
