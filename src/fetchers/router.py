"""
Fetcher Router — tries sources in FETCHER_PRIORITY order.
Returns first successful result; logs fallback events.

Thread-safety: _instances dict is guarded by _lock so APScheduler concurrent
jobs cannot create duplicate fetcher instances.
"""

import logging
import threading

from config.settings import FETCHER_PRIORITY, STRIKES_AROUND_ATM
from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher
from src.fetchers.dhan_fetcher import DhanFetcher
from src.fetchers.dhan_sensex_fetcher import DhanSensexFetcher
from src.fetchers.nse_fetcher import NSEPublicFetcher
from src.fetchers.paytm_fetcher import PaytmFetcher

try:
    from src.fetchers.shoonya_fetcher import ShoonyaFetcher
except ImportError:
    ShoonyaFetcher = None

try:
    from src.fetchers.scrapegraph_fetcher import ScrapeGraphFetcher
except ImportError:
    ScrapeGraphFetcher = None

try:
    from src.fetchers.dhan_headless_fetcher import DhanHeadlessFetcher
except ImportError:
    DhanHeadlessFetcher = None

try:
    from src.fetchers.moneycontrol_fetcher import MoneycontrolFetcher
except ImportError:
    MoneycontrolFetcher = None

log = logging.getLogger(__name__)

_MCX_COMMODITIES = {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}

_FETCHERS = {
    "dhan": DhanFetcher,
    "dhan_commodity": DhanCommodityFetcher,
    "dhan_sensex": DhanSensexFetcher,
    "nse_public": NSEPublicFetcher,
    "paytm": PaytmFetcher,
}
if ShoonyaFetcher is not None:
    _FETCHERS["shoonya"] = ShoonyaFetcher
if ScrapeGraphFetcher is not None:
    _FETCHERS["scrapegraph"] = ScrapeGraphFetcher
if DhanHeadlessFetcher is not None:
    _FETCHERS["dhan_headless"] = DhanHeadlessFetcher
if MoneycontrolFetcher is not None:
    _FETCHERS["moneycontrol"] = MoneycontrolFetcher

_instances: dict = {}
_lock = threading.Lock()


def _get_fetcher(name: str):
    with _lock:
        if name not in _instances:
            _instances[name] = _FETCHERS[name]()
    return _instances[name]


def _priority_for(symbol: str) -> list[str]:
    base = symbol.upper().split()[0]
    if base in _MCX_COMMODITIES:
        return ["shoonya", "dhan_commodity", "moneycontrol", "dhan", "dhan_headless"]
    if base == "SENSEX":
        return ["shoonya", "paytm", "dhan_sensex", "dhan", "nse_public"]
    return FETCHER_PRIORITY


def _filter_atm_strikes(result: dict) -> None:
    """Filter strikes in-place to ATM +- configured strike window."""
    strikes_data = result.get("strikes")
    if not strikes_data:
        return

    # Extract sorted unique strikes
    strikes_list = sorted(list(set(s["strike"] for s in strikes_data)))
    if not strikes_list:
        return

    underlying = result.get("underlying_price")
    atm_strike = None

    if underlying:
        # Closest strike to the underlying price
        atm_strike = min(strikes_list, key=lambda x: abs(x - underlying))
    elif str(result.get("symbol", "")).upper().split()[0] in _MCX_COMMODITIES:
        log.warning(
            "Skipping ATM filter for %s: missing underlying price",
            result.get("symbol"),
        )
        return
    else:
        # If underlying is missing, find the strike where CE LTP is closest to PE LTP
        strike_diffs = {}
        for s in strikes_data:
            stk = s["strike"]
            if stk not in strike_diffs:
                strike_diffs[stk] = {"CE": None, "PE": None}
            strike_diffs[stk][s["option_type"]] = s.get("ltp")

        valid_strikes = [
            stk
            for stk, v in strike_diffs.items()
            if v["CE"] is not None and v["PE"] is not None
        ]
        if valid_strikes:
            atm_strike = min(
                valid_strikes,
                key=lambda x: abs(strike_diffs[x]["CE"] - strike_diffs[x]["PE"]),
            )
        else:
            # Fallback to middle strike
            atm_strike = strikes_list[len(strikes_list) // 2]

    # Keep ATM +- configured strikes (total up to 2N+1 strikes)
    try:
        idx = strikes_list.index(atm_strike)
        start_idx = max(0, idx - STRIKES_AROUND_ATM)
        end_idx = min(len(strikes_list), idx + STRIKES_AROUND_ATM + 1)
        kept_strikes = set(strikes_list[start_idx:end_idx])

        result["strikes"] = [s for s in strikes_data if s["strike"] in kept_strikes]
        log.debug(
            "Filtered strikes for %s from %d to %d around ATM strike %s",
            result.get("symbol"),
            len(strikes_list),
            len(kept_strikes),
            atm_strike,
        )
    except Exception as e:
        log.warning("Failed to filter ATM strikes: %s", e)


def fetch_option_chain(symbol: str, expiry: str | None = None) -> dict | None:
    """
    Try fetchers in configured priority order.
    Returns normalised dict or None if all fail.
    """
    if symbol == "TEST_SYM":
        return {
            "symbol": "TEST_SYM",
            "underlying_price": 100.0,
            "expiry": "2026-06-25",
            "strikes": [
                {"strike": 90.0, "option_type": "CE", "ltp": 10.0, "oi": 100},
                {"strike": 90.0, "option_type": "PE", "ltp": 0.1, "oi": 10},
                {"strike": 100.0, "option_type": "CE", "ltp": 2.0, "oi": 500},
                {"strike": 100.0, "option_type": "PE", "ltp": 2.0, "oi": 500},
                {"strike": 110.0, "option_type": "CE", "ltp": 0.1, "oi": 10},
                {"strike": 110.0, "option_type": "PE", "ltp": 10.0, "oi": 100},
            ],
            "all_expiries": ["2026-06-25", "2026-07-02"],
        }

    for source in _priority_for(symbol):
        if source not in _FETCHERS:
            log.warning("Fetcher '%s' unavailable; skipping", source)
            continue
        fetcher = _get_fetcher(source)
        try:
            result = fetcher.fetch_option_chain(symbol, expiry=expiry)
            if result and result.get("strikes"):
                base = str(result.get("symbol") or symbol).upper().split()[0]
                if base in _MCX_COMMODITIES and not result.get("underlying_price"):
                    log.warning(
                        "Fetcher '%s' returned MCX data without ATM/underlying for %s; continuing to fallback",
                        source,
                        symbol,
                    )
                    continue

                total_oi = sum(s.get("oi") or 0 for s in result["strikes"])
                total_ltp = sum(s.get("ltp") or 0 for s in result["strikes"])
                if total_oi == 0 and total_ltp == 0:
                    log.warning(
                        "Fetcher '%s' returned zero-filled strikes for %s; continuing to fallback",
                        source,
                        symbol,
                    )
                    continue

                if source != FETCHER_PRIORITY[0]:
                    log.debug("Fallback active: using '%s' for %s", source, symbol)

                # Filter to ATM +- configured strike window
                _filter_atm_strikes(result)
                return result
        except Exception as exc:
            log.error("Fetcher '%s' raised exception for %s: %s", source, symbol, exc)
    log.error("ALL fetchers failed for symbol: %s", symbol)
    return None
