"""
Phase 5 Regression Suite: Signal Quality & Tuning Fixes
Covers findings:
- A4: Decision Pipeline Composite Gates (trend is HARD gate, N/A returns 50, fixed composite threshold).
- A5: Regime Scoring for Option Selling (RANGE=90, SELL PE in uptrend=100, SELL CE in downtrend=100).
- A10: Direction-Aware Confidence Bonuses (verdict_bias alignment on PCR confluence & level proximity,
       neutral alert bonus = 0, alert contract deduplication, zero chart bonus).
- A17: Enforce Strictly OTM for Single-Leg SELL inside select_candidate.
- A19: Reject LLM instrument overrides that invert option_type on SELL plans.
"""

import unittest
from unittest.mock import MagicMock

from src.engine.regime_detector import (
    REGIME_TRENDING_UP,
    REGIME_TRENDING_DOWN,
    REGIME_RANGE,
    REGIME_VOLATILE,
    REGIME_NO_TRADE,
    regime_score_for_trade,
)
from src.engine.decision_pipeline import (
    step_heavyweight_alignment,
    PipelineContext,
)
from src.engine.intelligence import (
    _score_alert_severity,
    _score_pcr_confluence,
    _score_level_proximity,
    _score_chart_alignment,
)
from src.engine.trade_plan import select_candidate
from src.engine.paper_plan import build_paper_trade_plan


class TestFindingA4DecisionPipelineCompositeGates(unittest.TestCase):
    """Test A4: Non-index heavyweight step returns 50, trend is hard gate, fixed composite threshold."""

    def test_heavyweight_alignment_non_index_returns_50(self):
        ctx = PipelineContext(
            symbol="NATURALGAS",
            engine="CORE_OI",
            direction="LONG",
            underlying=270.0,
            scan_context={"intel": {"verdict_label": "Bullish"}},
            ai_verdict=None,
            steps=[],
        )
        res = step_heavyweight_alignment(ctx)
        self.assertTrue(res.passed)
        self.assertEqual(res.score, 50, "Non-index symbols must return neutral score 50, not 100")

    def test_gate_definitions_in_pipeline(self):
        # Verify gate categorization via source or logic
        import inspect
        import src.engine.decision_pipeline as dp
        src_code = inspect.getsource(dp.run_entry_pipeline)
        self.assertIn('"trend"', src_code)
        self.assertIn('HARD_GATE_NAMES = {"signal", "rule", "ai", "tfss_handoff", "risk", "trend"}', src_code)
        self.assertIn('SOFT_GATE_NAMES = {"entry_quality", "regime", "heavyweight_alignment"}', src_code)

    def test_no_confidence_reduction_on_composite_threshold(self):
        import inspect
        import src.engine.decision_pipeline as dp
        src_code = inspect.getsource(dp.run_entry_pipeline)
        self.assertNotIn("threshold_reduction", src_code)


