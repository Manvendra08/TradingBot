"""
Tests for TFSS reversal sequencing and exit trigger priority (plan §4.15).
"""
import unittest
from unittest.mock import patch, MagicMock


class TestReversalSequencing(unittest.TestCase):
    """Plan §4.15: tested-side reduction precedes combined-cap recheck precedes reversal open."""

    def _mock_symbol_state(self, symbol="NIFTY", open_side="SELL_CE"):
        state = MagicMock()
        state.symbol = symbol
        state.open_side = open_side
        state.ctx = {}
        return state

    def _mock_market_state(self, current_delta=0.10, total_delta=0.20, dte=5):
        return {
            "current_delta": current_delta,
            "total_delta": total_delta,
            "dte": dte,
            "atr_state": {"is_tightened": False},
            "option_chain": [
                {"option_type": "PE", "delta": "0.12", "close": 5.0, "strike": 24000},
            ],
        }

    @patch("src.engine.trend_following_short_strangle.get_conn")
    @patch("src.engine.trend_following_short_strangle.select_candidate")
    @patch("src.engine.trend_following_short_strangle.check_trend_persistence", return_value=(True, ""))
    def test_reversal_blocked_by_persistence(self, mock_trend, mock_select, mock_get_conn):
        from src.engine.trend_following_short_strangle import evaluate_reversal
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        # Only 2 scans → persistence invalid
        mock_conn.execute.return_value.fetchall.return_value = [
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T10:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T09:00:00"},
        ]
        state = self._mock_symbol_state()
        market = self._mock_market_state()
        result = evaluate_reversal(state, market, {})
        self.assertEqual(result["action"], "BLOCK")
        self.assertEqual(result["reason"], "PERSISTENCE_NOT_CONFIRMED")

    @patch("src.engine.trend_following_short_strangle.get_conn")
    @patch("src.engine.trend_following_short_strangle.check_trend_persistence", return_value=(True, ""))
    def test_no_reversal_when_trend_agrees(self, mock_trend, mock_get_conn):
        from src.engine.trend_following_short_strangle import evaluate_reversal
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        # Holding SELL_CE (bearish), trend is BEARISH → no reversal
        mock_conn.execute.return_value.fetchall.return_value = [
            {"verdict_label": "Short Buildup", "fetched_at": "2026-07-12T10:00:00"},
            {"verdict_label": "Short Buildup", "fetched_at": "2026-07-12T09:00:00"},
            {"verdict_label": "Short Buildup", "fetched_at": "2026-07-12T08:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T07:00:00"},
            {"verdict_label": "Sideways", "fetched_at": "2026-07-12T06:00:00"},
        ]
        state = self._mock_symbol_state(open_side="SELL_CE")
        market = self._mock_market_state()
        result = evaluate_reversal(state, market, {})
        self.assertEqual(result["action"], "NO_REVERSAL_ACTION")

    def test_no_open_side_no_action(self):
        from src.engine.trend_following_short_strangle import evaluate_reversal, PersistenceResult
        state = MagicMock()
        state.symbol = "NIFTY"
        state.open_side = None
        state.ctx = {}
        market = self._mock_market_state()
        with patch("src.engine.trend_following_short_strangle.compute_persisted_trend",
                    return_value=PersistenceResult(is_valid=True, label="BULLISH", agreeing_count=4)):
            result = evaluate_reversal(state, market, {})
        self.assertEqual(result["action"], "NO_REVERSAL_ACTION")


class TestExitTriggerPriority(unittest.TestCase):
    """Plan §4.15: exit priority ordering with multiple simultaneous triggers."""

    def test_risk_cap_wins_over_delta_stop(self):
        from src.engine.risk_engine import exit_trigger_priority_list
        result = exit_trigger_priority_list(["DELTA_STOP", "RISK_CAP_EXCEEDED"])
        self.assertEqual(result, "RISK_CAP_EXCEEDED")

    def test_delta_stop_wins_over_profit_target(self):
        from src.engine.risk_engine import exit_trigger_priority_list
        result = exit_trigger_priority_list(["PROFIT_TARGET", "DELTA_STOP"])
        self.assertEqual(result, "DELTA_STOP")

    def test_reversal_wins_over_time_decay(self):
        from src.engine.risk_engine import exit_trigger_priority_list
        result = exit_trigger_priority_list(["TIME_DECAY_EXIT", "TREND_REVERSAL"])
        self.assertEqual(result, "TREND_REVERSAL")

    def test_profit_target_wins_over_time_decay(self):
        from src.engine.risk_engine import exit_trigger_priority_list
        result = exit_trigger_priority_list(["TIME_DECAY_EXIT", "PROFIT_TARGET"])
        self.assertEqual(result, "PROFIT_TARGET")

    def test_single_trigger_returns_itself(self):
        from src.engine.risk_engine import exit_trigger_priority_list
        result = exit_trigger_priority_list(["DELTA_STOP"])
        self.assertEqual(result, "DELTA_STOP")

    def test_empty_list_returns_none(self):
        from src.engine.risk_engine import exit_trigger_priority_list
        result = exit_trigger_priority_list([])
        self.assertIsNone(result)

    def test_unknown_trigger_gets_low_priority(self):
        from src.engine.risk_engine import exit_trigger_priority_list
        result = exit_trigger_priority_list(["UNKNOWN_TRIGGER", "RISK_CAP_EXCEEDED"])
        self.assertEqual(result, "RISK_CAP_EXCEEDED")


class TestRiskHelpers(unittest.TestCase):
    """Tests for check_tested_side and compute_combined_book."""

    def test_check_tested_side_within_threshold(self):
        from src.engine.risk_engine import check_tested_side
        result = check_tested_side("SELL_PE", {"current_delta": 0.10}, {})
        self.assertFalse(result.beyond_threshold)
        self.assertAlmostEqual(result.current_delta, 0.10)

    def test_check_tested_side_beyond_threshold(self):
        from src.engine.risk_engine import check_tested_side
        result = check_tested_side("SELL_PE", {"current_delta": 0.40}, {})
        self.assertTrue(result.beyond_threshold)
        self.assertIn("DELTA_STOP", result.reason)

    def test_check_tested_side_custom_threshold(self):
        from src.engine.risk_engine import check_tested_side
        result = check_tested_side("SELL_PE", {"current_delta": 0.30}, {"hard_stop_delta": 0.25})
        self.assertTrue(result.beyond_threshold)

    def test_compute_combined_book_within_caps(self):
        from src.engine.risk_engine import compute_combined_book
        result = compute_combined_book({"open_count": 2}, {"total_delta": 0.30})
        self.assertTrue(result.within_caps)

    def test_compute_combined_book_exceeds_open_cap(self):
        from src.engine.risk_engine import compute_combined_book
        result = compute_combined_book({"open_count": 3}, {"total_delta": 0.10})
        self.assertFalse(result.within_caps)
        self.assertIn("TFSS_OPEN_CAP", result.reason)

    def test_compute_combined_book_exceeds_delta_cap(self):
        from src.engine.risk_engine import compute_combined_book
        result = compute_combined_book({"open_count": 1}, {"total_delta": 0.65})
        self.assertFalse(result.within_caps)
        self.assertIn("TFSS_DELTA_CAP", result.reason)


if __name__ == "__main__":
    unittest.main()
