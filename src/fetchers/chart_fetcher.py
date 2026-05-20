"""
Server-side chart fetcher.

Returns the same outer shape as chrome.storage.local:
    {
        "NIFTY": {
            "1h": {...},
            "3h": {...}
        }
    }

Behavior:
  - NIFTY / BANKNIFTY / FINNIFTY: yfinance first
  - NATURALGAS: tvDatafeed first for MCX accuracy, then yfinance fallback
  - If all providers fail, returns {}

Each timeframe payload mirrors the extension shape:
    {
        "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",
        "ohlc": {"open": float, "high": float, "low": float, "close": float},
        "last_closed_ohlc": {...} | None,
        "updated_at": ISO-8601,
        "seen_at": ISO-8601,
        "changed_at": ISO-8601
    }
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

_YF_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "yf-cache"
_YF_CACHE_READY = False
_YF_ENV_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_STATE: dict[str, dict[str, dict]] = {}


_TV_SYMBOL_MAP: dict[str, tuple[str, str]] = {
    "NIFTY": ("NSE", "NIFTY"),
    "BANKNIFTY": ("NSE", "BANKNIFTY"),
    "FINNIFTY": ("NSE", "FINNIFTY"),
    "MIDCPNIFTY": ("NSE", "MIDCPNIFTY"),
    "NATURALGAS": ("MCX", "NATURALGAS"),
    "CRUDEOIL": ("MCX", "CRUDEOIL"),
    "GOLD": ("MCX", "GOLD"),
    "SILVER": ("MCX", "SILVER"),
}

_YF_SYMBOL_MAP: dict[str, str] = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "^NSMIDCP",
    "NATURALGAS": "NG=F",
    "CRUDEOIL": "CL=F",
    "GOLD": "GC=F",
    "SILVER": "SI=F",
}

_YF_TF_MAP: dict[str, tuple[str, str]] = {
    "5m": ("5m", "1d"),
    "15m": ("15m", "1d"),
    "30m": ("30m", "5d"),
    "1h": ("1h", "5d"),
    "3h": ("90m", "5d"),
    "4h": ("1h", "5d"),
    "1d": ("1d", "60d"),
}


def _base_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    if not s:
        return ""
    s = re.sub(r"\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4}(\s+FUT)?$", "", s)
    return s.split()[0]


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


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def _sentiment(open_: float, close: float, high: float, low: float) -> str:
    body = abs(close - open_)
    total_range = high - low if high != low else 1e-9
    if (body / total_range) < 0.15:
        return "NEUTRAL"
    return "BULLISH" if close > open_ else "BEARISH"


def _tv_interval(tf: str):
    try:
        from tvDatafeed import Interval
    except ImportError:
        return None

    mapping = {
        "1m": Interval.in_1_minute,
        "3m": Interval.in_3_minute,
        "5m": Interval.in_5_minute,
        "15m": Interval.in_15_minute,
        "30m": Interval.in_30_minute,
        "45m": Interval.in_45_minute,
        "1h": Interval.in_1_hour,
        "2h": Interval.in_2_hour,
        "3h": Interval.in_3_hour,
        "4h": Interval.in_4_hour,
        "1d": Interval.in_daily,
        "1w": Interval.in_weekly,
    }
    return mapping.get(tf)


_tv_local = threading.local()


def _get_tv_client():
    if not hasattr(_tv_local, "client"):
        try:
            from tvDatafeed import TvDatafeed
            from config.settings import TV_USERNAME, TV_PASSWORD
            if TV_USERNAME and TV_PASSWORD:
                log.info("[chart] tvdatafeed: authenticating as %s", TV_USERNAME)
                _tv_local.client = TvDatafeed(username=TV_USERNAME, password=TV_PASSWORD)
            else:
                log.warning(
                    "[chart] tvdatafeed: TV_USERNAME/TV_PASSWORD not set — "
                    "MCX commodity data (NATURALGAS, CRUDEOIL, GOLD, SILVER) will fail. "
                    "Set credentials in .env to enable MCX charts."
                )
                _tv_local.client = TvDatafeed()  # unauthenticated; NSE only
        except Exception as exc:
            log.warning("[chart] tvdatafeed init failed: %s", exc)
            _tv_local.client = None
    return _tv_local.client


def _provider_order(base_symbol: str) -> list[str]:
    if base_symbol == "NATURALGAS":
        return ["tvdatafeed", "yfinance"]
    return ["yfinance", "tvdatafeed"]


@contextlib.contextmanager
def _without_proxy_env():
    with _YF_ENV_LOCK:
        saved = {key: os.environ.get(key) for key in _PROXY_ENV_KEYS}
        try:
            for key in _PROXY_ENV_KEYS:
                os.environ.pop(key, None)
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _ensure_yf_cache() -> None:
    global _YF_CACHE_READY
    if _YF_CACHE_READY:
        return

    with _YF_ENV_LOCK:
        if _YF_CACHE_READY:
            return
        try:
            import yfinance.cache as yf_cache

            _YF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            yf_cache.set_cache_location(str(_YF_CACHE_DIR))
            yf_cache.set_tz_cache_location(str(_YF_CACHE_DIR))
        except Exception as exc:
            log.debug("[chart] yfinance cache setup failed: %s", exc)
        finally:
            _YF_CACHE_READY = True


def _flatten_yf_frame(df, ticker: str):
    if df is None or getattr(df, "empty", True):
        return None

    try:
        if getattr(df.columns, "nlevels", 1) > 1:
            if ticker in list(df.columns.get_level_values(-1)):
                df = df.xs(ticker, axis=1, level=-1)
            else:
                df.columns = [c[-1] if isinstance(c, tuple) else c for c in df.columns]
    except Exception:
        pass

    return df if not getattr(df, "empty", True) else None


def _bar_to_payload(bar) -> dict | None:
    try:
        def _pick(*keys):
            for key in keys:
                try:
                    return float(bar[key])
                except Exception:
                    continue
            raise KeyError(keys[0])

        o = _pick("Open", "open")
        h = _pick("High", "high")
        l = _pick("Low", "low")
        c = _pick("Close", "close")
    except Exception:
        return None

    if not all((x == x for x in (o, h, l, c))):
        return None

    return {
        "sentiment": _sentiment(o, c, h, l),
        "ohlc": {"open": o, "high": h, "low": l, "close": c},
    }


def _fetch_tv(base_symbol: str, tf: str) -> Optional[dict]:
    if not _tvdatafeed_available():
        return None

    tv_info = _TV_SYMBOL_MAP.get(base_symbol)
    if not tv_info:
        return None

    interval = _tv_interval(tf)
    if interval is None:
        return None

    client = _get_tv_client()
    if client is None:
        return None

    exchange, tv_sym = tv_info
    try:
        df = client.get_hist(symbol=tv_sym, exchange=exchange, interval=interval, n_bars=5)
        if df is None or getattr(df, "empty", True):
            return None

        bar = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
        payload = _bar_to_payload(bar)
        if payload is None:
            return None
        return payload
    except Exception as exc:
        log.warning("[chart] tvdatafeed fetch error %s %s: %s", base_symbol, tf, exc)
        _tv_local.client = None
        return None


def _fetch_yf(base_symbol: str, tf: str) -> Optional[dict]:
    if not _yfinance_available():
        return None

    yf_sym = _YF_SYMBOL_MAP.get(base_symbol)
    if not yf_sym:
        return None

    yf_interval, yf_period = _YF_TF_MAP.get(tf, (None, None))
    if not yf_interval:
        return None

    _ensure_yf_cache()

    try:
        import yfinance as yf

        with _without_proxy_env():
            df = yf.download(
                yf_sym,
                interval=yf_interval,
                period=yf_period,
                progress=False,
                auto_adjust=False,
                threads=False,
                group_by="column",
                keepna=False,
                session=None,
            )

        df = _flatten_yf_frame(df, yf_sym)
        if df is None:
            return None

        if tf == "3h":
            df = df.resample("3h").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
            ).dropna()
        elif tf == "4h":
            df = df.resample("4h").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
            ).dropna()

        if df is None or df.empty:
            return None

        bar = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
        return _bar_to_payload(bar)
    except Exception as exc:
        log.warning("[chart] yfinance fetch error %s %s: %s", base_symbol, tf, exc)
        return None


class ChartFetcher:
    DEFAULT_TIMEFRAMES = ("1h", "3h")

    def _merge_state(self, base_symbol: str, tf: str, payload: dict) -> dict:
        now = _now_iso()
        current_ohlc = payload.get("ohlc") or None
        current_sentiment = payload.get("sentiment") or "NEUTRAL"

        with _STATE_LOCK:
            symbol_state = _STATE.setdefault(base_symbol, {})
            prev = symbol_state.get(tf, {})
            prev_ohlc = prev.get("ohlc")
            changed = bool(prev) and (
                prev.get("sentiment") != current_sentiment or prev_ohlc != current_ohlc
            )

            entry = {
                "sentiment": current_sentiment,
                "ohlc": current_ohlc,
                "last_closed_ohlc": prev_ohlc if changed else prev.get("last_closed_ohlc"),
                "updated_at": now,
                "seen_at": now,
                "changed_at": now if changed or not prev else prev.get("changed_at", now),
            }
            symbol_state[tf] = entry
            return entry

    def fetch(self, symbol: str, timeframes: list[str] | None = None) -> dict:
        tfs = list(timeframes or self.DEFAULT_TIMEFRAMES)
        base = _base_symbol(symbol)

        if base not in _TV_SYMBOL_MAP and base not in _YF_SYMBOL_MAP:
            log.warning("[chart] unknown symbol %r (base=%r)", symbol, base)
            return {}

        result: dict[str, dict] = {}
        for tf in tfs:
            payload = None
            source = None

            for provider in _provider_order(base):
                if provider == "tvdatafeed":
                    payload = _fetch_tv(base, tf)
                    source = "tvdatafeed"
                else:
                    payload = _fetch_yf(base, tf)
                    source = "yfinance"

                if payload:
                    break

            if not payload:
                log.warning("[chart] %s %s -> no chart data", base, tf)
                continue

            merged = self._merge_state(base, tf, payload)
            result.setdefault(base, {})[tf] = merged
            log.debug("[chart] %s %s -> %s", base, tf, source)

        if not result:
            log.error("[chart] %s -> no chart data from any provider", base)
            return {}

        return result

    def is_operational(self) -> dict:
        return {
            "tvdatafeed": _tvdatafeed_available(),
            "yfinance": _yfinance_available(),
            "symbols_tv": list(_TV_SYMBOL_MAP.keys()),
            "symbols_yf": list(_YF_SYMBOL_MAP.keys()),
            "yf_cache_dir": str(_YF_CACHE_DIR),
        }


_instance: ChartFetcher | None = None
_instance_lock = threading.Lock()


def get_chart_fetcher() -> ChartFetcher:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ChartFetcher()
    return _instance
