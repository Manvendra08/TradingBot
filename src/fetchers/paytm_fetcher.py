"""
Paytm Money Option Chain Fetcher (Open API)
Config: GET /fno/v1/option-chain/config?symbol={symbol}
Chain:  GET /fno/v1/option-chain?symbol={symbol}&expiry={expiry}&type={type} (type=CALL/PUT)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from config.settings import _optional_env
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

BASE_URL = "https://developer.paytmmoney.com"
IST = timezone(timedelta(hours=5, minutes=30))

# Paytm API segment paths for option chains.
# NSE F&O is served under /fno/v1/ , BSE F&O under /bse-fo/v1/
_SEGMENT_PATH: dict[str, str] = {
    "NIFTY": "/fno/v1",
    "BANKNIFTY": "/fno/v1",
    "FINNIFTY": "/fno/v1",
    "MIDCPNIFTY": "/fno/v1",
    "SENSEX": "/bse-fo/v1",
}
_DEFAULT_SEGMENT = "/fno/v1"


class PaytmFetcher(BaseFetcher):
    name = "paytm"

    def __init__(self):
        super().__init__()
        self._jwt_token: str = _optional_env("PAYTM_JWT_TOKEN")
        self._api_key: str = _optional_env("PAYTM_API_KEY")
        self._api_secret: str = _optional_env("PAYTM_API_SECRET")
        # Config cache: symbol -> list of expiry strings "dd-mm-yyyy"
        self._expiry_cache: dict[str, list[str]] = {}

    def _auth_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-jwt-token": self._jwt_token,
        }

    def _refresh_token(self, request_token: str) -> bool:
        """Exchange request_token for JWT. Call once manually, store in .env."""
        try:
            r = self.session.post(
                f"{BASE_URL}/accounts/v2/gettoken",
                json={
                    "api_key": self._api_key,
                    "api_secret_key": self._api_secret,
                    "request_token": request_token,
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

    def _get_expiries(self, symbol: str) -> list[str]:
        if symbol in self._expiry_cache:
            return self._expiry_cache[symbol]
        base = symbol.upper().split()[0]
        segment = _SEGMENT_PATH.get(base, _DEFAULT_SEGMENT)
        try:
            r = self.session.get(
                f"{BASE_URL}{segment}/option-chain/config",
                headers=self._auth_headers(),
                params={"symbol": base},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

            # Response: {"data": {"exch_symbol": "...", "expires": [timestamp1, ...]}}
            expires_list = data.get("data", {}).get("expires") or []
            expiries = []
            for expiry_val in expires_list:
                if isinstance(expiry_val, int):
                    # Epoch timestamp in milliseconds
                    dt = datetime.fromtimestamp(expiry_val / 1000, tz=timezone.utc)
                    # Convert to local timezone date and format as dd-mm-yyyy
                    dt_local = dt.astimezone(IST)
                    expiries.append(dt_local.strftime("%d-%m-%Y"))
                elif expiry_val:
                    expiries.append(str(expiry_val))

            self._expiry_cache[symbol] = expiries
            return expiries
        except Exception as exc:
            log.warning("[paytm] expiry config fetch failed for %s: %s", symbol, exc)
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

    def fetch_option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
        if not self._jwt_token:
            log.warning("[paytm] PAYTM_JWT_TOKEN not set — skipping")
            return None

        base = symbol.upper().split()[0]

        # Get expiries
        expiries = self._get_expiries(base)
        if not expiries:
            log.warning("[paytm] no expiry found for %s", base)
            return None

        # Expiry format expected: "dd-mm-yyyy"
        if expiry:
            # If standard yyyy-mm-dd is passed, convert to dd-mm-yyyy
            try:
                dt = datetime.strptime(expiry, "%Y-%m-%d")
                expiry_str = dt.strftime("%d-%m-%Y")
            except ValueError:
                expiry_str = expiry
        else:
            expiry_str = self._nearest_expiry(expiries)

        if not expiry_str:
            log.warning("[paytm] no nearest expiry found for %s", base)
            return None

        log.info("[paytm] fetching %s option chain for expiry %s", base, expiry_str)

        segment = _SEGMENT_PATH.get(base, _DEFAULT_SEGMENT)

        # We must call both CALL and PUT option types and merge them
        try:
            # Fetch CALL options
            call_res = self.session.get(
                f"{BASE_URL}{segment}/option-chain",
                headers=self._auth_headers(),
                params={
                    "symbol": base,
                    "expiry": expiry_str,
                    "type": "CALL",
                },
                timeout=15,
            )
            call_res.raise_for_status()
            call_data = call_res.json()

            # Fetch PUT options
            put_res = self.session.get(
                f"{BASE_URL}{segment}/option-chain",
                headers=self._auth_headers(),
                params={
                    "symbol": base,
                    "expiry": expiry_str,
                    "type": "PUT",
                },
                timeout=15,
            )
            put_res.raise_for_status()
            put_data = put_res.json()

        except Exception as exc:
            log.error(
                "[paytm] request failed for %s expiry %s: %s", base, expiry_str, exc
            )
            return None

        return self._normalise(base, expiry_str, call_data, put_data)

    def _normalise(
        self, symbol: str, expiry_str: str, call_raw: dict, put_raw: dict
    ) -> dict | None:
        try:
            call_records = call_raw.get("data", {}).get("results") or []
            put_records = put_raw.get("data", {}).get("results") or []

            # Extract underlying price from the first available record
            underlying = 0.0
            all_records = call_records + put_records
            if all_records:
                first_rec = all_records[0]
                try:
                    underlying = float(first_rec.get("spot_price") or 0)
                except ValueError:
                    pass

            # Convert expiry "dd-mm-yyyy" -> "yyyy-mm-dd"
            try:
                expiry_iso = datetime.strptime(expiry_str, "%d-%m-%Y").strftime(
                    "%Y-%m-%d"
                )
            except ValueError:
                expiry_iso = expiry_str

            # Parse strikes
            strikes = []

            # Map strikes to Ce/Pe details
            for rec in all_records:
                try:
                    strike = float(rec.get("stk_price") or 0)
                except (ValueError, TypeError):
                    continue

                ot = rec.get("option_type")
                if ot not in ("CE", "PE"):
                    continue

                try:
                    ltp = float(rec.get("price") or 0)
                except ValueError:
                    ltp = 0.0

                try:
                    oi = int(rec.get("oi") or 0)
                except ValueError:
                    oi = 0

                try:
                    oi_change = int(rec.get("oi_net_chg") or 0)
                except ValueError:
                    oi_change = 0

                try:
                    volume = int(rec.get("traded_vol") or 0)
                except ValueError:
                    volume = 0

                try:
                    iv = float(rec.get("iv") or 0)
                except ValueError:
                    iv = 0.0

                strikes.append(
                    {
                        "strike": strike,
                        "option_type": ot,
                        "ltp": ltp,
                        "oi": oi,
                        "oi_change": oi_change,
                        "volume": volume,
                        "iv": iv,
                        "bid": 0.0,
                        "ask": 0.0,
                    }
                )

            if not strikes:
                log.warning("[paytm] no strikes parsed for %s", symbol)
                return None

            return {
                "symbol": symbol,
                "underlying_price": underlying,
                "expiry": expiry_iso,
                "strikes": strikes,
                "source": self.name,
            }
        except Exception as exc:
            log.error("[paytm] normalise failed for %s: %s", symbol, exc)
            return None
