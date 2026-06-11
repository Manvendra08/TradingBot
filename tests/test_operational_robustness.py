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


def test_dhan_resolver():
    from src.utils.dhan_resolver import get_dhan_security_id, _CACHE
    _CACHE.clear()

    # Test static indices resolve from setting fallbacks
    assert get_dhan_security_id("NIFTY") == 13
    assert get_dhan_security_id("BANKNIFTY") == 25

    # Mock urllib urlopen
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = b'<html><script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"scripData":{"scripId":"99999"}}}}</script></html>'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Natural gas slug is commodity
        res = get_dhan_security_id("NATURALGAS")
        assert res == 99999
        # Check cache
        assert _CACHE["NATURALGAS"] == 99999


def test_pnl_fallback_time_value():
    from src.models.schema import close_paper_trade, get_conn
    
    # Insert a dummy trade
    with get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM option_chain_snapshots")
        
        # Insert trade
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, side, option_type, strike, entry_underlying, entry_premium, lots, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-10T10:00:00", "NATURALGAS", "LONG", "BUY", "CE", 150.0, 150.0, 10.0, 1, "OPEN")
        )
        trade_id = conn.execute("SELECT id FROM paper_trades WHERE symbol='NATURALGAS'").fetchone()["id"]
        
        # Insert snapshot
        conn.execute(
            """
            INSERT INTO option_chain_snapshots (fetched_at, symbol, expiry, strike, option_type, ltp, underlying_price)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-10T10:05:00", "NATURALGAS", "2026-06-25", 150.0, "CE", 12.5, 152.0)
        )
    
    # Close paper trade with missing exit_premium
    close_paper_trade(trade_id, "2026-06-10T10:10:00", 152.0, None, "CLOSED_TARGET", "target hit")
    
    # Verify exit_premium was set to 12.5 (from snapshot) rather than intrinsic value 2.0 (152.0 - 150.0)
    with get_conn() as conn:
        trade = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        assert trade["exit_premium"] == 12.5
        assert trade["pnl_points"] == 2.5 # 12.5 (exit) - 10.0 (entry)


def test_underlying_cmp_prioritized_exit():
    from src.engine.paper_trading import _maybe_close_open_trade
    from src.models.schema import get_conn
    
    with get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, side, option_type, strike, entry_underlying, entry_premium, sl_underlying, target_underlying, sl_premium, target_premium, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-10T10:00:00", "NATURALGAS", "LONG", "BUY", "CE", 150.0, 150.0, 10.0, 145.0, 160.0, 5.0, 20.0, "OPEN")
        )
        trade_id = conn.execute("SELECT id FROM paper_trades WHERE symbol='NATURALGAS'").fetchone()["id"]
        
    # Test exit when underlying CMP crosses target (161.0 >= 160.0) but option premium hasn't hit target_premium (premium is e.g. 12.0)
    # The trade should close immediately because the underlying CMP crossed target
    _maybe_close_open_trade("NATURALGAS", 161.0, "2026-06-25", "2026-06-10T10:05:00", option_rows=[{"strike": 150.0, "option_type": "CE", "ltp": 12.0}])
    
    with get_conn() as conn:
        trade = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        assert trade["status"] == "CLOSED_TARGET"
        assert trade["exit_underlying"] == 161.0
        assert trade["exit_premium"] == 12.0


def test_merge_state_indicator_preservation():
    from src.fetchers.chart_fetcher import get_chart_fetcher
    fetcher = get_chart_fetcher()
    payload = {
        "sentiment": "BULLISH",
        "ohlc": {"open": 100, "high": 105, "low": 98, "close": 102},
        "bar_start_utc": "2026-06-10T10:00:00Z",
        "bar_end_utc": "2026-06-10T11:00:00Z",
        "atr_14": 4.5,
        "custom_indicator": "test_val"
    }
    merged = fetcher._merge_state("TESTSYM", "1h", payload)
    assert merged["sentiment"] == "BULLISH"
    assert merged["atr_14"] == 4.5
    assert merged["custom_indicator"] == "test_val"


@patch("src.fetchers.chart_fetcher._fetch_yf")
@patch("src.fetchers.chart_fetcher.get_dhan_security_id")
def test_dhan_builtup_fallback_indicators(mock_get_sec_id, mock_fetch_yf):
    from src.fetchers.chart_fetcher import _fetch_dhan_builtup_ohlc
    mock_get_sec_id.return_value = 504265
    
    # Mock fallback yfinance payload
    mock_fetch_yf.return_value = {
        "atr_14": 6.5,
        "prev_ohlc": {"open": 148, "high": 151, "low": 147, "close": 150}
    }
    
    # Mock response from Dhan builtup API
    from src.fetchers.chart_fetcher import _last_closed_window
    import json
    window = _last_closed_window("1h", "NATURALGAS")
    assert window is not None
    start_utc, end_utc = window
    st_val = start_utc.timestamp()
    et_val = end_utc.timestamp()
    
    mock_data = {
        "data": [
            {"st": st_val, "et": et_val, "o": 150.0, "h": 152.0, "l": 149.0, "c": 151.0, "v": 100}
        ]
    }
    
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_data).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        payload = _fetch_dhan_builtup_ohlc("NATURALGAS", "1h", reference_price=300.0)
        assert payload is not None
        assert payload["atr_14"] == 6.5
        assert payload["prev_ohlc"]["close"] == 150


def test_build_paper_trade_plan_fut_atr():
    from src.engine.paper_plan import build_paper_trade_plan
    
    ctx_regular = {
        "symbol": "NATURALGAS",
        "underlying": 300.0,
        "atm_strike": 300.0,
        "chart_indicators": {
            "3h": {
                "atr_14": 5.0,
                "prev_ohlc": {"open": 298, "high": 301, "low": 297, "close": 299}
            }
        }
    }
    
    # Regular FUT trade should use ATR(14)-based SL and TP
    plan_reg = build_paper_trade_plan("Long Buildup", 80, ctx_regular)
    assert plan_reg is not None
    assert plan_reg["option_type"] == "FUT"
    # SL = underlying - 1.5 * ATR = 300 - 1.5 * 5 = 292.5
    # TP = underlying + 2.0 * ATR = 300 + 2.0 * 5 = 310.0
    assert plan_reg["sl_underlying"] == 292.5
    assert plan_reg["target_underlying"] == 310.0
    
    # Timeframe FUT trade should bypass ATR SL/TP logic (default to Support/Resistance step-based fallback)
    ctx_tf = {**ctx_regular, "setup_type": "TIMEFRAME"}
    plan_tf = build_paper_trade_plan("Long Buildup", 80, ctx_tf)
    assert plan_tf is not None
    assert plan_tf["option_type"] == "FUT"
    # Fallback to strike step based SL/TP: strike step is 5 (for NG), so SL is 295.0, TP is 305.0
    assert plan_tf["sl_underlying"] != 292.5
    assert plan_tf["target_underlying"] != 310.0


