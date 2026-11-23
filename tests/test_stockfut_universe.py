"""Tests for stock futures universe builder."""

from datetime import date
from unittest.mock import MagicMock
from src.stockfut.universe import UniverseBuilder, count_trading_sessions, Contract


def test_trading_sessions_count():
    # Mon 2026-11-23 to Fri 2026-11-27 = 4 sessions (Tue, Wed, Thu, Fri)
    assert count_trading_sessions(date(2026, 11, 23), date(2026, 11, 27)) == 4
    # Mon to next Mon = 5 sessions
    assert count_trading_sessions(date(2026, 11, 23), date(2026, 11, 30)) == 5


def test_banned_and_event_names_excluded():
    mock_client = MagicMock()
    mock_client.list_stock_futures.return_value = [
        {"symbol": "IDEA", "tradingsymbol": "IDEA26NOVFUT", "expiry": "2026-11-26", "lot_size": 10000},
        {"symbol": "INFY", "tradingsymbol": "INFY26NOVFUT", "expiry": "2026-11-26", "lot_size": 400},
        {"symbol": "TCS", "tradingsymbol": "TCS26NOVFUT", "expiry": "2026-11-26", "lot_size": 175},
    ]

    builder = UniverseBuilder(kite_client=mock_client)
    res = builder.build_universe(
        asof=date(2026, 11, 10),
        capital=10000000.0,
        ban_list={"IDEA"},
        mwpl_dict={"INFY": 45.0, "TCS": 30.0},
        events_dict={"TCS": ["EARNINGS_2026-11-15"], "INFY": []},
    )

    symbols = [c.symbol for c in res]
    assert "IDEA" not in symbols  # in ban
    assert "TCS" not in symbols   # has event within 7 sessions
    assert "INFY" in symbols


def test_near_month_used_until_expiry_day():
    mock_client = MagicMock()
    # Contract 1: near month expiry 2026-11-26
    # Contract 2: next month expiry 2026-12-31
    mock_client.list_stock_futures.return_value = [
        {"symbol": "RELIANCE", "tradingsymbol": "RELIANCE26NOVFUT", "expiry": "2026-11-26", "lot_size": 250},
        {"symbol": "RELIANCE", "tradingsymbol": "RELIANCE26DECFUT", "expiry": "2026-12-31", "lot_size": 250},
    ]

    builder = UniverseBuilder(kite_client=mock_client)

    # 1. Day before expiry (2026-11-25) -> should pick current month (NOV)
    res_before = builder.build_universe(
        asof=date(2026, 11, 25),
        capital=10000000.0,
        ban_list=set(),
        mwpl_dict={},
        events_dict={},
    )
    assert len(res_before) == 1
    assert res_before[0].tradingsymbol == "RELIANCE26NOVFUT"

    # 2. Expiry day itself (2026-11-26) -> should pick next month (DEC)
    res_expiry = builder.build_universe(
        asof=date(2026, 11, 26),
        capital=10000000.0,
        ban_list=set(),
        mwpl_dict={},
        events_dict={},
    )
    assert len(res_expiry) == 1
    assert res_expiry[0].tradingsymbol == "RELIANCE26DECFUT"


def test_unsizable_two_lots_at_2atr_excluded():
    mock_client = MagicMock()
    # Huge lot size stock: lot_size 5000, ATR = 50 -> 2 lots at 2.0x ATR = 2 * (100 * 5000) = ₹1,000,000 risk!
    # Hard cap on 1 Cr = 1 Cr * 0.75% * 1.25 = ₹93,750
    mock_client.list_stock_futures.return_value = [
        {"symbol": "HUGEVOL", "tradingsymbol": "HUGEVOL26NOVFUT", "expiry": "2026-11-26", "lot_size": 5000},
        {"symbol": "NORMAL", "tradingsymbol": "NORMAL26NOVFUT", "expiry": "2026-11-26", "lot_size": 250},
    ]

    builder = UniverseBuilder(kite_client=mock_client)
    res = builder.build_universe(
        asof=date(2026, 11, 10),
        capital=10000000.0,
        ban_list=set(),
        mwpl_dict={},
        events_dict={},
        atr_map={"HUGEVOL": 50.0, "NORMAL": 30.0},  # NORMAL risk: 2 * (60 * 250) = ₹30,000 < ₹93,750
    )
    symbols = [c.symbol for c in res]
    assert "HUGEVOL" not in symbols
    assert "NORMAL" in symbols


def test_fetch_error_excludes_everything_and_logs(monkeypatch):
    from src.fetchers import nse_eod_fetcher

    def bad_ban():
        raise nse_eod_fetcher.FetchError("Network down")

    monkeypatch.setattr(nse_eod_fetcher, "fetch_ban_list", bad_ban)

    builder = UniverseBuilder()
    res = builder.build_universe(asof=date(2026, 11, 10), capital=10000000.0)
    assert res == []
