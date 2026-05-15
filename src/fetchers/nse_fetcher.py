"""
NSE India Public JSON API — fallback fetcher.
No auth required. Session warm-up needed (cookie handshake).
Rate-limited: use only as fallback, not primary.
"""
import logging
from datetime import datetime, timezone
from config.settings import NSE_BASE_URL, NSE_OPTION_CHAIN_URL, NSE_EQUITY_OC_URL, NSE_HEADERS
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}


class NSEPublicFetcher(BaseFetcher):
    name = "nse_public"
    _session_warmed = False

    def __init__(self):
        super().__init__()
        self.session.headers.update(NSE_HEADERS)

    def _warm_session(self):
        """Hit NSE homepage to acquire cookies — required for API calls."""
        if self._session_warmed:
            return
        
        # Add basic retry for session warm-up
        for attempt in range(3):
            try:
                self.session.get(NSE_BASE_URL, timeout=10)
                # Hit the option-chain page to ensure cookies are set for APIs
                self.session.get(f"{NSE_BASE_URL}/option-chain", timeout=10)
                NSEPublicFetcher._session_warmed = True
                log.debug("[nse_public] session warmed")
                return
            except Exception as exc:
                if attempt == 2:
                    log.warning("[nse_public] session warm-up failed after %d attempts: %s", attempt+1, exc)
                else:
                    time.sleep(2)

    def fetch_option_chain(self, symbol: str) -> dict | None:
        self._warm_session()
        if symbol in INDEX_SYMBOLS:
            url = NSE_OPTION_CHAIN_URL.format(symbol=symbol)
        else:
            url = NSE_EQUITY_OC_URL.format(symbol=symbol)

        raw = self._get(url)
        if not raw:
            return None
        return self._normalise(symbol, raw)

    def _nearest_expiry(self, expiry_dates: list[str]) -> str:
        today = datetime.now(timezone.utc).date()
        parsed: list[tuple] = []
        for exp in expiry_dates:
            try:
                dt = datetime.strptime(exp, "%d-%b-%Y").date()
                parsed.append((dt, exp))
            except ValueError:
                continue

        future = sorted((item for item in parsed if item[0] >= today), key=lambda x: x[0])
        if future:
            return future[0][0].strftime("%Y-%m-%d")

        if parsed:
            return sorted(parsed, key=lambda x: x[0])[0][0].strftime("%Y-%m-%d")

        return ""

    def _normalise(self, symbol: str, raw: dict) -> dict | None:
        try:
            filtered = raw.get("filtered", {})
            records  = filtered.get("data", [])
            underlying = float(
                raw.get("records", {}).get("underlyingValue", 0) or 0
            )
            expiry_dates = raw.get("records", {}).get("expiryDates", [])
            expiry = self._nearest_expiry(expiry_dates)

            strikes = []
            for record in records:
                strike = float(record.get("strikePrice", 0))
                for ot in ("CE", "PE"):
                    opt = record.get(ot)
                    if not opt:
                        continue
                    strikes.append({
                        "strike":      strike,
                        "option_type": ot,
                        "ltp":         float(opt.get("lastPrice", 0) or 0),
                        "oi":          int(opt.get("openInterest", 0) or 0),
                        "oi_change":   int(opt.get("changeinOpenInterest", 0) or 0),
                        "volume":      int(opt.get("totalTradedVolume", 0) or 0),
                        "iv":          float(opt.get("impliedVolatility", 0) or 0),
                        "bid":         float(opt.get("bidPrice", 0) or 0),
                        "ask":         float(opt.get("askPrice", 0) or 0),
                    })

            return {
                "symbol":           symbol,
                "underlying_price": underlying,
                "expiry":           expiry,
                "strikes":          strikes,
                "source":           self.name,
            }
        except Exception as exc:
            log.error("[nse_public] normalise failed for %s: %s", symbol, exc)
            return None
