"""
Unit tests for the Natural Gas Strategy components (Phase 2 to 7).
"""

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
import pytz

from src.engine.ng_risk_manager import check_ng_position_limit, check_ng_daily_loss_cap, calculate_ng_lot_size
from src.engine.ng_parity_strategy import check_deviation_stable_or_shrinking, run_ng_parity_strategy
from src.engine.ng_eia_strategy import parse_bcf_value, run_ng_eia_strategy

class TestNGRiskManager(unittest.TestCase):
    
    @patch('src.engine.ng_risk_manager.get_conn')
    def test_position_limit(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        
        # Test limit not hit
        mock_conn.execute.return_value.fetchone.return_value = [0]
        self.assertTrue(check_ng_position_limit())
        
        # Test limit hit
        mock_conn.execute.return_value.fetchone.return_value = [1]
        self.assertFalse(check_ng_position_limit())

    @patch('src.engine.ng_risk_manager.get_conn')
    def test_daily_loss_cap(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        
        # 0 closed trades today -> False
        mock_conn.execute.return_value.fetchall.return_value = []
        self.assertFalse(check_ng_daily_loss_cap())
        
        # 1 closed trade (SL) -> False
        mock_conn.execute.return_value.fetchall.return_value = [{"status": "CLOSED_SL"}]
        self.assertFalse(check_ng_daily_loss_cap())
        
        # 2 closed trades (SL, Target) -> False
        mock_conn.execute.return_value.fetchall.return_value = [{"status": "CLOSED_SL"}, {"status": "CLOSED_TARGET"}]
        self.assertFalse(check_ng_daily_loss_cap())
        
        # 2 closed trades (SL, SL) -> True (cap hit)
        mock_conn.execute.return_value.fetchall.return_value = [{"status": "CLOSED_SL"}, {"status": "CLOSED_SL"}]
        self.assertTrue(check_ng_daily_loss_cap())

    def test_lot_sizing(self):
        capital = 100000.0
        # stop distance 4.0 points, lot size 1250. Risk 1% (1000)
        # Sizing = floor(1000 / (4.0 * 1250)) = floor(0.2) = 0, clamping to 1 lot
        self.assertEqual(calculate_ng_lot_size(capital, 4.0), 1)
        
        # stop distance 0.5 points. Sizing = floor(1000 / 625) = 1 lot
        self.assertEqual(calculate_ng_lot_size(capital, 0.5), 1)

class TestNGParityStrategy(unittest.TestCase):
    
    @patch('src.engine.ng_parity_strategy.get_conn')
    def test_deviation_stable_or_shrinking(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        
        # No history -> True
        mock_conn.execute.return_value.fetchone.return_value = None
        self.assertTrue(check_deviation_stable_or_shrinking(0.5))
        
        # History dev 0.6%, current 0.5% (shrinking) -> True
        mock_conn.execute.return_value.fetchone.return_value = {"dev_pct": 0.6}
        self.assertTrue(check_deviation_stable_or_shrinking(0.5))
        
        # History dev 0.4%, current 0.5% (expanding) -> False
        mock_conn.execute.return_value.fetchone.return_value = {"dev_pct": 0.4}
        self.assertFalse(check_deviation_stable_or_shrinking(0.5))

    def test_insert_ng_parity_log(self):
        from src.models.schema import insert_ng_parity_log, get_conn
        
        log_data = {
            "timestamp": "2026-07-06T17:00:00+05:30",
            "nymex_last": 2.50,
            "usdinr": 83.50,
            "fair_value": 208.75,
            "mcx_last": 210.0,
            "dev_pct": 0.60,
            "nymex_age_sec": 5,
            "fx_age_sec": 10,
            "mcx_age_sec": 2,
            "mcx_src": "shoonya",
            "fx_src": "shoonya",
            "nymex_src": "yfinance",
            "valid": True,
            "ng_regime": "PARITY"
        }
        
        insert_ng_parity_log(log_data)
        
        with get_conn() as conn:
            row = conn.execute(
                "SELECT ts, nymex_last, usdinr, fair_value, mcx_last, dev_pct, regime, valid FROM ng_parity_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            
            self.assertIsNotNone(row)
            self.assertEqual(row["ts"], "2026-07-06T17:00:00+05:30")
            self.assertEqual(row["nymex_last"], 2.50)
            self.assertEqual(row["usdinr"], 83.50)
            self.assertEqual(row["fair_value"], 208.75)
            self.assertEqual(row["mcx_last"], 210.0)
            self.assertEqual(row["dev_pct"], 0.60)
            self.assertEqual(row["regime"], "PARITY")
            self.assertEqual(row["valid"], 1)

class TestNGEIAStrategy(unittest.TestCase):
    
    def test_parse_bcf_value(self):
        self.assertEqual(parse_bcf_value("87B"), 87.0)
        self.assertEqual(parse_bcf_value("-12B"), -12.0)
        self.assertEqual(parse_bcf_value("  5.5 B "), 5.5)
        self.assertIsNone(parse_bcf_value(None))
        self.assertIsNone(parse_bcf_value(""))
