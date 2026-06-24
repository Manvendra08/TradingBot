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
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from config.settings import DB_PATH
from src.utils.dhan_resolver import get_dhan_security_id

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
    "SENSEX": ("BSE", "SENSEX"),
    "NATURALGAS": ("MCX", "NATURALGAS"),
    "CRUDEOIL": ("MCX", "CRUDEOIL"),
    "GOLD": ("MCX", "GOLD"),
    "SILVER": ("MCX", "SILVER"),
}
_MCX_SYMBOLS = {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}
_DHAN_BUILTUP_SYMBOLS = {"NATURALGAS", "CRUDEOIL"}
_DHAN_BUILTUP_URL = "https://openweb-ticks.dhan.co/builtup"

_YF_SYMBOL_MAP: dict[str, str] = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "^NSMIDCP",
    "SENSEX": "^BSESN",
    "NATURALGAS": "NG=F",
    "CRUDEOIL": "CL=F",
    "GOLD": "GC=F",
    "SILVER": "SI=F",
}

_YF_TF_MAP: dict[str, tuple[str, str]] = {
    "5m": ("5m", "1d"),
    "15m": ("15m", "1d"),
    "30m": ("30m", "5d"),
    "1h": ("1h", "15d"),
    "3h": ("1h", "45d"),
    "4h": ("1h", "60d"),
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


def _to_utc_iso(ts_value, *, naive_tz=timezone.utc) -> str | None:
    if ts_value is None:
        return None
    try:
        if isinstance(ts_value, (int, float)):
            dt = datetime.fromtimestamp(float(ts_value), timezone.utc)
        elif isinstance(ts_value, datetime):
            dt = ts_value
        elif hasattr(ts_value, "to_pydatetime"):
            dt = ts_value.to_pydatetime()
        else:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=naive_tz)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _sentiment(open_: float, close: float, high: float, low: float) -> str:
    eps = 1e-9
    if close > open_ + eps:
        return "BULLISH"
    if close < open_ - eps:
        return "BEARISH"
    return "NEUTRAL"


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

# Circuit-breaker: after this many consecutive failures, back off for _TV_BACKOFF_SECONDS
_TV_MAX_FAILURES = 3
_TV_BACKOFF_SECONDS = 120  # 2 minutes


def _get_tv_client():
    """
    Return a thread-local TvDatafeed client, or None if:
    - credentials are missing
    - init failed
    - circuit-breaker is open (too many recent failures)
    """
    # Check circuit-breaker before attempting to (re)create client
    fail_count = getattr(_tv_local, "fail_count", 0)
    backoff_until = getattr(_tv_local, "backoff_until", None)
    if backoff_until is not None:
        import time as _time
        if _time.monotonic() < backoff_until:
            return None  # still in backoff window, skip silently
        else:
            # Backoff expired — reset and allow one retry
            _tv_local.fail_count = 0
            _tv_local.backoff_until = None
            _tv_local.client = None

    if not hasattr(_tv_local, "client") or _tv_local.client is None:
        try:
            from tvDatafeed import TvDatafeed
            from config.settings import TV_USERNAME, TV_PASSWORD, TV_SESSIONID
            if TV_SESSIONID:
                log.info("[chart] tvdatafeed: authenticating using sessionid cookie")
                _tv_local.client = TvDatafeed(sessionid=TV_SESSIONID)
            elif TV_USERNAME and TV_PASSWORD:
                log.info("[chart] tvdatafeed: authenticating as %s (warning: credentials may trigger CAPTCHA)", TV_USERNAME)
                _tv_local.client = TvDatafeed(username=TV_USERNAME, password=TV_PASSWORD)
            else:
                log.warning(
                    "[chart] tvdatafeed: TV_SESSIONID or credentials not set — "
                    "MCX commodity data (NATURALGAS, CRUDEOIL, GOLD, SILVER) will fail. "
                    "Set TV_SESSIONID in .env to enable MCX charts."
                )
                _tv_local.client = TvDatafeed()  # unauthenticated; NSE only
        except Exception as exc:
            log.warning("[chart] tvdatafeed init failed: %s", exc)
            _tv_local.client = None
    return _tv_local.client


