"""
Fetcher Router — tries sources in FETCHER_PRIORITY order.
Returns first successful result; logs fallback events.
"""
import logging
from config.settings import FETCHER_PRIORITY
from src.fetchers.dhan_fetcher import DhanFetcher
from src.fetchers.nse_fetcher import NSEPublicFetcher
from src.fetchers.upstox_fetcher import UpstoxFetcher
from src.fetchers.scrapegraph_fetcher import ScrapeGraphFetcher

log = logging.getLogger(__name__)

_FETCHERS = {
    "dhan":       DhanFetcher,
    "nse_public": NSEPublicFetcher,
    "scrapegraph": ScrapeGraphFetcher,
    "upstox":     UpstoxFetcher,
}

# Singleton instances
_instances: dict = {}


def _get_fetcher(name: str):
    if name not in _instances:
        _instances[name] = _FETCHERS[name]()
    return _instances[name]


def fetch_option_chain(symbol: str) -> dict | None:
    """
    Try fetchers in configured priority order.
    Returns normalised dict or None if all fail.
    """
    for source in FETCHER_PRIORITY:
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
