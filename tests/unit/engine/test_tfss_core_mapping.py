"""
Tests for TFSS core-to-execution mapping (plan §4.14).
Verifies that all qualifying Core verdicts route to TFSS and produce
only SELL_PE (bullish) or SELL_CE (bearish) — never BUY.
"""
import unittest
from unittest.mock import patch, MagicMock


class TestCoreVerdictNormalization(unittest.TestCase):
    """Plan §4.14: Core verdicts must normalize to TFSS_BULLISH or TFSS_BEARISH."""

    def test_go_long_normalizes_to_bullish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("GO_LONG")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BULLISH")
        self.assertEqual(result.execution_family, "TFSS_BULLISH")

    def test_go_short_normalizes_to_bearish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("GO_SHORT")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BEARISH")
        self.assertEqual(result.execution_family, "TFSS_BEARISH")

    def test_long_buildup_to_bullish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("Long Buildup")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BULLISH")

    def test_short_covering_to_bullish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("Short Covering")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BULLISH")

    def test_put_writing_to_bullish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("Put Writing")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BULLISH")

    def test_oi_bias_bullish_to_bullish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("OI Bias Bullish")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BULLISH")

    def test_short_buildup_to_bearish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("Short Buildup")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BEARISH")

    def test_long_unwinding_to_bearish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("Long Unwinding")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BEARISH")

    def test_call_writing_to_bearish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("Call Writing")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BEARISH")

    def test_oi_bias_bearish_to_bearish(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("OI Bias Bearish")
        self.assertIsNotNone(result)
        self.assertEqual(result.bias, "BEARISH")

    def test_sideways_returns_none(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("Sideways")
        self.assertIsNone(result)

    def test_low_conviction_returns_none(self):
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        result = normalize_core_verdict_to_tfss_intent("Low Conviction")
        self.assertIsNone(result)


class TestExecutionSideResolution(unittest.TestCase):
    """Plan §4.14: TFSS bullish → SELL_PE only, bearish → SELL_CE only."""

    def _make_persisted(self, valid=True, label="BULLISH"):
        from src.engine.trend_following_short_strangle import PersistenceResult
        return PersistenceResult(is_valid=valid, label=label)

    def test_bullish_resolves_to_sell_pe(self):
        from src.engine.trend_following_short_strangle import (
            normalize_core_verdict_to_tfss_intent, resolve_tfss_execution_side
        )
        intent = normalize_core_verdict_to_tfss_intent("GO_LONG")
        persisted = self._make_persisted(valid=True, label="BULLISH")
        side = resolve_tfss_execution_side(intent, persisted)
        self.assertEqual(side, "SELL_PE")

    def test_bearish_resolves_to_sell_ce(self):
        from src.engine.trend_following_short_strangle import (
            normalize_core_verdict_to_tfss_intent, resolve_tfss_execution_side
        )
        intent = normalize_core_verdict_to_tfss_intent("GO_SHORT")
        persisted = self._make_persisted(valid=True, label="BEARISH")
        side = resolve_tfss_execution_side(intent, persisted)
        self.assertEqual(side, "SELL_CE")

    def test_persistence_blocked_returns_block(self):
        from src.engine.trend_following_short_strangle import (
            normalize_core_verdict_to_tfss_intent, resolve_tfss_execution_side
        )
        intent = normalize_core_verdict_to_tfss_intent("GO_LONG")
        persisted = self._make_persisted(valid=False)
        side = resolve_tfss_execution_side(intent, persisted)
        self.assertIsInstance(side, dict)
        self.assertEqual(side["action"], "BLOCK")

    def test_all_bullish_verdicts_cannot_produce_buy(self):
        """Verify no bullish verdict produces BUY side."""
        from src.engine.trend_following_short_strangle import (
            normalize_core_verdict_to_tfss_intent, resolve_tfss_execution_side
        )
        bullish_verdicts = ["Long Buildup", "Short Covering", "GO_LONG", "Put Writing", "OI Bias Bullish"]
        for v in bullish_verdicts:
            intent = normalize_core_verdict_to_tfss_intent(v)
            if intent is None:
                continue
            persisted = self._make_persisted(valid=True, label="BULLISH")
            side = resolve_tfss_execution_side(intent, persisted)
            self.assertEqual(side, "SELL_PE", f"Verdict {v} produced {side} instead of SELL_PE")
            self.assertNotIn("BUY", str(side), f"Verdict {v} produced BUY which is forbidden")

    def test_all_bearish_verdicts_cannot_produce_buy(self):
        """Verify no bearish verdict produces BUY side."""
        from src.engine.trend_following_short_strangle import (
            normalize_core_verdict_to_tfss_intent, resolve_tfss_execution_side
        )
        bearish_verdicts = ["Short Buildup", "Long Unwinding", "GO_SHORT", "Call Writing", "OI Bias Bearish"]
        for v in bearish_verdicts:
            intent = normalize_core_verdict_to_tfss_intent(v)
            if intent is None:
                continue
            persisted = self._make_persisted(valid=True, label="BEARISH")
            side = resolve_tfss_execution_side(intent, persisted)
            self.assertEqual(side, "SELL_CE", f"Verdict {v} produced {side} instead of SELL_CE")
            self.assertNotIn("BUY", str(side), f"Verdict {v} produced BUY which is forbidden")


class TestTimeframeUnchanged(unittest.TestCase):
    """Plan §4.14: Timeframe path remains unchanged."""

    def test_tfss_handoff_not_in_timeframe_steps(self):
        from src.engine.decision_pipeline import TIMEFRAME_STEPS, step_tfss_handoff_core
        self.assertNotIn(step_tfss_handoff_core, TIMEFRAME_STEPS)

    def test_tfss_handoff_in_core_steps(self):
        from src.engine.decision_pipeline import CORE_OI_STEPS, step_tfss_handoff_core
        self.assertIn(step_tfss_handoff_core, CORE_OI_STEPS)


class TestPaperPlanNoBuy(unittest.TestCase):
    """Plan §4.14: paper_plan must never produce BUY for TFSS-eligible verdicts."""

    def test_go_long_produces_sell_pe(self):
        from src.engine.paper_plan import build_paper_trade_plan
        ctx = {
            "symbol": "NIFTY",
            "underlying": 24500,
            "atm_strike": 24500,
            "support": 24300,
            "resistance": 24700,
            "option_rows": [],
            "_tfss_execution_side": "SELL_PE",
        }
        with patch("src.engine.trade_plan.get_atr", return_value=100.0):
            plan = build_paper_trade_plan("GO_LONG", 80, ctx)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["side"], "SELL")
        self.assertEqual(plan["option_type"], "PE")

    def test_go_short_produces_sell_ce(self):
        from src.engine.paper_plan import build_paper_trade_plan
        ctx = {
            "symbol": "NIFTY",
            "underlying": 24500,
            "atm_strike": 24500,
            "support": 24300,
            "resistance": 24700,
            "option_rows": [],
            "_tfss_execution_side": "SELL_CE",
        }
        with patch("src.engine.trade_plan.get_atr", return_value=100.0):
            plan = build_paper_trade_plan("GO_SHORT", 80, ctx)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["side"], "SELL")
        self.assertEqual(plan["option_type"], "CE")

    def test_no_tfss_side_still_sell_for_go_long(self):
        """Without TFSS side, GO_LONG still uses VERDICT_ACTION_MAP (SELL PE), not BUY."""
        from src.engine.paper_plan import build_paper_trade_plan
        ctx = {
            "symbol": "NIFTY",
            "underlying": 24500,
            "atm_strike": 24500,
            "support": 24300,
            "resistance": 24700,
            "option_rows": [],
        }
        with patch("src.engine.trade_plan.get_atr", return_value=100.0):
            plan = build_paper_trade_plan("GO_LONG", 80, ctx)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["side"], "SELL", "GO_LONG without TFSS side must still be SELL, not BUY")


if __name__ == "__main__":
    unittest.main()
