"""
Dhan API v2 Option Chain Fetcher (primary source).
Docs: https://dhanhq.co/docs/v2/option-chain/
"""
import logging
from datetime import datetime, timezone
from config.settings import (
    DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, DHAN_BASE_URL,
    DHAN_SECURITY_IDS
)
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}


def _safe_float(value, default: float = 0.0) -> float:
    """Convert API numeric values safely without crashing normalisation."""
    try:
        if value is None:
            return default
        s = str(value).replace(",", "").strip()
        if not s or s in {"—", "-", "--", "NA", "N/A", "null", "None"}:
            return default
        return float(s)
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    """Convert API integer-ish values safely."""
    try:
        return int(round(_safe_float(value, float(default))))
    except Exception:
        return default


class DhanFetcher(BaseFetcher):
    name = "dhan"

    def __init__(self):
        super().__init__()
        self.session.headers.update({
            "access-token": DHAN_ACCESS_TOKEN,
            "client-id":    DHAN_CLIENT_ID,
            "Content-Type": "application/json",
        })

    def _nearest_expiry(self, expiries: list[str]) -> str | None:
        """Return soonest expiry >= today; safely handle empty/malformed lists."""
        if not expiries:
            return None

        today = datetime.now(timezone.utc).date()
        valid: list[str] = []
        parsed_any: list[str] = []

        for e in expiries:
            try:
                d = datetime.strptime(str(e), "%Y-%m-%d").date()
                parsed_any.append(str(e))
                if d >= today:
                    valid.append(str(e))
            except Exception:
                log.debug("[dhan] ignoring malformed expiry value: %r", e)

        if valid:
            return sorted(valid)[0]
        if parsed_any:
            return sorted(parsed_any)[0]

        # Fallback to first raw value only if nothing parsed but list is non-empty.
        return str(expiries[0]) if expiries else None

    def fetch_option_chain(self, symbol: str) -> dict | None:
        security_id = DHAN_SECURITY_IDS.get(symbol)
        if not security_id:
            log.warning("[dhan] No security_id configured for %s", symbol)
            return None

        instrument = "IDX" if symbol in INDEX_SYMBOLS else "STK"
        payload = {"UnderlyingScrip": security_id, "UnderlyingSeg": instrument}

        url = f"{DHAN_BASE_URL}/optionchain"
        try:
            r = self.session.post(url, json=payload, timeout=15)
            r.raise_for_status()
            raw = r.json()
        except Exception as exc:
            log.error("[dhan] fetch failed for %s: %s", symbol, exc)
            return None

        return self._normalise(symbol, raw)

    def _normalise(self, symbol: str, raw: dict) -> dict | None:
        try:
            data = raw.get("data", {}) if isinstance(raw, dict) else {}
            if not isinstance(data, dict):
                log.warning("[dhan] malformed response for %s: data is not dict", symbol)
                return None

            underlying = _safe_float(data.get("last_price"), 0.0)
            expiries = data.get("expiry_list", []) or []
            expiry = self._nearest_expiry(expiries)
            if not expiry:
                log.warning("[dhan] no expiry returned for %s", symbol)
                return None

            oc_root = data.get("oc_data", {}) or {}
            oc_data = oc_root.get(expiry, []) if isinstance(oc_root, dict) else []
            if not isinstance(oc_data, list):
                log.warning("[dhan] malformed oc_data for %s expiry=%s", symbol, expiry)
                return None

            strikes = []
            for entry in oc_data:
                if not isinstance(entry, dict):
                    continue
                strike = _safe_float(entry.get("strike_price"), 0.0)
                if strike <= 0:
                    continue

                for ot in ("CE", "PE"):
                    opt = entry.get(ot, {}) or {}
                    if not isinstance(opt, dict) or not opt:
                        continue
                    strikes.append({
                        "strike":      strike,
                        "option_type": ot,
                        "ltp":         _safe_float(opt.get("last_price"), 0.0),
                        "oi":          _safe_int(opt.get("oi"), 0),
                        "oi_change":   _safe_int(opt.get("oi_change"), 0),
                        "volume":      _safe_int(opt.get("volume"), 0),
                        "iv":          _safe_float(opt.get("implied_volatility"), 0.0),
                        "bid":         _safe_float(opt.get("bid_price"), 0.0),
                        "ask":         _safe_float(opt.get("ask_price"), 0.0),
                    })

            if not strikes:
                log.warning("[dhan] empty option chain after normalise for %s expiry=%s", symbol, expiry)
                return None

            return {
                "symbol":           symbol,
                "underlying_price": underlying,
                "expiry":           expiry,
                "strikes":          strikes,
                "source":           self.name,
            }
        except Exception as exc:
            log.exception("[dhan] normalise failed for %s: %s", symbol, exc)
            return None
