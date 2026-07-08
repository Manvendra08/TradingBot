import pytest
from datetime import datetime, date, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytz

# Test formatting.py
from src.utils.formatting import safe_num, fmt_oi, fmt_pct, fmt_int

def test_safe_num():
    assert safe_num(10) == 10.0
    assert safe_num("12.5") == 12.5
    assert safe_num("1,200.5") == 1200.5
    assert safe_num(None) == 0.0
    assert safe_num(None, 5.0) == 5.0
    assert safe_num("—") == 0.0
    assert safe_num("N/A") == 0.0
    assert safe_num("invalid") == 0.0
    assert safe_num(float('nan')) == 0.0

def test_fmt_oi():
    assert fmt_oi(None) == "0"
    assert fmt_oi(123) == "123"
    assert fmt_oi(1500) == "1.5K"
    assert fmt_oi(250000) == "2.50L"
    assert fmt_oi(35000000) == "3.50Cr"
    assert fmt_oi(-1500) == "-1.5K"

def test_fmt_pct():
    assert fmt_pct(None) == "0.0%"
    assert fmt_pct(12.5) == "+12.5%"
    assert fmt_pct(-5.1) == "-5.1%"

def test_fmt_int():
    assert fmt_int(None) == "0"
    assert fmt_int(123) == "123"
    assert fmt_int(123.0) == "123"
    assert fmt_int(123.4) == "123.4"

# Test time_guards.py
from src.engine.time_guards import is_trading_allowed_now
from datetime import datetime as real_datetime

IST = pytz.timezone("Asia/Kolkata")

class MockDatetime(real_datetime):
    _mock_now = None

    @classmethod
    def now(cls, tz=None):
        if cls._mock_now is not None:
            if tz is not None:
                # Convert the naive/aware mock datetime to target timezone
                if cls._mock_now.tzinfo is None:
                    return IST.localize(cls._mock_now).astimezone(tz)
                return cls._mock_now.astimezone(tz)
            return cls._mock_now
        return real_datetime.now(tz)

@pytest.fixture(autouse=True)
def patch_datetime():
    with patch("src.engine.time_guards.datetime", MockDatetime):
        yield

def set_mock_now(year, month, day, hour, minute, second=0):
    MockDatetime._mock_now = datetime(year, month, day, hour, minute, second, tzinfo=IST)

def test_time_guards_opening_auction():
    # 09:20 IST -> Opening auction noise window
    set_mock_now(2026, 7, 8, 9, 20)
    allowed, reason = is_trading_allowed_now("NIFTY")
    assert allowed is False
    assert "Opening auction noise" in reason

def test_time_guards_expiry_end_of_session():
    # 15:15 IST on NSE -> Expiry end-of-session window
    set_mock_now(2026, 7, 8, 15, 15)
    allowed, reason = is_trading_allowed_now("NIFTY")
    assert allowed is False
    assert "Expiry end-of-session" in reason

def test_time_guards_eia_natural_gas():
    # Thursday 20:05 IST -> EIA window for NATURALGAS
    set_mock_now(2026, 7, 9, 20, 5)
    allowed, reason = is_trading_allowed_now("NATURALGAS")
    assert allowed is False
    assert "EIA Natural Gas Storage Report" in reason

def test_time_guards_eia_crudeoil():
    # Wednesday 19:55 IST -> EIA window for CRUDEOIL
    set_mock_now(2026, 7, 8, 19, 55)
    allowed, reason = is_trading_allowed_now("CRUDEOIL")
    assert allowed is False
    assert "EIA Weekly Petroleum Status" in reason

@patch("config.cme_holidays.is_cme_closed", return_value=True)
def test_time_guards_cme_holiday(mock_cme_closed):
    set_mock_now(2026, 7, 8, 12, 0)
    allowed, reason = is_trading_allowed_now("NATURALGAS")
    assert allowed is False
    assert "CME holiday" in reason

@patch("config.cme_holidays.is_cme_early_close", return_value=True)
@patch("config.cme_holidays.is_cme_closed", return_value=False)
def test_time_guards_cme_early_close(mock_cme_closed, mock_cme_early):
    set_mock_now(2026, 7, 8, 18, 0)
    allowed, reason = is_trading_allowed_now("NATURALGAS")
    assert allowed is False
    assert "CME early close" in reason

def test_time_guards_expiry_cutoff_nse():
    # Expiry day NSE -> 14:45 IST (after 14:30)
    set_mock_now(2026, 7, 8, 14, 45)
    allowed, reason = is_trading_allowed_now("NIFTY", expiry_str="2026-07-08")
    assert allowed is False
    assert "Expiry day trading cutoff" in reason

def test_time_guards_expiry_cutoff_mcx():
    # Expiry day MCX -> 20:15 IST (after 20:00)
    set_mock_now(2026, 7, 8, 20, 15)
    allowed, reason = is_trading_allowed_now("NATURALGAS", expiry_str="2026-07-08")
    assert allowed is False
    assert "Expiry day trading cutoff" in reason

def test_time_guards_allowed():
    # normal trading time
    set_mock_now(2026, 7, 8, 11, 0)
    allowed, reason = is_trading_allowed_now("NIFTY")
    assert allowed is True
    assert reason == ""

def test_time_guards_error_fallback():
    # force exception during datetime.now()
    MockDatetime._mock_now = None
    with patch.object(MockDatetime, "now", side_effect=Exception("test error")):
        allowed, reason = is_trading_allowed_now("NIFTY")
        assert allowed is True
        assert reason == ""

# Test RBI announcement time
@patch("config.runtime_config.load_runtime_config")
def test_time_guards_rbi_announcement(mock_load_config):
    mock_load_config.return_value = {"rbi_announcement_time": "12:00"}
    set_mock_now(2026, 7, 8, 12, 2)
    allowed, reason = is_trading_allowed_now("BANKNIFTY")
    assert allowed is False
    assert "RBI announcement window" in reason
