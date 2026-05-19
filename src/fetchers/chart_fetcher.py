"""
chart_fetcher.py  —  Server-side OHLC + sentiment fetcher v1.0
Replaces Chrome/tv_content.js DOM scraping for chart data.

Priority chain per symbol:
  1. tvdatafeed  (unofficial TradingView WebSocket — real-time, no auth)
  2. yfinance    (Yahoo Finance — ~15min delay, free, reliable fallback)
  3. None        (graceful degradation — pipeline continues without chart)

Output schema (same as _normalize_chart_indicators in extension_bridge.py):
  {
    "1h": {
      "sentiment":   "BULLISH" | "BEARISH" | "NEUTRAL",
      "ohlc":        {"open": float, "high": float, "low": float, "close": float},
      "updated_at":  ISO-8601 str,
      "source":      "tvdatafeed" | "yfinance",
    },
    "3h": { ... },
  }

MCX note:
  NATURALGAS, CRUDEOIL, GOLD → MCX contracts. yfinance uses NYMEX/COMEX
  proxies (NG=F, CL=F, GC=F). Sentiment direction is reliable; absolute
  levels differ by premium/discount (typically ₹5–20 for NATURALGAS).
  tvdatafeed uses MCX directly when available — preferred for MCX symbols.

Usage:
  from src.fetchers.chart_fetcher import ChartFetcher
  fetcher = ChartFetcher()
  chart = fetcher.fetch(symbol="NATURALGAS", timeframes=["1h", "3h"])
  # Returns dict or None on total failure
"""

import logging
import threading
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Optional

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Symbol maps ────────────────────────────────────────────────────────────

# tvdatafeed: (exchange, symbol_on_tv)
_TV_SYMBOL_MAP: dict[str, tuple[str, str]] = {
    "NIFTY":      ("NSE", "NIFTY"),
    "BANKNIFTY":  ("NSE", "BANKNIFTY"),
    "FINNIFTY":   ("NSE", "FINNIFTY"),
    "MIDCPNIFTY": ("NSE", "MIDCPNIFTY"),
    "NATURALGAS": ("MCX", "NATURALGAS"),
    "CRUDEOIL":   ("MCX", "CRUDEOIL"),
    "GOLD":       ("MCX", "GOLD"),
    "SILVER":     ("MCX", "SILVER"),
}

# yfinance ticker symbols (fallback)
_YF_SYMBOL_MAP: dict[str, str] = {
    "NIFTY":      "^NSEI",
    "BANKNIFTY":  "^NSEBANK",
    "FINNIFTY":   "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "^NSMIDCP",
    "NATURALGAS": "NG=F",    # NYMEX front-month proxy
    "CRUDEOIL":   "CL=F",    # NYMEX WTI proxy
    "GOLD":       "GC=F",    # COMEX Gold proxy
    "SILVER":     "SI=F",    # COMEX Silver proxy
}

# tvdatafeed interval mapping
_TV_INTERVAL_MAP: dict[str, object] = {}   # populated lazily after import

# yfinance interval + period mapping
_YF_TF_MAP: dict[str, tuple[str, str]] = {
    "5m":  ("5m",  "1d"),
    "15m": ("15m", "1d"),
    "30m": ("30m", "5d"),
    "1h":  ("1h",  "5d"),
    "3h":  ("90m", "5d"),   # yfinance has no 3h; use 90m, resample to 3h
    "4h":  ("1h",  "5d"),   # resample 1h → 4h
    "1d":  ("1d",  "60d"),
}


# ── Availability checks (cached — import cost paid once) ──────────────────

@lru_cache(maxsize=1)
def _tvdatafeed_available() -> bool:
    try:
        from tvDatafeed import TvDatafeed, Interval  # noqa: F401
        return True
    except ImportError:
        return False


@lru_cache(maxsize=1)
def _yfinance_available() -> bool:
    try:
        import yfinance  # noqa: F401
        return True
    except ImportError:
        return False


# ── Sentiment from OHLC ───────────────────────────────────────────────────

def _sentiment(open_: float, close: float, high: float, low: float) -> str:
    """
    Determine candle sentiment.
    Uses body-to-wick ratio for higher accuracy than simple open<close.
    """
    body = abs(close - open_)
    total_range = high - low if high != low else 1e-9
    body_ratio = body / total_range

    if body_ratio < 0.15:
        # Doji / spinning top — no clear direction
        return "NEUTRAL"
    return "BULLISH" if close > open_ else "BEARISH"


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


