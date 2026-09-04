"""Strict LLM Execution Parser.

Validates multi-leg options execution proposals from LLM outputs to guarantee
leg count, strike bounds, option types, and leg action validity prior to trade instantiation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedExecution:
    is_valid: bool
    rejection_reason: str = ""
    strategy: str = ""
    action: str = ""
    legs: list[dict[str, Any]] = field(default_factory=list)
    net_credit: float = 0.0
    max_risk: float = 0.0


def parse_llm_execution(raw_response: dict[str, Any] | None, underlying: float = 0.0) -> ParsedExecution:
    """Parse and strictly validate raw LLM response dict for execution proposal."""
    if not isinstance(raw_response, dict):
        return ParsedExecution(is_valid=False, rejection_reason="Invalid response format: payload is not a dict")

    strategy = str(raw_response.get("strategy") or raw_response.get("proposed_strategy") or "").upper()
    action = str(raw_response.get("action") or "").upper()
    raw_legs = raw_response.get("legs")

    if not isinstance(raw_legs, list) or len(raw_legs) < 1 or len(raw_legs) > 6:
        return ParsedExecution(
            is_valid=False,
            rejection_reason=f"Invalid leg count: expected 1-6 legs, got {len(raw_legs) if isinstance(raw_legs, list) else 0}",
            strategy=strategy,
            action=action,
        )

    validated_legs: list[dict[str, Any]] = []

    for i, leg in enumerate(raw_legs, 1):
        if not isinstance(leg, dict):
            return ParsedExecution(
                is_valid=False,
                rejection_reason=f"Invalid leg #{i}: leg is not an object",
                strategy=strategy,
                action=action,
            )

        leg_action = str(leg.get("action") or "").upper()
        if leg_action not in {"BUY", "SELL"}:
            return ParsedExecution(
                is_valid=False,
                rejection_reason=f"Invalid leg action for leg #{i}: {leg.get('action')}",
                strategy=strategy,
                action=action,
            )

        option_type = str(leg.get("option_type") or "").upper()
        if option_type not in {"CE", "PE"}:
            return ParsedExecution(
                is_valid=False,
                rejection_reason=f"Invalid option type for leg #{i}: {leg.get('option_type')}",
                strategy=strategy,
                action=action,
            )

        try:
            strike = float(leg.get("strike", 0))
        except (ValueError, TypeError):
            strike = 0.0

        if strike <= 0:
            return ParsedExecution(
                is_valid=False,
                rejection_reason=f"Invalid strike for leg #{i}: {leg.get('strike')}",
                strategy=strategy,
                action=action,
            )

        try:
            ratio = int(leg.get("ratio", 1))
        except (ValueError, TypeError):
            ratio = 1

        if ratio <= 0:
            return ParsedExecution(
                is_valid=False,
                rejection_reason=f"Invalid ratio for leg #{i}: {leg.get('ratio')}",
                strategy=strategy,
                action=action,
            )

        try:
            entry_premium = float(leg.get("entry_premium", 0.0))
        except (ValueError, TypeError):
            entry_premium = 0.0

        validated_legs.append({
            "action": leg_action,
            "strike": strike,
            "option_type": option_type,
            "ratio": ratio,
            "entry_premium": max(0.0, entry_premium),
        })

    net_credit = float(raw_response.get("net_credit", 0.0) or 0.0)
    max_risk = float(raw_response.get("max_risk", 0.0) or 0.0)

    return ParsedExecution(
        is_valid=True,
        rejection_reason="",
        strategy=strategy,
        action=action,
        legs=validated_legs,
        net_credit=net_credit,
        max_risk=max_risk,
    )
