import pytest
import time
from datetime import datetime, date
import pytz
from unittest.mock import MagicMock, patch

from config.holidays import is_market_holiday, NSE_HOLIDAYS_2026, MCX_FULL_HOLIDAYS_2026, MCX_PARTIAL_HOLIDAYS_2026
from src.scheduler.job_runner import run_with_timeout
from src.fetchers.nse_fetcher import NSEPublicFetcher

IST = pytz.timezone("Asia/Kolkata")


def test_holiday_calendar_nse():
    # Republic Day 2026 (Monday) - NSE should be holiday
    dt_republic = IST.localize(datetime(2026, 1, 26, 10, 0))
    assert is_market_holiday("NIFTY", dt_republic) is True

    # Normal Tuesday in Jan 2026 - NOT a holiday
    dt_normal = IST.localize(datetime(2026, 1, 27, 10, 0))
    assert is_market_holiday("NIFTY", dt_normal) is False


def test_holiday_calendar_mcx():
    # Republic day - MCX Full Holiday
    dt_republic = IST.localize(datetime(2026, 1, 26, 19, 0))
    assert is_market_holiday("NATURALGAS", dt_republic) is True

    # Holi - MCX Partial Holiday (Morning closed, Evening open)
    dt_holi_morning = IST.localize(datetime(2026, 3, 3, 11, 0))
    assert is_market_holiday("NATURALGAS", dt_holi_morning) is True
    
    dt_holi_evening = IST.localize(datetime(2026, 3, 3, 18, 0))
    assert is_market_holiday("NATURALGAS", dt_holi_evening) is False

    # New Year's Day - MCX Morning Open, Evening Closed
    dt_ny_morning = IST.localize(datetime(2026, 1, 1, 11, 0))
    assert is_market_holiday("NATURALGAS", dt_ny_morning) is False

    dt_ny_evening = IST.localize(datetime(2026, 1, 1, 18, 0))
    assert is_market_holiday("NATURALGAS", dt_ny_evening) is True


def test_scheduler_watchdog():
    # Test quick success
    def quick_func():
        return "ok"
    assert run_with_timeout(quick_func, timeout=1) is True

    # Test timeout on hang
    def slow_func():
        time.sleep(2)
    assert run_with_timeout(slow_func, timeout=0.1) is False


@patch("requests.Session")
def test_nse_fetcher_session_expiry(mock_session_cls):
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    fetcher = NSEPublicFetcher()
    
    # Reset state
    NSEPublicFetcher._session_warmed = False
    NSEPublicFetcher._last_warmed_time = 0.0

    # First warm session should trigger GET requests
    fetcher._warm_session()
    assert NSEPublicFetcher._session_warmed is True
    assert NSEPublicFetcher._last_warmed_time > 0
    assert mock_session.get.call_count == 2

    # Second warm session immediately after should NOT trigger more GET requests
    mock_session.reset_mock()
    fetcher._warm_session()
    assert mock_session.get.call_count == 0

    # Backdating the last warmed time to 6 minutes ago should force a re-warm
    mock_session.reset_mock()
    NSEPublicFetcher._last_warmed_time = time.time() - 360
    fetcher._warm_session()
    assert mock_session.get.call_count == 2
