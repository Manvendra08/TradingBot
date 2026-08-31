"""
Pydantic schemas for multi-leg short options strategy LLM output.

Flat structure (not deeply nested) — proven to work across all providers
in the LLM chain (OpenCode Zen, OmniRouter, Groq, GitHub, NVIDIA, Bedrock, etc.).
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class LLMLeg(BaseModel):
    """A single leg in a multi-leg strategy. Side can be BUY or SELL."""

    side: str = Field(description="BUY or SELL")
    option_type: str = Field(description="CE or PE")
    strike: float = Field(description="Strike price")
    premium: float = Field(description="Expected entry premium")
    delta: float = Field(description="Expected delta at entry")
    rationale: str = Field(description="Why this specific leg")


class LLMMultiLegVerdict(BaseModel):
    """Complete multi-leg strategy verdict from LLM.

    The LLM acts as an experienced options trader, selecting strategy type,
    legs, entry rationale, exit plan, and adjustment plan.
    """

    strategy_type: str = Field(
        description="IRON_CONDOR | SHORT_STRANGLE | SHORT_STRADDLE | BEAR_CALL_SPREAD | BULL_PUT_SPREAD | JADE_LIZARD | CUSTOM"
    )
    legs: List[LLMLeg] = Field(description="List of legs (BUY or SELL)")
    net_premium: float = Field(description="Total premium collected across all legs")
    net_delta: float = Field(description="Combined book delta")
    net_theta: float = Field(description="Combined book theta (daily decay)")
    net_vega: float = Field(description="Combined book vega")
    max_profit: float = Field(description="Maximum profit (net premium collected)")
    max_loss: float = Field(description="Maximum possible loss")
    breakeven_upper: float = Field(description="Upper breakeven point")
    breakeven_lower: float = Field(description="Lower breakeven point")
    entry_rationale: str = Field(description="Why this strategy in this market condition")
    confidence: int = Field(description="Confidence 0-100")
    thesis: str = Field(description="Rich narrative explaining the setup and reasoning")
    # Exit plan
    profit_target_pct: float = Field(
        description="Close book when profit reaches X% of max profit (e.g. 0.50 = 50%)"
    )
    stop_loss_pct: float = Field(
        description="Close book when loss reaches X% of max loss (e.g. 2.0 = 200%)"
    )
    time_decay_exit_dte: int = Field(
        description="Close remaining legs when DTE drops below this value"
    )
    per_leg_exit_triggers: str = Field(description="Per-leg exit conditions")
    book_level_exit_triggers: str = Field(description="Book-level exit conditions")
    adjustment_plan: str = Field(description="What to do if market moves against")
    model_name: Optional[str] = Field(default=None, description="Model used for this verdict")


class LLMMultiLegAdjustment(BaseModel):
    """Roll/repair instruction for one tested leg of an open book."""

    close_strike: float = Field(default=0.0, description="Strike of the leg to close")
    close_option_type: str = Field(default="", description="CE or PE of the leg to close")
    new_strike: float = Field(default=0.0, description="Strike of the replacement leg")
    new_option_type: str = Field(default="", description="CE or PE of the replacement leg")
    new_side: str = Field(default="SELL", description="BUY or SELL for the replacement leg")
    rationale: str = Field(default="", description="Why this roll improves the risk profile")


class LLMMultiLegExit(BaseModel):
    """Exit/adjustment decision for an open multi-leg book."""

    action: str = Field(description="HOLD, ADJUST, or CLOSE")
    reasoning: str = Field(description="1-3 sentence rationale citing P&L, delta, or DTE from the data")
    urgency: str = Field(default="LOW", description="LOW, MEDIUM, or HIGH")
    adjustment: Optional[LLMMultiLegAdjustment] = Field(
        default=None, description="Required only when action=ADJUST; null otherwise"
    )
    model_name: Optional[str] = Field(default=None, description="Model used for this decision")