class TestFindingA5RegimeScoringForOptionSelling(unittest.TestCase):
    """Test A5: Regime scoring correctly supports option selling semantics."""

    def test_selling_options_regime_scores(self):
        # Range is ideal for sellers: theta decay without directional challenge
        self.assertEqual(regime_score_for_trade(REGIME_RANGE, "CE", side="SELL"), 90)
        self.assertEqual(regime_score_for_trade(REGIME_RANGE, "PE", side="SELL"), 90)

        # Selling PE is Bullish:
        # In Uptrend: protected -> 100
        self.assertEqual(regime_score_for_trade(REGIME_TRENDING_UP, "PE", side="SELL"), 100)
        # In Downtrend: challenged -> 30
        self.assertEqual(regime_score_for_trade(REGIME_TRENDING_DOWN, "PE", side="SELL"), 30)

        # Selling CE is Bearish:
        # In Downtrend: protected -> 100
        self.assertEqual(regime_score_for_trade(REGIME_TRENDING_DOWN, "CE", side="SELL"), 100)
        # In Uptrend: challenged -> 30
        self.assertEqual(regime_score_for_trade(REGIME_TRENDING_UP, "CE", side="SELL"), 30)

        # Volatile is risky for short gamma -> 40
        self.assertEqual(regime_score_for_trade(REGIME_VOLATILE, "CE", side="SELL"), 40)
        self.assertEqual(regime_score_for_trade(REGIME_VOLATILE, "PE", side="SELL"), 40)

    def test_buying_options_regime_scores(self):
        # Buying CE is Bullish:
        self.assertEqual(regime_score_for_trade(REGIME_TRENDING_UP, "CE", side="BUY"), 100)
        self.assertEqual(regime_score_for_trade(REGIME_TRENDING_DOWN, "CE", side="BUY"), 30)

        # Buying PE is Bearish:
        self.assertEqual(regime_score_for_trade(REGIME_TRENDING_DOWN, "PE", side="BUY"), 100)
        self.assertEqual(regime_score_for_trade(REGIME_TRENDING_UP, "PE", side="BUY"), 30)

        # Buying in Range suffers theta decay -> 30
        self.assertEqual(regime_score_for_trade(REGIME_RANGE, "CE", side="BUY"), 30)
        # Buying in Volatile gains from vega/expansion -> 60
        self.assertEqual(regime_score_for_trade(REGIME_VOLATILE, "CE", side="BUY"), 60)


class TestFindingA10DirectionAwareConfidenceBonuses(unittest.TestCase):
    """Test A10: Confidence bonuses are strictly direction-aware, deduplicated, and chart bonus is 0."""

    def test_alert_severity_deduplication_and_neutral_zeros(self):
        # Neutral alert with verdict_bias="NEUTRAL" earns 0 bonus
        neutral_alert = {
            "severity": "HIGH",
            "alert_type": "STRADDLE_PREMIUM",
            "detail_json": '{"trend": "rising"}',
        }
        score = _score_alert_severity([neutral_alert], verdict_bias="NEUTRAL")
        self.assertEqual(score, 0, "Neutral alert must earn 0 bonus")

        # Duplicate alerts on the same (strike, option_type)
        dup_alerts = [
            {"severity": "HIGH", "alert_type": "OI_SPIKE", "strike": 24000, "option_type": "PE", "detail_json": ""},
            {"severity": "HIGH", "alert_type": "VOLUME_AGGRESSION", "strike": 24000, "option_type": "PE", "detail_json": ""},
        ]
        # On PE, alert direction for OI_SPIKE/VOLUME_AGGRESSION is BULLISH if PE put writing / support
        score_dup = _score_alert_severity(dup_alerts, verdict_bias="BULLISH")
        # Should be scored only once (weight 20), not twice (40)
        self.assertEqual(score_dup, 20, "Duplicate contract alerts must be scored only once")

    def test_pcr_confluence_directional_alignment(self):
        # Bullish confluence: pcr < 0.75 and price > 0
        self.assertEqual(_score_pcr_confluence(0.65, 0.5, verdict_bias="BULLISH"), 15)
        self.assertEqual(_score_pcr_confluence(0.65, 0.5, verdict_bias="BEARISH"), 0, "Must not reward wrong direction")
        self.assertEqual(_score_pcr_confluence(0.65, 0.5, verdict_bias="NEUTRAL"), 0)

        # Bearish confluence: pcr > 1.25 and price < 0
        self.assertEqual(_score_pcr_confluence(1.35, -0.5, verdict_bias="BEARISH"), 15)
        self.assertEqual(_score_pcr_confluence(1.35, -0.5, verdict_bias="BULLISH"), 0, "Must not reward wrong direction")

    def test_level_proximity_non_broken_bounds(self):
        scan_ctx = {
            "underlying": 24050,
            "support": 24000,
            "resistance": 24500,
        }
        # Underlying 24050 is within 15% range of support (50 / 500 = 10%)
        # Positive price move and BULLISH bias -> +15
        self.assertEqual(_score_level_proximity(scan_ctx, 0.2, verdict_bias="BULLISH"), 15)
        # If BEARISH bias, 0
        self.assertEqual(_score_level_proximity(scan_ctx, 0.2, verdict_bias="BEARISH"), 0)

        # Broken support: underlying < support (e.g. 23950 < 24000)
        broken_scan_ctx = {
            "underlying": 23950,
            "support": 24000,
            "resistance": 24500,
        }
        self.assertEqual(_score_level_proximity(broken_scan_ctx, 0.2, verdict_bias="BULLISH"), 0,
                         "Broken support must not earn a bounce bonus")

    def test_chart_alignment_zero_bonus(self):
        parsed_chart = {
            "1h": {"sentiment": "BULLISH"},
            "3h": {"sentiment": "BULLISH"},
        }
        score, conflict = _score_chart_alignment(parsed_chart, verdict_bias="BULLISH")
        self.assertEqual(score, 0, "Chart alignment must earn 0 bonus per Timeframe Role Separation")
        self.assertFalse(conflict)

        # Conflict detection still functions
        conflicting_chart = {
            "1h": {"sentiment": "BULLISH"},
            "3h": {"sentiment": "BEARISH"},
        }
        score, conflict = _score_chart_alignment(conflicting_chart, verdict_bias="BULLISH")
        self.assertTrue(conflict)
        self.assertEqual(score, 0)


