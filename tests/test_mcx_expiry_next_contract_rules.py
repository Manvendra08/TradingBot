import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
import pytz

from src.engine.pipeline import _prefetch_symbol_data, _process_prefetched_symbol

IST = pytz.timezone("Asia/Kolkata")


class TestMCXExpiryNextContractRules:
    """Tests for MCX monthly expiry pre-fetch (<5 days) and expiry day (DTE=0) next-contract OI verdict swap."""

    @patch("src.engine.pipeline.fetch_option_chain")
    def test_mcx_prefetch_triggered_when_dte_less_than_5(self, mock_fetch):
        from datetime import timedelta
        today_dt = datetime.now(IST).date()
        today_str = today_dt.strftime("%Y-%m-%d")
        exp_4d_str = (today_dt + timedelta(days=4)).strftime("%Y-%m-%d")  # 4 days away
        next_exp_str = (today_dt + timedelta(days=32)).strftime("%Y-%m-%d")

        mock_fetch.return_value = {
            "underlying_price": 280.0,
            "expiry": exp_4d_str,
            "all_expiries": [exp_4d_str, next_exp_str],
            "strikes": [{"strike": 280.0 + i, "option_type": "CE", "ltp": 10.0} for i in range(5)],
        }

        packet = _prefetch_symbol_data("NATURALGAS", today_str)

        # MCX symbol with DTE < 5 days MUST trigger next_expiry_future background pre-fetch
        assert "next_expiry_future" in packet
        assert packet["next_expiry_future"] is not None

    @patch("src.engine.pipeline.detect_anomalies")
    @patch("src.engine.pipeline.generate_intelligence_structured")
    @patch("src.engine.pipeline.send_text")
    def test_mcx_expiry_day_swaps_core_engine_verdict_to_next_expiry(
        self, mock_send, mock_gen_intel, mock_detect
    ):
        from datetime import timedelta
        today_dt = datetime.now(IST).date()
        today_str = today_dt.strftime("%Y-%m-%d")
        next_exp_str = (today_dt + timedelta(days=28)).strftime("%Y-%m-%d")

        mock_detect.return_value = ([], {"expiry": next_exp_str, "underlying": 280.0})
        mock_gen_intel.return_value = {"telegram_text": "OK", "verdict_label": "Short Buildup", "confidence": 80}

        curr_oc = {
            "underlying_price": 280.0,
            "expiry": today_str,
            "all_expiries": [today_str, next_exp_str],
            "strikes": [{"strike": 280.0 + i, "option_type": "CE", "ltp": 1.0} for i in range(5)],
        }

        next_oc = {
            "underlying_price": 280.0,
            "expiry": next_exp_str,
            "all_expiries": [today_str, next_exp_str],
            "strikes": [{"strike": 280.0 + i, "option_type": "CE", "ltp": 15.0} for i in range(5)],
        }

        mock_future = MagicMock()
        mock_res = MagicMock()
        mock_res.ok = True
        mock_res.data = next_oc
        mock_future.result.return_value = mock_res

        packet = {
            "symbol": "NATURALGAS",
            "fetched_at": today_str,
            "option_chain_result": MagicMock(ok=True),
            "oc_data": curr_oc,
            "next_expiry_future": mock_future,
            "chart_result": {"ok": True, "data": {}},
            "news_result": {"ok": True, "data": None, "bypassed": True},
        }

        _process_prefetched_symbol(packet, is_test=True)

        # Verified: On MCX expiry day (DTE=0), packet oc_data was swapped to next_oc (expiry 2026-08-25)
        assert packet["oc_data"]["expiry"] == next_exp_str
        assert packet["oc_data"]["strikes"][0]["ltp"] == 15.0
