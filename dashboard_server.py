"""
NSEBOT FastAPI Dashboard Server
Run: python dashboard_server.py
Deps: pip install fastapi uvicorn
No pandas required.
"""
# ── Force IPv4 globally (Kite whitelists IPv4 only) ───────────────────────
import socket as _socket
_orig_getaddrinfo = _socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
_socket.getaddrinfo = _ipv4_only_getaddrinfo

import json
import logging
import re
import sqlite3

# ── Patch sqlite3 to automatically merge paper_trades and shadow live_trades ──
class PatchedCursor(sqlite3.Cursor):
    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and re.match(r'(?i)^\s*(select|with)\b', sql) and re.search(r'(?i)\bfrom\s+paper_trades\b', sql) and not re.search(r'(?i)\bfrom\s+live_trades\b', sql):
            subquery = """(
                SELECT id, opened_at, closed_at, symbol, verdict_label, option_type, strike, entry_underlying, exit_underlying, sl_underlying, target_underlying, pnl_points, status, reason, digest_id, entry_premium, exit_premium, sl_premium, target_premium, lots, pnl_rupees, trade_status, setup_type, decision_reason, confidence_score, entry_quality_score, trend_alignment_score, regime_score, signal_key, pyramid_level, max_favorable_r, side, expiry
                FROM paper_trades
                UNION ALL
                SELECT id, opened_at, closed_at, symbol, verdict_label, option_type, strike, entry_underlying, exit_underlying, sl_underlying, target_underlying, pnl_points, status, reason, digest_id, entry_premium, exit_premium, sl_premium, target_premium, lots, pnl_rupees, trade_status, setup_type, decision_reason, confidence_score, entry_quality_score, trend_alignment_score, regime_score, signal_key, pyramid_level, max_favorable_r, side, expiry
                FROM live_trades
                WHERE status = 'CLOSED_SHADOW' OR trade_status = 'SHADOW' OR broker_status = 'SHADOW'
            )"""
            sql = re.sub(r'(?i)\bfrom\s+paper_trades\b', f'FROM {subquery}', sql)
        return super().execute(sql, *args, **kwargs)

class PatchedConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and re.match(r'(?i)^\s*(select|with)\b', sql) and re.search(r'(?i)\bfrom\s+paper_trades\b', sql) and not re.search(r'(?i)\bfrom\s+live_trades\b', sql):
            subquery = """(
                SELECT id, opened_at, closed_at, symbol, verdict_label, option_type, strike, entry_underlying, exit_underlying, sl_underlying, target_underlying, pnl_points, status, reason, digest_id, entry_premium, exit_premium, sl_premium, target_premium, lots, pnl_rupees, trade_status, setup_type, decision_reason, confidence_score, entry_quality_score, trend_alignment_score, regime_score, signal_key, pyramid_level, max_favorable_r, side, expiry
                FROM paper_trades
                UNION ALL
                SELECT id, opened_at, closed_at, symbol, verdict_label, option_type, strike, entry_underlying, exit_underlying, sl_underlying, target_underlying, pnl_points, status, reason, digest_id, entry_premium, exit_premium, sl_premium, target_premium, lots, pnl_rupees, trade_status, setup_type, decision_reason, confidence_score, entry_quality_score, trend_alignment_score, regime_score, signal_key, pyramid_level, max_favorable_r, side, expiry
                FROM live_trades
                WHERE status = 'CLOSED_SHADOW' OR trade_status = 'SHADOW' OR broker_status = 'SHADOW'
            )"""
            sql = re.sub(r'(?i)\bfrom\s+paper_trades\b', f'FROM {subquery}', sql)
        return super().execute(sql, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return super().cursor(factory=PatchedCursor, *args, **kwargs)

_orig_connect = sqlite3.connect
def _patched_connect(*args, **kwargs):
    if "factory" not in kwargs:
        kwargs["factory"] = PatchedConnection
    return _orig_connect(*args, **kwargs)

sqlite3.connect = _patched_connect
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Resolve project root so imports work regardless of cwd ────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from config.settings import DB_PATH, WATCH_SYMBOLS, LOT_SIZES
    from config.runtime_config import (
        ALLOWED_SCAN_FREQUENCIES,
        MIN_SCAN_FREQUENCY,
        MAX_SCAN_FREQUENCY,
        get_scan_frequency_minutes,
        set_scan_frequency_minutes,
        get_scan_frequency_nse,
        get_scan_frequency_mcx,
        set_scan_frequency_nse,
        set_scan_frequency_mcx,
    )
except ImportError:
    DB_PATH = ROOT / "data" / "nsebot.db"
    WATCH_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "NATURALGAS"]
    ALLOWED_SCAN_FREQUENCIES = [5, 15, 30, 60, 180, 1440]
    MIN_SCAN_FREQUENCY = 5
    MAX_SCAN_FREQUENCY = 1440
    LOT_SIZES = {
        "NIFTY": 25,
        "BANKNIFTY": 15,
        "FINNIFTY": 25,
        "MIDCPNIFTY": 50,
        "NATURALGAS": 1250,
        "CRUDEOIL": 100,
        "GOLD": 100,
        "SILVER": 30,
    }


    def get_scan_frequency_minutes() -> int:
        return 5

    def set_scan_frequency_minutes(minutes: int) -> int:
        return int(minutes)

try:
    from fastapi import FastAPI, Query, Depends, HTTPException, status
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.security import HTTPBasic, HTTPBasicCredentials
    import uvicorn
    import secrets
