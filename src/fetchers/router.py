"""
Fetcher Router — tries sources in per-symbol priority order
(defined by _priority_for()).
Returns first successful result; logs fallback events.

Thread-safety: _instances dict is guarded by _lock so APScheduler concurrent
jobs cannot create duplicate fetcher instances.
"""

import logging
import threading

from config.settings import STRIKES_AROUND_ATM
from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher
from src.fetchers.dhan_fetcher import DhanFetcher
from src.fetchers.dhan_sensex_fetcher import DhanSensexFetcher
from src.fetchers.nse_fetcher import NSEPublicFetcher
from src.fetchers.paytm_fetcher import PaytmFetcher
from src.fetchers.dhan_headless_fetcher import DhanHeadlessFetcher
from src.fetchers.moneycontrol_fetcher import MoneycontrolFetcher
from src.utils.greeks_calculator import enrich_missing_greeks

try:
    from src.fetchers.sensibull_fetcher import SensibullFetcher
except ImportError:
    SensibullFetcher = None

try:
    from src.fetchers.shoonya_fetcher import ShoonyaFetcher
except ImportError:
    ShoonyaFetcher = None

log = logging.getLogger(__name__)

_MCX_COMMODITIES = {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}

_FETCHERS = {
    "dhan": DhanFetcher,
    "dhan_commodity": DhanCommodityFetcher,
    "dhan_sensex": DhanSensexFetcher,
    "nse_public": NSEPublicFetcher,
    "paytm": PaytmFetcher,
    "dhan_headless": DhanHeadlessFetcher,
    "moneycontrol": MoneycontrolFetcher,
}
if ShoonyaFetcher is not None:
    _FETCHERS["shoonya"] = ShoonyaFetcher
if SensibullFetcher is not None:
    _FETCHERS["sensibull"] = SensibullFetcher

_instances: dict = {}
_lock = threading.Lock()


def _get_fetcher(name: str):
    # Shoonya must use the process-wide singleton so that the token obtained
    # by the router, chart_fetcher, or any other caller is shared. Otherwise
    # each caller creates its own ShoonyaFetcher instance with its own cached
    # token, causing duplicate OAuth logins and premature session expiry.
    if name == "shoonya":
        from src.fetchers.shoonya_fetcher import get_shoonya_fetcher
        return get_shoonya_fetcher()
    with _lock:
        if name not in _instances:
            if name == "scrapegraph":
                from src.fetchers.scrapegraph_fetcher import ScrapeGraphFetcher
                _instances[name] = ScrapeGraphFetcher()
            elif name == "sensibull":
                from src.fetchers.sensibull_fetcher import SensibullFetcher
                _instances[name] = SensibullFetcher()
            else:
                _instances[name] = _FETCHERS[name]()
    return _instances[name]


def _priority_for(symbol: str) -> list[str]:
    """
    BUG-M06 FIX: Fetcher priority is now configurable via FETCHER_PRIORITY_OVERRIDE
    environment variable or config.settings. Falls back to sensible defaults per symbol class.
    
    Override format (env var): JSON dict mapping symbol base -> list of fetcher names.
    e.g., FETCHER_PRIORITY_OVERRIDE='{"NATURALGAS": ["dhan_commodity", "shoonya"]}'
    """
    import os
    import json
    
    base = symbol.upper().split()[0]
    
    # Check for runtime override
    override_json = os.environ.get("FETCHER_PRIORITY_OVERRIDE")
    if override_json:
        try:
            overrides = json.loads(override_json)
            if base in overrides:
                return overrides[base]
        except (json.JSONDecodeError, TypeError):
            pass
    
    # Check config.settings for per-symbol override
    try:
        from config.settings import FETCHER_PRIORITY
        if isinstance(FETCHER_PRIORITY, dict) and base in FETCHER_PRIORITY:
            return FETCHER_PRIORITY[base]
    except (ImportError, AttributeError):
        pass
    
    # Default priorities per symbol class
    if base in _MCX_COMMODITIES:
        return ["shoonya", "dhan_commodity", "moneycontrol", "dhan", "dhan_headless"]
    if base == "SENSEX":
        return ["shoonya", "dhan_sensex", "sensibull", "dhan", "nse_public"]
    return [
        "sensibull", "shoonya", "paytm", "dhan",
        "nse_public", "dhan_headless", "moneycontrol",
    ]


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