def _tv_record_failure():
    """Increment failure counter; open circuit-breaker if threshold reached."""
    import time as _time
    _tv_local.fail_count = getattr(_tv_local, "fail_count", 0) + 1
    _tv_local.client = None
    if _tv_local.fail_count >= _TV_MAX_FAILURES:
        _tv_local.backoff_until = _time.monotonic() + _TV_BACKOFF_SECONDS
        log.warning(
            "[chart] tvdatafeed: %d consecutive failures — backing off for %ds",
            _tv_local.fail_count,
            _TV_BACKOFF_SECONDS,
        )


def _tv_record_success():
    """Reset failure counter on a successful fetch."""
    _tv_local.fail_count = 0
    _tv_local.backoff_until = None


def _provider_order(base_symbol: str) -> list[str]:
    # For NSE index symbols, prefer Yahoo and explicitly ignore in-progress bars.
    # tvDatafeed often returns the current forming candle with odd timestamps.
    if base_symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}:
        return ["yfinance"]
    # For commodities, TradingView (if logged in) is closest to what traders see,
    # with Yahoo as fallback.
    return ["tvdatafeed", "yfinance"]


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


def _bar_to_payload(bar, *, bar_start_ts=None, bar_end_ts=None, naive_tz=timezone.utc) -> dict | None:
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
        "bar_start_utc": _to_utc_iso(bar_start_ts, naive_tz=naive_tz),
        "bar_end_utc": _to_utc_iso(bar_end_ts or bar_start_ts, naive_tz=naive_tz),
    }


def _payload_from_closed_bars(
    bars: list[dict],
    *,
    bar_minutes: int,
    aggregate_count: int = 1,
) -> dict | None:
    """
    Build payload from the last *closed* bars only.
    `bars` expects Yahoo-like entries with Open/High/Low/Close/_ts(epoch).
    """
    if bar_minutes <= 0 or aggregate_count <= 0:
        return None

    now_utc = datetime.now(timezone.utc)
    closed: list[tuple[datetime, dict]] = []
    for bar in bars:
        ts = bar.get("_ts")
        if ts is None:
            continue
        try:
            start_dt = datetime.fromtimestamp(float(ts), timezone.utc)
        except Exception:
            continue
        end_dt = start_dt + timedelta(minutes=bar_minutes)
        if end_dt <= now_utc:
            closed.append((start_dt, bar))

    if len(closed) < aggregate_count:
        return None

    closed.sort(key=lambda item: item[0])
    selected = closed[-aggregate_count:]

    try:
        o = float(selected[0][1]["Open"])
        h = max(float(item[1]["High"]) for item in selected)
        l = min(float(item[1]["Low"]) for item in selected)
        c = float(selected[-1][1]["Close"])
    except Exception:
        return None

    prev_ohlc = None
    if len(closed) >= 2 * aggregate_count:
        prev_selected = closed[-2 * aggregate_count : -aggregate_count]
        try:
            p_o = float(prev_selected[0][1]["Open"])
            p_h = max(float(item[1]["High"]) for item in prev_selected)
            p_l = min(float(item[1]["Low"]) for item in prev_selected)
            p_c = float(prev_selected[-1][1]["Close"])
            prev_ohlc = {"open": p_o, "high": p_h, "low": p_l, "close": p_c}
        except Exception:
            pass

    start_dt = selected[0][0]
    end_dt = selected[-1][0] + timedelta(minutes=bar_minutes)
    return {
        "sentiment": _sentiment(o, c, h, l),
        "ohlc": {"open": o, "high": h, "low": l, "close": c},
        "prev_ohlc": prev_ohlc,
        "bar_start_utc": start_dt.isoformat(),
        "bar_end_utc": end_dt.isoformat(),
    }