except ImportError:
    print("[ERROR] Run: python -m pip install fastapi uvicorn")
    sys.exit(1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        import anyio.to_thread
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = 15
        log.info("Startup: Limited AnyIO default thread pool capacity to 15 to prevent thread/memory exhaustion")
    except Exception as exc:
        log.warning("Could not set AnyIO thread pool limit: %s", exc)

    # Warm up instrument cache at startup so resolve_instrument() doesn't miss
    import threading
    def _warmup_instrument_cache():
        try:
            from src.engine.symbol_resolver import _instrument_cache_is_ready, fetch_and_cache_instruments
            if _instrument_cache_is_ready():
                return
            from src.engine.live_trading import get_kite_client
            kite = get_kite_client()
            if kite:
                log.info("[startup] Warming up instrument cache in background...")
                fetch_and_cache_instruments(kite)
            else:
                log.info("[startup] Kite not connected; instrument cache warm-up skipped.")
        except Exception as exc:
            log.warning("[startup] Instrument cache warm-up failed: %s", exc)

    threading.Thread(target=_warmup_instrument_cache, daemon=True, name="instrument-cache-warmup").start()

    yield


app = FastAPI(title="NSEBOT Dashboard API", lifespan=lifespan)
log = logging.getLogger("nsebot.dashboard")

security = HTTPBasic(auto_error=False)

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    import secrets
    from fastapi import HTTPException, status
    from config.runtime_config import load_runtime_config
    
    runtime_config = load_runtime_config()
    
    # If authentication is disabled (default), bypass credentials check
    if not runtime_config.get("dashboard_auth_enabled", False):
        return "anonymous"
        
    # Authentication is enabled, check credentials
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
        
    from config.settings import DASHBOARD_USERNAME, DASHBOARD_PASSWORD
    correct_username = secrets.compare_digest(credentials.username, DASHBOARD_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# Graceful Disconnect Middleware to suppress noisy ASGI connection reset tracebacks
try:
    from starlette.exceptions import ClientDisconnect
except ImportError:
    class ClientDisconnect(Exception):
        pass

try:
    import anyio
    _DISCONNECT_ERRORS = (
        ConnectionResetError,
        OSError,
        ClientDisconnect,
        anyio.BrokenResourceError,
        anyio.EndOfStream,
    )
except Exception:
    _DISCONNECT_ERRORS = (ConnectionResetError, OSError, ClientDisconnect)


class GracefulDisconnectMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            try:
                await send(message)
            except _DISCONNECT_ERRORS:
                pass

        try:
            await self.app(scope, receive, send_wrapper)
        except _DISCONNECT_ERRORS:
            pass


app.add_middleware(GracefulDisconnectMiddleware)





try:
    from src.engine.intelligence import generate_intelligence
except Exception:
    generate_intelligence = None

try:
    from src.fetchers.chart_fetcher import get_chart_fetcher
except Exception:
    get_chart_fetcher = None

_SCANX_HEATMAP_API = "https://ow-scanx-analytics.dhan.co/customscan/fetchdt"
_TV_NEWS_API = (
    "https://news-mediator.tradingview.com/public/news-flow/v2/news"
    "?filter=lang%3Aen&filter=symbol%3AMCX%3ANATURALGAS1!"
    "&client=landing&streaming=false&user_prostatus=non_pro"
)
_SCANX_INDEX_IDS = {"NIFTY": "13", "BANKNIFTY": "25"}
_EXT_CACHE: dict[str, dict] = {}
_MCX_SYMBOLS = {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}

# ── DB helper ─────────────────────────────────────────────────────────────

def _db():
    try:
        from config.settings import DB_PATH as settings_db_path
        db_p = settings_db_path
    except ImportError:
        db_p = DB_PATH
    conn = sqlite3.connect(db_p)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql: str, params: tuple = ()):
    conn = _db()
    try:
        with conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _cache_get(key: str, ttl_sec: int):
    item = _EXT_CACHE.get(key)
    if not item:
        return None
    if time.time() - float(item.get("ts") or 0) > ttl_sec:
        return None
    return item.get("data")


def _cache_set(key: str, data):
    _EXT_CACHE[key] = {"ts": time.time(), "data": data}
    return data


def _latest_snapshot_rows(symbol: str) -> list[dict]:
    symbols = _matching_symbols(symbol)
    placeholders, params = _in_clause(symbols)
    rows = _q(
        f"""
        SELECT * FROM option_chain_snapshots
        WHERE symbol IN ({placeholders}) AND fetched_at=(
            SELECT MAX(fetched_at) FROM option_chain_snapshots WHERE symbol IN ({placeholders})
        )
        ORDER BY strike
        """,
        (*params, *params),
    )
    return rows


def _latest_underlying_rows(symbol: str, n: int = 2) -> list[dict]:
    symbols = _matching_symbols(symbol)
    placeholders, params = _in_clause(symbols)
    rows = _q(
        f"SELECT fetched_at, price FROM underlying_price WHERE symbol IN ({placeholders}) ORDER BY fetched_at DESC LIMIT ?",
        (*params, int(n)),
    )
    return rows


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _max_pain(rows: list[dict]) -> float | None:
    ce_map = {float(r["strike"]): int(r.get("oi") or 0) for r in rows if r.get("option_type") == "CE"}
    pe_map = {float(r["strike"]): int(r.get("oi") or 0) for r in rows if r.get("option_type") == "PE"}
    strikes = sorted(set(ce_map) | set(pe_map))
    if not strikes:
        return None
    best = None
    best_strike = None
    for cand in strikes:
        pain = sum((cand - s) * oi for s, oi in ce_map.items() if cand > s)
        pain += sum((s - cand) * oi for s, oi in pe_map.items() if cand < s)
        if best is None or pain < best:
            best = pain
            best_strike = cand
    return best_strike


def _chain_context(rows: list[dict], underlying: float, prev_underlying: float | None) -> dict:
    ce_rows = [r for r in rows if r.get("option_type") == "CE"]
    pe_rows = [r for r in rows if r.get("option_type") == "PE"]
    strikes = sorted({float(r.get("strike") or 0) for r in rows if r.get("strike") is not None})
    atm = min(strikes, key=lambda s: abs(s - underlying)) if strikes and underlying > 0 else (strikes[0] if strikes else None)

    total_ce_oi = sum(int(r.get("oi") or 0) for r in ce_rows)
    total_pe_oi = sum(int(r.get("oi") or 0) for r in pe_rows)
    ce_oi_change = sum(int(r.get("oi_change") or 0) for r in ce_rows)
    pe_oi_change = sum(int(r.get("oi_change") or 0) for r in pe_rows)
    pcr = (total_pe_oi / total_ce_oi) if total_ce_oi else None

    support = None
    resistance = None
    if pe_rows:
        below = [r for r in pe_rows if r.get("strike") is not None and float(r["strike"]) <= underlying]
        support = float(max((below or pe_rows), key=lambda r: int(r.get("oi") or 0))["strike"])
    if ce_rows:
        above = [r for r in ce_rows if r.get("strike") is not None and float(r["strike"]) >= underlying]
        resistance = float(max((above or ce_rows), key=lambda r: int(r.get("oi") or 0))["strike"])
    if support is not None and resistance is not None and support >= resistance and strikes:
        lower = [s for s in strikes if s < underlying]
        upper = [s for s in strikes if s > underlying]
        if lower:
            support = lower[-1]
        if upper:
            resistance = upper[0]

    price_change_pct = None
    if prev_underlying and prev_underlying != 0 and underlying > 0:
        price_change_pct = round((underlying - prev_underlying) / abs(prev_underlying) * 100, 4)

    return {
        "underlying": underlying,
        "price_change_pct": price_change_pct,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "ce_oi_change": ce_oi_change,
        "pe_oi_change": pe_oi_change,
        "pcr": round(pcr, 4) if pcr is not None else None,
        "atm_strike": atm,
        "max_pain": _max_pain(rows),
        "support": support,
        "resistance": resistance,
    }


def _chart_payload(symbol: str, underlying: float) -> dict:
    key = f"chart:{symbol.upper()}"
    cached = _cache_get(key, 240)
    if cached is not None:
        return cached
    if get_chart_fetcher is None:
        return {}
    try:
        payload = get_chart_fetcher().fetch(symbol, reference_price=underlying) or {}
        slim = {}
        for tf in ("1h", "3h"):
            tf_data = payload.get(tf)
            if isinstance(tf_data, dict):
                slim[tf] = {
                    "sentiment": str(tf_data.get("sentiment") or "NEUTRAL").upper(),
                    "ohlc": tf_data.get("ohlc") or {},
                }
        return _cache_set(key, slim)
    except Exception as exc:
        log.warning("chart payload failed for %s: %s", symbol, exc)
        return {}


def _synthetic_chart_payload(symbol: str) -> dict:
    """
    Build fallback 1H/3H sentiments from underlying_price history.
    Used when external chart payload is unavailable.
    """
    symbols = _matching_symbols(symbol)
    placeholders, params = _in_clause(symbols)
    rows = _q(
        f"SELECT fetched_at, price FROM underlying_price WHERE symbol IN ({placeholders}) ORDER BY fetched_at",
        params,
    )
    if len(rows) < 2:
        return {}

    try:
        latest_ts = datetime.fromisoformat(str(rows[-1]["fetched_at"]))
    except Exception:
        return {}

    def _window(tf_min: int) -> dict | None:
        cutoff = latest_ts - timedelta(minutes=tf_min)
        in_window = []
        for r in rows:
            try:
                ts = datetime.fromisoformat(str(r["fetched_at"]))
            except Exception:
                continue
            if ts >= cutoff:
                in_window.append(r)
        if len(in_window) < 2:
            in_window = rows[-2:]
        if len(in_window) < 2:
            return None
        o = _safe_float(in_window[0].get("price"), 0.0)
        c = _safe_float(in_window[-1].get("price"), 0.0)
        h = max(_safe_float(x.get("price"), 0.0) for x in in_window)
        l = min(_safe_float(x.get("price"), 0.0) for x in in_window)
        sentiment = "NEUTRAL"
        if c > o:
            sentiment = "BULLISH"
        elif c < o:
            sentiment = "BEARISH"
        return {
            "sentiment": sentiment,
            "ohlc": {"open": round(o, 2), "high": round(h, 2), "low": round(l, 2), "close": round(c, 2)},
        }

    out = {}
    h1 = _window(60)
    h3 = _window(180)
    if h1:
        out["1h"] = h1
    if h3:
        out["3h"] = h3
    return out


def _news_sentiment_score(title: str) -> int:
    t = (title or "").lower()
    pos = ["rally", "rises", "rise", "gain", "surge", "jump", "bullish", "tight", "demand", "up"]
    neg = ["fall", "falls", "drop", "retreat", "decline", "slump", "bearish", "oversupply", "cools", "down"]
    score = 0
    for w in pos:
        if w in t:
            score += 1
    for w in neg:
        if w in t:
            score -= 1
    return score


def _dir_label(score: float) -> str:
    if score >= 0.35:
        return "BULLISH"
    if score <= -0.35:
        return "BEARISH"
    return "MIXED"


def _fetch_natgas_news() -> dict:
    cached = _cache_get("natgas_news", 600)
    if cached is not None:
        return cached
    try:
        res = requests.get(_TV_NEWS_API, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        payload = res.json() if res.content else {}
        items = payload.get("items") if isinstance(payload, dict) else []
        cutoff = int(time.time()) - 86400
        rows = []
        for item in (items or []):
            pub = int(item.get("published") or 0)
            if pub < cutoff:
                continue
            title = str(item.get("title") or "").strip()
            prov_val = item.get("provider")
            if isinstance(prov_val, dict):
                provider = (prov_val.get("name") or "").strip()
            elif isinstance(prov_val, str):
                provider = prov_val.strip()
            else:
                provider = ""
            story_path = str(item.get("storyPath") or "").strip()
            story_url = f"https://in.tradingview.com{story_path}" if story_path.startswith("/") else ""
            rows.append(
                {
                    "title": title,
                    "provider": provider,
                    "published": pub,
                    "published_at": datetime.fromtimestamp(pub, timezone.utc).isoformat(),
                    "url": story_url,
                    "score": _news_sentiment_score(title),
                }
            )
        rows.sort(key=lambda x: x["published"], reverse=True)
        current_items = rows[:5]
        current_score = (sum(r["score"] for r in current_items) / len(current_items)) if current_items else 0.0
        day_score = (sum(r["score"] for r in rows) / len(rows)) if rows else 0.0
        out = {
            "items": rows[:20],
            "count_24h": len(rows),
            "current_news_direction": _dir_label(current_score),
            "potential_news_direction": _dir_label(day_score),
            "news_score_current": round(current_score, 3),
            "news_score_day": round(day_score, 3),
        }
        return _cache_set("natgas_news", out)
    except Exception as exc:
        log.warning("natgas news fetch failed: %s", exc)
        return {"items": [], "count_24h": 0, "current_news_direction": "MIXED", "potential_news_direction": "MIXED"}


def _fetch_scanx_heatmap(symbol: str) -> dict:
    sym = symbol.upper()
    if sym not in _SCANX_INDEX_IDS:
        return {}
    key = f"heatmap:{sym}"
    cached = _cache_get(key, 300)
    if cached is not None:
        return cached
    idx_id = _SCANX_INDEX_IDS[sym]
    payload = {
        "data": {
            "params": [
                {"field": "idxlist.Indexid", "op": "", "val": idx_id},
                {"field": "Exch", "op": "", "val": "NSE"},
            ],
            "logic_op": "AND",
            "fields": [
                "Mcap", "Pe", "Pb", "Volume", "AvgVol1week", "AvgVol1mon",
                "PPerchange", "PricePerchng1week", "PricePerchng1mon",
                "PricePerchng3mon", "PricePerchng6mon", "PricePerchng1year",
                "PricePerchng5year", "Sym", "Sector", "PChange", "Sid",
                "Exch", "Isin", "DispSym",
            ],
            "count": 500,
            "sort": "Mcap",
            "sorder": "desc",
        }
    }
    # Retry up to 3 times; 3rd attempt disables SSL verify as fallback
    rows = []
    last_exc = None
    for attempt in range(3):
        try:
            res = requests.post(
                _SCANX_HEATMAP_API,
                json=payload,
                timeout=15,
                verify=(attempt < 2),  # SSL verify on attempts 0,1; off on attempt 2
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            res.raise_for_status()
            body = res.json() if res.content else {}
            data = body.get("data") if isinstance(body, dict) else []
            rows = data if isinstance(data, list) else []
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    try:
        if last_exc is not None:
            raise last_exc
        adv = 0
        dec = 0
        total_mcap = 0.0
        weighted = 0.0
        for r in rows:
            pch = _safe_float(r.get("PPerchange"), 0.0)
            mcap = _safe_float(r.get("Mcap"), 0.0)
            if pch > 0:
                adv += 1
            elif pch < 0:
                dec += 1
            if mcap > 0:
                total_mcap += mcap
                weighted += pch * mcap
        wm = weighted / total_mcap if total_mcap > 0 else 0.0
        top = sorted(rows, key=lambda x: abs(_safe_float(x.get("PPerchange"), 0.0)), reverse=True)[:8]
        out = {
            "total": len(rows),
            "adv": adv,
            "dec": dec,
            "weighted_change": round(wm, 3),
            "heatmap_direction": _dir_label(wm / 1.5 if wm else 0.0),
            "top_moves": [
                {
                    "symbol": str(r.get("Sym") or ""),
                    "name": str(r.get("DispSym") or ""),
                    "pchange": round(_safe_float(r.get("PPerchange"), 0.0), 3),
                }
                for r in top
            ],
        }
        return _cache_set(key, out)
    except Exception as exc:
        log.warning("scanx heatmap fetch failed for %s: %s", sym, exc)
        return {"total": 0, "adv": 0, "dec": 0, "weighted_change": 0.0, "heatmap_direction": "MIXED", "top_moves": []}


def _parse_intel_fields(raw) -> dict:
    # Fast-path: IntelligenceResult has all fields natively (Phase 3)
    try:
        from src.engine.intelligence import IntelligenceResult
        if isinstance(raw, IntelligenceResult):
            summary_lines = [
                x for x in [
                    f"Verdict: {raw.verdict_label}",
                    f"Confidence: {raw.confidence}%",
                    raw.action_plan,
                    raw.risk_note,
                    f"Trend: {raw.trend}" if raw.trend else "",
                ] if x
            ]
            return {
                "verdict":       raw.verdict_label,
                "confidence":    raw.confidence,
                "action":        raw.action_plan,
                "warning":       raw.risk_note,
                "trend":         raw.trend,
                "summary_lines": summary_lines,
            }
    except ImportError:
        pass
    # Legacy: parse from Telegram text string
    text = str(raw or "")
    verdict = "UNKNOWN"
    confidence = 0
    trend = ""
    action = ""
    warning = ""
    m = re.search(r"\*Verdict:\s*([^\*]+)\*", text)
    if m:
        verdict = m.group(1).strip()
    m = re.search(r"Confidence:\s*([0-9]{1,3})%", text)
    if m:
        confidence = int(m.group(1))
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- Action Plan:"):
            action = s.split(":", 1)[1].strip()
        elif s.startswith("- Critical Warning:"):
            warning = s.split(":", 1)[1].strip()
        elif "Broader Trend:" in s:
            trend = s.split(":", 1)[1].strip().strip("*")
    summary_lines = [x for x in [f"Verdict: {verdict}", f"Confidence: {confidence}%", action, warning, f"Trend: {trend}" if trend else ""] if x]
    return {"verdict": verdict, "confidence": confidence, "action": action, "warning": warning, "trend": trend, "summary_lines": summary_lines}


def _chart_dir_score(chart_payload: dict) -> float:
    score_map = {"BULLISH": 1.0, "BEARISH": -1.0, "NEUTRAL": 0.0}
    vals = []
    for tf in ("1h", "3h"):
        tf_data = chart_payload.get(tf) if isinstance(chart_payload, dict) else None
        if isinstance(tf_data, dict):
            vals.append(score_map.get(str(tf_data.get("sentiment") or "NEUTRAL").upper(), 0.0))
    return (sum(vals) / len(vals)) if vals else 0.0


# ── API routes ────────────────────────────────────────────────────────────

# Known valid symbols — filter junk from chrome extension
CANONICAL_SYMBOLS = [
    "NIFTY", "BANKNIFTY", "NATURALGAS", "CRUDEOIL"
]

_CANONICAL_SET = set(CANONICAL_SYMBOLS)
_ALIASES = {
    "NIFTY 50": "NIFTY",
    "NIFTY50": "NIFTY",
    "HDFC BANK": "HDFCBANK",
}


def _canonical_symbol(symbol: str | None) -> str:
    s = str(symbol or "").upper().strip()
    if not s:
        return ""
    if s in _ALIASES:
        return _ALIASES[s]
    first = s.split()[0]
    if first in _CANONICAL_SET:
        return first
    return s


def _matching_symbols(symbol: str) -> list[str]:
    canonical = _canonical_symbol(symbol)
    rows = _q("SELECT DISTINCT symbol FROM option_chain_snapshots")
    matches = [r["symbol"] for r in rows if _canonical_symbol(r["symbol"]) == canonical]
    return sorted(set(matches)) or [canonical or symbol]


def _in_clause(values: list[str]) -> tuple[str, tuple]:
    return ",".join("?" for _ in values), tuple(values)

@app.get("/api/symbols")
async def get_symbols():
    rows = _q("SELECT DISTINCT symbol FROM option_chain_snapshots ORDER BY symbol")
    configured = [_canonical_symbol(s) for s in WATCH_SYMBOLS]
    from_db = [_canonical_symbol(r["symbol"]) for r in rows]
    seen = set()
    out = []
    for sym in [*configured, *CANONICAL_SYMBOLS, *from_db]:
        if sym in _CANONICAL_SET and sym not in seen:
            out.append(sym)
            seen.add(sym)
    return out


@app.get("/api/meta")
async def get_meta(symbol: str):
    symbols = _matching_symbols(symbol)
    placeholders, params = _in_clause(symbols)
    rows = _q(
        "SELECT MAX(fetched_at) AS last_fetch, COUNT(DISTINCT fetched_at) AS snapshots "
        f"FROM option_chain_snapshots WHERE symbol IN ({placeholders})",
        params
    )
    return rows[0] if rows else {}


@app.get("/api/price")
async def get_price(symbol: str, hours: int = 6):
    symbols = _matching_symbols(symbol)
    placeholders, symbol_params = _in_clause(symbols)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = _q(
        "SELECT fetched_at, price FROM underlying_price "
        f"WHERE symbol IN ({placeholders}) AND fetched_at>=? ORDER BY fetched_at",
        (*symbol_params, cutoff)
    )
    # Fallback: return all data if nothing in window
    if not rows:
        rows = _q(
            "SELECT fetched_at, price FROM underlying_price "
            f"WHERE symbol IN ({placeholders}) ORDER BY fetched_at",
            symbol_params
        )
    return rows


@app.get("/api/topbar_cmps")
def get_topbar_cmps():
    symbols = ["NIFTY", "BANKNIFTY", "NATURALGAS", "CRUDEOIL"]
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    res = {}
    for sym in symbols:
        # Find latest price
        latest_price = None
        latest_sym = sym
        row = _q("SELECT symbol, price FROM underlying_price WHERE symbol=? ORDER BY fetched_at DESC LIMIT 1", (sym,))
        if row:
            latest_price = float(row[0]["price"])
            latest_sym = row[0]["symbol"]
        else:
            row2 = _q("SELECT symbol, price FROM underlying_price WHERE symbol LIKE ? ORDER BY fetched_at DESC LIMIT 20", (f"{sym}%",))
            for r in row2:
                if _canonical_symbol(r["symbol"]) == sym:
                    latest_price = float(r["price"])
                    latest_sym = r["symbol"]
                    break
        
        if latest_price is not None:
            # Find previous close price (last price before today_start UTC)
            prev_price = None
            prev_row = _q(
                "SELECT price FROM underlying_price WHERE symbol=? AND fetched_at < ? ORDER BY fetched_at DESC LIMIT 1",
                (latest_sym, today_start)
            )
            if prev_row:
                prev_price = float(prev_row[0]["price"])
            else:
                # Fallback to general LIKE search
                prev_row2 = _q(
                    "SELECT symbol, price FROM underlying_price WHERE symbol LIKE ? AND fetched_at < ? ORDER BY fetched_at DESC LIMIT 20",
                    (f"{sym}%", today_start)
                )
                for pr in prev_row2:
                    if _canonical_symbol(pr["symbol"]) == sym:
                        prev_price = float(pr["price"])
                        break
            
            if prev_price is not None and prev_price > 0:
                change = latest_price - prev_price
                pct_change = (change / prev_price) * 100
            else:
                change = 0.0
                pct_change = 0.0
                
            res[sym] = {
                "price": round(latest_price, 2),
                "change": round(change, 2),
                "pct_change": round(pct_change, 2)
            }
        else:
            res[sym] = None
    return res



@app.get("/api/oi")
async def get_oi(symbol: str):
    symbols = _matching_symbols(symbol)
    placeholders, params = _in_clause(symbols)
    rows = _q(
        f"""
        SELECT strike, option_type, oi, oi_change, ltp, iv
        FROM option_chain_snapshots
        WHERE symbol IN ({placeholders}) AND fetched_at=(
            SELECT MAX(fetched_at) FROM option_chain_snapshots WHERE symbol IN ({placeholders})
        )
        ORDER BY strike
        """,
        (*params, *params)
    )
    return rows


@app.get("/api/pcr")
async def get_pcr(symbol: str, hours: int = 6):
    symbols = _matching_symbols(symbol)
    placeholders, symbol_params = _in_clause(symbols)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = _q(
        f"""
        SELECT fetched_at,
               ROUND(
                 SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) * 1.0 /
                 NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END), 0),
                 3
               ) AS pcr
        FROM option_chain_snapshots
        WHERE symbol IN ({placeholders}) AND fetched_at>=?
        GROUP BY fetched_at
        ORDER BY fetched_at
        """,
        (*symbol_params, cutoff)
    )
    # Fallback: return all data if nothing in window
    if not rows:
        rows = _q(
            f"""
            SELECT fetched_at,
                   ROUND(
                     SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) * 1.0 /
                     NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END), 0),
                     3
                   ) AS pcr
            FROM option_chain_snapshots
            WHERE symbol IN ({placeholders})
            GROUP BY fetched_at
            ORDER BY fetched_at
            """,
            symbol_params
        )
    return rows


@app.get("/api/alerts")
async def get_alerts(symbol: str, limit: int = 100):
    symbols = _matching_symbols(symbol)
    placeholders, params = _in_clause(symbols)
    rows = _q(
        f"SELECT fired_at, alert_type, strike, option_type, expiry, severity, telegram_sent, detail_json "
        f"FROM anomaly_alerts WHERE symbol IN ({placeholders}) ORDER BY fired_at DESC LIMIT ?",
        (*params, limit)
    )
    return rows


@app.get("/api/intelligence_summary")
def get_intelligence_summary(symbol: str):
    sym = _canonical_symbol(symbol)
    rows = _latest_snapshot_rows(sym)
    if not rows:
        return {
            "symbol": sym,
            "available": False,
            "summary_lines": ["No latest scan snapshot available."],
            "news": None,
            "heatmap": None,
        }

    latest_fetch = rows[0].get("fetched_at")
    up_rows = _latest_underlying_rows(sym, n=2)
    underlying = _safe_float((up_rows[0] if up_rows else {}).get("price"), 0.0)
    prev_underlying = _safe_float((up_rows[1] if len(up_rows) > 1 else {}).get("price"), 0.0) or None
    ctx = _chain_context(rows, underlying, prev_underlying)
    chart_payload = _chart_payload(sym, underlying)
    fallback_chart = _synthetic_chart_payload(sym)
    if "1h" not in chart_payload and "1h" in fallback_chart:
        chart_payload["1h"] = fallback_chart["1h"]
    if "3h" not in chart_payload and "3h" in fallback_chart:
        chart_payload["3h"] = fallback_chart["3h"]
    if chart_payload:
        ctx["chart_indicators"] = chart_payload

    symbols = _matching_symbols(sym)
    placeholders, params = _in_clause(symbols)
    alert_rows = _q(
        f"SELECT fired_at, symbol, alert_type, strike, option_type, expiry, detail_json, severity "
        f"FROM anomaly_alerts WHERE symbol IN ({placeholders}) ORDER BY fired_at DESC LIMIT 80",
        params,
    )
    alert_rows = list(reversed(alert_rows))

    intel_text = ""
    if generate_intelligence is not None:
        try:
            intel_text = generate_intelligence(sym, alert_rows, scan_context=ctx) or ""
        except Exception as exc:
            log.warning("intelligence generation failed for %s: %s", sym, exc)
    intel = _parse_intel_fields(intel_text)

    oi_score = 0.0
    ce_chg = _safe_float(ctx.get("ce_oi_change"), 0.0)
    pe_chg = _safe_float(ctx.get("pe_oi_change"), 0.0)
    pcr = _safe_float(ctx.get("pcr"), 0.0)
    if pe_chg > ce_chg:
        oi_score += 0.6
    elif ce_chg > pe_chg:
        oi_score -= 0.6
    if pcr > 1.0:
        oi_score += 0.4
    elif 0 < pcr < 1.0:
        oi_score -= 0.4
    chart_score = _chart_dir_score(chart_payload)

    out = {
        "symbol": sym,
        "available": True,
        "latest_fetch": latest_fetch,
        "summary_lines": intel.get("summary_lines") or [f"Verdict: {intel.get('verdict', 'UNKNOWN')}"],
        "verdict": intel.get("verdict", "UNKNOWN"),
        "confidence": int(intel.get("confidence") or 0),
        "trend": intel.get("trend") or "",
        "context": {
            "underlying": ctx.get("underlying"),
            "atm_strike": ctx.get("atm_strike"),
            "pcr": ctx.get("pcr"),
            "support": ctx.get("support"),
            "resistance": ctx.get("resistance"),
            "max_pain": ctx.get("max_pain"),
            "ce_oi_change": ctx.get("ce_oi_change"),
            "pe_oi_change": ctx.get("pe_oi_change"),
            "chart": chart_payload,
        },
        "components": {
            "oi_score": round(oi_score, 3),
            "chart_score": round(chart_score, 3),
        },
        "news": None,
        "heatmap": None,
        "market_direction_current": "MIXED",
        "market_direction_potential": "MIXED",
    }

    if sym == "NATURALGAS":
        news = _fetch_natgas_news()
        news_cur = _safe_float(news.get("news_score_current"), 0.0)
        news_day = _safe_float(news.get("news_score_day"), 0.0)
        current_score = (news_cur * 0.60) + (oi_score * 0.25) + (chart_score * 0.15)
        potential_score = (news_day * 0.50) + (oi_score * 0.30) + (chart_score * 0.20)
        out["news"] = news
        out["components"]["news_score_current"] = round(news_cur, 3)
        out["components"]["news_score_day"] = round(news_day, 3)
        out["market_direction_current"] = _dir_label(current_score)
        out["market_direction_potential"] = _dir_label(potential_score)
    elif sym in ("NIFTY", "BANKNIFTY"):
        heat = _fetch_scanx_heatmap(sym)
        heat_norm = max(-1.0, min(1.0, _safe_float(heat.get("weighted_change"), 0.0) / 1.5))
        combined = (heat_norm * 0.50) + (oi_score * 0.30) + (chart_score * 0.20)
        out["heatmap"] = heat
        out["components"]["heatmap_score"] = round(heat_norm, 3)
        out["market_direction_current"] = _dir_label(combined)
        out["market_direction_potential"] = _dir_label((combined * 0.7) + (chart_score * 0.3))
    else:
        combo = (oi_score * 0.6) + (chart_score * 0.4)
        out["market_direction_current"] = _dir_label(combo)
        out["market_direction_potential"] = _dir_label(combo)

    return out


@app.get("/api/expiries")
async def get_expiries(symbol: str):
    symbols = _matching_symbols(symbol)
    placeholders, params = _in_clause(symbols)
    rows = _q(
        f"SELECT DISTINCT expiry FROM option_chain_snapshots WHERE symbol IN ({placeholders}) ORDER BY expiry",
        params
    )
    return [r["expiry"] for r in rows]


@app.get("/api/runtime")
async def get_runtime():
    return {
        "scan_frequency_minutes": get_scan_frequency_minutes(),
        "scan_frequency_nse": get_scan_frequency_nse(),
        "scan_frequency_mcx": get_scan_frequency_mcx(),
        "scan_frequency_options": ALLOWED_SCAN_FREQUENCIES,
        "min_scan_frequency_minutes": MIN_SCAN_FREQUENCY,
        "max_scan_frequency_minutes": MAX_SCAN_FREQUENCY,
    }


@app.post("/api/runtime")
async def set_runtime(
    scan_frequency_minutes: int | None = Query(None),
    scan_frequency_nse: int | None = Query(None),
    scan_frequency_mcx: int | None = Query(None),
):
    if scan_frequency_minutes is not None:
        if scan_frequency_minutes not in ALLOWED_SCAN_FREQUENCIES:
            return JSONResponse(
                {"ok": False, "error": "invalid scan_frequency_minutes", "allowed": ALLOWED_SCAN_FREQUENCIES},
                status_code=400,
            )
        set_scan_frequency_minutes(scan_frequency_minutes)
    if scan_frequency_nse is not None:
        if scan_frequency_nse not in ALLOWED_SCAN_FREQUENCIES:
            return JSONResponse(
                {"ok": False, "error": "invalid scan_frequency_nse", "allowed": ALLOWED_SCAN_FREQUENCIES},
                status_code=400,
            )
        set_scan_frequency_nse(scan_frequency_nse)
    if scan_frequency_mcx is not None:
        if scan_frequency_mcx not in ALLOWED_SCAN_FREQUENCIES:
            return JSONResponse(
                {"ok": False, "error": "invalid scan_frequency_mcx", "allowed": ALLOWED_SCAN_FREQUENCIES},
                status_code=400,
            )
        set_scan_frequency_mcx(scan_frequency_mcx)
    
    return {
        "ok": True,
        "scan_frequency_minutes": get_scan_frequency_minutes(),
        "scan_frequency_nse": get_scan_frequency_nse(),
        "scan_frequency_mcx": get_scan_frequency_mcx(),
    }


def _enrich_open_trades_with_live_pnl(rows: list[dict]) -> None:
    for row in rows:
        if row.get("status") != "OPEN":
            continue
            
        symbol = str(row.get("symbol") or "").upper().strip()
        option_type = str(row.get("option_type") or "").upper().strip()
        strike = row.get("strike")
        
        cmp = None
        if option_type == "FUT" or not option_type or strike is None:
            res = _q("SELECT price FROM underlying_price WHERE symbol=? ORDER BY fetched_at DESC LIMIT 1", (symbol,))
            if res:
                cmp = res[0]["price"]
        else:
            try:
                strike_val = float(strike)
            except (ValueError, TypeError):
                strike_val = 0.0
            res = _q(
                "SELECT ltp FROM option_chain_snapshots WHERE symbol=? AND ABS(strike - ?) < 0.01 AND option_type=? ORDER BY fetched_at DESC LIMIT 1",
                (symbol, strike_val, option_type)
            )
            if res:
                cmp = res[0]["ltp"]
                
        if cmp is not None and cmp > 0:
            if option_type == "FUT":
                entry = float(row.get("entry_underlying") or 0.0)
            else:
                entry = float(row.get("entry_premium") or row.get("entry_underlying") or 0.0)
            
            lots = int(row.get("lots") or 1)
            side = str(row.get("side") or "BUY").upper().strip()
            lot_size = LOT_SIZES.get(symbol, 1)
            
            pnl = (cmp - entry) * lots * lot_size if side == "BUY" else (entry - cmp) * lots * lot_size
            
            row["pnl_rupees"] = round(pnl, 2)
            row["cmp"] = cmp



def _enrich_trade_details(rows: list[dict]) -> None:
    from datetime import datetime
    for row in rows:
        # 1. Prefix verdict label with TF- if setup_type is TIMEFRAME or reason contains timeframe
        is_tf = (row.get("setup_type") == "TIMEFRAME") or ("timeframe" in str(row.get("reason") or "").lower())
        if is_tf:
            v = row.get("verdict_label")
            if v and not v.startswith("TF-"):
                row["verdict_label"] = f"TF-{v}"

        # 2. Calculate duration for closed trades
        if row.get("closed_at") and row.get("opened_at"):
            try:
                opened = datetime.fromisoformat(row["opened_at"].replace("Z", "+00:00"))
                closed = datetime.fromisoformat(row["closed_at"].replace("Z", "+00:00"))
                duration_sec = (closed - opened).total_seconds()
                row["duration_minutes"] = round(duration_sec / 60, 1)
                
                # Human-readable duration
                if duration_sec < 60:
                    row["duration_text"] = f"{int(duration_sec)}s"
                elif duration_sec < 3600:
                    row["duration_text"] = f"{int(duration_sec / 60)}m"
                else:
                    hours = int(duration_sec / 3600)
                    mins = int((duration_sec % 3600) / 60)
                    row["duration_text"] = f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
            except:
                row["duration_minutes"] = None
                row["duration_text"] = "-"
        else:
            row["duration_minutes"] = None
            row["duration_text"] = "-"
        
        # 3. Enrich with human-readable verdict explanation
        row["verdict_explanation"] = _explain_verdict(row.get("verdict_label"), row.get("option_type"))


@app.get("/api/paper_trades")
async def get_paper_trades(symbol: str = "", status: str = "", limit: int = 300):
    clauses = []
    params: list = []
    if symbol:
        clauses.append("symbol=?")
        params.append(symbol.upper().strip())
    if status:
        clauses.append("status=? COLLATE NOCASE")
        params.append(status.strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    
    # Query paper_trades UNION with live shadow trades
    sql = f"""
    SELECT * FROM paper_trades {where}
    UNION ALL
    SELECT * FROM live_trades WHERE trade_status='SHADOW' {f"AND {clauses[0]}" if clauses and symbol else ""} {f"AND status=? COLLATE NOCASE" if status else ""}
    ORDER BY opened_at DESC LIMIT ?
    """
    
    # Build params: paper_trades params + live_trades params + limit
    all_params = list(params)
    if symbol:
        all_params.append(symbol.upper().strip())
    if status:
        all_params.append(status.strip())
    all_params.append(int(limit))
    
    rows = _q(sql, all_params)
    _enrich_open_trades_with_live_pnl(rows)
    _enrich_trade_details(rows)
    return rows


def _explain_verdict(verdict: str | None, option_type: str | None) -> dict:
    """Convert verdict_label into human-readable explanation."""
    if not verdict:
        return {"bias": "Unknown", "strategy": "No verdict", "description": ""}
    
    ot = (option_type or "").upper()
    
    explanations = {
        "Long Buildup": {
            "bias": "Bullish",
            "strategy": "Fresh buying with rising OI",
            "description": "Price rising + Call OI increasing = Strong bullish momentum",
            "action": "Buy CE" if ot == "CE" else "Sell PE",
            "emoji": "📗"
        },
        "Short Buildup": {
            "bias": "Bearish",
            "strategy": "Fresh selling with rising OI",
            "description": "Price falling + Put OI increasing = Strong bearish momentum",
            "action": "Buy PE" if ot == "PE" else "Sell CE",
            "emoji": "📕"
        },
        "Put Writing": {
            "bias": "Bullish",
            "strategy": "Selling puts (bullish bet)",
            "description": "Put sellers confident price won't fall",
            "action": "Legacy CE proxy; current engine skips writing trades" if ot == "CE" else "Sell PE",
            "emoji": "📗"
        },
        "Call Writing": {
            "bias": "Bearish",
            "strategy": "Selling calls (bearish bet)",
            "description": "Call sellers confident price won't rise",
            "action": "Legacy PE proxy; current engine skips writing trades" if ot == "PE" else "Sell CE",
            "emoji": "📕"
        },
        "OI Bias Bullish": {
            "bias": "Cautious Bullish",
            "strategy": "OI + chart sentiment aligned bullish",
            "description": "1H/3H charts bullish + supportive OI pattern",
            "action": "Buy CE on breakout",
            "emoji": "🟡"
        },
        "OI Bias Bearish": {
            "bias": "Cautious Bearish",
            "strategy": "OI + chart sentiment aligned bearish",
            "description": "1H/3H charts bearish + supportive OI pattern",
            "action": "Buy PE on breakdown",
            "emoji": "🟠"
        },
        "Short Covering": {
            "bias": "Cautious Bullish",
            "strategy": "Rally from short exit",
            "description": "Price rising but from shorts closing, not fresh buying",
            "action": "Trail longs, avoid fresh entry",
            "emoji": "📒"
        },
        "Long Unwinding": {
            "bias": "Cautious Bearish",
            "strategy": "Decline from long exit",
            "description": "Price falling from longs closing, not aggressive shorts",
            "action": "Trail shorts, avoid fresh entry",
            "emoji": "📙"
        },
        "Sideways": {
            "bias": "Neutral",
            "strategy": "Range-bound market",
            "description": "No clear directional bias",
            "action": "Wait for breakout",
            "emoji": "⚪"
        },
        "TF-LONG": {
            "bias": "TF-Bullish",
            "strategy": "Timeframe Crossover Long",
            "description": "3H close breakout above previous candle high",
            "action": "Buy FUT" if ot == "FUT" else "Buy CE",
            "emoji": "🟦"
        },
        "TF-SHORT": {
            "bias": "TF-Bearish",
            "strategy": "Timeframe Crossover Short",
            "description": "3H close breakdown below previous candle low",
            "action": "Sell FUT" if ot == "FUT" else "Buy PE",
            "emoji": "🟦"
        }
    }
    
    return explanations.get(verdict, {
        "bias": verdict,
        "strategy": "Custom verdict",
        "description": "",
        "action": "",
        "emoji": "📘"
    })


@app.get("/api/paper_summary")
async def get_paper_summary(symbol: str = ""):
    clauses = []
    params: list = []
    if symbol:
        clauses.append("symbol=?")
        params.append(symbol.upper().strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    totals = _q(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross') THEN 1 ELSE 0 END) AS closed_count,
            SUM(CASE WHEN (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees < 0 THEN 1 ELSE 0 END) AS losses,
            ROUND(COALESCE(SUM(CASE WHEN status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross') THEN pnl_rupees ELSE 0 END), 0), 2) AS closed_pnl,
            ROUND(COALESCE(AVG(CASE WHEN status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross') THEN pnl_rupees END), 0), 2) AS avg_pnl,
            ROUND(COALESCE(AVG(CASE WHEN (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees > 0 THEN pnl_rupees END), 0), 2) AS avg_win,
            ROUND(COALESCE(AVG(CASE WHEN (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees < 0 THEN pnl_rupees END), 0), 2) AS avg_loss,
            MAX(CASE WHEN status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross') THEN pnl_rupees ELSE 0 END) AS max_win,
            MIN(CASE WHEN status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross') THEN pnl_rupees ELSE 0 END) AS max_loss
        FROM paper_trades
        {where}
        """,
        tuple(params),
    )
    open_rows = _q(
        f"SELECT * FROM paper_trades {where} {'AND' if where else 'WHERE'} status='OPEN' ORDER BY opened_at DESC",
        tuple(params),
    )
    _enrich_open_trades_with_live_pnl(open_rows)
    _enrich_trade_details(open_rows)
    
    # Symbol breakdown — normalise symbol to UPPER to avoid crudeoil/CRUDEOIL duplicates
    symbol_stats = _q(
        f"""
        SELECT
            UPPER(symbol) AS symbol,
            COUNT(*) AS total_trades,
            SUM(CASE WHEN (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees < 0 THEN 1 ELSE 0 END) AS losses,
            ROUND(COALESCE(SUM(CASE WHEN status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross') THEN pnl_rupees ELSE 0 END), 0), 2) AS total_pnl,
            ROUND(COALESCE(AVG(CASE WHEN status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross') THEN pnl_rupees END), 0), 2) AS avg_pnl,
            SUM(CASE WHEN status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross') THEN 1 ELSE 0 END) AS closed_count
        FROM paper_trades
        {where}
        GROUP BY UPPER(symbol)
        ORDER BY total_pnl DESC
        """,
        tuple(params),
    )
    
    # Calculate win rate per symbol
    for s in symbol_stats:
        closed = int(s.get("closed_count") or 0)
        wins = int(s.get("wins") or 0)
        s["win_rate"] = round((wins / closed) * 100, 2) if closed > 0 else 0.0
    
    out = totals[0] if totals else {}
    wins = int(out.get("wins") or 0)
    losses = int(out.get("losses") or 0)
    closed = int(out.get("closed_count") or 0)
    out["win_rate"] = round((wins / closed) * 100, 2) if closed > 0 else 0.0
    
    # Profit factor calculation
    if where:
        wins_where = f"{where} AND (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees > 0"
        losses_where = f"{where} AND (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees < 0"
    else:
        wins_where = "WHERE (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees > 0"
        losses_where = "WHERE (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees < 0"
    
    total_wins = sum(float(r.get("pnl_rupees") or 0) for r in _q(
        f"SELECT pnl_rupees FROM paper_trades {wins_where}",
        tuple(params)
    ))
    total_losses = abs(sum(float(r.get("pnl_rupees") or 0) for r in _q(
        f"SELECT pnl_rupees FROM paper_trades {losses_where}",
        tuple(params)
    )))
    out["profit_factor"] = round(total_wins / total_losses, 2) if total_losses > 0 else 0.0
    out["consecutive_wins"] = _calculate_consecutive_wins(where, tuple(params))
    
    # Phase 2: Holding period analysis
    out["holding_analysis"] = _calculate_holding_analysis(where, tuple(params))
    
    out["open_trades"] = open_rows
    out["symbol_breakdown"] = symbol_stats
    return out


def _calculate_holding_analysis(where: str, params: tuple) -> dict:
    """Calculate holding period distribution and metrics."""
    from datetime import datetime
    
    # Build WHERE clause properly
    if where:
        sql_where = f"{where} AND (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND closed_at IS NOT NULL"
    else:
        sql_where = "WHERE (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND closed_at IS NOT NULL"
    
    rows = _q(
        f"SELECT opened_at, closed_at, status FROM paper_trades {sql_where}",
        params
    )
    
    if not rows:
        return {
            "avg_duration_minutes": 0,
            "median_duration_minutes": 0,
            "min_duration_minutes": 0,
            "max_duration_minutes": 0,
            "distribution": {
                "under_5min": 0,
                "5_to_15min": 0,
                "15_to_30min": 0,
                "30_to_60min": 0,
                "over_60min": 0
            },
            "fastest_trade": None,
            "slowest_trade": None
        }
    
    durations = []
    for r in rows:
        try:
            opened = datetime.fromisoformat(r["opened_at"].replace("Z", "+00:00"))
            closed = datetime.fromisoformat(r["closed_at"].replace("Z", "+00:00"))
            duration_min = (closed - opened).total_seconds() / 60
            durations.append(duration_min)
        except:
            continue
    
    if not durations:
        return {
            "avg_duration_minutes": 0,
            "median_duration_minutes": 0,
            "min_duration_minutes": 0,
            "max_duration_minutes": 0,
            "distribution": {
                "under_5min": 0,
                "5_to_15min": 0,
                "15_to_30min": 0,
                "30_to_60min": 0,
                "over_60min": 0
            },
            "fastest_trade": None,
            "slowest_trade": None
        }
    
    # Calculate distribution
    under_5 = sum(1 for d in durations if d < 5)
    five_to_15 = sum(1 for d in durations if 5 <= d < 15)
    fifteen_to_30 = sum(1 for d in durations if 15 <= d < 30)
    thirty_to_60 = sum(1 for d in durations if 30 <= d < 60)
    over_60 = sum(1 for d in durations if d >= 60)
    
    # Sort for median
    sorted_durations = sorted(durations)
    median_idx = len(sorted_durations) // 2
    median = sorted_durations[median_idx] if sorted_durations else 0
    
    return {
        "avg_duration_minutes": round(sum(durations) / len(durations), 1),
        "median_duration_minutes": round(median, 1),
        "min_duration_minutes": round(min(durations), 1),
        "max_duration_minutes": round(max(durations), 1),
        "distribution": {
            "under_5min": under_5,
            "5_to_15min": five_to_15,
            "15_to_30min": fifteen_to_30,
            "30_to_60min": thirty_to_60,
            "over_60min": over_60
        },
        "distribution_pct": {
            "under_5min": round((under_5 / len(durations)) * 100, 1) if durations else 0,
            "5_to_15min": round((five_to_15 / len(durations)) * 100, 1) if durations else 0,
            "15_to_30min": round((fifteen_to_30 / len(durations)) * 100, 1) if durations else 0,
            "30_to_60min": round((thirty_to_60 / len(durations)) * 100, 1) if durations else 0,
            "over_60min": round((over_60 / len(durations)) * 100, 1) if durations else 0
        },
        "fastest_trade": _format_duration(min(durations)),
        "slowest_trade": _format_duration(max(durations))
    }


def _format_duration(minutes: float) -> str:
    """Format duration in human-readable format."""
    if minutes < 1:
        return f"{int(minutes * 60)}s"
    elif minutes < 60:
        return f"{int(minutes)}m"
    else:
        hours = int(minutes / 60)
        mins = int(minutes % 60)
        return f"{hours}h {mins}m" if mins > 0 else f"{hours}h"


def _calculate_consecutive_wins(where: str, params: tuple) -> int:
    """Calculate current consecutive wins/losses streak."""
    # Build WHERE clause properly
    if where:
        sql_where = f"{where} AND (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross'))"
    else:
        sql_where = "WHERE (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross'))"
    
    rows = _q(
        f"SELECT status, pnl_rupees FROM paper_trades {sql_where} ORDER BY closed_at DESC LIMIT 20",
        params
    )
    streak = 0
    for r in rows:
        pnl = float(r.get("pnl_rupees") or 0.0)
        if pnl > 0:
            streak += 1
        else:
            break
    return streak



@app.post("/api/paper_trades/close")
async def manual_close_paper_trade(trade_id: int = Query(...)):
    from src.models.schema import close_paper_trade, close_live_trade
    from datetime import datetime, timezone
    
    rows = _q("SELECT * FROM paper_trades WHERE id=?", (trade_id,))
    if not rows:
        return JSONResponse({"ok": False, "error": "Trade not found"}, status_code=404)
    row = rows[0]
    if row.get("status") != "OPEN":
        return JSONResponse({"ok": False, "error": "Trade is already closed"}, status_code=400)
        
    symbol = str(row.get("symbol") or "").upper().strip()
    option_type = str(row.get("option_type") or "").upper().strip()
    strike = row.get("strike")
    entry_underlying = float(row.get("entry_underlying") or 0.0)
    
    # 1. Fetch latest underlying price
    exit_und = None
    res_und = _q("SELECT price FROM underlying_price WHERE symbol=? ORDER BY fetched_at DESC LIMIT 1", (symbol,))
    if res_und:
        exit_und = res_und[0]["price"]
    else:
        exit_und = entry_underlying
        
    # 2. Fetch exit premium
    exit_prem = None
    if option_type in ("CE", "PE") and strike is not None:
        try:
            strike_val = float(strike)
        except (ValueError, TypeError):
            strike_val = 0.0
        res_opt = _q(
            "SELECT ltp FROM option_chain_snapshots WHERE symbol=? AND ABS(strike - ?) < 0.01 AND option_type=? ORDER BY fetched_at DESC LIMIT 1",
            (symbol, strike_val, option_type)
        )
        if res_opt:
            exit_prem = res_opt[0]["ltp"]
        else:
            exit_prem = row.get("entry_premium") or exit_und
    else:
        # FUT
        exit_prem = exit_und

    now_iso = datetime.now(timezone.utc).isoformat()
    
    # Check if the trade actually belongs to live_trades
    is_live = False
    with get_conn() as conn:
        res = conn.execute("SELECT 1 FROM live_trades WHERE id=?", (trade_id,)).fetchone()
        if res:
            is_live = True

    if is_live:
        close_live_trade(
            trade_id=trade_id,
            closed_at=now_iso,
            exit_underlying=exit_und,
            exit_premium=exit_prem,
            status="CLOSED_MANUAL",
            reason="Manual close via dashboard"
        )
    else:
        close_paper_trade(
            trade_id=trade_id,
            closed_at=now_iso,
            exit_underlying=exit_und,
            exit_premium=exit_prem,
            status="CLOSED_MANUAL",
            reason="Manual close via dashboard"
        )
    return {"ok": True, "trade_id": trade_id}


@app.delete("/api/paper_trades")
async def delete_paper_trades(date_from: str = "", date_to: str = ""):
    """
    Delete paper trades by date range.
    date_from / date_to: ISO date strings e.g. '2026-05-01' or '2026-05-26T23:59:59'
    At least one of date_from or date_to must be provided.
    """
    if not date_from and not date_to:
        return JSONResponse({"ok": False, "error": "Provide at least date_from or date_to"}, status_code=400)
    clauses = []
    params: list = []
    if date_from:
        clauses.append("opened_at >= ?")
        params.append(date_from)
    if date_to:
        # include the full end day
        end = date_to if "T" in date_to else date_to + "T23:59:59"
        clauses.append("opened_at <= ?")
        params.append(end)
    where = "WHERE " + " AND ".join(clauses)
    
    # Count from both tables first
    count_rows = _q(f"SELECT COUNT(*) AS n FROM paper_trades {where}", tuple(params))
    n = int((count_rows[0] if count_rows else {}).get("n") or 0)
    
    where_live = where + " AND (status = 'CLOSED_SHADOW' OR trade_status = 'SHADOW' OR broker_status = 'SHADOW')"
    count_rows_live = _q(f"SELECT COUNT(*) AS n FROM live_trades {where_live}", tuple(params))
    n += int((count_rows_live[0] if count_rows_live else {}).get("n") or 0)
    
    conn = _db()
    try:
        with conn:
            conn.execute(f"DELETE FROM paper_trades {where}", tuple(params))
            conn.execute(f"DELETE FROM live_trades {where_live}", tuple(params))
            conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": n, "date_from": date_from, "date_to": date_to}


@app.get("/api/paper_equity")
async def get_paper_equity(symbol: str = ""):
    clauses = ["(status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross'))"]
    params: list = []
    if symbol:
        clauses.append("symbol=?")
        params.append(symbol.upper().strip())
    where = "WHERE " + " AND ".join(clauses)
    rows = _q(
        f"SELECT closed_at, pnl_rupees FROM paper_trades {where} ORDER BY closed_at",
        tuple(params),
    )
    equity = 0.0
    out = []
    for row in rows:
        equity += float(row.get("pnl_rupees") or 0.0)
        out.append({
            "closed_at": row.get("closed_at"),
            "pnl_rupees": round(float(row.get("pnl_rupees") or 0.0), 2),
            "equity": round(equity, 2),
        })
    return out


# ── Zerodha Broker and Settings API Endpoints ───────────────────────────────

@app.get("/api/settings", dependencies=[Depends(authenticate)])
async def get_settings():
    from config.runtime_config import load_runtime_config
    return load_runtime_config()


@app.post("/api/settings", dependencies=[Depends(authenticate)])
async def post_settings(data: dict):
    from config.runtime_config import save_runtime_config
    save_runtime_config(data)
    return {"status": "SUCCESS", "message": "Runtime settings updated successfully"}


_positions_cache = None
_positions_cache_ts = 0.0

def _fetch_real_kite_positions(kite) -> list[dict]:
    global _positions_cache, _positions_cache_ts
    now = time.time()
    if _positions_cache is not None and (now - _positions_cache_ts) < 3.0:
        return _positions_cache
        
    try:
        from src.engine.symbol_resolver import resolve_instrument
        positions_data = kite.positions()
        net_positions = positions_data.get("net", [])
        
        # Fetch active GTTs to map to positions
        gtt_map = {}
        try:
            gtt_list = kite.get_gtts() or []
            for gtt in gtt_list:
                if gtt.get("status") == "active":
                    cond = gtt.get("condition", {})
                    tsym = cond.get("tradingsymbol")
                    if tsym:
                        gtt_map[str(tsym).upper()] = gtt
        except Exception as ge:
            log.warning("Failed to fetch GTTs from Kite in _fetch_real_kite_positions: %s", ge)

        open_db_trades = _q("SELECT * FROM live_trades WHERE status='OPEN' AND (trade_status IS NULL OR trade_status != 'SHADOW')")
        db_map = {}
        db_fallback_map = {}
        for t in open_db_trades:
            strike_val = float(t["strike"]) if t.get("strike") is not None else None
            expiry_val = t.get("expiry") or ""
            resolved = None
            if expiry_val:
                resolved = resolve_instrument(t["symbol"], expiry_val, strike_val or 0.0, t["option_type"])
            if resolved and resolved.get("tradingsymbol"):
                db_map[(resolved["tradingsymbol"].upper(), t["side"].upper())] = t
            else:
                key = (t["symbol"].upper(), t["option_type"].upper(), strike_val, t["side"].upper())
                db_fallback_map[key] = t
        
        parsed_positions = []
        for pos in net_positions:
            qty = int(pos.get("quantity", 0))
            if qty == 0:
                continue
                
            tradingsymbol = pos.get("tradingsymbol", "")
            
            symbol = tradingsymbol
            option_type = "FUT"
            strike = None
            
            m_opt = re.match(r"^([A-Z\-]+)(\d{2}[A-Z]{3})(\d+)(CE|PE)$", tradingsymbol, re.IGNORECASE)
            if m_opt:
                symbol = m_opt.group(1).upper()
                option_type = m_opt.group(4).upper()
                strike = float(m_opt.group(3))
            else:
                m_fut = re.match(r"^([A-Z\-]+)(\d{2}[A-Z]{3})(FUT)?$", tradingsymbol, re.IGNORECASE)
                if m_fut:
                    symbol = m_fut.group(1).upper()
                    option_type = "FUT"
            
            # Resolve position's expiry first
            from src.engine.symbol_resolver import get_expiry_for_tradingsymbol, resolve_instrument
            expiry_val = get_expiry_for_tradingsymbol(tradingsymbol)
            if not expiry_val:
                m_opt = re.match(r"^([A-Z\-]+)(\d{2}[A-Z]{3}|\d{2}[0-9OND][0-9]{2})(\d+)(CE|PE)$", tradingsymbol, re.IGNORECASE)
                if m_opt:
                    expiry_val = m_opt.group(2).upper()
                else:
                    m_fut = re.match(r"^([A-Z\-]+)(\d{2}[A-Z]{3}|\d{2}[0-9OND][0-9]{2})(FUT)?$", tradingsymbol, re.IGNORECASE)
                    if m_fut:
                        expiry_val = m_fut.group(2).upper()
            
            if not expiry_val:
                expiry_val = "—"

            # Resolve lot_size using the resolved expiry and instrument details
            lot_size = 1
            if expiry_val and expiry_val != "—":
                try:
                    res_inst = resolve_instrument(symbol, expiry_val, strike or 0.0, option_type)
                    if res_inst and res_inst.get("lot_size"):
                        lot_size = int(res_inst["lot_size"])
                except Exception as le:
                    log.debug("Failed to resolve instrument lot_size: %s", le)
            if lot_size == 1:
                lot_size = LOT_SIZES.get(symbol.upper(), LOT_SIZES.get(symbol, 1))

            side = "BUY" if qty > 0 else "SELL"
            exchange = pos.get("exchange", "")
            
            if exchange == "MCX":
                lots_count = abs(qty)
            else:
                lots_count = round(abs(qty) / lot_size, 2)
            
            pnl = float(pos.get("pnl", 0.0))
            cmp = float(pos.get("last_price", 0.0))
            entry_px = float(pos.get("average_price") or pos.get("buy_price") or pos.get("sell_price") or 0.0)
            
            db_trade = db_map.get((tradingsymbol.upper(), side.upper()))
                
            if not db_trade:
                candidate_db_trade = db_fallback_map.get((symbol, option_type, strike, side.upper()))
                if candidate_db_trade:
                    db_expiry = candidate_db_trade.get("expiry") or ""
                    
                    # Helper to smart-match expiries (YYYY-MM-DD vs YYMMM etc)
                    def _expiries_match(e1: str, e2: str) -> bool:
                        if not e1 or not e2 or e1 == "—" or e2 == "—":
                            return True
                        e1_clean = e1.strip().upper().replace("-", "")
                        e2_clean = e2.strip().upper().replace("-", "")
                        if e1_clean == e2_clean:
                            return True
                        
                        def standardize(e: str) -> str:
                            if len(e) == 8 and e.isdigit():
                                return f"20{e[2:4]}-{e[4:6]}-{e[6:]}"
                            m = re.match(r"^(\d{2})([A-Z]{3})$", e)
                            if m:
                                months = {"JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
                                          "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12"}
                                return f"20{m.group(1)}-{months.get(m.group(2), '01')}"
                            return e
                            
                        s1 = standardize(e1_clean)
                        s2 = standardize(e2_clean)
                        return s1 == s2 or s1.startswith(s2) or s2.startswith(s1)
                        
                    if _expiries_match(expiry_val, db_expiry):
                        db_trade = candidate_db_trade

            if db_trade and db_trade.get("expiry"):
                expiry_val = db_trade.get("expiry")
            
            if db_trade:
                db_lots = int(db_trade.get("lots") or 1)
                db_side = db_trade.get("side", "BUY").upper()
                db_qty = db_lots if exchange == "MCX" else db_lots * lot_size
                
                sl_val = db_trade.get("sl_premium") or db_trade.get("sl_underlying") or "—"
                tgt_val = db_trade.get("target_premium") or db_trade.get("target_underlying") or "—"
                
                if sl_val == "—" or tgt_val == "—":
                    gtt = gtt_map.get(tradingsymbol.upper())
                    if gtt and gtt.get("status") == "active":
                        cond = gtt.get("condition", {})
                        triggers = [float(x) for x in cond.get("trigger_values", [])]
                        if len(triggers) == 2:
                            t1, t2 = triggers[0], triggers[1]
                            if side == "BUY":
                                if sl_val == "—": sl_val = min(t1, t2)
                                if tgt_val == "—": tgt_val = max(t1, t2)
                            else:
                                if sl_val == "—": sl_val = max(t1, t2)
                                if tgt_val == "—": tgt_val = min(t1, t2)
                        elif len(triggers) == 1:
                            t = triggers[0]
                            if side == "BUY":
                                if t < entry_px:
                                    if sl_val == "—": sl_val = t
                                else:
                                    if tgt_val == "—": tgt_val = t
                            else:
                                if t > entry_px:
                                    if sl_val == "—": sl_val = t
                                else:
                                    if tgt_val == "—": tgt_val = t

                trade_status = db_trade.get("trade_status") or "LIVE"
                
                # Check for same-expiry split where user has manual overlay
                if db_side == side and abs(qty) > db_qty:
                    # 1. BOT portion
                    if option_type == "FUT":
                        bot_entry = float(db_trade.get("entry_underlying") or entry_px)
                    else:
                        bot_entry = float(db_trade.get("entry_premium") or db_trade.get("entry_underlying") or entry_px)
                    
                    bot_pnl = (cmp - bot_entry) * db_lots * lot_size if side == "BUY" else (bot_entry - cmp) * db_lots * lot_size
                    
                    parsed_positions.append({
                        "symbol": symbol,
                        "expiry": expiry_val,
                        "side": side,
                        "option_type": option_type,
                        "strike": strike,
                        "lots": db_lots,
                        "entry_premium": bot_entry,
                        "cmp": cmp,
                        "sl_premium": sl_val,
                        "target_premium": tgt_val,
                        "pnl_rupees": round(bot_pnl, 2),
                        "exit_mode": "BOT",
                        "trade_status": trade_status,
                        "status": "OPEN",
                        "tradingsymbol": tradingsymbol
                    })
                    
                    # 2. KITE/Manual portion
                    manual_lots = lots_count - db_lots
                    manual_pnl = pnl - bot_pnl
                    
                    parsed_positions.append({
                        "symbol": symbol,
                        "expiry": expiry_val,
                        "side": side,
                        "option_type": option_type,
                        "strike": strike,
                        "lots": manual_lots,
                        "entry_premium": entry_px,
                        "cmp": cmp,
                        "sl_premium": "—",
                        "target_premium": "—",
                        "pnl_rupees": round(manual_pnl, 2),
                        "exit_mode": "KITE",
                        "trade_status": "LIVE",
                        "status": "OPEN",
                        "tradingsymbol": tradingsymbol
                    })
                else:
                    # Single BOT trade display (either same qty or reduced qty)
                    if option_type == "FUT":
                        bot_entry = float(db_trade.get("entry_underlying") or entry_px)
                    else:
                        bot_entry = float(db_trade.get("entry_premium") or db_trade.get("entry_underlying") or entry_px)
                    
                    parsed_positions.append({
                        "symbol": symbol,
                        "expiry": expiry_val,
                        "side": side,
                        "option_type": option_type,
                        "strike": strike,
                        "lots": lots_count,
                        "entry_premium": bot_entry,
                        "cmp": cmp,
                        "sl_premium": sl_val,
                        "target_premium": tgt_val,
                        "pnl_rupees": pnl,
                        "exit_mode": "BOT",
                        "trade_status": trade_status,
                        "status": "OPEN",
                        "tradingsymbol": tradingsymbol
                    })
            else:
                sl_val = "—"
                tgt_val = "—"
                gtt = gtt_map.get(tradingsymbol.upper())
                if gtt and gtt.get("status") == "active":
                    cond = gtt.get("condition", {})
                    triggers = [float(x) for x in cond.get("trigger_values", [])]
                    if len(triggers) == 2:
                        t1, t2 = triggers[0], triggers[1]
                        if side == "BUY":
                            sl_val = min(t1, t2)
                            tgt_val = max(t1, t2)
                        else:
                            sl_val = max(t1, t2)
                            tgt_val = min(t1, t2)
                    elif len(triggers) == 1:
                        t = triggers[0]
                        if side == "BUY":
                            if t < entry_px: sl_val = t
                            else: tgt_val = t
                        else:
                            if t > entry_px: sl_val = t
                            else: tgt_val = t

                parsed_positions.append({
                    "symbol": symbol,
                    "expiry": expiry_val,
                    "side": side,
                    "option_type": option_type,
                    "strike": strike,
                    "lots": lots_count,
                    "entry_premium": entry_px,
                    "cmp": cmp,
                    "sl_premium": sl_val,
                    "target_premium": tgt_val,
                    "pnl_rupees": pnl,
                    "exit_mode": "KITE",
                    "trade_status": "LIVE",
                    "status": "OPEN",
                    "tradingsymbol": tradingsymbol
                })
        _positions_cache = parsed_positions
        _positions_cache_ts = now
        return parsed_positions
    except Exception as e:
        log.error("Failed to fetch positions from Kite: %s", e)
        if _positions_cache is not None:
            return _positions_cache
        return []


def _get_kite_closed_trades(kite) -> list[dict]:
    import re
    from datetime import datetime
    import pytz
    from config.settings import LOT_SIZES
    
    try:
        positions_data = kite.positions()
        net_positions = positions_data.get("net", [])
    except Exception as e:
        log.error("Failed to fetch positions in _get_kite_closed_trades: %s", e)
        return []
        
    closed_positions = []
    
    # Fetch orders to map exact close times and entry sides
    orders = []
    try:
        orders = kite.orders() or []
    except Exception as oe:
        log.warning("Failed to fetch orders in _get_kite_closed_trades: %s", oe)
        
    completed_orders_map = {}
    for o in orders:
        if o.get("status") == "COMPLETE":
            tsym = o.get("tradingsymbol", "").upper()
            if tsym:
                if tsym not in completed_orders_map:
                    completed_orders_map[tsym] = []
                completed_orders_map[tsym].append(o)
                
    for tsym in completed_orders_map:
        completed_orders_map[tsym].sort(key=lambda x: str(x.get("order_timestamp") or ""))
        
    for pos in net_positions:
        qty = int(pos.get("quantity", 0))
        if qty != 0:
            continue
            
        buy_qty = int(pos.get("buy_quantity", 0))
        sell_qty = int(pos.get("sell_quantity", 0))
        if buy_qty == 0 and sell_qty == 0:
            continue
            
        tradingsymbol = pos.get("tradingsymbol", "").upper()
        pnl = float(pos.get("pnl", 0.0))
        
        # Parse fields
        symbol = tradingsymbol
        option_type = "FUT"
        strike = None
        
        m_opt = re.match(r"^([A-Z\-]+)(\d{2}[A-Z]{3})(\d+)(CE|PE)$", tradingsymbol, re.IGNORECASE)
        if m_opt:
            symbol = m_opt.group(1).upper()
            option_type = m_opt.group(4).upper()
            strike = float(m_opt.group(3))
        else:
            m_fut = re.match(r"^([A-Z\-]+)(\d{2}[A-Z]{3})(FUT)?$", tradingsymbol, re.IGNORECASE)
            if m_fut:
                symbol = m_fut.group(1).upper()
                option_type = "FUT"
                
        # Expiry
        from src.engine.symbol_resolver import get_expiry_for_tradingsymbol
        expiry_val = get_expiry_for_tradingsymbol(tradingsymbol)
        if not expiry_val:
            m_opt = re.match(r"^([A-Z\-]+)(\d{2}[A-Z]{3}|\d{2}[0-9OND][0-9]{2})(\d+)(CE|PE)$", tradingsymbol, re.IGNORECASE)
            if m_opt:
                expiry_val = m_opt.group(2).upper()
            else:
                m_fut = re.match(r"^([A-Z\-]+)(\d{2}[A-Z]{3}|\d{2}[0-9OND][0-9]{2})(FUT)?$", tradingsymbol, re.IGNORECASE)
                if m_fut:
                    expiry_val = m_fut.group(2).upper()
                    
        if not expiry_val:
            expiry_val = "—"
            
        side = "BUY"
        closed_at = None
        
        tsym_orders = completed_orders_map.get(tradingsymbol, [])
        if tsym_orders:
            entry_order = tsym_orders[0]
            exit_order = tsym_orders[-1]
            side = entry_order.get("transaction_type", "BUY").upper()
            
            raw_ts = exit_order.get("order_timestamp")
            if raw_ts:
                if isinstance(raw_ts, str):
                    closed_at = raw_ts.replace(" ", "T")
                else:
                    closed_at = raw_ts.isoformat()
        else:
            overnight_qty = int(pos.get("overnight_quantity", 0))
            if overnight_qty > 0:
                side = "BUY"
            elif overnight_qty < 0:
                side = "SELL"
            else:
                side = "BUY"
                
        buy_price = float(pos.get("buy_price") or pos.get("average_price") or 0.0)
        sell_price = float(pos.get("sell_price") or pos.get("average_price") or 0.0)
        
        if side == "BUY":
            entry_px = buy_price
            exit_px = sell_price
        else:
            entry_px = sell_price
            exit_px = buy_price
            
        if not closed_at:
            IST = pytz.timezone("Asia/Kolkata")
            closed_at = datetime.now(IST).isoformat()
            
        lot_size = LOT_SIZES.get(symbol, 1)
        lots_traded = max(buy_qty, sell_qty)
        lots_count = round(lots_traded / lot_size, 2) if lot_size else lots_traded
        
        closed_positions.append({
            "id": f"kite-{tradingsymbol}",
            "symbol": symbol,
            "expiry": expiry_val,
            "side": side,
            "option_type": option_type,
            "strike": strike,
            "lots": lots_count,
            "entry_premium": entry_px,
            "exit_premium": exit_px,
            "pnl_rupees": round(pnl, 2),
            "status": "CLOSED",
            "closed_at": closed_at,
            "opened_at": None,
            "reason": "Kite Manual Exit",
            "trade_status": "LIVE",
            "exit_mode": "KITE",
            "tradingsymbol": tradingsymbol
        })
        
    return closed_positions


def _is_duplicate_closed_trade(kite_pos, db_trades, today_str) -> bool:
    from src.engine.symbol_resolver import resolve_instrument
    ktsym = kite_pos.get("tradingsymbol", "").upper()
    k_strike = float(kite_pos["strike"]) if kite_pos.get("strike") is not None else None
    k_opt = kite_pos.get("option_type", "").upper()
    k_sym = kite_pos.get("symbol", "").upper()
    
    for t in db_trades:
        db_close = t.get("closed_at") or ""
        if not db_close.startswith(today_str):
            continue
            
        db_strike = float(t["strike"]) if t.get("strike") is not None else None
        db_opt = t.get("option_type", "").upper()
        db_sym = t.get("symbol", "").upper()
        
        db_tsym = ""
        db_expiry = t.get("expiry") or ""
        if db_expiry:
            try:
                resolved = resolve_instrument(db_sym, db_expiry, db_strike or 0.0, db_opt)
                if resolved:
                    db_tsym = resolved.get("tradingsymbol", "").upper()
            except Exception:
                pass
                
        if db_tsym and ktsym:
            if db_tsym == ktsym:
                return True
        else:
            if db_sym == k_sym and db_opt == k_opt:
                if k_strike is None or db_strike is None or abs(db_strike - k_strike) < 0.01:
                    return True
    return False


@app.get("/api/open_orders")
def get_open_orders():
    from src.engine.live_trading import get_kite_client
    kite = get_kite_client()
    if not kite:
        return []
    try:
        orders = kite.orders() or []
        open_orders = []
        for o in orders:
            status = o.get("status", "")
            if status not in ("COMPLETE", "REJECTED", "CANCELLED"):
                open_orders.append({
                    "order_timestamp": str(o.get("order_timestamp")),
                    "tradingsymbol": o.get("tradingsymbol"),
                    "transaction_type": o.get("transaction_type"),
                    "order_type": o.get("order_type"),
                    "quantity": o.get("quantity"),
                    "price": o.get("price"),
                    "trigger_price": o.get("trigger_price"),
                    "status": status,
                    "status_message": o.get("status_message") or ""
                })
        open_orders.sort(key=lambda x: x["order_timestamp"], reverse=True)
        return open_orders
    except Exception as e:
        log.error("Failed to fetch open orders: %s", e)
        return []


@app.get("/api/live_trades")
def get_live_trades(symbol: str = "", status: str = "", limit: int = 300):
    from config.runtime_config import load_runtime_config
    config = load_runtime_config()
    shadow_mode = config.get("live_shadow_mode", True)
    
    if status.upper() == "OPEN":
        from src.engine.live_trading import get_kite_client
        kite = get_kite_client()
        real_positions = []
        if kite:
            real_positions = _fetch_real_kite_positions(kite)
            # Filter out any closed manual positions (with status="CLOSED")
            real_positions = [p for p in real_positions if p.get("status") != "CLOSED" and float(p.get("lots") or p.get("quantity", 0)) != 0]
            
        if shadow_mode:
            db_rows = _q("SELECT * FROM live_trades WHERE status='OPEN'")
            _enrich_open_trades_with_live_pnl(db_rows)
            _enrich_trade_details(db_rows)
            
            real_tsyms = {(p["tradingsymbol"].upper(), p["side"].upper()) for p in real_positions if p.get("tradingsymbol")}
            real_keys = {(p["symbol"].upper(), p["option_type"].upper(), float(p["strike"]) if p.get("strike") is not None else None, p["side"].upper()) 
                         for p in real_positions if not p.get("tradingsymbol")}
            
            from src.engine.symbol_resolver import resolve_instrument
            for row in db_rows:
                strike_val = float(row["strike"]) if row.get("strike") is not None else None
                expiry_val = row.get("expiry") or ""
                resolved = None
                if expiry_val:
                    resolved = resolve_instrument(row["symbol"], expiry_val, strike_val or 0.0, row["option_type"])
                
                db_tsym = resolved["tradingsymbol"].upper() if (resolved and resolved.get("tradingsymbol")) else ""
                
                is_duplicate = False
                if db_tsym:
                    if (db_tsym, row["side"].upper()) in real_tsyms:
                        is_duplicate = True
                else:
                    key = (row["symbol"].upper(), row["option_type"].upper(), strike_val, row["side"].upper())
                    if key in real_keys:
                        is_duplicate = True
                        
                if not is_duplicate:
                    real_positions.append(row)
                    
        if symbol:
            sym_upper = symbol.upper().strip()
            real_positions = [p for p in real_positions if p["symbol"] == sym_upper]
            
        return real_positions
        
    clauses = []
    params: list = []
    if symbol:
        clauses.append("symbol=?")
        params.append(symbol.upper().strip())
    if status:
        stat_up = status.upper().strip()
        if stat_up == "CLOSED":
            clauses.append("(status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross'))")
        else:
            clauses.append("status=?")
            params.append(stat_up)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    
    order_by = "closed_at DESC" if status.upper() == "CLOSED" else "opened_at DESC"
    
    rows = _q(
        f"SELECT * FROM live_trades {where} ORDER BY {order_by} LIMIT ?",
        (*params, int(limit)),
    )
    _enrich_open_trades_with_live_pnl(rows)
    _enrich_trade_details(rows)
    
    if status.upper() == "CLOSED":
        from src.engine.live_trading import get_kite_client
        kite = get_kite_client()
        if kite:
            try:
                import pytz
                IST = pytz.timezone("Asia/Kolkata")
                today_str = datetime.now(IST).strftime("%Y-%m-%d")
                
                kite_closed = _get_kite_closed_trades(kite)
                unique_kite_closed = []
                for kp in kite_closed:
                    if not _is_duplicate_closed_trade(kp, rows, today_str):
                        unique_kite_closed.append(kp)
                
                if symbol:
                    sym_upper = symbol.upper().strip()
                    unique_kite_closed = [kp for kp in unique_kite_closed if kp["symbol"] == sym_upper]
                    
                rows.extend(unique_kite_closed)
                
                def get_closed_at_key(x):
                    ca = x.get("closed_at") or ""
                    return ca.replace("T", " ").replace("Z", "")
                    
                rows.sort(key=get_closed_at_key, reverse=True)
                rows = rows[:limit]
            except Exception as ke:
                log.error("Failed to merge closed Kite positions in live_trades: %s", ke)
                
    return rows


@app.get("/api/portfolio_metrics")
def get_portfolio_metrics():
    try:
        # Fetch live open trades (reusing logic from get_live_trades)
        open_positions = get_live_trades(symbol="", status="OPEN", limit=300)
    except Exception as e:
        log.error("Failed to get_live_trades in portfolio_metrics: %s", e)
        return {}
        
    # Group by base symbol
    groups = {}
    for p in open_positions:
        sym = p.get("symbol", "")
        base = sym
        if sym.startswith("NATURALGAS"): base = "NATURALGAS"
        elif sym.startswith("NIFTY"): base = "NIFTY"
        elif sym.startswith("BANKNIFTY"): base = "BANKNIFTY"
        elif sym.startswith("CRUDEOIL"): base = "CRUDEOIL"
        elif sym.startswith("GOLD"): base = "GOLD"
        elif sym.startswith("MCX"): base = "MCX"
        else:
            import re
            m = re.match(r"^[A-Z]+", sym)
            if m: base = m.group(0)
            
        if base not in groups:
            groups[base] = []
        groups[base].append(p)
        
    metrics = {}
    for base, positions in groups.items():
        from config.settings import LOT_SIZES
        lot_size = LOT_SIZES.get(base, 1)
        
        # Get the latest underlying and delta from DB for options
        rows = _q("SELECT * FROM option_chain_snapshots WHERE symbol=? ORDER BY id DESC LIMIT 200", (base,))
        if rows:
            latest_time = rows[0]["fetched_at"]
            rows = [r for r in rows if r["fetched_at"] == latest_time]
            
        net_delta = 0.0
        min_strike = float('inf')
        max_strike = 0.0
        underlying = None
        
        for p in positions:
            side = 1 if p.get("side") == "BUY" else -1
            # Robust lots: prefer "lots" field, fallback to raw quantity / lot_size
            raw_lots = p.get("lots")
            if raw_lots is None or raw_lots == "" or float(raw_lots or 0) == 0:
                raw_qty = float(p.get("quantity", 0) or p.get("qty", 0) or 0)
                raw_lots = raw_qty / lot_size if lot_size else 0
            lots_val = float(raw_lots or 0)
            qty = lots_val * lot_size
            opt_type = (p.get("option_type") or "").upper()
            
            if opt_type == "FUT":
                delta = lots_val * side * 1.0
                if not (delta != delta):  # NaN guard
                    net_delta += delta
            elif opt_type in ("CE", "PE"):
                strike = p.get("strike")
                if strike is not None:
                    try:
                        strike = float(strike)
                    except (ValueError, TypeError):
                        strike = None
                if strike:
                    min_strike = min(min_strike, strike)
                    max_strike = max(max_strike, strike)
                    match = None
                    if rows:
                        match = next((r for r in rows if abs(float(r["strike"] or 0) - strike) < 0.01 and r["option_type"] == opt_type), None)
                    
                    d = None
                    if match:
                        d = float(match["delta"] or 0)
                        if underlying is None:
                            u = match.get("underlying_price")
                            if u:
                                underlying = float(u)
                    else:
                        d = 0.5 if opt_type == "CE" else -0.5
                        
                    if d is not None and not (d != d):  # NaN guard
                        net_delta += lots_val * side * d
        
        if underlying is None and rows:
            u = rows[0].get("underlying_price")
            if u:
                underlying = float(u)
                
        # Additional robust fallbacks for underlying spot price
        if underlying is None or underlying == 0:
            res_up = _q("SELECT price FROM underlying_price WHERE symbol=? ORDER BY fetched_at DESC LIMIT 1", (base,))
            if res_up:
                underlying = float(res_up[0]["price"])
                
        if underlying is None or underlying == 0:
            if min_strike != float('inf') and min_strike > 0:
                underlying = min_strike
                
        if underlying is None or underlying == 0:
            for p in positions:
                u = p.get("cmp") or p.get("entry_underlying") or p.get("entry_premium")
                if u and float(u) > 0:
                    underlying = float(u)
                    break
                    
        if underlying is None or underlying == 0:
            underlying = 100.0  # Fallback to prevent division by zero or empty values
            
        if min_strike == float('inf'):
            min_strike = underlying
            max_strike = underlying
            
        sim_min = min_strike * 0.7
        sim_max = max_strike * 1.3
        prices = [sim_min + i * (sim_max - sim_min) / 100 for i in range(101)]
        
        payoffs = []
        for S in prices:
            pnl = 0.0
            for p in positions:
                side = 1 if p.get("side") == "BUY" else -1
                raw_lots = p.get("lots")
                if raw_lots is None or raw_lots == "" or float(raw_lots or 0) == 0:
                    raw_qty = float(p.get("quantity", 0) or p.get("qty", 0) or 0)
                    raw_lots = raw_qty / lot_size if lot_size else 0
                lots_val = float(raw_lots or 0)
                qty = lots_val * lot_size
                opt_type = (p.get("option_type") or "").upper()
                
                if opt_type == "FUT":
                    entry = float(p.get("entry_underlying") or p.get("entry_premium") or 0)
                    if entry > 0:  # Only include in payoff if valid entry
                        pnl += side * qty * (S - entry)
                elif opt_type == "CE":
                    entry = float(p.get("entry_premium") or 0)
                    strike = float(p.get("strike") or 0)
                    if strike > 0:  # Valid position
                        pnl += side * qty * (max(0, S - strike) - entry)
                elif opt_type == "PE":
                    entry = float(p.get("entry_premium") or 0)
                    strike = float(p.get("strike") or 0)
                    if strike > 0:  # Valid position
                        pnl += side * qty * (max(0, strike - S) - entry)
            payoffs.append(pnl)
            
        if not payoffs:
            metrics[base] = {"net_delta": round(net_delta, 2), "max_profit": 0, "max_loss": 0}
            continue
            
        max_p = max(payoffs)
        min_p = min(payoffs)
        
        is_max_inf = (max_p == payoffs[-1] and payoffs[-1] > payoffs[-2]) or (max_p == payoffs[0] and payoffs[0] > payoffs[1])
        is_min_inf = (min_p == payoffs[-1] and payoffs[-1] < payoffs[-2]) or (min_p == payoffs[0] and payoffs[0] < payoffs[1])
        
        has_fut = any((p.get("option_type") or "").upper() == "FUT" for p in positions)
        has_options = any((p.get("option_type") or "").upper() in ("CE", "PE") for p in positions)
        is_naked_fut = has_fut and not has_options
        if is_naked_fut:
            is_max_inf = True
            is_min_inf = True
            
        metrics[base] = {
            "net_delta": round(net_delta, 2),
            "max_profit": "∞" if is_max_inf else round(max_p, 2),
            "max_loss": "∞" if is_min_inf else round(min_p, 2)
        }
        
    return metrics


@app.get("/api/risk_metrics")
def get_risk_metrics(mode: str = "live"):
    from config.settings import MAX_DAILY_LOSS_RUPEES, LOT_SIZES
    from src.models.schema import get_conn
    import pytz
    
    IST = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    
    # today_start in UTC for day boundary calculations
    ist_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = ist_midnight.astimezone(timezone.utc).isoformat()
    
    # 1. Fetch available cash
    available_cash = 0.0
    if mode == "live":
        from src.engine.live_trading import get_kite_client
        kite = get_kite_client()
        if kite:
            try:
                margins = kite.margins()
                section = margins.get("equity", {})
                if isinstance(section, dict):
                    net = float(section.get("net") or 0.0)
                    debits = float(section.get("utilised", {}).get("debits") or 0.0)
                    available_cash = net + debits
            except Exception as e:
                log.error("Failed to fetch margins from Kite in risk_metrics: %s", e)
                available_cash = 1000000.0  # fallback
        else:
            available_cash = 1000000.0  # fallback mock cash
    else:
        # For paper: available_cash = 1,000,000 + closed_pnl
        closed_pnl = 0.0
        with get_conn() as conn:
            row = conn.execute(
                "SELECT SUM(pnl_rupees) AS total FROM paper_trades WHERE (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross'))"
            ).fetchone()
            if row and row["total"] is not None:
                closed_pnl = float(row["total"])
        available_cash = 1000000.0 + closed_pnl

    # 2. Fetch open positions & calculate MTM (total_open_pnl)
    open_positions = []
    if mode == "live":
        try:
            open_positions = get_live_trades(symbol="", status="OPEN", limit=300)
        except Exception as e:
            log.error("Failed to get live trades in risk_metrics: %s", e)
    else:
        # Paper open trades
        with get_conn() as conn:
            open_rows = conn.execute("SELECT * FROM paper_trades WHERE status='OPEN'").fetchall()
            open_positions = [dict(r) for r in open_rows]
            _enrich_open_trades_with_live_pnl(open_positions)
            _enrich_trade_details(open_positions)
            
    total_open_pnl = sum(_safe_float(p.get("pnl_rupees")) for p in open_positions)
    current_equity = available_cash + total_open_pnl
    
    # 3. Update & fetch Peak Equity / calculate Max Drawdown
    peak_equity = current_equity
    with get_conn() as conn:
        row = conn.execute(
            "SELECT peak_equity FROM daily_equity_peaks WHERE date=? AND mode=?",
            (today_str, mode)
        ).fetchone()
        if row:
            peak_equity = max(float(row["peak_equity"]), current_equity)
            if current_equity > float(row["peak_equity"]):
                conn.execute(
                    "UPDATE daily_equity_peaks SET peak_equity=? WHERE date=? AND mode=?",
                    (current_equity, today_str, mode)
                )
        else:
            conn.execute(
                "INSERT INTO daily_equity_peaks (date, mode, peak_equity) VALUES (?, ?, ?)",
                (today_str, mode, current_equity)
            )
            
    drawdown_abs = max(0.0, peak_equity - current_equity)
    drawdown_pct = (drawdown_abs / peak_equity * 100.0) if peak_equity > 0 else 0.0
    
    # 4. Profit Factor
    table_name = "live_trades" if mode == "live" else "paper_trades"
    total_wins = 0.0
    total_losses = 0.0
    with get_conn() as conn:
        wins_row = conn.execute(
            f"SELECT SUM(pnl_rupees) AS total FROM {table_name} WHERE (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees > 0"
        ).fetchone()
        if wins_row and wins_row["total"] is not None:
            total_wins = float(wins_row["total"])
            
        losses_row = conn.execute(
            f"SELECT SUM(pnl_rupees) AS total FROM {table_name} WHERE (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross')) AND pnl_rupees < 0"
        ).fetchone()
        if losses_row and losses_row["total"] is not None:
            total_losses = abs(float(losses_row["total"]))
            
    # 5. Daily Loss Limit %
    # Fetch realized daily PnL (closed today)
    today_realized_pnl = 0.0
    db_today_closed_pnl = 0.0
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT SUM(pnl_rupees) AS total FROM {table_name} WHERE closed_at >= ? AND (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross'))",
            (today_start,)
        ).fetchone()
        if row and row["total"] is not None:
            db_today_closed_pnl = float(row["total"])
            
    today_realized_pnl = db_today_closed_pnl
    
    if mode == "live":
        from src.engine.live_trading import get_kite_client
        kite = get_kite_client()
        if kite:
            try:
                db_today_trades = _q(
                    f"SELECT * FROM {table_name} WHERE closed_at >= ? AND (status LIKE 'CLOSED_%' OR status IN ('Dead Trade', 'TF-1H-Cross'))",
                    (today_start,)
                )
                kite_closed = _get_kite_closed_trades(kite)
                for kp in kite_closed:
                    if not _is_duplicate_closed_trade(kp, db_today_trades, today_str):
                        kp_pnl = kp.get("pnl_rupees", 0.0)
                        if kp_pnl > 0:
                            total_wins += kp_pnl
                        else:
                            total_losses += abs(kp_pnl)
                        today_realized_pnl += kp_pnl
            except Exception as e:
                log.error("Failed to merge closed trades for Profit Factor and realized PnL: %s", e)
                
    profit_factor = round(total_wins / total_losses, 2) if total_losses > 0 else (99.99 if total_wins > 0 else 0.0)
            
    # Daily running PnL = realized today + unrealized today (all open positions)
    today_running_pnl = today_realized_pnl + total_open_pnl
    from config.runtime_config import load_runtime_config
    runtime_config = load_runtime_config()
    daily_loss_limit = float(runtime_config.get("live_max_daily_loss_rupees", MAX_DAILY_LOSS_RUPEES))
    
    # Daily loss limit % utilization is calculated only if running pnl is negative (a loss)
    if today_running_pnl < 0:
        daily_loss_pct = round((abs(today_running_pnl) / daily_loss_limit) * 100.0, 2)
    else:
        daily_loss_pct = 0.0
        
    # 6. Risk-to-Reward Ratio (Avg Active R:R)
    rr_values = []
    for p in open_positions:
        opt_type = (p.get("option_type") or "").upper()
        if opt_type == "FUT":
            entry = _safe_float(p.get("entry_underlying"))
            sl = _safe_float(p.get("sl_underlying"))
            target = _safe_float(p.get("target_underlying"))
        else:
            entry = _safe_float(p.get("entry_premium") or p.get("entry_underlying"))
            sl = _safe_float(p.get("sl_premium") or p.get("sl_underlying"))
            target = _safe_float(p.get("target_premium") or p.get("target_underlying"))
            
        if entry > 0 and sl > 0 and target > 0:
            target_diff = abs(target - entry)
            sl_diff = abs(entry - sl)
            if sl_diff > 0:
                rr_values.append(target_diff / sl_diff)
                
    avg_rr = round(sum(rr_values) / len(rr_values), 2) if rr_values else 0.0
    
    # 7. Total Exposure (Notional Exposure)
    total_notional_exposure = 0.0
    for p in open_positions:
        sym = p.get("symbol", "")
        base = sym
        if sym.startswith("NATURALGAS"): base = "NATURALGAS"
        elif sym.startswith("NIFTY"): base = "NIFTY"
        elif sym.startswith("BANKNIFTY"): base = "BANKNIFTY"
        elif sym.startswith("CRUDEOIL"): base = "CRUDEOIL"
        elif sym.startswith("GOLD"): base = "GOLD"
        elif sym.startswith("MCX"): base = "MCX"
        else:
            import re
            m = re.match(r"^[A-Z]+", sym)
            if m: base = m.group(0)
            
        lot_size = LOT_SIZES.get(base, 1)
        
        # Prefer "lots", fallback to qty / lot_size
        raw_lots = p.get("lots")
        if raw_lots is None or raw_lots == "" or float(raw_lots or 0) == 0:
            raw_qty = float(p.get("quantity", 0) or p.get("qty", 0) or 0)
            raw_lots = raw_qty / lot_size if lot_size else 0
        lots_val = float(raw_lots or 0)
        qty = lots_val * lot_size
        
        opt_type = (p.get("option_type") or "").upper()
        
        if opt_type == "FUT":
            val = qty * float(p.get("entry_underlying") or 0.0)
        else:
            strike = p.get("strike")
            if strike is not None:
                try: strike = float(strike)
                except: strike = None
            if strike:
                val = qty * strike
            else:
                val = qty * float(p.get("entry_underlying") or 0.0)
        total_notional_exposure += val
        
    return {
        "mode": mode,
        "available_cash": round(available_cash, 2),
        "total_open_pnl": round(total_open_pnl, 2),
        "current_equity": round(current_equity, 2),
        "peak_equity": round(peak_equity, 2),
        "drawdown_abs": round(drawdown_abs, 2),
        "drawdown_pct": round(drawdown_pct, 2),
        "profit_factor": profit_factor,
        "today_running_pnl": round(today_running_pnl, 2),
        "daily_loss_pct": daily_loss_pct,
        "avg_rr": avg_rr,
        "total_notional_exposure": round(total_notional_exposure, 2),
        "max_daily_loss_limit": daily_loss_limit
    }


@app.get("/api/broker_status", dependencies=[Depends(authenticate)])
def get_broker_status():
    from src.models.schema import get_broker_config
    config = get_broker_config()
    if not config:
        return {
            "status": "NOT_CONFIGURED",
            "api_key": None,
            "last_login_date": None,
            "kill_switch_active": 0,
            "has_totp": False
        }
    
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
    is_connected = bool(config.get("access_token")) and config.get("last_login_date") == today
    
    return {
        "status": "CONNECTED" if is_connected else "DISCONNECTED",
        "api_key": config.get("api_key"),
        "last_login_date": config.get("last_login_date"),
        "kill_switch_active": config.get("kill_switch_active", 0),
        "has_totp": bool(config.get("totp_secret"))
    }


@app.post("/api/broker_config", dependencies=[Depends(authenticate)])
async def post_broker_config(data: dict):
    api_key = data.get("api_key")
    api_secret = data.get("api_secret")
    totp_secret = data.get("totp_secret")
    
    if api_key == "": api_key = None
    if api_secret == "": api_secret = None
    if totp_secret == "": totp_secret = None
    
    encrypted_totp = None
    if totp_secret:
        from src.services.zerodha_auth import encrypt_secret
        encrypted_totp = encrypt_secret(totp_secret)
        
    from src.models.schema import update_broker_config
    update_broker_config(
        api_key=api_key,
        api_secret=api_secret,
        totp_secret=encrypted_totp
    )
    return {"status": "SUCCESS", "message": "Broker configurations updated successfully"}


@app.post("/api/broker/killswitch", dependencies=[Depends(authenticate)])
async def toggle_kill_switch(data: dict):
    active = bool(data.get("active", False))
    from src.models.schema import set_kill_switch
    set_kill_switch(active)
    return {"status": "SUCCESS", "message": f"Kill Switch set to {active}"}


_margins_cache = None
_margins_cache_ts = 0.0

@app.get("/api/broker_margin", dependencies=[Depends(authenticate)])
def get_broker_margin():
    global _margins_cache, _margins_cache_ts
    now = time.time()
    from config.runtime_config import load_runtime_config
    config = load_runtime_config()
    
    if _margins_cache is not None and (now - _margins_cache_ts) < 10.0:
        # Update shadow_mode to reflect latest config dynamically
        _margins_cache["shadow_mode"] = config.get("live_shadow_mode", True)
        return _margins_cache

    from src.engine.live_trading import get_kite_client
    
    # ALWAYS try to get real margins if connected, regardless of shadow mode
    kite = get_kite_client()
    if kite:
        try:
            margins = kite.margins()
            
            # Helper to extract available margin safely
            def get_avail(sec):
                section = margins.get(sec, {})
                if not isinstance(section, dict):
                    return 0.0
                if "net" in section and isinstance(section["net"], (int, float)):
                    return float(section["net"])
                avail = section.get("available", {})
                if isinstance(avail, dict):
                    return float(avail.get("live_balance", avail.get("cash", avail.get("opening_balance", 0.0))))
                return 0.0

            # Helper to extract utilized margin safely
            def get_util(sec):
                section = margins.get(sec, {})
                if not isinstance(section, dict):
                    return 0.0
                utilised = section.get("utilised", {})
                if isinstance(utilised, dict):
                    return float(utilised.get("debits", utilised.get("exposure", 0.0)))
                elif isinstance(utilised, (int, float)):
                    return float(utilised)
                return 0.0

            result = {
                "shadow_mode": config.get("live_shadow_mode", True),
                "equity": {
                    "available": get_avail("equity"),
                    "utilized": get_util("equity")
                },
                "commodity": {
                    "available": get_avail("commodity"),
                    "utilized": get_util("commodity")
                }
            }
            _margins_cache = result
            _margins_cache_ts = now
            return result
        except Exception as e:
            log.error("Failed to fetch margins from Kite: %s", e)
            if _margins_cache is not None:
                _margins_cache["shadow_mode"] = config.get("live_shadow_mode", True)
                return _margins_cache
            
    # Mock data fallback
    return {
        "shadow_mode": True,
        "equity": {
            "available": 1000000.0,
            "utilized": 0.0
        },
        "commodity": {
            "available": 500000.0,
            "utilized": 0.0
        }
    }


@app.post("/api/broker/logout", dependencies=[Depends(authenticate)])
async def broker_logout():
    global _positions_cache, _positions_cache_ts, _margins_cache, _margins_cache_ts
    from src.models.schema import update_broker_config
    update_broker_config(
        access_token="",
        request_token="",
        last_login_date=""
    )
    try:
        from src.engine.live_trading import clear_kite_client_cache
        clear_kite_client_cache()
    except Exception:
        log.exception("Failed to clear Kite client cache during logout")
    _positions_cache = None
    _positions_cache_ts = 0.0
    _margins_cache = None
    _margins_cache_ts = 0.0
    return {"status": "SUCCESS", "message": "Logged out successfully"}


@app.get("/api/zerodha/callback")
@app.get("/brokers/zerodha/redirect/{client_id}")
def zerodha_callback(client_id: str = None, request_token: str = None):
    if not request_token:
        return HTMLResponse("<h1>Error: request_token is missing</h1>", status_code=400)
    
    from src.models.schema import get_broker_config, update_broker_config
    config = get_broker_config()
    if not config or not config.get("api_key") or not config.get("api_secret"):
        return HTMLResponse("<h1>Error: Zerodha api_key or api_secret not configured in database</h1>", status_code=400)
    
    from kiteconnect import KiteConnect
    import time
    try:
        kite = KiteConnect(api_key=config["api_key"])
        
        # Mount resilient TLS adapter with pool-eviction retry logic
        try:
            from src.utils.tls_adapter import mount_resilient_tls
            mount_resilient_tls(kite.reqsession)
        except Exception as ssl_err:
            log.warning("Failed to configure TLS adapter for Zerodha callback: %s", ssl_err)

        # Retry generate_session up to 3 times to handle transient SSL EOFs or timeouts
        session = None
        last_err = None
        for attempt in range(1, 4):
            try:
                session = kite.generate_session(request_token, api_secret=config["api_secret"])
                break
            except Exception as err:
                last_err = err
                err_msg = str(err).lower()
                if "token is invalid" in err_msg or "token" in err_msg:
                    break
                log.warning("generate_session attempt %d/3 failed: %s. Retrying in 1s...", attempt, err)
                time.sleep(1)

        if not session:
            raise last_err or Exception("Failed to generate session after 3 attempts")

        access_token = session["access_token"]
        
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
        
        update_broker_config(
            access_token=access_token,
            request_token=request_token,
            last_login_date=today
        )
        # Invalidate cached Kite client so live_trading picks up the new token
        try:
            from src.engine.live_trading import clear_kite_client_cache
            clear_kite_client_cache()
        except Exception:
            pass
        return HTMLResponse("""
            <html>
                <body style="background:#121212; color:#fff; font-family: sans-serif; text-align:center; padding-top:100px;">
                    <h1 style="color:#4caf50;">Authentication Successful!</h1>
                    <p>Zerodha Access Token has been updated. You can close this window now.</p>
                    <script>
                        if (window.opener) {
                            window.opener.postMessage("kite_login_success", "*");
                        }
                        setTimeout(function() { window.close(); }, 3000);
                    </script>
                </body>
            </html>
        """)
    except Exception as e:
        log.exception("Failed to generate Zerodha session")
        return HTMLResponse(f"<h1>Failed to generate Zerodha session: {e}</h1>", status_code=500)


from fastapi import Request

@app.post("/api/zerodha/postback")
async def zerodha_postback(request: Request):
    from src.models.schema import get_broker_config, get_conn, close_live_trade
    import hmac, hashlib

    body = await request.body()

    config = get_broker_config()
    if not config or not config.get("api_secret"):
        log.warning("Postback received but api_secret is not configured")
        return JSONResponse({"error": "Broker config missing"}, status_code=400)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    log.info("Zerodha postback received: %s", payload)

    order_id = str(payload.get("order_id") or "")
    order_timestamp = str(payload.get("order_timestamp") or "")
    status = str(payload.get("status") or "").upper()
    checksum = str(payload.get("checksum") or "")
    tradingsymbol = str(payload.get("tradingsymbol") or "")

    if not order_id or not order_timestamp or not status or not checksum:
        return JSONResponse({"error": "Missing required postback fields"}, status_code=400)

    expected = hashlib.sha256(f"{order_id}{order_timestamp}{config['api_secret']}".encode("utf-8")).hexdigest()
    if not hmac.compare_digest(checksum, expected):
        log.warning("Postback received with invalid checksum")
        return JSONResponse({"error": "Invalid checksum"}, status_code=401)

    close_args = None
    with get_conn() as conn:
        trade = conn.execute("SELECT * FROM live_trades WHERE broker_order_id=? AND status='OPEN'", (order_id,)).fetchone()
        if trade:
            trade = dict(trade)
            if status in ("REJECTED", "CANCELLED"):
                close_args = (
                    trade["id"],
                    datetime.now(timezone.utc).isoformat(),
                    trade["entry_underlying"],
                    0.0,
                    f"CLOSED_{status}",
                    f"Entry order {status}: {payload.get('status_message')}"
                )
                log.info("Live trade %s closed because entry order was %s", trade["id"], status)
            elif status == "COMPLETE":
                conn.execute("UPDATE live_trades SET broker_status='COMPLETE' WHERE id=?", (trade["id"],))
                log.info("Live trade %s entry order COMPLETE", trade["id"])
            if not close_args:
                return {"status": "processed"}

        trade = None
        if payload.get("gtt_id"):
            trade = conn.execute("SELECT * FROM live_trades WHERE gtt_order_id=? AND status='OPEN'", (str(payload.get("gtt_id")),)).fetchone()

        if not trade and status == "COMPLETE" and tradingsymbol:
            tx_side = str(payload.get("transaction_type") or "").upper()
            open_trades = conn.execute("SELECT * FROM live_trades WHERE status='OPEN'").fetchall()
            from src.engine.symbol_resolver import resolve_instrument
            matches = []
            for ot in open_trades:
                ot = dict(ot)
                resolved = resolve_instrument(ot["symbol"], ot["expiry"], ot["strike"], ot["option_type"])
                expected_exit_side = "SELL" if ot.get("side") == "BUY" else "BUY"
                if (
                    resolved
                    and resolved.get("tradingsymbol") == tradingsymbol
                    and tx_side == expected_exit_side
                    and order_id != ot.get("broker_order_id")
                ):
                    matches.append(ot)
            if len(matches) == 1:
                trade = matches[0]
            elif len(matches) > 1:
                log.warning("Ambiguous Zerodha exit postback for %s matched %d open trades", tradingsymbol, len(matches))

        if trade and status == "COMPLETE":
            trade = dict(trade)
            exit_premium = float(payload.get("average_price") or payload.get("price") or 0.0)
            res_u = conn.execute("SELECT price FROM underlying_price WHERE symbol=? ORDER BY fetched_at DESC LIMIT 1", (trade["symbol"],)).fetchone()
            exit_underlying = res_u["price"] if res_u else trade["entry_underlying"]
            close_args = (
                trade["id"],
                datetime.now(timezone.utc).isoformat(),
                exit_underlying,
                exit_premium,
                "CLOSED_GTT",
                "Closed via verified Zerodha postback"
            )
            log.info("Live trade %s closed via verified Zerodha postback", trade["id"])

    if close_args:
        close_live_trade(*close_args)
    return {"status": "processed"}


# ── Serve Static Assets ────────────────────────────────────────────────────

@app.get("/static/theme.css")
def get_theme_css():
    from fastapi import Response
    css_path = ROOT / "src" / "dashboard" / "theme.css"
    if css_path.exists():
        return Response(content=css_path.read_text(encoding="utf-8"), media_type="text/css")
    return Response(status_code=404)


@app.get("/static/theme.js")
def get_theme_js():
    from fastapi import Response
    js_path = ROOT / "src" / "dashboard" / "theme.js"
    if js_path.exists():
        return Response(content=js_path.read_text(encoding="utf-8"), media_type="application/javascript")
    return Response(status_code=404)


@app.get("/static/kite-logo.png")
def get_kite_logo():
    from fastapi.responses import FileResponse
    logo_path = ROOT / "src" / "dashboard" / "kite-logo.png"
    if logo_path.exists():
        return FileResponse(logo_path, media_type="image/png")
    from fastapi import Response
    return Response(status_code=404)



# ── Serve dashboard HTML ───────────────────────────────────────────────────

@app.get("/broker", response_class=HTMLResponse)
async def broker_page(username: str = Depends(authenticate)):
    html_path = ROOT / "src" / "dashboard" / "broker.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>broker.html not found</h1>", status_code=404)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(username: str = Depends(authenticate)):
    html_path = ROOT / "src" / "dashboard" / "settings.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>settings.html not found</h1>", status_code=404)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = ROOT / "src" / "dashboard" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


@app.get("/paper", response_class=HTMLResponse)
async def paper_dashboard():
    html_path = ROOT / "src" / "dashboard" / "paper.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>paper.html not found</h1>", status_code=404)


if __name__ == "__main__":
    print(f"  DB: {DB_PATH}")
    print(f"  Dashboard: http://localhost:8080")
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