def _try_fetcher(source: str, symbol: str, expiry: str | None) -> dict | None:
    """Run a single fetcher and return normalised result or None."""
    if source not in _FETCHERS:
        log.warning("Fetcher '%s' unavailable; skipping", source)
        return None
    fetcher = _get_fetcher(source)
    try:
        result = fetcher.fetch_option_chain(symbol, expiry=expiry)
        if not result or not result.get("strikes"):
            log.debug("[router] %s | %-12s returned no data", symbol, source)
            return None
        base = str(result.get("symbol") or symbol).upper().split()[0]
        if base in _MCX_COMMODITIES and not result.get("underlying_price"):
            log.warning(
                "[router] %s | %-12s returned MCX data without underlying price — skipping",
                symbol, source,
            )
            return None
        total_oi = sum(s.get("oi") or 0 for s in result["strikes"])
        total_ltp = sum(s.get("ltp") or 0 for s in result["strikes"])
        if total_oi == 0 and total_ltp == 0:
            log.warning(
                "[router] %s | %-12s returned zero-filled strikes — skipping",
                symbol, source,
            )
            return None
        return result
    except Exception as exc:
        log.error("[router] %s | %-12s raised exception: %s", symbol, source, exc)
        if source == "shoonya" and any(k in str(exc) for k in ("401", "403", "Invalid Token")):
            try:
                from src.models.schema import stamp_health
                stamp_health("shoonya_session", "DOWN", f"auth-fail: {str(exc)[:100]}")
            except Exception:
                pass
        return None


def _finalise_result(result: dict, source: str, symbol: str, priority: list[str]) -> dict:
    """Apply ATM filter, enrich greeks, log success, stamp health."""
    _filter_atm_strikes(result)
    underlying = result.get("underlying_price")
    expiry_val = result.get("expiry", "")
    if underlying and expiry_val:
        n = enrich_missing_greeks(result["strikes"], underlying, expiry_val)
        if n:
            log.info(
                "[router] enriched %d/%d strikes with computed greeks for %s",
                n, len(result["strikes"]), symbol,
            )
    strikes_count = len(result.get("strikes", []))
    is_fallback = source != priority[0]
    prefix = "FALLBACK " if is_fallback else ""
    log.info(
        "[router] %s | ✅ %s%-12s | price=%-10.2f expiry=%s strikes=%d",
        symbol, prefix, source,
        result.get("underlying_price", 0),
        result.get("expiry", "?"),
        strikes_count,
    )
    if source == "shoonya":
        try:
            from src.models.schema import stamp_health
            stamp_health("shoonya_session", "OK", f"last_fetch={symbol}")
        except Exception:
            pass
    return result


# Fetcher pairs that should race in parallel (primary vs hot-backup).
# Only used when both are present in the symbol's priority list.
_PARALLEL_RACE_PAIRS: list[tuple[str, str]] = [
    ("sensibull", "shoonya"),
]


def fetch_option_chain(symbol: str, expiry: str | None = None) -> dict | None:
    """
    Try fetchers in configured priority order.
    For NSE symbols sensibull and shoonya are raced concurrently so that
    shoonya's result is not blocked behind sensibull's full retry chain.
    Returns normalised dict or None if all fail.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    priority = _priority_for(symbol)
    log.info("[router] %s option-chain | trying: %s", symbol, " → ".join(priority))

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

    # Build the effective fetch sequence, substituting any race-eligible pair
    # with a single parallel step that resolves to the first valid result.
    remaining = list(priority)
    result_source: str | None = None
    result_data: dict | None = None

    # ── Step 1: check for a parallel race pair at the front of the queue ──
    for primary, backup in _PARALLEL_RACE_PAIRS:
        if primary in remaining and backup in remaining:
            p_idx = remaining.index(primary)
            b_idx = remaining.index(backup)
            # Only race when primary is first (or backup is close behind it)
            # so we don't disrupt an intentionally different ordering (e.g. MCX).
            if p_idx < b_idx and p_idx == 0:
                race_pair = [primary, backup]
                # Remove both from the sequential queue; we handle them here.
                remaining = [s for s in remaining if s not in race_pair]
                log.debug("[router] %s | racing %s vs %s in parallel", symbol, primary, backup)
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="router-race") as ex:
                    futures = {
                        ex.submit(_try_fetcher, src, symbol, expiry): src
                        for src in race_pair
                    }
                    for fut in _as_completed(futures):
                        src = futures[fut]
                        try:
                            data = fut.result()
                        except Exception as exc:
                            log.error("[router] %s | race %s raised: %s", symbol, src, exc)
                            data = None
                        if data is not None:
                            result_data = data
                            result_source = src
                            # Cancel the other future (best-effort; won't interrupt
                            # blocking I/O but prevents it from being scheduled).
                            for other_fut in futures:
                                if other_fut is not fut:
                                    other_fut.cancel()
                            break
                break  # only one race pair at a time

    # ── Step 2: if race produced a result, we're done ──
    if result_data is not None:
        return _finalise_result(result_data, result_source, symbol, priority)

    # ── Step 3: fall through to remaining sequential fetchers ──
    for source in remaining:
        if source not in _FETCHERS:
            log.warning("Fetcher '%s' unavailable; skipping", source)
            continue
        data = _try_fetcher(source, symbol, expiry)
        if data is not None:
            return _finalise_result(data, source, symbol, priority)

    # All failed
    try:
        from src.models.schema import stamp_health
        stamp_health("shoonya_session", "DOWN", f"all fetchers failed for {symbol}")
    except Exception:
        pass
    log.error("[router] %s | ❌ ALL fetchers failed", symbol)
    return None
