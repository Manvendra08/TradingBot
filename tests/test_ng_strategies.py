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
        from src.engine.ng_risk_manager import NG_DAILY_LOSS_CAP
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        
        # 0 closed trades today -> False
        mock_conn.execute.return_value.fetchone.return_value = [0]
        self.assertFalse(check_ng_daily_loss_cap())
        
        # 1 closed trade (SL) -> False
        mock_conn.execute.return_value.fetchone.return_value = [1]
        self.assertFalse(check_ng_daily_loss_cap())
        
        # NG_DAILY_LOSS_CAP-1 trades (SL repeated) -> False
        mock_conn.execute.return_value.fetchone.return_value = [NG_DAILY_LOSS_CAP - 1]
        self.assertFalse(check_ng_daily_loss_cap())
        
        # NG_DAILY_LOSS_CAP consecutive SLs -> True (cap hit)
        mock_conn.execute.return_value.fetchone.return_value = [NG_DAILY_LOSS_CAP]
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


class TestWeatherFetcher(unittest.TestCase):

    def test_hdd_calculation(self):
        from src.fetchers.weather_fetcher import _hdd, _cdd
        # avg temp 50°F → HDD = 15
        self.assertAlmostEqual(_hdd(55.0, 45.0), 15.0)
        # avg temp 70°F → HDD = 0
        self.assertAlmostEqual(_hdd(75.0, 65.0), 0.0)
        # avg temp 75°F → CDD = 10
        self.assertAlmostEqual(_cdd(80.0, 70.0), 10.0)

    def test_is_season(self):
        from src.fetchers.weather_fetcher import _is_winter, _is_summer, _is_shoulder
        self.assertTrue(_is_winter(1))
        self.assertTrue(_is_winter(12))
        self.assertFalse(_is_winter(7))
        self.assertTrue(_is_summer(7))
        self.assertFalse(_is_summer(12))
        self.assertTrue(_is_shoulder(4))
        self.assertFalse(_is_shoulder(7))

    @patch("src.fetchers.weather_fetcher.requests.get")
    def test_fetch_open_meteo_success(self, mock_get):
        from src.fetchers.weather_fetcher import _fetch_open_meteo
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "daily": {
                "temperature_2m_max": [10.0] * 15,
                "temperature_2m_min": [0.0] * 15,
            }
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        daily = _fetch_open_meteo(15)
        self.assertIsNotNone(daily)
        self.assertEqual(len(daily["temperature_2m_max"]), 15)

    @patch("src.fetchers.weather_fetcher.requests.get")
    def test_fetch_open_meteo_failure(self, mock_get):
        from src.fetchers.weather_fetcher import _fetch_open_meteo
        mock_get.side_effect = Exception("timeout")
        daily = _fetch_open_meteo(15)
        self.assertIsNone(daily)

    @patch("src.fetchers.weather_fetcher.requests.get")
    def test_check_gulf_storm_positive(self, mock_get):
        from src.fetchers.weather_fetcher import _check_gulf_storm
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "activeStorms": [{"name": "Test Storm", "lat": "25.0", "lon": "-90.0"}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        self.assertTrue(_check_gulf_storm())

    @patch("src.fetchers.weather_fetcher.requests.get")
    def test_check_gulf_storm_negative(self, mock_get):
        from src.fetchers.weather_fetcher import _check_gulf_storm
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"activeStorms": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        self.assertFalse(_check_gulf_storm())

    def test_compute_weighted_dd_empty(self):
        from src.fetchers.weather_fetcher import _compute_weighted_dd
        hdd, cdd = _compute_weighted_dd({})
        self.assertAlmostEqual(hdd, 0.0)
        self.assertAlmostEqual(cdd, 0.0)

    @patch("src.fetchers.weather_fetcher.get_latest_weather")
    def test_weather_signal_none_when_no_data(self, mock_latest):
        from src.fetchers.weather_fetcher import get_weather_signal
        mock_latest.return_value = None
        self.assertIsNone(get_weather_signal())

    @patch("src.fetchers.weather_fetcher.get_weather_signal")
    def test_weather_confidence_boost(self, mock_signal):
        from src.fetchers.weather_fetcher import weather_confidence_boost
        mock_signal.return_value = None
        self.assertEqual(weather_confidence_boost(), 0)

        mock_signal.return_value = {"zscore": 2.0, "direction": "bullish"}
        self.assertEqual(weather_confidence_boost(), 5)

    @patch("src.fetchers.weather_fetcher.get_conn")
    def test_get_trailing_zscore_insufficient_data(self, mock_get_conn):
        from src.fetchers.weather_fetcher import _get_trailing_zscore
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = []
        z = _get_trailing_zscore(10.0, "test-source")
        self.assertAlmostEqual(z, 0.0)
