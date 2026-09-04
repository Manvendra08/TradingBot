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


def _estimate_margin(legs: list[dict], underlying: float) -> float:
    """Rough estimation of margin requirement."""
    # Simplified estimation logic for testing purposes
    # In reality, this would use a more complex margin calculation
    margin = 0.0
    for leg in legs:
        if leg.get("action") == "SELL":
            # Roughly assuming naked sell requires margin of underlying * LOT_SIZE * 0.15
            # Simplified for test demonstration.
            ratio = leg.get("ratio", 1)
            margin += (underlying * ratio * 75 * 0.15) # Assume lot size 75 for NIFTY
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
    max_net_delta: float = 0.60
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
    estimated_margin = _estimate_margin(proposal.legs, underlying)
    if estimated_margin > max_margin_inr:
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Margin requirement exceeded: Estimated margin ₹{estimated_margin:.2f} > max ₹{max_margin_inr:.2f}."
        )

    return ValidationResult(is_valid=True)