# ── tvdatafeed provider ───────────────────────────────────────────────────

def _tv_interval(tf: str):
    """Map timeframe string to tvDatafeed Interval enum."""
    global _TV_INTERVAL_MAP
    if not _TV_INTERVAL_MAP:
        try:
            from tvDatafeed import Interval
            _TV_INTERVAL_MAP = {
                "1m":  Interval.in_1_minute,
                "3m":  Interval.in_3_minute,
                "5m":  Interval.in_5_minute,
                "15m": Interval.in_15_minute,
                "30m": Interval.in_30_minute,
                "45m": Interval.in_45_minute,
                "1h":  Interval.in_1_hour,
                "2h":  Interval.in_2_hour,
                "3h":  Interval.in_3_hour,
                "4h":  Interval.in_4_hour,
                "1d":  Interval.in_daily,
                "1w":  Interval.in_weekly,
            }
        except ImportError:
            return None
    return _TV_INTERVAL_MAP.get(tf)


# Thread-local tvdatafeed instance — avoids WebSocket collision across threads
_tv_local = threading.local()


def _get_tv_client():
    """Return thread-local TvDatafeed instance (anonymous login)."""
    if not hasattr(_tv_local, "client"):
        try:
            from tvDatafeed import TvDatafeed
            _tv_local.client = TvDatafeed()   # anonymous — no auth needed
            log.debug("[chart] tvdatafeed client initialised (thread=%s)",
                      threading.current_thread().name)
        except Exception as exc:
            log.warning("[chart] tvdatafeed init failed: %s", exc)
            _tv_local.client = None
    return _tv_local.client


def _fetch_tv(base_symbol: str, tf: str) -> Optional[dict]:
    """
    Fetch latest closed candle via tvdatafeed.
    Returns normalised single-timeframe dict or None.
    """
    if not _tvdatafeed_available():
        return None

    tv_info = _TV_SYMBOL_MAP.get(base_symbol)
    if not tv_info:
        return None
    exchange, tv_sym = tv_info

    interval = _tv_interval(tf)
    if interval is None:
        log.debug("[chart] tvdatafeed: no interval mapping for %s", tf)
        return None

    client = _get_tv_client()
    if client is None:
        return None

    try:
        df = client.get_hist(
            symbol=tv_sym,
            exchange=exchange,
            interval=interval,
            n_bars=5,          # last 5 bars — use index -2 (last closed)
        )
        if df is None or df.empty:
            log.debug("[chart] tvdatafeed empty result: %s %s %s", base_symbol, tf, exchange)
            return None

        # Last closed candle = second to last row (last row may be forming)
        bar = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
        o, h, l, c = float(bar["open"]), float(bar["high"]), \
                     float(bar["low"]),  float(bar["close"])
        ts = bar.name.isoformat() if hasattr(bar.name, "isoformat") else _now_iso()

        return {
            "sentiment":  _sentiment(o, c, h, l),
            "ohlc":       {"open": o, "high": h, "low": l, "close": c},
            "updated_at": ts,
            "source":     "tvdatafeed",
        }
    except Exception as exc:
        log.warning("[chart] tvdatafeed fetch error %s %s: %s", base_symbol, tf, exc)
        # Reset client so next call re-initialises WebSocket
        _tv_local.client = None
        return None


# ── yfinance provider ─────────────────────────────────────────────────────

