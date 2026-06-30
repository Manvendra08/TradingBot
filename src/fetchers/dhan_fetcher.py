"""
Dhan API v2 Option Chain Fetcher (primary source).
Docs: https://dhanhq.co/docs/v2/option-chain/
Now migrated to ScanX public URL to avoid auth token expiration.
"""
import csv
import io
import logging
from datetime import datetime, timezone
from functools import lru_cache
from config.settings import (
    DHAN_SECURITY_IDS, DHAN_SEGMENTS
)
from src.utils.dhan_resolver import get_dhan_security_id
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
        # Removed auth headers since we now use public ScanX API
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://dhan.co",
            "Referer": "https://dhan.co/",
        })

    def _base_symbol(self, symbol: str) -> str:
        return str(symbol or "").upper().strip().split()[0]

    def _nearest_expiry(self, expiries: list[str], symbol: str = "") -> str | None:
        """Return soonest expiry >= today; strictly weekly for NIFTY, monthly for others."""
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
            valid = sorted(valid)
            base_sym = self._base_symbol(symbol)
            if base_sym != "NIFTY":
                from collections import defaultdict
                month_groups = defaultdict(list)
                for v in valid:
                    d = datetime.strptime(v, "%Y-%m-%d").date()
                    month_groups[(d.year, d.month)].append(d)
                
                monthly_expiries = []
                for (y, m), d_list in month_groups.items():
                    monthly_expiries.append(max(d_list))
                
                monthly_expiries.sort()
                return monthly_expiries[0].strftime("%Y-%m-%d") if monthly_expiries else valid[0]
            
            return valid[0]
        if parsed_any:
            return sorted(parsed_any)[0]

        # Fallback to first raw value only if nothing parsed but list is non-empty.
        return str(expiries[0]) if expiries else None

    def _fallback_commodity(self, symbol: str, reason: str) -> dict | None:
        base_symbol = self._base_symbol(symbol)
        if DHAN_SEGMENTS.get(base_symbol) != "MCX_COMM":
            return None

        log.warning("[dhan] commodity fetch failed for %s: %s. Returning None for router fallback.", symbol, reason)
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

    def fetch_option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
        base_symbol = self._base_symbol(symbol)
        segment = DHAN_SEGMENTS.get(base_symbol, "NSE_FNO")
        security_id = get_dhan_security_id(base_symbol, target_expiry=expiry)
        if not security_id and segment == "MCX_COMM":
            security_id = self._resolve_mcx_future_security_id(base_symbol)
        if not security_id:
            log.warning("[dhan] No security_id configured for %s", symbol)
            return self._fallback_commodity(symbol, "missing security_id")

        # Map to ScanX segment. 0 = NSE Indices, 5 = MCX, 1 = NSE Equity
        scanx_seg = 0
        if segment == "MCX_COMM":
            scanx_seg = 5
        elif segment == "BSE_IND":
            scanx_seg = 3

        scanx_url = "https://open-web-scanx.dhan.co/scanx/optchainactive"
        
        # 1. Fetch expirylist
        fl_payload = {"Data": {"Seg": scanx_seg, "Sid": int(security_id), "Exp": 0}}
        try:
            r = self.session.post(scanx_url, json=fl_payload, timeout=15)
            r.raise_for_status()
            fl_data = r.json()
        except Exception as exc:
            log.warning("[dhan] ScanX expirylist fetch failed for %s: %s", symbol, exc)
            return self._fallback_commodity(symbol, f"ScanX api fetch failed: {exc}")

        from src.fetchers.dhan_commodity_fetcher import _julian_1980_to_expiry_iso, _normalise_scanx_oc
        
        fl_dict = (fl_data.get("data") or {}).get("fl", {})
        expjs_list = sorted([int(k) for k in fl_dict.keys() if str(k).isdigit()])
        
        all_expiries = sorted(list(set([
            _julian_1980_to_expiry_iso(exp)
            for exp in expjs_list if exp
        ])))

        target_expiry = expiry or self._nearest_expiry(all_expiries, symbol=symbol)
        if not target_expiry:
            log.warning("[dhan] no expiry returned for %s", symbol)
            return self._fallback_commodity(symbol, "no expiry returned")

        target_expj = None
        for expj in expjs_list:
            if _julian_1980_to_expiry_iso(expj) == target_expiry:
                target_expj = expj
                break
                
        if not target_expj:
            log.warning("[dhan] Could not find expj for %s expiry %s", symbol, target_expiry)
            return self._fallback_commodity(symbol, "missing expj")

        # 2. Fetch the actual option chain
        oc_payload = {"Data": {"Seg": scanx_seg, "Sid": int(security_id), "Exp": target_expj}}
        try:
            r = self.session.post(scanx_url, json=oc_payload, timeout=15)
            r.raise_for_status()
            oc_data = r.json()
        except Exception as exc:
            log.warning("[dhan] ScanX optchainactive fetch failed for %s: %s", symbol, exc)
            return self._fallback_commodity(symbol, f"ScanX api fetch failed: {exc}")
            
        strikes = _normalise_scanx_oc(oc_data)
        if not strikes:
            log.warning("[dhan] empty option chain after normalise for %s expiry=%s", symbol, target_expiry)
            return self._fallback_commodity(symbol, "normalise returned empty result")

        underlying = _safe_float((oc_data.get("data") or {}).get("sltp"), 0.0)

        return {
            "symbol":           symbol,
            "underlying_price": underlying,
            "expiry":           target_expiry,
            "strikes":          strikes,
            "source":           self.name,
            "all_expiries":     all_expiries,
        }

