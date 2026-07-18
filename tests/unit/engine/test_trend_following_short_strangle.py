"""
Tests for TFSS core engine: native persistence, delta-band selection,
ATR tightening, same-strike block, worsening-delta add block.
Plan §4.13.
"""
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# Patch DTE_DELTA_BANDS and load_runtime_config globally for this test session
OLD_DTE_DELTA_BANDS = [
    {
        "min_dte": 0, "max_dte": 2,
        "base_delta_min": 0.05, "base_delta_max": 0.15,
        "tight_delta_min": 0.02, "tight_delta_max": 0.08,
    },
    {
        "min_dte": 3, "max_dte": 7,
        "base_delta_min": 0.10, "base_delta_max": 0.20,
        "tight_delta_min": 0.05, "tight_delta_max": 0.15,
    },
    {
        "min_dte": 8, "max_dte": 30,
        "base_delta_min": 0.15, "base_delta_max": 0.25,
        "tight_delta_min": 0.10, "tight_delta_max": 0.20,
    }
]

import config.trend_following_short_strangle
config.trend_following_short_strangle.DTE_DELTA_BANDS = OLD_DTE_DELTA_BANDS

import config.runtime_config
config.runtime_config.load_runtime_config = lambda: {
    "enable_tfss_trade_blocked_rules": True,
    "strategies": {
        "TFSS": {
            "enabled": True,
            "params": {
                "delta_entry_band": [0.10, 0.20],
                "delta_hard_stop": 0.38,
                "atr_trailing_window": 10,
                "persistence_scans_required": 3,
                "persistence_window": 5,
                "scale_sequence": [0.5, 0.3, 0.2]
            }
        }
    }
}


class TestNativePersistence(unittest.TestCase):
    """AC-011: native compute_persisted_trend uses >=3 of last 5 scans."""

    @patch("src.engine.trend_following_short_strangle.get_conn")
    def test_3_of_5_passes(self, mock_get_conn):
        from src.engine.trend_following_short_strangle import compute_persisted_trend
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        # 3 BULLISH, 1 BEARISH, 1 NEUTRAL — most recent is BULLISH
        mock_conn.execute.return_value.fetchall.return_value = [
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T10:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T09:00:00"},
            {"verdict_label": "Short Buildup", "fetched_at": "2026-07-12T08:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T07:00:00"},
            {"verdict_label": "Sideways", "fetched_at": "2026-07-12T06:00:00"},
        ]
        with patch("src.engine.trend_following_short_strangle.check_trend_persistence", return_value=(True, "")):
            result = compute_persisted_trend("NIFTY")
        self.assertTrue(result.is_valid)
        self.assertEqual(result.label, "BULLISH")
        self.assertEqual(result.agreeing_count, 3)
        self.assertEqual(result.source, "native_5scan")

    @patch("src.engine.trend_following_short_strangle.get_conn")
    def test_2_of_5_fails(self, mock_get_conn):
        from src.engine.trend_following_short_strangle import compute_persisted_trend
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        # 2 BULLISH, 2 BEARISH, 1 NEUTRAL — below min match
        mock_conn.execute.return_value.fetchall.return_value = [
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T10:00:00"},
            {"verdict_label": "Short Buildup", "fetched_at": "2026-07-12T09:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T08:00:00"},
            {"verdict_label": "Short Buildup", "fetched_at": "2026-07-12T07:00:00"},
            {"verdict_label": "Sideways", "fetched_at": "2026-07-12T06:00:00"},
        ]
        result = compute_persisted_trend("NIFTY")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "BELOW_MIN_MATCH")

    @patch("src.engine.trend_following_short_strangle.get_conn")
    def test_2_of_5_fails_even_when_broad_trend_bullish(self, mock_get_conn):
        """Broad trend bullish but native 2/5 still fails — native is the gate."""
        from src.engine.trend_following_short_strangle import compute_persisted_trend
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = [
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T10:00:00"},
            {"verdict_label": "Short Buildup", "fetched_at": "2026-07-12T09:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T08:00:00"},
            {"verdict_label": "Short Buildup", "fetched_at": "2026-07-12T07:00:00"},
            {"verdict_label": "Sideways", "fetched_at": "2026-07-12T06:00:00"},
        ]
        with patch("src.engine.trend_following_short_strangle.check_trend_persistence", return_value=(True, "")):
            result = compute_persisted_trend("NIFTY")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "BELOW_MIN_MATCH")

    @patch("src.engine.trend_following_short_strangle.get_conn")
    def test_insufficient_history(self, mock_get_conn):
        from src.engine.trend_following_short_strangle import compute_persisted_trend
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = [
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T10:00:00"},
        ]
        result = compute_persisted_trend("NIFTY")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "INSUFFICIENT_SCAN_HISTORY")

    @patch("src.engine.trend_following_short_strangle.get_conn")
    def test_most_recent_neutral_fails(self, mock_get_conn):
        from src.engine.trend_following_short_strangle import compute_persisted_trend
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = [
            {"verdict_label": "Sideways", "fetched_at": "2026-07-12T10:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T09:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T08:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T07:00:00"},
            {"verdict_label": "Long Buildup", "fetched_at": "2026-07-12T06:00:00"},
        ]
        result = compute_persisted_trend("NIFTY")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "MOST_RECENT_IS_NEUTRAL")


