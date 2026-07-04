from unittest.mock import MagicMock

import pytest

from config.symbol_classes import get_kite_exchange
from src.engine import symbol_resolver as sr


@pytest.fixture(autouse=True)
def reset_cache():
    sr._INSTRUMENT_CACHE.clear()
    sr._TSYM_EXPIRY_CACHE.clear()
    sr._INSTRUMENT_CACHE_TS = 0.0
    sr._REFRESH_IN_PROGRESS = False
    if hasattr(sr, "_INSTRUMENT_CACHE_PATH") and sr._INSTRUMENT_CACHE_PATH.exists():
        sr._INSTRUMENT_CACHE_PATH.unlink()
    if hasattr(sr, "_TSYM_CACHE_PATH") and sr._TSYM_CACHE_PATH.exists():
        sr._TSYM_CACHE_PATH.unlink()
    yield
    sr._INSTRUMENT_CACHE.clear()
    sr._TSYM_EXPIRY_CACHE.clear()
    sr._INSTRUMENT_CACHE_TS = 0.0
    sr._REFRESH_IN_PROGRESS = False


def test_fetch_and_cache_includes_bfo():
    kite = MagicMock()
    kite.instruments.side_effect = lambda ex: {
        "NFO": [
            {
                "name": "NIFTY",
                "expiry": "2026-06-26",
                "strike": 25000,
                "instrument_type": "CE",
                "tradingsymbol": "NIFTY26JUN25000CE",
                "instrument_token": 1,
                "lot_size": 65,
                "tick_size": 0.05,
            }
        ],
        "BFO": [
            {
                "name": "SENSEX",
                "expiry": "2026-06-25",
                "strike": 76000,
                "instrument_type": "CE",
                "tradingsymbol": "SENSEX2562576000CE",
                "instrument_token": 2,
                "lot_size": 20,
                "tick_size": 0.05,
            },
        ],
        "MCX": [],
    }[ex]

    sr.fetch_and_cache_instruments(kite)

    assert [c.args[0] for c in kite.instruments.call_args_list] == ["NFO", "BFO", "MCX"]
    hit = sr.resolve_instrument("SENSEX", "2026-06-25", 76000.0, "CE")
    assert hit is not None
    assert hit["instrument_token"] == 2
    assert hit["tradingsymbol"] == "SENSEX2562576000CE"
    assert hit["lot_size"] == 20


def test_get_kite_exchange_routing():
    assert get_kite_exchange("SENSEX") == "BFO"
    assert get_kite_exchange("NIFTY") == "NFO"
    assert get_kite_exchange("BANKNIFTY") == "NFO"
    assert get_kite_exchange("NATURALGAS") == "MCX"
    assert get_kite_exchange("CRUDEOIL") == "MCX"


def test_live_trading_exchange_alias():
    from src.engine.live_trading import _get_exchange

    assert _get_exchange("SENSEX") == "BFO"
    assert _get_exchange("NIFTY") == "NFO"
