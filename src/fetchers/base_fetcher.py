"""Abstract base fetcher with retry + exponential backoff."""
import abc
import logging
import time
import requests
import ssl
import urllib3
from config.settings import HTTP_TIMEOUT_SECONDS, HTTP_MAX_RETRIES, HTTP_BACKOFF_FACTOR, STRIKES_AROUND_ATM
from src.utils.tls_adapter import ResilientTLSAdapter, DEFAULT_RETRY

log = logging.getLogger(__name__)

def filter_atm_strikes(
    strikes: list[dict],
    underlying_price: float | None = None,
    strikes_around_atm: int = STRIKES_AROUND_ATM,
    required_strikes: set[float] | None = None,
) -> list[dict]:
    """Filter strikes list to ATM +- strikes_around_atm strikes (typically +-15 strikes).
    
    Preserves any required_strikes (e.g. from open positions).
    If underlying_price is missing or invalid, finds ATM via CE/PE LTP parity or median strike.
    """
    if not strikes:
        return strikes

    # Extract sorted unique strike prices
    strikes_list = sorted(list(set(
        s["strike"] for s in strikes
        if isinstance(s, dict) and s.get("strike") is not None
    )))
    if not strikes_list:
        return strikes

    # If count of strikes already within window and no extra required_strikes needed
    if len(strikes_list) <= (2 * strikes_around_atm + 1) and not required_strikes:
        return strikes

    atm_strike = None
    if underlying_price and underlying_price > 0:
        atm_strike = min(strikes_list, key=lambda x: abs(x - underlying_price))
    else:
        # Fallback 1: strike where CE LTP is closest to PE LTP
        strike_diffs: dict[float, dict[str, float | None]] = {}
        for s in strikes:
            if not isinstance(s, dict):
                continue
            stk = s.get("strike")
            if stk not in strike_diffs:
                strike_diffs[stk] = {"CE": None, "PE": None}
            strike_diffs[stk][s.get("option_type")] = s.get("ltp")

        valid_strikes = [
            stk for stk, v in strike_diffs.items()
            if v["CE"] is not None and v["PE"] is not None and v["CE"] > 0 and v["PE"] > 0
        ]
        if valid_strikes:
            atm_strike = min(
                valid_strikes,
                key=lambda x: abs(strike_diffs[x]["CE"] - strike_diffs[x]["PE"]),
            )
        else:
            # Fallback 2: middle strike
            atm_strike = strikes_list[len(strikes_list) // 2]

    try:
        idx = strikes_list.index(atm_strike)
        start_idx = max(0, idx - strikes_around_atm)
        end_idx = min(len(strikes_list), idx + strikes_around_atm + 1)
        kept_strikes = set(strikes_list[start_idx:end_idx])
        if required_strikes:
            kept_strikes |= set(required_strikes)
        return [s for s in strikes if isinstance(s, dict) and s.get("strike") in kept_strikes]
    except Exception as e:
        log.warning("filter_atm_strikes failed: %s; returning original strikes", e)
        return strikes


# BUG-C04 FIX: Removed global SSL verification disable.
# Previously this set ssl._create_default_https_context = ssl._create_unverified_context
# which disabled certificate verification for ALL HTTPS connections in the process,
# creating a man-in-the-middle vulnerability. SSL verification is now controlled
# per-session via ResilientTLSAdapter(ssl_verify=True) by default.
# Subclasses that absolutely MUST disable SSL verification (e.g. for internal
# endpoints with self-signed certs) can set DISABLE_SSL_VERIFICATION = True
# on their class. THIS IS A SECURITY RISK AND SHOULD BE AVOIDED.

# Suppress insecure request warnings from urllib3 (only for sessions that opt out)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class BaseFetcher(abc.ABC):
    name: str = "base"
    # Set to True ONLY if you MUST disable SSL verification (e.g. internal endpoints
    # with self-signed certificates). THIS DISABLES CERTIFICATE VALIDATION AND IS
    # A SECURITY RISK. Use only as a last resort for internal endpoints.
    DISABLE_SSL_VERIFICATION = False
    @staticmethod
    def filter_atm_strikes(
        strikes: list[dict],
        underlying_price: float | None = None,
        strikes_around_atm: int = STRIKES_AROUND_ATM,
        required_strikes: set[float] | None = None,
    ) -> list[dict]:
        return filter_atm_strikes(strikes, underlying_price, strikes_around_atm, required_strikes)


    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        
        # Determine SSL verification setting
        ssl_verify = not getattr(self, "DISABLE_SSL_VERIFICATION", False)
        self.session.verify = ssl_verify
        
        if not ssl_verify:
            log.warning(
                "[%s] SSL VERIFICATION DISABLED! This fetcher will NOT verify "
                "TLS certificates. Use only for internal endpoints with self-signed "
                "certificates. This is a SECURITY RISK.",
                self.name
            )
        
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        adapter = ResilientTLSAdapter(max_retries=DEFAULT_RETRY, ssl_verify=ssl_verify)
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
