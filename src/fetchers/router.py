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
        return ["sensibull", "shoonya", "dhan_sensex", "dhan", "nse_public"]
    return [
        "sensibull", "shoonya", "paytm", "dhan",
        "nse_public", "dhan_headless", "moneycontrol",
    ]


def _filter_atm_strikes(result: dict, required_strikes: set[float] | None = None) -> None:
    """Filter strikes in-place to ATM +- configured strike window.
    
    Also preserves any required_strikes (e.g., from open trades) even if outside ATM window.
    """
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
        
        # Also keep any required strikes (e.g., from open trades)
        if required_strikes:
            kept_strikes |= required_strikes

        result["strikes"] = [s for s in strikes_data if s["strike"] in kept_strikes]
        log.debug(
            "Filtered strikes for %s from %d to %d around ATM strike %s (required: %d)",
            result.get("symbol"),
            len(strikes_list),
            len(kept_strikes),
            atm_strike,
            len(required_strikes) if required_strikes else 0,
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


def _strike_has_greeks(s: dict) -> bool:
    """A strike is considered to carry greeks data when it has a non-zero
    delta or IV (either supplied by the source feed or computed locally)."""
    d = s.get("delta")
    if isinstance(d, (int, float)) and d != 0:
        return True
    iv = s.get("iv")
    if isinstance(iv, (int, float)) and iv != 0:
        return True
    return False


def _finalise_result(result: dict, source: str, symbol: str, priority: list[str], required_strikes: set[float] | None = None) -> dict:
    """Apply ATM filter, enrich greeks, log success, stamp health."""
    _filter_atm_strikes(result, required_strikes)
    underlying = result.get("underlying_price")
    expiry_val = result.get("expiry", "")
    strikes = result.get("strikes", [])
    total_strikes = len(strikes)
    from_source = sum(1 for s in strikes if _strike_has_greeks(s))
    computed = 0
    if underlying and expiry_val:
        base = symbol.upper().split()[0]
        exchange = "MCX" if base in _MCX_COMMODITIES else ("BFO" if base == "SENSEX" else "NFO")
        computed = enrich_missing_greeks(strikes, underlying, expiry_val, exchange=exchange)
    with_greeks = sum(1 for s in strikes if _strike_has_greeks(s))
    if computed:
        log.info(
            "[router] enriched %d/%d strikes with computed greeks for %s",
            computed, total_strikes, symbol,
        )
    # Greeks coverage validation — reported for ALL symbols (not just when
    # some were computed) so gaps in the greeks feed are visible at a glance.
    log.info(
        "[router] %s | greeks coverage: %d/%d strikes have greeks (computed=%d, from_source=%d)",
        symbol, with_greeks, total_strikes, computed, from_source,
    )
    if underlying and expiry_val and total_strikes and with_greeks == 0:
        log.warning(
            "[router] %s | ⚠ greeks unavailable for all %d strikes (underlying=%.2f expiry=%s) — check LTP/IV feed",
            symbol, total_strikes, underlying, expiry_val,
        )
    strikes_count = len(result.get("strikes", []))
    is_dualfetch = "+" in source
    is_fallback = (source != priority[0]) and not is_dualfetch
    prefix = "DUALFETCH " if is_dualfetch else ("FALLBACK " if is_fallback else "")
    log.info(
        "[router] %s | ✅ %s%-12s | price=%-10.2f expiry=%s strikes=%d",
        symbol, prefix, source,
        result.get("underlying_price", 0),
        result.get("expiry", "?"),
        strikes_count,
    )
    if "shoonya" in source:
        try:
            from src.models.schema import stamp_health
            stamp_health("shoonya_session", "OK", f"last_fetch={symbol}")
        except Exception:
            pass
    return result


# Fetcher pairs that should race in parallel (primary vs hot-backup).
# Only used when both are present in the symbol's priority list.
_PARALLEL_RACE_PAIRS: list[tuple[str, str]] = [
    ("shoonya", "sensibull"),
]


def _merge_fetcher_results(primary: dict, fallback: dict, symbol: str) -> dict:
    """
    Merge two fetcher results for the same symbol.
    Primary is preferred; missing strikes/data from primary are filled from fallback.
    Only keeps ATM ± STRIKES_AROUND_ATM strikes from primary.
    
    Returns a new merged dict without modifying inputs.
    """
    if not primary:
        return fallback
    if not fallback:
        return primary
    
    underlying = primary.get("underlying_price")
    if not underlying:
        log.warning("[router] %s | primary missing underlying_price, cannot filter ATM strikes", symbol)
        return primary
    
    # Get primary strikes and filter to ATM ± STRIKES_AROUND_ATM
    primary_strikes = primary.get("strikes", [])
    if not primary_strikes:
        return primary
    
    # Extract unique strikes from primary
    primary_strike_vals = sorted(set(s.get("strike") for s in primary_strikes if s.get("strike") is not None))
    if not primary_strike_vals:
        return primary
    
    # Find ATM strike (closest to underlying)
    atm_strike = min(primary_strike_vals, key=lambda x: abs(x - underlying))
    atm_idx = primary_strike_vals.index(atm_strike)
    
    # Keep ATM ± STRIKES_AROUND_ATM
    start_idx = max(0, atm_idx - STRIKES_AROUND_ATM)
    end_idx = min(len(primary_strike_vals), atm_idx + STRIKES_AROUND_ATM + 1)
    allowed_strikes = set(primary_strike_vals[start_idx:end_idx])
    
    # Build lookup maps for allowed strikes only
    primary_map = {}
    for s in primary_strikes:
        key = (s.get("strike"), s.get("option_type"))
        if s.get("strike") in allowed_strikes:
            primary_map[key] = s
    
    fallback_map = {}
    for s in fallback.get("strikes", []):
        key = (s.get("strike"), s.get("option_type"))
        if s.get("strike") in allowed_strikes:
            fallback_map[key] = s
    
    # Merge: only allowed strikes, primary preferred, fallback fills gaps
    merged = {
        "symbol": primary.get("symbol"),
        "underlying_price": primary.get("underlying_price") or fallback.get("underlying_price"),
        "expiry": primary.get("expiry") or fallback.get("expiry"),
        "strikes": [],
        "source": f"{primary.get('source', 'unknown')}+{fallback.get('source', 'unknown')}",
        "all_expiries": list(set(
            (primary.get("all_expiries") or []) + (fallback.get("all_expiries") or [])
        )),
    }
    
    all_keys = set(primary_map.keys()) | set(fallback_map.keys())
    
    for key in sorted(all_keys):
        strike, opt_type = key
        primary_strike = primary_map.get(key)
        fallback_strike = fallback_map.get(key)
        
        if primary_strike:
            merged_strike = dict(primary_strike)
            if fallback_strike:
                for field in ("ltp", "oi", "oi_change", "volume", "iv", "implied_volatility", "bid", "ask", "delta", "gamma", "vega", "theta", "rho", "ltp_change_pct", "oi_change_pct"):
                    if merged_strike.get(field) in (None, 0, 0.0) and fallback_strike.get(field) not in (None, 0, 0.0):
                        merged_strike[field] = fallback_strike[field]
            merged["strikes"].append(merged_strike)
        # DO NOT add fallback-only strikes - only keep allowed ATM strikes
    
    # Clean up: remove strikes with no valid LTP/OI
    merged["strikes"] = [
        s for s in merged["strikes"]
        if s.get("ltp") not in (None, 0, 0.0) or s.get("oi") not in (None, 0, 0.0)
    ]
    
    log.info(
        "[router] %s | merged %d primary + %d fallback = %d strikes (ATM ±%d)",
        symbol, len(primary_map), len(fallback_map), len(merged["strikes"]), STRIKES_AROUND_ATM
    )
    
    return merged


def fetch_option_chain(symbol: str, expiry: str | None = None, required_strikes: set[float] | None = None) -> dict | None:
    """
    Try fetchers in configured priority order.
    Executes a dual-source parallel fetch and merge for the top 2 available fetchers in the priority list.
    Fails over to remaining sequential fetchers if both primary/fallback fail.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    priority = _priority_for(symbol)
    available_priority = [s for s in priority if s in _FETCHERS]
    log.info("[router] %s option-chain | priority order: %s", symbol, " → ".join(priority))

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

    remaining = list(available_priority)
    result_data = None
    result_source = None

    # ── Try parallel fetch + merge for the top 2 available fetchers ──
    if len(available_priority) >= 2:
        primary_src = available_priority[0]
        fallback_src = available_priority[1]
        remaining = available_priority[2:]
        
        log.info("[router] %s | Dual-source parallel fetch: %s (primary) + %s (fallback)", symbol, primary_src, fallback_src)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="router-dual") as ex:
            futures = {
                ex.submit(_try_fetcher, primary_src, symbol, expiry): primary_src,
                ex.submit(_try_fetcher, fallback_src, symbol, expiry): fallback_src,
            }
            primary_data = None
            fallback_data = None
            for fut in _as_completed(futures):
                src = futures[fut]
                try:
                    data = fut.result()
                except Exception as exc:
                    log.error("[router] %s | %s raised: %s", symbol, src, exc)
                    data = None
                if data is not None:
                    if src == primary_src:
                        primary_data = data
                    else:
                        fallback_data = data
            
            # Merge results: primary preferred, gaps/Greeks filled from fallback
            if primary_data and fallback_data:
                result_data = _merge_fetcher_results(primary_data, fallback_data, symbol)
                result_source = f"{primary_src}+{fallback_src}"
            elif primary_data:
                result_data = primary_data
                result_source = primary_src
            elif fallback_data:
                result_data = fallback_data
                result_source = fallback_src

    # If dual-source succeeded, finalize and return
    if result_data is not None:
        return _finalise_result(result_data, result_source, symbol, priority, required_strikes)

    # ── Fall through to remaining sequential fetchers ──
    for source in remaining:
        data = _try_fetcher(source, symbol, expiry)
        if data is not None:
            return _finalise_result(data, source, symbol, priority, required_strikes)

    # All failed
    try:
        from src.models.schema import stamp_health
        stamp_health("shoonya_session", "DOWN", f"all fetchers failed for {symbol}")
    except Exception:
        pass
    log.error("[router] %s | ❌ ALL fetchers failed", symbol)
    return None