class TestFindingA17StrictlyOTMForSingleLegSell(unittest.TestCase):
    """Test A17: Single-leg SELL candidates must be strictly OTM."""

    def test_select_candidate_rejects_itm_and_atm(self):
        underlying = 24000.0
        chain = [
            # ITM put (strike 24100 > underlying 24000)
            {"option_type": "PE", "strike": 24100, "delta": "0.15", "close": 120.0},
            # ATM put (strike 24000 == underlying 24000)
            {"option_type": "PE", "strike": 24000, "delta": "0.15", "close": 80.0},
            # Strictly OTM put (strike 23800 < underlying 24000)
            {"option_type": "PE", "strike": 23800, "delta": "0.15", "close": 25.0},
        ]
        atr_state = {"underlying": underlying, "is_tightened": False}

        cand = select_candidate("SELL_PE", "BULLISH", dte=5, atr_state=atr_state, option_chain=chain)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["strike"], 23800, "Only strictly OTM strike (23800 < 24000) must be selected")

    def test_select_candidate_ce_rejects_itm_and_atm(self):
        underlying = 24000.0
        chain = [
            # ITM call (strike 23900 < underlying 24000)
            {"option_type": "CE", "strike": 23900, "delta": "0.15", "close": 150.0},
            # ATM call (strike 24000 == underlying 24000)
            {"option_type": "CE", "strike": 24000, "delta": "0.15", "close": 90.0},
            # Strictly OTM call (strike 24200 > underlying 24000)
            {"option_type": "CE", "strike": 24200, "delta": "0.15", "close": 30.0},
        ]
        atr_state = {"underlying": underlying, "is_tightened": False}

        cand = select_candidate("SELL_CE", "BEARISH", dte=5, atr_state=atr_state, option_chain=chain)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["strike"], 24200, "Only strictly OTM strike (24200 > 24000) must be selected")


class TestFindingA19RejectLLMInversionForSellPlans(unittest.TestCase):
    """Test A19: LLM instrument override cannot invert option_type on SELL plans."""

    def test_sell_plan_preserves_canonical_option_type(self):
        # A bullish scan should sell PE
        ctx = {
            "symbol": "NIFTY",
            "verdict": "Long Buildup",
            "underlying": 24000.0,
            "instrument": "NIFTY 24200 CE",  # Hallucinated CE by LLM
            "option_rows": [
                {"option_type": "PE", "strike": 23800, "delta": 0.15, "ltp": 25.0},
                {"option_type": "CE", "strike": 24200, "delta": 0.15, "ltp": 25.0},
            ],
            "dte": 5,
        }
        plan = build_paper_trade_plan("Long Buildup", 80, ctx)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["side"], "SELL")
        self.assertEqual(plan["option_type"], "PE", "Bullish SELL plan must remain PE despite LLM instrument having CE")


if __name__ == "__main__":
    unittest.main()
