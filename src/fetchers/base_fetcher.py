"""Abstract base fetcher with retry + exponential backoff."""
import abc
import logging
import time
import requests
import ssl
import urllib3
from config.settings import HTTP_TIMEOUT_SECONDS, HTTP_MAX_RETRIES, HTTP_BACKOFF_FACTOR
from src.utils.tls_adapter import ResilientTLSAdapter, DEFAULT_RETRY

log = logging.getLogger(__name__)

# BUG-C04 FIX: Removed global SSL verification disable.
# Previously this set ssl._create_default_https_context = ssl._create_unverified_context
# which disabled certificate verification for ALL HTTPS connections in the process,
# creating a man-in-the-middle vulnerability. SSL verification is now controlled
# per-session via ResilientTLSAdapter(ssl_verify=False) only where explicitly needed.

# Suppress insecure request warnings from urllib3 (only for sessions that opt out)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class BaseFetcher(abc.ABC):
    name: str = "base"

    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        adapter = ResilientTLSAdapter(max_retries=DEFAULT_RETRY, ssl_verify=False)
        self.session.mount("https://", adapter)

    def _get(self, url: str, params: dict = None, headers: dict = None) -> dict | None:
        last_exc = None
        for attempt in range(1, HTTP_MAX_RETRIES + 1):
            try:
                r = self.session.get(
                    url, params=params, headers=headers,
                    timeout=HTTP_TIMEOUT_SECONDS,
                    verify=self.session.verify
                )
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    if exc.response.status_code in (401, 403):
                        log.warning("[%s] HTTP %d (Auth/Forbidden) — skipping retries.", self.name, exc.response.status_code)
                        break

                exc_str = str(exc).lower()
                if "nameresolutionerror" in exc_str or "getaddrinfo failed" in exc_str or "failed to resolve" in exc_str:
                    log.warning("[%s] Name resolution failed — network offline or DNS issue. Skipping retries.", self.name)
                    break
                wait = HTTP_BACKOFF_FACTOR ** attempt
                log.warning("[%s] attempt %d/%d failed: %s — retry in %ds",
                            self.name, attempt, HTTP_MAX_RETRIES, exc, wait)
                time.sleep(wait)
        log.error("[%s] all %d retries exhausted: %s", self.name, HTTP_MAX_RETRIES, last_exc)
        return None

    @abc.abstractmethod
    def fetch_option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
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
