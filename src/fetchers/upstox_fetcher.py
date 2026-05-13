"""
Upstox API v2 Option Chain Fetcher — redundant/tertiary source.
Docs: https://upstox.com/developer/api-documentation/option-chain
"""
import logging
from datetime import datetime, timezone
from config.settings import UPSTOX_ACCESS_TOKEN, UPSTOX_BASE_URL
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

# Upstox instrument keys for indices
INSTRUMENT_KEYS = {
    "NIFTY":     "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY":  "NSE_INDEX|Nifty Fin Service",
}


class UpstoxFetcher(BaseFetcher):
    name = "upstox"

    def __init__(self):
        super().__init__()
        self.session.headers.update({
            "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
            "Accept":        "application/json",
        })

    def _get_expiries(self, instrument_key: str) -> list[str]:
        url = f"{UPSTOX_BASE_URL}/option/contract"
        data = self._get(url, params={"instrument_key": instrument_key})
        if not data:
            return []
        expiries = list({r.get("expiry") for r in data.get("data", []) if r.get("expiry")})
        return sorted(expiries)

    def _nearest_expiry(self, expiries: list[str]) -> str:
        today = datetime.now(timezone.utc).date()
        for exp in expiries:
            try:
                if datetime.strptime(exp, "%Y-%m-%d").date() >= today:
                    return exp
            except ValueError:
                continue
        return expiries[0] if expiries else ""

    def fetch_option_chain(self, symbol: str) -> dict | None:
        instrument_key = INSTRUMENT_KEYS.get(symbol)
        if not instrument_key:
            log.warning("[upstox] No instrument_key for %s", symbol)
            return None

        expiries = self._get_expiries(instrument_key)
        if not expiries:
            return None
        expiry = self._nearest_expiry(expiries)

        url = f"{UPSTOX_BASE_URL}/option/chain"
        raw = self._get(url, params={"instrument_key": instrument_key, "expiry_date": expiry})
        if not raw:
            return None
        return self._normalise(symbol, expiry, raw)

    def _normalise(self, symbol: str, expiry: str, raw: dict) -> dict | None:
        try:
            records    = raw.get("data", [])
            underlying = float(raw.get("underlying_spot_price", 0) or 0)
            strikes    = []
            for record in records:
                strike = float(record.get("strike_price", 0))
                for ot in ("call_options", "put_options"):
                    side = "CE" if ot == "call_options" else "PE"
                    opt  = record.get(ot, {})
                    if not opt:
                        continue
                    md = opt.get("market_data", {})
                    greeks = opt.get("option_greeks", {})
                    strikes.append({
                        "strike":      strike,
                        "option_type": side,
                        "ltp":         float(md.get("ltp", 0) or 0),
                        "oi":          int(md.get("oi", 0) or 0),
                        "oi_change":   int(md.get("oi_change", 0) or 0),
                        "volume":      int(md.get("volume", 0) or 0),
                        "iv":          float(greeks.get("iv", 0) or 0),
                        "bid":         float(md.get("bid_price", 0) or 0),
                        "ask":         float(md.get("ask_price", 0) or 0),
                    })
            return {
                "symbol":           symbol,
                "underlying_price": underlying,
                "expiry":           expiry,
                "strikes":          strikes,
                "source":           self.name,
            }
        except Exception as exc:
            log.error("[upstox] normalise failed for %s: %s", symbol, exc)
            return None
