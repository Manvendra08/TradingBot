"""
NSEBOT FastAPI Dashboard Server
Run: python dashboard_server.py
Deps: pip install fastapi uvicorn
No pandas required.
"""
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Resolve project root so imports work regardless of cwd ────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from config.settings import DB_PATH, WATCH_SYMBOLS
    from config.runtime_config import (
        ALLOWED_SCAN_FREQUENCIES,
        MIN_SCAN_FREQUENCY,
        MAX_SCAN_FREQUENCY,
        get_scan_frequency_minutes,
        set_scan_frequency_minutes,
    )
except ImportError:
    DB_PATH = ROOT / "data" / "nsebot.db"
    WATCH_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "NATURALGAS"]
    ALLOWED_SCAN_FREQUENCIES = [5, 15, 30, 60, 180, 1440]
    MIN_SCAN_FREQUENCY = 5
    MAX_SCAN_FREQUENCY = 1440

    def get_scan_frequency_minutes() -> int:
        return 5

    def set_scan_frequency_minutes(minutes: int) -> int:
        return int(minutes)

try:
    from fastapi import FastAPI, Query
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
except ImportError:
    print("[ERROR] Run: python -m pip install fastapi uvicorn")
    sys.exit(1)

app = FastAPI(title="NSEBOT Dashboard API")
log = logging.getLogger("nsebot.dashboard")

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql: str, params: tuple = ()):
    with _db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


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
        pct = ((c - o) / abs(o) * 100.0) if o else 0.0
        sentiment = "NEUTRAL"
        if pct > 0.1:
            sentiment = "BULLISH"
        elif pct < -0.1:
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
            provider = ((item.get("provider") or {}).get("name") or "").strip()
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
    try:
        res = requests.post(
            _SCANX_HEATMAP_API,
            json=payload,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
        )
        res.raise_for_status()
        body = res.json() if res.content else {}
        data = body.get("data") if isinstance(body, dict) else []
        rows = data if isinstance(data, list) else []
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


def _parse_intel_fields(raw: str) -> dict:
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
def get_symbols():
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
def get_meta(symbol: str):
    symbols = _matching_symbols(symbol)
    placeholders, params = _in_clause(symbols)
    rows = _q(
        "SELECT MAX(fetched_at) AS last_fetch, COUNT(DISTINCT fetched_at) AS snapshots "
        f"FROM option_chain_snapshots WHERE symbol IN ({placeholders})",
        params
    )
    return rows[0] if rows else {}


@app.get("/api/price")
def get_price(symbol: str, hours: int = 6):
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


@app.get("/api/oi")
def get_oi(symbol: str):
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
def get_pcr(symbol: str, hours: int = 6):
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
def get_alerts(symbol: str, limit: int = 100):
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
    chart_payload = {}
    # Avoid slow/credential-bound external chart fetch for MCX symbols here.
    if sym not in _MCX_SYMBOLS:
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
def get_expiries(symbol: str):
    symbols = _matching_symbols(symbol)
    placeholders, params = _in_clause(symbols)
    rows = _q(
        f"SELECT DISTINCT expiry FROM option_chain_snapshots WHERE symbol IN ({placeholders}) ORDER BY expiry",
        params
    )
    return [r["expiry"] for r in rows]


@app.get("/api/runtime")
def get_runtime():
    return {
        "scan_frequency_minutes": get_scan_frequency_minutes(),
        "scan_frequency_options": ALLOWED_SCAN_FREQUENCIES,
        "min_scan_frequency_minutes": MIN_SCAN_FREQUENCY,
        "max_scan_frequency_minutes": MAX_SCAN_FREQUENCY,
    }


@app.post("/api/runtime")
def set_runtime(scan_frequency_minutes: int = Query(...)):
    if scan_frequency_minutes not in ALLOWED_SCAN_FREQUENCIES:
        return JSONResponse(
            {"ok": False, "error": "invalid scan_frequency_minutes", "allowed": ALLOWED_SCAN_FREQUENCIES},
            status_code=400,
        )
    value = set_scan_frequency_minutes(scan_frequency_minutes)
    return {"ok": True, "scan_frequency_minutes": value}


@app.get("/api/paper_trades")
def get_paper_trades(symbol: str = "", status: str = "", limit: int = 300):
    clauses = []
    params: list = []
    if symbol:
        clauses.append("symbol=?")
        params.append(symbol.upper().strip())
    if status:
        clauses.append("status=?")
        params.append(status.upper().strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = _q(
        f"SELECT * FROM paper_trades {where} ORDER BY opened_at DESC LIMIT ?",
        (*params, int(limit)),
    )
    return rows


@app.get("/api/paper_summary")
def get_paper_summary(symbol: str = ""):
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
            SUM(CASE WHEN status LIKE 'CLOSED_%' THEN 1 ELSE 0 END) AS closed_count,
            SUM(CASE WHEN status='CLOSED_TARGET' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN status='CLOSED_SL' THEN 1 ELSE 0 END) AS losses,
            ROUND(COALESCE(SUM(CASE WHEN status LIKE 'CLOSED_%' THEN pnl_points ELSE 0 END), 0), 2) AS closed_pnl,
            ROUND(COALESCE(AVG(CASE WHEN status LIKE 'CLOSED_%' THEN pnl_points END), 0), 2) AS avg_pnl
        FROM paper_trades
        {where}
        """,
        tuple(params),
    )
    open_rows = _q(
        f"SELECT * FROM paper_trades {where} {'AND' if where else 'WHERE'} status='OPEN' ORDER BY opened_at DESC",
        tuple(params),
    )
    out = totals[0] if totals else {}
    wins = int(out.get("wins") or 0)
    closed = int(out.get("closed_count") or 0)
    out["win_rate"] = round((wins / closed) * 100, 2) if closed > 0 else 0.0
    out["open_trades"] = open_rows
    return out


@app.get("/api/paper_equity")
def get_paper_equity(symbol: str = ""):
    clauses = ["status LIKE 'CLOSED_%'"]
    params: list = []
    if symbol:
        clauses.append("symbol=?")
        params.append(symbol.upper().strip())
    where = "WHERE " + " AND ".join(clauses)
    rows = _q(
        f"SELECT closed_at, pnl_points FROM paper_trades {where} ORDER BY closed_at",
        tuple(params),
    )
    equity = 0.0
    out = []
    for row in rows:
        equity += float(row.get("pnl_points") or 0.0)
        out.append({
            "closed_at": row.get("closed_at"),
            "pnl_points": round(float(row.get("pnl_points") or 0.0), 4),
            "equity": round(equity, 4),
        })
    return out


# ── Serve dashboard HTML ───────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    html_path = ROOT / "src" / "dashboard" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


@app.get("/paper", response_class=HTMLResponse)
def paper_dashboard():
    html_path = ROOT / "src" / "dashboard" / "paper.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>paper.html not found</h1>", status_code=404)


if __name__ == "__main__":
    print(f"  DB: {DB_PATH}")
    print(f"  Dashboard: http://localhost:8080")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
