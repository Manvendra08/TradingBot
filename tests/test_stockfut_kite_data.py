"""Tests for Kite futures data layer."""

from unittest.mock import MagicMock
from src.fetchers.kite_fetcher import KiteDataClient


def test_quote_includes_oi_and_depth():
    client = KiteDataClient()
    mock_kite = MagicMock()
    mock_kite.quote.return_value = {
        "NFO:RELIANCE26NOVFUT": {
            "last_price": 1305.5,
            "oi": 15400000,
            "volume": 250000,
            "depth": {
                "buy": [{"price": 1305.4, "quantity": 500, "orders": 2}],
                "sell": [{"price": 1305.6, "quantity": 750, "orders": 3}],
            },
            "ohlc": {"open": 1300.0, "high": 1310.0, "low": 1295.0, "close": 1298.0},
        }
    }
    client._kite_client = mock_kite

    quotes = client.get_futures_quotes(["RELIANCE26NOVFUT"])
    assert "RELIANCE26NOVFUT" in quotes
    q = quotes["RELIANCE26NOVFUT"]
    assert q["price"] == 1305.5
    assert q["oi"] == 15400000
    assert q["bid"] == 1305.4
    assert q["ask"] == 1305.6
    assert q["bid_qty"] == 500
    assert q["ask_qty"] == 750


def test_missing_quote_is_absent_not_zero():
    client = KiteDataClient()
    mock_kite = MagicMock()
    # Missing contract or zero price is not returned as zeros
    mock_kite.quote.return_value = {
        "NFO:BADCONTRACT": {"last_price": 0.0, "oi": 0}
    }
    client._kite_client = mock_kite

    quotes = client.get_futures_quotes(["BADCONTRACT", "NONEXISTENT"])
    assert "BADCONTRACT" not in quotes
    assert "NONEXISTENT" not in quotes
    assert quotes == {}


def test_lot_size_read_per_contract():
    client = KiteDataClient()
    # Mock get_instruments
    client.get_instruments = MagicMock(return_value=[
        {
            "instrument_type": "FUT",
            "segment": "NFO-FUT",
            "name": "RELIANCE",
            "tradingsymbol": "RELIANCE26NOVFUT",
            "instrument_token": 111111,
            "expiry": "2026-11-26",
            "lot_size": 250,
        },
        {
            "instrument_type": "FUT",
            "segment": "NFO-FUT",
            "name": "RELIANCE",
            "tradingsymbol": "RELIANCE26DECFUT",
            "instrument_token": 222222,
            "expiry": "2026-12-31",
            "lot_size": 250,
        },
        {
            "instrument_type": "FUT",
            "segment": "NFO-FUT",
            "name": "NIFTY",  # Should be excluded
            "tradingsymbol": "NIFTY26NOVFUT",
            "instrument_token": 333333,
            "expiry": "2026-11-26",
            "lot_size": 65,
        },
    ])

    futs = client.list_stock_futures()
    symbols = [f["symbol"] for f in futs]
    assert "NIFTY" not in symbols
    assert "RELIANCE" in symbols
    assert len(futs) == 2
    assert futs[0]["tradingsymbol"] == "RELIANCE26NOVFUT"
    assert futs[0]["lot_size"] == 250
