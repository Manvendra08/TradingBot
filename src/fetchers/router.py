"""
Fetcher Router — tries sources in FETCHER_PRIORITY order.
Returns first successful result; logs fallback events.

Thread-safety: _instances dict is guarded by _lock so APScheduler concurrent
jobs cannot create duplicate fetcher instances.
"""
import logging
import threading
from config.settings import FETCHER_PRIORITY
from src.fetchers.dhan_fetcher import DhanFetcher
from src.fetchers.nse_fetcher import NSEPublicFetcher
from src.fetchers.upstox_fetcher import UpstoxFetcher
from src.fetchers.paytm_fetcher import PaytmFetcher

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

_FETCHERS = {
    "paytm":       PaytmFetcher,
    "dhan":        DhanFetcher,
    "nse_public":  NSEPublicFetcher,
    "upstox":      UpstoxFetcher,
}
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


def fetch_option_chain(symbol: str) -> dict | None:
    """
    Try fetchers in configured priority order.
    Returns normalised dict or None if all fail.
    """
    for source in FETCHER_PRIORITY:
        if source not in _FETCHERS:
            log.warning("Fetcher '%s' unavailable; skipping", source)
            continue
        fetcher = _get_fetcher(source)
        try:
            result = fetcher.fetch_option_chain(symbol)
            if result and result.get("strikes"):
                if source != FETCHER_PRIORITY[0]:
                    log.warning("Fallback active: using '%s' for %s", source, symbol)
                return result
        except Exception as exc:
            log.error("Fetcher '%s' raised exception for %s: %s", source, symbol, exc)
    log.error("ALL fetchers failed for symbol: %s", symbol)
    return None