def _aggregate_bars_list(bars: list[dict], bar_minutes: int, aggregate_count: int) -> list[dict]:
    now_utc = datetime.now(timezone.utc)
    closed = []
    for bar in bars:
        ts = bar.get("_ts")
        if ts is None:
            continue
        try:
            start_dt = datetime.fromtimestamp(float(ts), timezone.utc)
        except Exception:
            continue
        end_dt = start_dt + timedelta(minutes=bar_minutes)
        if end_dt <= now_utc:
            closed.append((start_dt, bar))
    
    closed.sort(key=lambda item: item[0])
    
    agg_bars = []
    n = len(closed)
    for i in range(n, 0, -aggregate_count):
        chunk = closed[i-aggregate_count:i]
        if len(chunk) < aggregate_count:
            continue
        try:
            o = float(chunk[0][1]["Open"])
            h = max(float(item[1]["High"]) for item in chunk)
            l = min(float(item[1]["Low"]) for item in chunk)
            c = float(chunk[-1][1]["Close"])
            agg_bars.append({
                "Open": o,
                "High": h,
                "Low": l,
                "Close": c,
                "_ts": chunk[0][1]["_ts"]
            })
        except Exception:
            continue
    agg_bars.reverse()
    return agg_bars


def _aggregate_bars_grid(bars: list[dict], tf_mins: int, base_symbol: str) -> list[dict]:
    """
    Groups bars by daily market grids and aggregates them.
    Only completed slots that fit entirely within market hours are returned.
    """
    import pytz
    from config.symbol_classes import market_window

    tz_local = pytz.timezone("Asia/Kolkata")
    open_t, close_t, _ = market_window(base_symbol)
    open_h, open_m = map(int, open_t.split(":"))
    close_h, close_m = map(int, close_t.split(":"))
    now_local = datetime.now(tz_local)

    parsed_bars = []
    for bar in bars:
        ts = bar.get("_ts")
        if ts is None:
            continue
        try:
            dt_utc = datetime.fromtimestamp(float(ts), timezone.utc)
            dt_local = dt_utc.astimezone(tz_local)
            parsed_bars.append((dt_local, bar))
        except Exception:
            continue

    if not parsed_bars:
        return []

    by_date = {}
    for dt_local, bar in parsed_bars:
        date_str = dt_local.date().isoformat()
        by_date.setdefault(date_str, []).append((dt_local, bar))

    aggregated_bars = []
    for date_str in sorted(by_date.keys()):
        day_bars = by_date[date_str]
        day_bars.sort(key=lambda x: x[0])

        y, m, d = map(int, date_str.split("-"))
        mkt_open = tz_local.localize(datetime(y, m, d, open_h, open_m, 0))
        mkt_close = tz_local.localize(datetime(y, m, d, close_h, close_m, 0))

        slot_start = mkt_open
        while True:
            slot_end = slot_start + timedelta(minutes=tf_mins)
            if slot_end > mkt_close or slot_end > now_local:
                break

            slot_bars = [bar for dt, bar in day_bars if slot_start <= dt < slot_end]
            if slot_bars:
                try:
                    o = float(slot_bars[0]["Open"])
                    h = max(float(b["High"]) for b in slot_bars)
                    l = min(float(b["Low"]) for b in slot_bars)
                    c = float(slot_bars[-1]["Close"])
                    ts_utc = slot_start.astimezone(timezone.utc).timestamp()
                    aggregated_bars.append({
                        "Open": o,
                        "High": h,
                        "Low": l,
                        "Close": c,
                        "_ts": ts_utc,
                        "_slot_start": slot_start,
                        "_slot_end": slot_end
                    })
                except Exception:
                    pass
            slot_start = slot_end

    return aggregated_bars


def _payload_from_grid_bars(agg_bars: list[dict]) -> dict | None:
    if not agg_bars:
        return None

    last_bar = agg_bars[-1]
    o = last_bar["Open"]
    h = last_bar["High"]
    l = last_bar["Low"]
    c = last_bar["Close"]

    prev_ohlc = None
    if len(agg_bars) >= 2:
        prev_bar = agg_bars[-2]
        prev_ohlc = {
            "open": prev_bar["Open"],
            "high": prev_bar["High"],
            "low": prev_bar["Low"],
            "close": prev_bar["Close"]
        }

    start_dt = last_bar["_slot_start"].astimezone(timezone.utc)
    end_dt = last_bar["_slot_end"].astimezone(timezone.utc)

    return {
        "sentiment": _sentiment(o, c, h, l),
        "ohlc": {"open": o, "high": h, "low": l, "close": c},
        "prev_ohlc": prev_ohlc,
        "bar_start_utc": start_dt.isoformat(),
        "bar_end_utc": end_dt.isoformat(),
    }



