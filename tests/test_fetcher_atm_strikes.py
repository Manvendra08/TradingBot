"""
Targeted tests for +-15 strike ATM filtering across all fetchers.
"""
import pytest
from src.fetchers.base_fetcher import filter_atm_strikes, BaseFetcher
from src.fetchers.niftytrader_fetcher import _normalise as nt_normalise
from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher
from src.fetchers.dhan_fetcher import DhanFetcher
from src.fetchers.dhan_sensex_fetcher import DhanSensexFetcher
from src.fetchers.nse_fetcher import NSEPublicFetcher


def test_filter_atm_strikes_underlying_filtering():
    # 50 strikes: 100 to 345 in steps of 5
    strikes = []
    for s in range(100, 350, 5):
        strikes.append({"strike": float(s), "option_type": "CE", "ltp": 10.0})
        strikes.append({"strike": float(s), "option_type": "PE", "ltp": 10.0})

    # Underlying is 200.0 -> ATM is 200.0
    # +-15 strikes around 200.0 means 15 below (125 to 195) + ATM (200) + 15 above (205 to 275) = 31 unique strikes = 62 legs
    filtered = filter_atm_strikes(strikes, underlying_price=200.0, strikes_around_atm=15)
    unique_filtered_strikes = sorted(set(s["strike"] for s in filtered))

    assert len(unique_filtered_strikes) == 31
    assert min(unique_filtered_strikes) == 125.0
    assert max(unique_filtered_strikes) == 275.0
    assert 200.0 in unique_filtered_strikes
    assert len(filtered) == 62


def test_filter_atm_strikes_preserves_required_strikes():
    strikes = []
    for s in range(100, 350, 5):
        strikes.append({"strike": float(s), "option_type": "CE", "ltp": 10.0})
        strikes.append({"strike": float(s), "option_type": "PE", "ltp": 10.0})

    # Required strikes outside the +-15 window (e.g. 100.0 and 340.0 from open positions)
    required = {100.0, 340.0}
    filtered = filter_atm_strikes(strikes, underlying_price=200.0, strikes_around_atm=15, required_strikes=required)
    unique_filtered_strikes = set(s["strike"] for s in filtered)

    assert 100.0 in unique_filtered_strikes
    assert 340.0 in unique_filtered_strikes
    assert 200.0 in unique_filtered_strikes
    assert len(unique_filtered_strikes) == 33  # 31 + 2


def test_filter_atm_strikes_fallback_ce_pe_parity():
    # When underlying_price is None or <= 0
    strikes = [
        {"strike": 180.0, "option_type": "CE", "ltp": 25.0},
        {"strike": 180.0, "option_type": "PE", "ltp": 1.0},
        {"strike": 190.0, "option_type": "CE", "ltp": 15.0},
        {"strike": 190.0, "option_type": "PE", "ltp": 3.0},
        {"strike": 200.0, "option_type": "CE", "ltp": 8.0},
        {"strike": 200.0, "option_type": "PE", "ltp": 8.2},  # ATM by parity
        {"strike": 210.0, "option_type": "CE", "ltp": 2.0},
        {"strike": 210.0, "option_type": "PE", "ltp": 16.0},
    ]
    # Window of +-1 strike around ATM (200) -> 190, 200, 210
    filtered = filter_atm_strikes(strikes, underlying_price=None, strikes_around_atm=1)
    unique = sorted(set(s["strike"] for s in filtered))
    assert unique == [190.0, 200.0, 210.0]


def test_niftytrader_normalise_trims_to_atm_window():
    # Synthetic NiftyTrader payload with 50 strikes
    initial_chain = []
    for s in range(200, 300, 2):
        initial_chain.append({
            "strike_price": float(s),
            "expiry_date": "2026-11-20",
            "calls_close": 5.0,
            "calls_oi": 100,
            "puts_close": 5.0,
            "puts_oi": 100,
        })
    payload = {
        "props": {
            "pageProps": {
                "initialOptionChainData": initial_chain,
                "initialSpot": {"last_price": 250.0},
            }
        }
    }
    result = nt_normalise("NATURALGAS", "NATURALGAS", None, payload, "niftytrader")
    assert result is not None
    unique_strikes = set(s["strike"] for s in result["strikes"])
    # 50 strikes reduced to 31 (ATM 250 +- 15 strikes of step 2 -> 220 to 280)
    assert len(unique_strikes) == 31
    assert min(unique_strikes) == 220.0
    assert max(unique_strikes) == 280.0
    assert 250.0 in unique_strikes


def test_base_fetcher_subclass_has_filter_atm_strikes():
    f = DhanCommodityFetcher()
    assert hasattr(f, "filter_atm_strikes")
    strikes = [{"strike": float(s), "option_type": "CE", "ltp": 1.0} for s in range(100, 200, 2)]
    res = f.filter_atm_strikes(strikes, underlying_price=150.0, strikes_around_atm=5)
    unique = sorted(set(s["strike"] for s in res))
    assert len(unique) == 11
    assert 150.0 in unique