class TestDeltaBandSelection(unittest.TestCase):
    """Plan §4.13: delta-band selection and ATR tightening."""

    def test_select_candidate_basic(self):
        from src.engine.trade_plan import select_candidate
        chain = [
            {"option_type": "PE", "delta": "0.12", "close": 5.0, "strike": 24000},
            {"option_type": "PE", "delta": "0.18", "close": 8.0, "strike": 24100},
            {"option_type": "CE", "delta": "0.15", "close": 6.0, "strike": 24500},
        ]
        # DTE 5 → band 0.10-0.20, is_tightened=False
        result = select_candidate("SELL_PE", "BULLISH", 5, {"is_tightened": False}, chain)
        self.assertIsNotNone(result)
        self.assertEqual(result["option_type"], "PE")
        self.assertGreaterEqual(result["delta"], 0.10)
        self.assertLessEqual(result["delta"], 0.20)

    def test_select_candidate_tightened(self):
        from src.engine.trade_plan import select_candidate
        chain = [
            {"option_type": "PE", "delta": "0.06", "close": 3.0, "strike": 23900},
            {"option_type": "PE", "delta": "0.12", "close": 7.0, "strike": 24000},
            {"option_type": "PE", "delta": "0.18", "close": 9.0, "strike": 24100},
        ]
        # DTE 5, tightened → band 0.05-0.15
        result = select_candidate("SELL_PE", "BULLISH", 5, {"is_tightened": True}, chain)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["delta"], 0.05)
        self.assertLessEqual(result["delta"], 0.15)

    def test_select_candidate_empty_chain(self):
        from src.engine.trade_plan import select_candidate
        result = select_candidate("SELL_PE", "BULLISH", 5, {}, [])
        self.assertIsNone(result)

    def test_select_candidate_no_match_in_band(self):
        from src.engine.trade_plan import select_candidate
        chain = [
            {"option_type": "PE", "delta": "0.50", "close": 20.0, "strike": 24000},
        ]
        result = select_candidate("SELL_PE", "BULLISH", 5, {"is_tightened": False}, chain)
        self.assertIsNone(result)


class TestReversalDetection(unittest.TestCase):
    """Plan §4.13: reversal detection logic."""

    def test_ce_held_bullish_trend_is_reversal(self):
        from src.engine.trend_following_short_strangle import is_confirmed_reversal
        self.assertTrue(is_confirmed_reversal("BULLISH", "SELL_CE"))

    def test_pe_held_bearish_trend_is_reversal(self):
        from src.engine.trend_following_short_strangle import is_confirmed_reversal
        self.assertTrue(is_confirmed_reversal("BEARISH", "SELL_PE"))

    def test_ce_held_bearish_trend_no_reversal(self):
        from src.engine.trend_following_short_strangle import is_confirmed_reversal
        self.assertFalse(is_confirmed_reversal("BEARISH", "SELL_CE"))

    def test_no_open_side_no_reversal(self):
        from src.engine.trend_following_short_strangle import is_confirmed_reversal
        self.assertFalse(is_confirmed_reversal("BULLISH", ""))

    def test_side_opposite(self):
        from src.engine.trend_following_short_strangle import side_opposite
        self.assertEqual(side_opposite("SELL_PE"), "SELL_CE")
        self.assertEqual(side_opposite("SELL_CE"), "SELL_PE")
        self.assertEqual(side_opposite("BUY"), "BUY")


if __name__ == "__main__":
    unittest.main()