def _calculate_atr_from_bars(bars: list[dict], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    tr = []
    for i in range(1, len(bars)):
        try:
            h = float(bars[i]["High"])
            l = float(bars[i]["Low"])
            c_prev = float(bars[i-1]["Close"])
            tr.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
        except Exception:
            continue
    if len(tr) < period:
        return None
    # Wilder's smoothing
    atr = sum(tr[:period]) / period
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
    return atr



def _parse_dt_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _is_payload_stale(payload: dict, tf: str) -> bool:
    end_raw = (payload or {}).get("bar_end_utc") or (payload or {}).get("bar_start_utc")
    end_dt = _parse_dt_utc(end_raw)
    if end_dt is None:
        return True
    age_mins = (datetime.now(timezone.utc) - end_dt).total_seconds() / 60.0
    ttl = 120 if tf == "1h" else 240 if tf == "3h" else 180
    return age_mins > ttl


def _last_closed_window(tf: str, base_symbol: str) -> tuple[datetime, datetime] | None:
    now_ist = datetime.now(IST)
    from config.symbol_classes import market_window
    
    open_t, close_t, _ = market_window(base_symbol)
    open_h, open_m = map(int, open_t.split(":"))
    
    market_open = now_ist.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    delta_mins = (now_ist - market_open).total_seconds() / 60.0
    
    if tf.endswith("m"):
        tf_mins = int(tf[:-1])
    elif tf.endswith("h"):
        tf_mins = int(tf[:-1]) * 60
    else:
        tf_mins = 60
        
    if delta_mins < tf_mins:
        start_ist = market_open
        end_ist = now_ist
    else:
        import math
        completed_intervals = math.floor(delta_mins / tf_mins)
        start_ist = market_open + timedelta(minutes=(completed_intervals - 1) * tf_mins)
        end_ist = market_open + timedelta(minutes=completed_intervals * tf_mins)

    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def _aggregate_rows_to_payload(rows: list[dict], start_utc: datetime, end_utc: datetime) -> Optional[dict]:
    selected: list[dict] = []
    duration = end_utc - start_utc
    prev_start_utc = start_utc - duration
    prev_end_utc = end_utc - duration
    prev_selected: list[dict] = []

    for row in rows:
        try:
            st = datetime.fromtimestamp(float(row.get("st")), timezone.utc)
            et = datetime.fromtimestamp(float(row.get("et")), timezone.utc)
        except Exception:
            continue
        if st >= start_utc and et <= end_utc:
            selected.append(row)
        elif st >= prev_start_utc and et <= prev_end_utc:
            prev_selected.append(row)

    selected.sort(key=lambda r: float(r.get("st") or 0))
    prev_selected.sort(key=lambda r: float(r.get("st") or 0))

    if not selected:
        return None
    try:
        o = float(selected[0]["o"])
        h = max(float(r["h"]) for r in selected)
        l = min(float(r["l"]) for r in selected)
        c = float(selected[-1]["c"])
    except Exception:
        return None

    prev_ohlc = None
    if prev_selected:
        try:
            po = float(prev_selected[0]["o"])
            ph = max(float(r["h"]) for r in prev_selected)
            pl = min(float(r["l"]) for r in prev_selected)
            pc = float(prev_selected[-1]["c"])
            prev_ohlc = {"open": po, "high": ph, "low": pl, "close": pc}
        except Exception:
            pass

    return {
        "sentiment": _sentiment(o, c, h, l),
        "ohlc": {"open": o, "high": h, "low": l, "close": c},
        "prev_ohlc": prev_ohlc,
        "bar_start_utc": start_utc.isoformat(),
        "bar_end_utc": end_utc.isoformat(),
    }


def _fetch_dhan_builtup_ohlc(base_symbol: str, tf: str, reference_price: float | None = None) -> Optional[dict]:
    if base_symbol not in _DHAN_BUILTUP_SYMBOLS:
        return None
    window = _last_closed_window(tf, base_symbol)
    if not window:
        return None
    sid = get_dhan_security_id(base_symbol)
    if not sid:
        return None

    payload = {
        "Data": {
            "Exch": "MCX",
            "Seg": "M",
            "Inst": "FUTCOM",
            "Timeinterval": "15",
            "Secid": int(sid),
        }
    }
    try:
        import urllib.request

        with _without_proxy_env():
            req = urllib.request.Request(
                _DHAN_BUILTUP_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as res:
                raw = json.loads(res.read().decode("utf-8"))
        rows = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return None
        out = _aggregate_rows_to_payload(rows, *window)
        if out:
            log.info("[chart] %s %s -> using dhan_builtup last-closed candle", base_symbol, tf)
            # Fetch historical ATR and prev_ohlc from yfinance as a fallback
            try:
                yf_payload = _fetch_yf(base_symbol, tf, reference_price=reference_price)
                if yf_payload:
                    if "atr_14" in yf_payload:
                        out["atr_14"] = yf_payload["atr_14"]
                    if "prev_ohlc" in yf_payload:
                        out["prev_ohlc"] = yf_payload["prev_ohlc"]
            except Exception as e:
                log.warning("[chart] failed to fetch yfinance fallback indicators for %s %s: %s", base_symbol, tf, e)
        return out
    except Exception as exc:
        log.warning("[chart] dhan_builtup fetch failed %s %s: %s", base_symbol, tf, exc)
        return None


def _fetch_local_ohlc_from_db(base_symbol: str, tf: str) -> Optional[dict]:
    if tf not in {"1h", "3h"}:
        return None
    window = _last_closed_window(tf, base_symbol)
    if not window:
        return None
    start_utc, end_utc = window

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT fetched_at, price
                FROM underlying_price
                WHERE symbol=? AND fetched_at >= ? AND fetched_at < ?
                ORDER BY fetched_at ASC
                """,
                (base_symbol, start_utc.isoformat(), end_utc.isoformat()),
            ).fetchall()
            if not rows:
                return None

            prices: list[float] = []
            first_dt_utc = None
            last_dt_utc = None
            for r in rows:
                try:
                    prices.append(float(r["price"]))
                    dt_utc = _parse_dt_utc(r["fetched_at"])
                    if dt_utc is not None:
                        if first_dt_utc is None:
                            first_dt_utc = dt_utc
                        last_dt_utc = dt_utc
                except Exception:
                    continue
            if not prices:
                return None

            o = prices[0]
            h = max(prices)
            l = min(prices)
            c = prices[-1]
            return {
                "sentiment": _sentiment(o, c, h, l),
                "ohlc": {"open": o, "high": h, "low": l, "close": c},
                "bar_start_utc": start_utc.isoformat(),
                "bar_end_utc": end_utc.isoformat(),
            }
        finally:
            conn.close()
    except Exception as exc:
        log.debug("[chart] local DB OHLC build failed for %s %s: %s", base_symbol, tf, exc)
        return None


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
        df = client.get_hist(symbol=tv_sym, exchange=exchange, interval=interval, n_bars=30)
        if df is None or getattr(df, "empty", True):
            _tv_record_failure()
            return None

        # Build list of bars for ATR calculation
        tv_bars = []
        for idx, row in df.iterrows():
            try:
                tv_bars.append({
                    "Open": float(row["open"]),
                    "High": float(row["high"]),
                    "Low": float(row["low"]),
                    "Close": float(row["close"]),
                })
            except Exception:
                continue

        bar = df.iloc[-1]
        bar_ts = None
        try:
            bar_ts = df.index[-1]
        except Exception:
            pass
        payload = _bar_to_payload(bar, bar_start_ts=bar_ts, bar_end_ts=bar_ts, naive_tz=IST)
        if payload is None:
            _tv_record_failure()
            return None

        # Calculate ATR
        atr = _calculate_atr_from_bars(tv_bars, period=14)
        if atr is not None:
            payload["atr_14"] = atr

        # Extract prev_ohlc if available
        if len(df) >= 2:
            prev_bar = df.iloc[-2]
            prev_ts = None
            try:
                prev_ts = df.index[-2]
            except Exception:
                pass
            prev_payload = _bar_to_payload(prev_bar, bar_start_ts=prev_ts, bar_end_ts=prev_ts, naive_tz=IST)
            if prev_payload:
                payload["prev_ohlc"] = prev_payload["ohlc"]

        _tv_record_success()
        return payload
    except Exception as exc:
        log.warning("[chart] tvdatafeed fetch error %s %s: %s", base_symbol, tf, exc)
        _tv_record_failure()
        return None


def _apply_price_scale(payload: dict, scale: float) -> dict:
    try:
        ohlc = payload.get("ohlc") or {}
        prev = payload.get("prev_ohlc")
        res = {
            **payload,
            "ohlc": {
                "open": float(ohlc.get("open", 0.0)) * scale,
                "high": float(ohlc.get("high", 0.0)) * scale,
                "low": float(ohlc.get("low", 0.0)) * scale,
                "close": float(ohlc.get("close", 0.0)) * scale,
            },
        }
        if prev:
            res["prev_ohlc"] = {
                "open": float(prev.get("open", 0.0)) * scale,
                "high": float(prev.get("high", 0.0)) * scale,
                "low": float(prev.get("low", 0.0)) * scale,
                "close": float(prev.get("close", 0.0)) * scale,
            }
        return res
    except Exception:
        return payload


def _fetch_yf(base_symbol: str, tf: str, reference_price: float | None = None) -> Optional[dict]:
    yf_sym = _YF_SYMBOL_MAP.get(base_symbol)
    if not yf_sym:
        return None

    yf_interval, yf_period = _YF_TF_MAP.get(tf, (None, None))
    if not yf_interval:
        return None

    # 1. Pure HTTP query API (zero-dependency, extremely fast & robust)
    try:
        import urllib.request
        import json
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}?interval={yf_interval}&range={yf_period}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8"))
            
        result = data["chart"]["result"][0]
        indicators = result["indicators"]["quote"][0]
        timestamps = result.get("timestamp", []) or []
        opens = indicators.get("open", [])
        highs = indicators.get("high", [])
        lows = indicators.get("low", [])
        closes = indicators.get("close", [])
        
        valid_bars = []
        for i in range(len(opens)):
            o = opens[i]
            h = highs[i]
            l = lows[i]
            c = closes[i]
            if o is not None and h is not None and l is not None and c is not None:
                ts = timestamps[i] if i < len(timestamps) else None
                valid_bars.append({
                    "Open": float(o),
                    "High": float(h),
                    "Low": float(l),
                    "Close": float(c),
                    "_ts": ts,
                })
                
        if valid_bars:
            if tf.endswith("m"):
                tf_mins = int(tf[:-1])
            elif tf.endswith("h"):
                tf_mins = int(tf[:-1]) * 60
            elif tf.endswith("d"):
                tf_mins = int(tf[:-1]) * 1440
            else:
                tf_mins = 60

            agg_bars = _aggregate_bars_grid(valid_bars, tf_mins, base_symbol)
            payload = _payload_from_grid_bars(agg_bars)
            atr = _calculate_atr_from_bars(agg_bars, period=14)
            if payload and atr is not None:
                payload["atr_14"] = atr

            if payload:
                # MCX symbols from Yahoo (NG=F/CL=F/GC=F/SI=F) are in global units.
                # Scale to local underlying so OHLC shown in Telegram/UI remains meaningful.
                if base_symbol in _MCX_SYMBOLS and reference_price and reference_price > 0:
                    try:
                        close_px = float((payload.get("ohlc") or {}).get("close") or 0.0)
                        if close_px > 0:
                            scale = reference_price / close_px
                            payload = _apply_price_scale(payload, scale)
                            if "atr_14" in payload and payload["atr_14"] is not None:
                                payload["atr_14"] = payload["atr_14"] * scale
                    except Exception:
                        pass
                log.info("[chart] successfully fetched %s %s using pure-HTTP API", base_symbol, tf)
                return payload
    except Exception as exc:
        log.warning("[chart] pure-HTTP Yahoo Finance query failed for %s %s: %s", base_symbol, tf, exc)

    # 2. Fallback to standard yfinance package
    if not _yfinance_available():
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

        if df is None or df.empty:
            return None

        bars: list[dict] = []
        for ts, row in df.tail(64).iterrows():
            try:
                bars.append(
                    {
                        "Open": float(row["Open"]),
                        "High": float(row["High"]),
                        "Low": float(row["Low"]),
                        "Close": float(row["Close"]),
                        "_ts": float(ts.timestamp()),
                    }
                )
            except Exception:
                continue

        if bars:
            if tf.endswith("m"):
                tf_mins = int(tf[:-1])
            elif tf.endswith("h"):
                tf_mins = int(tf[:-1]) * 60
            elif tf.endswith("d"):
                tf_mins = int(tf[:-1]) * 1440
            else:
                tf_mins = 60

            agg_bars = _aggregate_bars_grid(bars, tf_mins, base_symbol)
            payload = _payload_from_grid_bars(agg_bars)
            atr = _calculate_atr_from_bars(agg_bars, period=14)
            if payload and atr is not None:
                payload["atr_14"] = atr
        if payload and base_symbol in _MCX_SYMBOLS and reference_price and reference_price > 0:
            try:
                close_px = float((payload.get("ohlc") or {}).get("close") or 0.0)
                if close_px > 0:
                    scale = reference_price / close_px
                    payload = _apply_price_scale(payload, scale)
                    if "atr_14" in payload and payload["atr_14"] is not None:
                        payload["atr_14"] = payload["atr_14"] * scale
            except Exception:
                pass
        return payload
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
                "bar_start_utc": payload.get("bar_start_utc"),
                "bar_end_utc": payload.get("bar_end_utc"),
                "prev_ohlc": payload.get("prev_ohlc") or prev_ohlc or prev.get("prev_ohlc"),
                "last_closed_ohlc": prev_ohlc if changed else prev.get("last_closed_ohlc"),
                "updated_at": now,
                "seen_at": now,
                "changed_at": now if changed or not prev else prev.get("changed_at", now),
            }
            # Preserve additional indicators like atr_14, support, resistance, etc. from raw payload
            for k, v in payload.items():
                if k not in entry:
                    entry[k] = v
            symbol_state[tf] = entry
            return entry

    def fetch(self, symbol: str, timeframes: list[str] | None = None, reference_price: float | None = None) -> dict:
        tfs = list(timeframes or self.DEFAULT_TIMEFRAMES)
        base = _base_symbol(symbol)

        if base not in _TV_SYMBOL_MAP and base not in _YF_SYMBOL_MAP:
            log.warning("[chart] unknown symbol %r (base=%r)", symbol, base)
            return {}

        result: dict[str, dict] = {}
        for tf in tfs:
            payload = None
            source = None

            if base in _DHAN_BUILTUP_SYMBOLS:
                payload = _fetch_dhan_builtup_ohlc(base, tf, reference_price=reference_price)
                source = "dhan_builtup" if payload else None

            if not payload:
                for provider in _provider_order(base):
                    if provider == "tvdatafeed":
                        payload = _fetch_tv(base, tf)
                        source = "tvdatafeed"
                    else:
                        payload = _fetch_yf(base, tf, reference_price=reference_price)
                        source = "yfinance"

                    if payload:
                        break

            if base in _MCX_SYMBOLS and (payload is None or _is_payload_stale(payload, tf)):
                local_payload = _fetch_local_ohlc_from_db(base, tf)
                if local_payload:
                    payload = local_payload
                    source = "local_underlying_db"
                    log.info("[chart] %s %s -> using local_underlying_db (fresh MCX fallback)", base, tf)

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
