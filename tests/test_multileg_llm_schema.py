"""
Tests for the multi-leg LLM schema (Phase 2).
"""
import pytest
from src.engine.multileg_llm_schema import LLMLeg, LLMMultiLegVerdict


class TestLLMLeg:
    """Tests for LLMLeg Pydantic model."""

    def test_valid_leg(self):
        leg = LLMLeg(side="SELL", option_type="CE", strike=24800, premium=85.0, delta=0.22, rationale="OTM CE near resistance")
        assert leg.side == "SELL"
        assert leg.strike == 24800
        assert leg.delta == 0.22

    def test_leg_defaults(self):
        leg = LLMLeg(side="SELL", option_type="PE", strike=24200, premium=40.0, delta=-0.18, rationale="OTM PE near support")
        assert leg.side == "SELL"


class TestLLMMultiLegVerdict:
    """Tests for LLMMultiLegVerdict Pydantic model."""

    def test_valid_verdict(self):
        legs = [
            LLMLeg(side="SELL", option_type="CE", strike=24800, premium=85.0, delta=0.22, rationale="OTM CE"),
            LLMLeg(side="SELL", option_type="PE", strike=24200, premium=40.0, delta=-0.18, rationale="OTM PE"),
        ]
        verdict = LLMMultiLegVerdict(
            strategy_type="SHORT_STRANGLE",
            legs=legs,
            net_premium=125.0,
            net_delta=0.04,
            net_theta=-12.5,
            net_vega=-45.0,
            max_profit=125.0,
            max_loss=5000.0,
            breakeven_upper=24675.0,
            breakeven_lower=24325.0,
            entry_rationale="Rangebound market with high IV, collecting premium from both sides",
            confidence=75,
            thesis="NIFTY is in a tight range with high IV. Selling strangle to collect premium.",
            profit_target_pct=0.50,
            stop_loss_pct=2.0,
            time_decay_exit_dte=3,
            per_leg_exit_triggers="Close individual leg if delta > 0.50",
            book_level_exit_triggers="Close book at 50% profit or 200% loss",
            adjustment_plan="If tested, roll tested leg further OTM",
        )
        assert verdict.strategy_type == "SHORT_STRANGLE"
        assert len(verdict.legs) == 2
        assert verdict.net_premium == 125.0
        assert verdict.confidence == 75
        assert verdict.model_name is None

    def test_verdict_with_model_name(self):
        legs = [
            LLMLeg(side="SELL", option_type="CE", strike=24800, premium=85.0, delta=0.22, rationale="test"),
        ]
        verdict = LLMMultiLegVerdict(
            strategy_type="BEAR_CALL_SPREAD",
            legs=legs,
            net_premium=85.0,
            net_delta=-0.22,
            net_theta=-8.0,
            net_vega=-20.0,
            max_profit=85.0,
            max_loss=415.0,
            breakeven_upper=24715.0,
            breakeven_lower=0,
            entry_rationale="Bearish outlook",
            confidence=68,
            thesis="Bearish setup",
            profit_target_pct=0.50,
            stop_loss_pct=2.0,
            time_decay_exit_dte=3,
            per_leg_exit_triggers="delta > 0.50",
            book_level_exit_triggers="50% profit",
            adjustment_plan="Roll up if wrong",
            model_name="claude-opus-5",
        )
        assert verdict.model_name == "claude-opus-5"

    def test_json_serialization(self):
        legs = [
            LLMLeg(side="SELL", option_type="CE", strike=24800, premium=85.0, delta=0.22, rationale="test"),
            LLMLeg(side="SELL", option_type="PE", strike=24200, premium=40.0, delta=-0.18, rationale="test"),
        ]
        verdict = LLMMultiLegVerdict(
            strategy_type="IRON_CONDOR",
            legs=legs,
            net_premium=125.0,
            net_delta=0.04,
            net_theta=-12.5,
            net_vega=-45.0,
            max_profit=125.0,
            max_loss=875.0,
            breakeven_upper=24675.0,
            breakeven_lower=24325.0,
            entry_rationale="test",
            confidence=75,
            thesis="test",
            profit_target_pct=0.50,
            stop_loss_pct=2.0,
            time_decay_exit_dte=3,
            per_leg_exit_triggers="test",
            book_level_exit_triggers="test",
            adjustment_plan="test",
        )
        d = verdict.dict()
        assert d["strategy_type"] == "IRON_CONDOR"
        assert len(d["legs"]) == 2
        assert d["net_premium"] == 125.0