def _fetch_yf(base_symbol: str, tf: str) -> Optional[dict]:
    """
    Fetch latest closed candle via yfinance.
    3h and 4h are computed by resampling finer intervals.
    Returns normalised single-timeframe dict or None.
    """
    if not _yfinance_available():
        return None

    yf_sym = _YF_SYMBOL_MAP.get(base_symbol)
    if not yf_sym:
        log.debug("[chart] yfinance: no symbol mapping for %s", base_symbol)
        return None

    yf_interval, yf_period = _YF_TF_MAP.get(tf, (None, None))
    if not yf_interval:
        return None

    try:
        import yfinance as yf
        ticker = yf.Ticker(yf_sym)
        df = ticker.history(period=yf_period, interval=yf_interval)

        if df is None or df.empty:
            log.debug("[chart] yfinance empty: %s %s", yf_sym, yf_interval)
            return None

        # Resample coarser timeframes
        if tf == "3h":
            df = df.resample("3h").agg({
                "Open": "first", "High": "max",
                "Low": "min",    "Close": "last",
            }).dropna()
        elif tf == "4h":
            df = df.resample("4h").agg({
                "Open": "first", "High": "max",
                "Low": "min",    "Close": "last",
            }).dropna()

        if df.empty:
            return None

        # Use last completed candle (exclude currently forming bar)
        bar = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]

        # Column names vary by yfinance version
        def _col(primary, fallback):
            if primary in bar.index:
                return float(bar[primary])
            if fallback in bar.index:
                return float(bar[fallback])
            return 0.0

        o = _col("Open", "open")
        h = _col("High", "high")
        l = _col("Low",  "low")
        c = _col("Close","close")

        ts = bar.name.isoformat() if hasattr(bar.name, "isoformat") else _now_iso()

        return {
            "sentiment":  _sentiment(o, c, h, l),
            "ohlc":       {"open": o, "high": h, "low": l, "close": c},
            "updated_at": ts,
            "source":     "yfinance",
        }
    except Exception as exc:
        log.warning("[chart] yfinance fetch error %s %s: %s", base_symbol, tf, exc)
        return None


# ── Public interface ───────────────────────────────────────────────────────

class ChartFetcher:
    """
    Server-side chart data fetcher.
    Drop-in replacement for Chrome extension chart telemetry.

    Thread-safe — safe to call from scheduler or pipeline threads.
    """

    DEFAULT_TIMEFRAMES = ["1h", "3h"]

    def _base_symbol(self, symbol: str) -> str:
        """Strip expiry/month suffix: 'NATURALGAS MAY FUT' → 'NATURALGAS'."""
        import re
        s = symbol.upper().strip()
        s = re.sub(
            r"\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
            r"\d{0,4}(\s+FUT)?$", "", s
        )
        return s.split()[0]

    def fetch(
        self,
        symbol: str,
        timeframes: list[str] | None = None,
    ) -> dict | None:
        """
        Fetch chart data for symbol across requested timeframes.

        Returns:
            {
              "1h": { sentiment, ohlc, updated_at, source },
              "3h": { ... },
            }
            or None if all providers fail for all timeframes.

        Never raises — logs warnings and returns partial result or None.
        """
        tfs = timeframes or self.DEFAULT_TIMEFRAMES
        base = self._base_symbol(symbol)

        if base not in _TV_SYMBOL_MAP and base not in _YF_SYMBOL_MAP:
            log.warning("[chart] unknown symbol %r (base=%r) — no provider mapped", symbol, base)
            return None

        result: dict = {}

        for tf in tfs:
            data = None

            # Provider 1: tvdatafeed (real-time, MCX-native)
            if _tvdatafeed_available():
                data = _fetch_tv(base, tf)
                if data:
                    log.debug("[chart] %s %s → tvdatafeed %s", base, tf, data["sentiment"])

            # Provider 2: yfinance (delayed fallback)
            if data is None and _yfinance_available():
                data = _fetch_yf(base, tf)
                if data:
                    log.debug("[chart] %s %s → yfinance %s (delayed)", base, tf, data["sentiment"])

            if data is None:
                log.warning("[chart] %s %s — all providers failed", base, tf)
            else:
                result[tf] = data

        if not result:
            log.error("[chart] %s — no chart data from any provider for tfs=%s", base, tfs)
            return None

        log.info(
            "[chart] %s | %s | %s",
            base,
            "  ".join(
                f"{tf}:{v['sentiment'][:4]}({v['source'][:2].upper()})"
                for tf, v in result.items()
            ),
            f"providers: tvdf={'✓' if _tvdatafeed_available() else '✗'}  "
            f"yf={'✓' if _yfinance_available() else '✗'}",
        )
        return result

    def is_operational(self) -> dict:
        """Health check — returns provider availability."""
        return {
            "tvdatafeed": _tvdatafeed_available(),
            "yfinance":   _yfinance_available(),
            "symbols_tv": list(_TV_SYMBOL_MAP.keys()),
            "symbols_yf": list(_YF_SYMBOL_MAP.keys()),
        }


# ── Module-level singleton ─────────────────────────────────────────────────
_instance: ChartFetcher | None = None
_lock = threading.Lock()


def get_chart_fetcher() -> ChartFetcher:
    """Return module-level singleton. Thread-safe."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = ChartFetcher()
    return _instance
