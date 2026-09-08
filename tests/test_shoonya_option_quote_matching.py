"""
Unit tests for Shoonya option quote parsing & index leak validation.
Verifies that NFO option symbols like NIFTY08SEP26C23850 (C/P format) are accepted,
while genuine index spot tokens (e.g. token 26000 / Nifty 50) are discarded.
"""

from unittest.mock import MagicMock, patch
from src.fetchers.shoonya_fetcher import ShoonyaFetcher, _is_option_tsym


def test_is_option_tsym_patterns():
    # NFO option formats (C/P + strike)
    assert _is_option_tsym("NIFTY08SEP26C23850") is True
    assert _is_option_tsym("NIFTY08SEP26P23850") is True
    assert _is_option_tsym("BANKNIFTY29SEP26C57400") is True
    assert _is_option_tsym("FINNIFTY15SEP26P24000") is True

    # BFO / MCX / Monthly NFO formats (CE/PE + strike or strike + CE/PE)
    assert _is_option_tsym("SENSEX26JUN77100CE") is True
    assert _is_option_tsym("SENSEX2670266500PE") is True
    assert _is_option_tsym("NATURALGAS26SEPE250CE") is True

    # Index / spot names (must NOT match)
    assert _is_option_tsym("Nifty 50") is False
    assert _is_option_tsym("Nifty Bank") is False
    assert _is_option_tsym("NIFTY") is False
    assert _is_option_tsym("BANKNIFTY") is False
    assert _is_option_tsym("SENSEX") is False
    assert _is_option_tsym("FINNIFTY") is False
    assert _is_option_tsym("MIDCPNIFTY") is False


def test_fetch_option_chain_accepts_nfo_c_p_quotes():
    f = ShoonyaFetcher.__new__(ShoonyaFetcher)
    f.access_token = "tok-123"
    f.user_id = "TESTU1"
    f.actid = "TESTU1"
    f.login = MagicMock(return_value=True)
    f._save_token = MagicMock()
    f._load_cached_token = MagicMock()

    # Mock underlying search and quote
    f._search_scrip = MagicMock(side_effect=lambda exch, query: {
        "stat": "Ok",
        "values": [{
            "tsym": "NIFTY24SEP26F",
            "token": "26000",
            "instname": "FUTIDX",
            "exd": "24-SEP-2026",
        }]
    })

    # GetOptionChain returns 2 strikes (CE and PE) missing lp/oi
    f._get_option_chain = MagicMock(return_value={
        "stat": "Ok",
        "values": [
            {
                "optt": "CE",
                "strprc": "23850.00",
                "token": "42633",
                "expiry": "08-SEP-2026",
                "tsym": "NIFTY08SEP26C23850",
            },
            {
                "optt": "PE",
                "strprc": "23850.00",
                "token": "42634",
                "expiry": "08-SEP-2026",
                "tsym": "NIFTY08SEP26P23850",
            },
        ]
    })

    # _get_quotes mock: returns quotes for 26000 (spot), 42633 (CE), 42634 (PE)
    def mock_get_quotes(exch, token):
        if str(token) == "26000":
            return {"stat": "Ok", "token": "26000", "tsym": "Nifty 50", "lp": "23698.80"}
        elif str(token) == "42633":
            return {
                "stat": "Ok",
                "token": "42633",
                "tsym": "NIFTY08SEP26C23850",
                "lp": "50.60",
                "oi": "154200",
                "oichg": "1200",
                "v": "5000",
                "iv": "14.2",
                "bp1": "50.50",
                "sp1": "50.70",
            }
        elif str(token) == "42634":
            return {
                "stat": "Ok",
                "token": "42634",
                "tsym": "NIFTY08SEP26P23850",
                "lp": "45.20",
                "oi": "120000",
                "oichg": "-800",
                "v": "4200",
                "iv": "13.8",
                "bp1": "45.10",
                "sp1": "45.30",
            }
        return None

    f._get_quotes = MagicMock(side_effect=mock_get_quotes)

    result = f.fetch_option_chain("NIFTY", expiry="2026-09-08")

    assert result is not None
    assert result["symbol"] == "NIFTY"
    assert len(result["strikes"]) == 2
    # Verify non-zero values were populated from q
    ce_strike = next(s for s in result["strikes"] if s["option_type"] == "CE")
    assert ce_strike["ltp"] == 50.60
    assert ce_strike["oi"] == 154200
    pe_strike = next(s for s in result["strikes"] if s["option_type"] == "PE")
    assert pe_strike["ltp"] == 45.20
    assert pe_strike["oi"] == 120000


def test_fetch_option_chain_discards_leaked_index_quotes():
    f = ShoonyaFetcher.__new__(ShoonyaFetcher)
    f.access_token = "tok-123"
    f.user_id = "TESTU1"
    f.actid = "TESTU1"
    f.login = MagicMock(return_value=True)
    f._save_token = MagicMock()
    f._load_cached_token = MagicMock()

    f._search_scrip = MagicMock(side_effect=lambda exch, query: {
        "stat": "Ok",
        "values": [{
            "tsym": "NIFTY24SEP26F",
            "token": "26000",
            "instname": "FUTIDX",
            "exd": "24-SEP-2026",
        }]
    })

    # Strike 42645 where Shoonya erroneously returns index quote (token 26000, Nifty 50)
    f._get_option_chain = MagicMock(return_value={
        "stat": "Ok",
        "values": [
            {
                "optt": "CE",
                "strprc": "24150.00",
                "token": "42645",
                "expiry": "08-SEP-2026",
                "tsym": "NIFTY08SEP26C24150",
            },
        ]
    })

    def mock_get_quotes(exch, token):
        if str(token) == "26000":
            return {"stat": "Ok", "token": "26000", "tsym": "Nifty 50", "lp": "23698.80"}
        elif str(token) == "42645":
            # Leaked index quote returned by broker for option token 42645!
            return {
                "stat": "Ok",
                "token": "26000",
                "tsym": "Nifty 50",
                "lp": "23698.80",
            }
        return None

    f._get_quotes = MagicMock(side_effect=mock_get_quotes)

    result = f.fetch_option_chain("NIFTY", expiry="2026-09-08")

    assert result is not None
    # The strike was discarded because it received an index quote, so ltp remains 0.0
    ce_strike = result["strikes"][0]
    assert ce_strike["ltp"] == 0.0
    assert ce_strike["oi"] == 0
