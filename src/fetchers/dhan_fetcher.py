"""
Dhan API v2 Option Chain Fetcher (primary source).
Docs: https://dhanhq.co/docs/v2/option-chain/
"""
import csv
import io
import logging
from datetime import datetime, timezone
from functools import lru_cache
from config.settings import (
    DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, DHAN_BASE_URL,
    DHAN_SECURITY_IDS, DHAN_SEGMENTS
)
from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


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

    def _base_symbol(self, symbol: str) -> str:
        return str(symbol or "").upper().strip().split()[0]

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

    def _get_expiries(self, payload: dict) -> list[str]:
        url = f"{DHAN_BASE_URL}/optionchain/expirylist"
        try:
            r = self.session.post(url, json=payload, timeout=15)
            r.raise_for_status()
            raw = r.json()
        except Exception as exc:
            log.error("[dhan] expirylist failed for %s/%s: %s",
                      payload.get("UnderlyingSeg"), payload.get("UnderlyingScrip"), exc)
            return []

        data = raw.get("data", []) if isinstance(raw, dict) else []
        return data if isinstance(data, list) else []

    def _fallback_commodity(self, symbol: str, reason: str) -> dict | None:
        base_symbol = self._base_symbol(symbol)
        if DHAN_SEGMENTS.get(base_symbol) != "MCX_COMM":
            return None

        log.warning("[dhan] falling back to public commodity scraper for %s: %s", symbol, reason)
        try:
            return DhanCommodityFetcher().fetch_option_chain(symbol)
        except Exception as exc:
            log.error("[dhan] public commodity fallback failed for %s: %s", symbol, exc)
            return None

    @staticmethod
    @lru_cache(maxsize=16)
    def _resolve_mcx_future_security_id(base_symbol: str) -> int | None:
        try:
            import requests
            r = requests.get(DHAN_MASTER_URL, timeout=30)
            r.raise_for_status()
            rows = csv.DictReader(io.StringIO(r.text))
            today = datetime.now(timezone.utc).date()
            candidates: list[tuple] = []
            for row in rows:
                name = (row.get("SEM_TRADING_SYMBOL") or "").upper()
                custom = (row.get("SEM_CUSTOM_SYMBOL") or "").upper()
                if row.get("SEM_EXM_EXCH_ID") != "MCX":
                    continue
                if row.get("SEM_INSTRUMENT_NAME") != "FUTCOM":
                    continue
                if not (name.startswith(f"{base_symbol}-") or custom.startswith(f"{base_symbol} ")):
                    continue
                try:
                    expiry = datetime.strptime(
                        row.get("SEM_EXPIRY_DATE", ""), "%Y-%m-%d %H:%M:%S"
                    ).date()
                    security_id = int(row.get("SEM_SMST_SECURITY_ID") or 0)
                except Exception:
                    continue
                if security_id and expiry >= today:
                    candidates.append((expiry, security_id))
            if candidates:
                return sorted(candidates, key=lambda item: item[0])[0][1]
        except Exception as exc:
            log.warning("[dhan] MCX master lookup failed for %s: %s", base_symbol, exc)
        return None

    def fetch_option_chain(self, symbol: str) -> dict | None:
        base_symbol = self._base_symbol(symbol)
        segment = DHAN_SEGMENTS.get(base_symbol, "NSE_FNO")
        security_id = DHAN_SECURITY_IDS.get(base_symbol)
        if not security_id and segment == "MCX_COMM":
            security_id = self._resolve_mcx_future_security_id(base_symbol)
        if not security_id:
            log.warning("[dhan] No security_id configured for %s", symbol)
            return self._fallback_commodity(symbol, "missing security_id")

        base_payload = {"UnderlyingScrip": security_id, "UnderlyingSeg": segment}
        expiry = self._nearest_expiry(self._get_expiries(base_payload))
        if not expiry:
            log.warning("[dhan] no expiry returned for %s", symbol)
            return self._fallback_commodity(symbol, "no expiry returned")

        payload = {**base_payload, "Expiry": expiry}

        url = f"{DHAN_BASE_URL}/optionchain"
        try:
            r = self.session.post(url, json=payload, timeout=15)
            r.raise_for_status()
            raw = r.json()
        except Exception as exc:
            log.error("[dhan] fetch failed for %s: %s", symbol, exc)
            return self._fallback_commodity(symbol, f"api fetch failed: {exc}")

        result = self._normalise(base_symbol, raw, expiry)
        if result:
            return result

        return self._fallback_commodity(symbol, "normalise returned empty result")

    def _normalise(self, symbol: str, raw: dict, requested_expiry: str | None = None) -> dict | None:
        try:
            data = raw.get("data", {}) if isinstance(raw, dict) else {}
            if not isinstance(data, dict):
                log.warning("[dhan] malformed response for %s: data is not dict", symbol)
                return None

            underlying = _safe_float(data.get("last_price"), 0.0)
            expiry = requested_expiry or self._nearest_expiry(data.get("expiry_list", []) or [])
            strikes = self._normalise_current_oc(data.get("oc", {}))
            if not strikes:
                strikes = self._normalise_legacy_oc(data.get("oc_data", {}), expiry)

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

    def _normalise_current_oc(self, oc_data: dict) -> list[dict]:
        """Normalise Dhan's documented data.oc strike dictionary."""
        if not isinstance(oc_data, dict):
            return []

        strikes: list[dict] = []
        for strike_key, entry in oc_data.items():
            if not isinstance(entry, dict):
                continue
            strike = _safe_float(strike_key, 0.0)
            if strike <= 0:
                continue
            for raw_side, side in (("ce", "CE"), ("pe", "PE")):
                opt = entry.get(raw_side, {}) or {}
                if not isinstance(opt, dict) or not opt:
                    continue
                oi = _safe_int(opt.get("oi"), 0)
                prev_oi = _safe_int(opt.get("previous_oi"), oi)
                strikes.append({
                    "strike":      strike,
                    "option_type": side,
                    "ltp":         _safe_float(opt.get("last_price"), 0.0),
                    "oi":          oi,
                    "oi_change":   oi - prev_oi,
                    "volume":      _safe_int(opt.get("volume"), 0),
                    "iv":          _safe_float(opt.get("implied_volatility"), 0.0),
                    "bid":         _safe_float(opt.get("top_bid_price"), 0.0),
                    "ask":         _safe_float(opt.get("top_ask_price"), 0.0),
                    "delta":       _safe_float((opt.get("greeks") or {}).get("delta"), 0.0),
                })
        return strikes

    def _normalise_legacy_oc(self, oc_root: dict, expiry: str | None) -> list[dict]:
        """Keep support for the older expiry-keyed shape used by earlier code."""
        oc_data = oc_root.get(expiry, []) if isinstance(oc_root, dict) and expiry else []
        if not isinstance(oc_data, list):
            return []

        strikes: list[dict] = []
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
                    "delta":       _safe_float(opt.get("delta"), 0.0),
                })
        return strikes
