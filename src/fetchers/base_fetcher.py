"""Abstract base fetcher with retry + exponential backoff."""
import abc
import logging
import time
import requests
from config.settings import HTTP_TIMEOUT_SECONDS, HTTP_MAX_RETRIES, HTTP_BACKOFF_FACTOR

log = logging.getLogger(__name__)


class BaseFetcher(abc.ABC):
    name: str = "base"

    def __init__(self):
        self.session = requests.Session()

    def _get(self, url: str, params: dict = None, headers: dict = None) -> dict | None:
        last_exc = None
        for attempt in range(1, HTTP_MAX_RETRIES + 1):
            try:
                r = self.session.get(
                    url, params=params, headers=headers,
                    timeout=HTTP_TIMEOUT_SECONDS
                )
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                wait = HTTP_BACKOFF_FACTOR ** attempt
                log.warning("[%s] attempt %d/%d failed: %s — retry in %ds",
                            self.name, attempt, HTTP_MAX_RETRIES, exc, wait)
                time.sleep(wait)
        log.error("[%s] all %d retries exhausted: %s", self.name, HTTP_MAX_RETRIES, last_exc)
        return None

    @abc.abstractmethod
    def fetch_option_chain(self, symbol: str) -> dict | None:
        """
        Returns normalised dict:
        {
          "symbol": str,
          "underlying_price": float,
          "expiry": str (YYYY-MM-DD),
          "strikes": [
            {
              "strike": float,
              "option_type": "CE"|"PE",
              "ltp": float, "oi": int, "oi_change": int,
              "volume": int, "iv": float, "bid": float, "ask": float,
            }, ...
          ]
        }
        Returns None on unrecoverable failure.
        """
        ...
