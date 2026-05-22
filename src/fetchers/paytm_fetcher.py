"""
Paytm Money Option Chain Fetcher
Auth: POST /accounts/v2/gettoken → x-jwt-token
Chain: GET /primary-market/v1/optionchain
Config: GET /primary-market/v1/optionchain/config  (expiry discovery)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from config.settings import _optional_env
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

BASE_URL = "https://developer.paytmmoney.com"
IST = timezone(timedelta(hours=5, minutes=30))

# underlying_id map — GET /primary-market/v1/optionchain/config for full list
_UNDERLYING_IDS: dict[str, tuple[str, str]] = {
    "NIFTY":       ("13",  "INDEX"),
    "BANKNIFTY":   ("25",  "INDEX"),
    "FINNIFTY":    ("27",  "INDEX"),
    "MIDCPNIFTY":  ("442", "INDEX"),
    "SENSEX":      ("51",  "INDEX"),
    "NATURALGAS":  ("488505", "INDEX"),
    "CRUDEOIL":    ("499095", "INDEX"),
}


class PaytmFetcher(BaseFetcher):
    name = "paytm"

    def __init__(self):
        super().__init__()
        self._jwt_token: str = _optional_env("PAYTM_JWT_TOKEN")
        self._api_key: str = _optional_env("PAYTM_API_KEY")
        self._api_secret: str = _optional_env("PAYTM_API_SECRET")
        # Config cache: symbol -> list of expiry strings "dd-mm-yyyy"
        self._expiry_cache: dict[str, list[str]] = {}

    # ── Auth ────────────────────────────────────────────────────────────────

    def _auth_headers(self) -> dict:
        return {
            "Content-Type":  "application/json",
            "x-jwt-token":   self._jwt_token,
        }

    def _refresh_token(self, request_token: str) -> bool:
        """Exchange request_token for JWT. Call once manually, store in .env."""
        try:
            r = self.session.post(
                f"{BASE_URL}/accounts/v2/gettoken",
                json={
                    "api_key":        self._api_key,
                    "api_secret_key": self._api_secret,
                    "request_token":  request_token,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            token = data.get("data", {}).get("access_token") or data.get("access_token")
            if token:
                self._jwt_token = token
                log.info("[paytm] token refreshed")
                return True
            log.error("[paytm] token refresh: no access_token in response: %s", data)
            return False
        except Exception as exc:
            log.error("[paytm] token refresh failed: %s", exc)
            return False

    # ── Config / Expiry discovery ────────────────────────────────────────────

    def _get_expiries(self, symbol: str, underlying_id: str) -> list[str]:
        if symbol in self._expiry_cache:
            return self._expiry_cache[symbol]
        try:
            r = self.session.get(
                f"{BASE_URL}/primary-market/v1/optionchain/config",
                headers=self._auth_headers(),
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            # Response shape: {"data": {"expiry": [...], ...}} or {"expiry": [...]}
            expiries = (
                data.get("data", {}).get("expiry")
                or data.get("expiry")
                or []
            )
            # Filter to string list; API returns "dd-mm-yyyy"
            expiries = [str(e) for e in expiries if e]
            self._expiry_cache[symbol] = expiries
            return expiries
        except Exception as exc:
            log.warning("[paytm] expiry config fetch failed: %s", exc)
            return []

    def _nearest_expiry(self, expiries: list[str]) -> str:
        """Pick nearest future expiry from dd-mm-yyyy list."""
        today = datetime.now(IST).date()
        parsed = []
        for e in expiries:
            try:
                parsed.append((datetime.strptime(e, "%d-%m-%Y").date(), e))
            except ValueError:
                continue
        future = sorted((p for p in parsed if p[0] >= today), key=lambda x: x[0])
        return future[0][1] if future else (expiries[0] if expiries else "")

    # ── Fetch ────────────────────────────────────────────────────────────────

    def fetch_option_chain(self, symbol: str) -> dict | None:
        if not self._jwt_token:
            log.warning("[paytm] PAYTM_JWT_TOKEN not set — skipping")
            return None

        base = symbol.upper().split()[0]
        meta = _UNDERLYING_IDS.get(base)
        if not meta:
            log.warning("[paytm] unknown symbol %s", base)
            return None

        underlying_id, underlying_type = meta

        expiries = self._get_expiries(base, underlying_id)
        expiry_str = self._nearest_expiry(expiries)  # "dd-mm-yyyy"
        if not expiry_str:
            log.warning("[paytm] no expiry found for %s", base)
            return None

        try:
            r = self.session.get(
                f"{BASE_URL}/primary-market/v1/optionchain",
                headers=self._auth_headers(),
                params={
                    "underlying_id":   underlying_id,
                    "underlying_type": underlying_type,
                    "expiry":          expiry_str,
                },
                timeout=15,
            )
            r.raise_for_status()
            raw = r.json()
        except Exception as exc:
            log.error("[paytm] option chain request failed for %s: %s", base, exc)
            return None

        return self._normalise(base, expiry_str, raw)

    # ── Normalise ────────────────────────────────────────────────────────────

    def _normalise(self, symbol: str, expiry_str: str, raw: dict) -> dict | None:
        try:
            data = raw.get("data") or raw
            # Underlying price — try common keys
            underlying = float(
                data.get("underlyingValue")
                or data.get("underlying_value")
                or data.get("ltp")
                or 0
            )

            records = (
                data.get("optionChainData")
                or data.get("option_chain_data")
                or data.get("records")
                or []
            )

            # Convert expiry "dd-mm-yyyy" -> "yyyy-mm-dd"
            try:
                expiry_iso = datetime.strptime(expiry_str, "%d-%m-%Y").strftime("%Y-%m-%d")
            except ValueError:
                expiry_iso = expiry_str

            strikes = []
            for rec in records:
                strike = float(rec.get("strikePrice") or rec.get("strike_price") or 0)
                for ot, key in (("CE", "callOption"), ("PE", "putOption")):
                    opt = rec.get(key) or rec.get(ot) or {}
                    if not opt:
                        continue
                    strikes.append({
                        "strike":      strike,
                        "option_type": ot,
                        "ltp":         float(opt.get("lastPrice") or opt.get("ltp") or 0),
                        "oi":          int(opt.get("openInterest") or opt.get("oi") or 0),
                        "oi_change":   int(opt.get("changeinOpenInterest") or opt.get("oi_change") or 0),
                        "volume":      int(opt.get("totalTradedVolume") or opt.get("volume") or 0),
                        "iv":          float(opt.get("impliedVolatility") or opt.get("iv") or 0),
                        "bid":         float(opt.get("bidPrice") or opt.get("bid") or 0),
                        "ask":         float(opt.get("askPrice") or opt.get("ask") or 0),
                    })

            if not strikes:
                log.warning("[paytm] no strikes parsed for %s", symbol)
                return None

            return {
                "symbol":           symbol,
                "underlying_price": underlying,
                "expiry":           expiry_iso,
                "strikes":          strikes,
                "source":           self.name,
            }
        except Exception as exc:
            log.error("[paytm] normalise failed for %s: %s", symbol, exc)
            return None
