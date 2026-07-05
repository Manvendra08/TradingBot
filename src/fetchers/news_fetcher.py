"""
News Fetcher — reusable news sentiment module for the trading pipeline.

Extracted from dashboard_server.py to allow the scan pipeline (not just the UI)
to feed news context to the AI brain.

Supports:
  - NATURALGAS / CRUDEOIL: TradingView news API
  - NIFTY / BANKNIFTY: (placeholder, extensible)

Returns: {items, count_24h, current_news_direction, news_score_current}
"""

import logging
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
# Suppress noisy urllib3 retry warnings for transient connection resets
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

# ── NewsAPI.org ────────────────────────────────────────────────────────────
_NEWSAPI_BASE = "https://newsapi.org/v2/everything"
from config.settings import NEWSAPI_KEY as _NEWSAPI_KEY

# Map symbols to NewsAPI search queries (Indian-market focused)
_SYMBOL_NEWSAPI_QUERIES: dict[str, str] = {
    "NIFTY": "Nifty OR Sensex India stock market NSE",
    "BANKNIFTY": "Bank Nifty OR Nifty Bank banking stocks India",
    "FINNIFTY": "Fin Nifty OR Nifty Financial Services India",
    "MIDCPNIFTY": "Midcap Nifty OR Nifty Midcap India stock market",
    "SENSEX": "Sensex OR BSE India stock market NSE",
    "NATURALGAS": "Natural Gas India MCX commodity price",
    "CRUDEOIL": "Crude Oil India MCX commodity price",
    "GOLD": "Gold price India MCX commodity",
    "SILVER": "Silver price India MCX commodity",
}

# ── TradingView News API ──────────────────────────────────────────────────
_TV_NEWS_API = "https://news-headlines.tradingview.com/v2/view/headlines/symbol?client=web&lang=en&category=base&symbol=MCX:NATURALGAS1!"

# ── In-memory cache ──────────────────────────────────────────────────────
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600  # 10 minutes


def _cache_get(key: str) -> dict | None:
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None


def _cache_set(key: str, data: dict) -> dict:
    _cache[key] = (time.time(), data)
    return data


# ── Sentiment scoring ────────────────────────────────────────────────────

_POS_WORDS = [
    "rally",
    "rises",
    "rise",
    "gain",
    "surge",
    "jump",
    "bullish",
    "tight",
    "demand",
    "up",
    "climb",
    "soar",
    "recover",
    "rebound",
]
_NEG_WORDS = [
    "fall",
    "falls",
    "drop",
    "retreat",
    "decline",
    "slump",
    "bearish",
    "oversupply",
    "cools",
    "down",
    "crash",
    "plunge",
    "weak",
    "tumble",
]


def _news_sentiment_score(title: str) -> int:
    t = (title or "").lower()
    score = 0
    for w in _POS_WORDS:
        if w in t:
            score += 1
    for w in _NEG_WORDS:
        if w in t:
            score -= 1
    return score


def _dir_label(score: float) -> str:
    if score >= 0.35:
        return "BULLISH"
    if score <= -0.35:
        return "BEARISH"
    return "MIXED"


# ── Fetchers ─────────────────────────────────────────────────────────────


def _fetch_tv_commodity_news(symbol: str) -> dict:
    """Fetch TradingView commodity news headlines (last 24h)."""
    # Map symbol to TradingView streaming symbol
    tv_symbols = {
        "NATURALGAS": "MCX:NATURALGAS1!",
        "CRUDEOIL": "MCX:CRUDEOIL1!",
    }
    stream_sym = tv_symbols.get(symbol)
    if not stream_sym:
        return _empty_news()

    url = f"https://news-headlines.tradingview.com/v2/view/headlines/symbol?client=web&lang=en&category=base&symbol={stream_sym}"

    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        # Configure retry strategy
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session = requests.Session()
        session.mount("https://", adapter)

        log.debug("[news] Sending request to TradingView for %s", symbol)
        res = session.get(
            url,
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
            },
        )
        log.debug(
            "[news] Received response status %s from TradingView for %s",
            res.status_code,
            symbol,
        )
        res.raise_for_status()
        payload = res.json() if res.content else {}
        items = payload.get("items") if isinstance(payload, dict) else []
        # Commodities look back up to 10 days to ensure recent headlines are not filtered out
        cutoff = int(time.time()) - (10 * 86400)
        rows = []
        for item in items or []:
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
            story_url = (
                f"https://in.tradingview.com{story_path}"
                if story_path.startswith("/")
                else ""
            )
            rows.append(
                {
                    "title": title,
                    "provider": provider,
                    "published": pub,
                    "published_at": datetime.fromtimestamp(
                        pub, timezone.utc
                    ).isoformat(),
                    "url": story_url,
                    "score": _news_sentiment_score(title),
                }
            )
        rows.sort(key=lambda x: x["published"], reverse=True)
        current_items = rows[:5]
        current_score = (
            (sum(r["score"] for r in current_items) / len(current_items))
            if current_items
            else 0.0
        )
        day_score = (sum(r["score"] for r in rows) / len(rows)) if rows else 0.0
        return {
            "items": rows[:10],
            "count_24h": len(rows),
            "current_news_direction": _dir_label(current_score),
            "news_score_current": round(current_score, 3),
            "news_score_day": round(day_score, 3),
        }
    except Exception as exc:
        log.warning("news fetch failed for %s: %s", symbol, exc)
        return _empty_news()


def _empty_news() -> dict:
    return {
        "items": [],
        "count_24h": 0,
        "current_news_direction": "MIXED",
        "news_score_current": 0.0,
        "news_score_day": 0.0,
    }


# ── NewsAPI.org Fetcher ──────────────────────────────────────────────────────


def _fetch_newsapi_news(symbol: str) -> list[dict]:
    """
    Fetch Indian-market news from NewsAPI.org as a supplemental source.
    Free tier: 100 req/day, only articles from last 24h.
    Returns list of items in the same format as other fetchers.
    """
    api_key = _NEWSAPI_KEY
    if not api_key:
        log.debug("[newsapi] NEWSAPI_KEY not configured, skipping")
        return []

    query = _SYMBOL_NEWSAPI_QUERIES.get(symbol.upper())
    if not query:
        return []

    try:
        params = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 20,
            "apiKey": api_key,
        }
        log.debug("[newsapi] Fetching news for %s with query: %s", symbol, query)
        res = requests.get(_NEWSAPI_BASE, params=params, timeout=15)
        res.raise_for_status()
        payload = res.json()

        if payload.get("status") != "ok":
            log.warning(
                "[newsapi] API returned status=%s for %s",
                payload.get("status"),
                symbol,
            )
            return []

        articles = payload.get("articles") or []
        rows = []
        now_ts = int(time.time())
        # Free tier articles may be up to ~48h old; use a loose cutoff
        # to ensure we capture them despite staggered API caching
        cutoff = now_ts - (48 * 86400)

        for art in articles:
            title = (art.get("title") or "").strip()
            if not title:
                continue
            # Parse publishedAt to timestamp
            pub_str = art.get("publishedAt") or ""
            pub_ts = now_ts
            if pub_str:
                try:
                    dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                    pub_ts = int(dt.timestamp())
                except (ValueError, TypeError):
                    pass

            if pub_ts < cutoff:
                continue

            source_info = art.get("source") or {}
            provider = source_info.get("name") or "NewsAPI"
            url = art.get("url") or ""
            description = (art.get("description") or "").strip()

            rows.append(
                {
                    "title": title,
                    "provider": provider,
                    "published": pub_ts,
                    "published_at": datetime.fromtimestamp(
                        pub_ts, timezone.utc
                    ).isoformat(),
                    "url": url,
                    "score": _news_sentiment_score(title + " " + description),
                }
            )

        rows.sort(key=lambda x: x["published"], reverse=True)
        log.debug("[newsapi] Fetched %d articles for %s", len(rows), symbol)
        return rows

    except requests.exceptions.Timeout:
        log.debug("[newsapi] Timeout fetching news for %s", symbol)
        return []
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        if status == 426:
            log.debug(
                "[newsapi] HTTP 426 (upgrade required) for %s — free tier may"
                " be exhausted today",
                symbol,
            )
        else:
            log.debug(
                "[newsapi] HTTP %s fetching news for %s: %s",
                status,
                symbol,
                e,
            )
        return []
    except Exception as exc:
        log.debug("[newsapi] Failed to fetch news for %s: %s", symbol, exc)
        return []


# ── Commentary Scrapers ───────────────────────────────────────────────────


def _fetch_icici_commentary() -> list[dict]:
    url = "https://www.icicidirect.com/share-market-today/market-news-commentary"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    }
    rows = []
    try:
        session = requests.Session()
        # No retries to fail fast and avoid warnings for connection resets
        res = session.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            elements = soup.find_all(["p", "div", "span", "li"])
            for el in elements:
                t = el.text.strip().replace("\xa0", " ").replace("\u200b", "")
                if len(t) > 60 and any(
                    w in t
                    for w in [
                        "Nifty",
                        "Sensex",
                        "market",
                        "GIFT",
                        "benchmark",
                        "index",
                        "indices",
                    ]
                ):
                    if any(
                        skip in t.lower()
                        for skip in [
                            "relationship manager",
                            "kra",
                            "kyc",
                            "cheque",
                            "disclaimer",
                            "open an account",
                        ]
                    ):
                        continue
                    t = " ".join(t.split())
                    title = t[:200] + "..." if len(t) > 200 else t
                    if not any(r["title"] == title for r in rows):
                        rows.append(
                            {
                                "title": title,
                                "provider": "ICICIDirect",
                                "published": int(time.time()),
                                "published_at": datetime.now(timezone.utc).isoformat(),
                                "url": url,
                                "score": _news_sentiment_score(t),
                            }
                        )
    except Exception as e:
        log.info("ICICIDirect fetch failed (possibly blocked by WAF): %s", e)
    return rows


# ── Public API ───────────────────────────────────────────────────────────


def fetch_news(symbol: str) -> dict:
    """
    Fetch latest news and sentiment for a symbol.
    Returns cached result if available (10-min TTL).

    Returns:
        {
            "items": list[{title, provider, score, ...}],
            "count_24h": int,
            "current_news_direction": "BULLISH" | "BEARISH" | "MIXED",
            "news_score_current": float,
            "news_score_day": float,
        }
    """
    cache_key = f"news:{symbol}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    sym = symbol.upper().strip()

    # Step 1: Fetch symbol-specific primary source
    primary_items: list[dict] = []
    if sym in ("NATURALGAS", "CRUDEOIL"):
        tv_result = _fetch_tv_commodity_news(sym)
        primary_items = tv_result.get("items", [])
    elif sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        icici_items = _fetch_icici_commentary()
        all_items = icici_items

        for item in all_items:
            t = item["title"].lower()
            if sym == "BANKNIFTY":
                if any(
                    w in t for w in ["bank", "nifty", "index", "benchmark", "indices"]
                ):
                    primary_items.append(item)
            else:
                primary_items.append(item)

    # Step 2: Supplement with NewsAPI.org for ALL symbols
    newsapi_items = _fetch_newsapi_news(sym)

    # Step 3: Merge — deduplicate by title, prefer primary source order
    seen_titles: set[str] = set()
    merged: list[dict] = []
    for item in primary_items + newsapi_items:
        t = item["title"]
        if t not in seen_titles:
            seen_titles.add(t)
            merged.append(item)

    merged.sort(key=lambda x: x["published"], reverse=True)

    current_items = merged[:5]
    current_score = (
        (sum(r["score"] for r in current_items) / len(current_items))
        if current_items
        else 0.0
    )
    day_score = (sum(r["score"] for r in merged) / len(merged)) if merged else 0.0

    result = {
        "items": merged[:10],
        "count_24h": len(merged),
        "current_news_direction": _dir_label(current_score),
        "news_score_current": round(current_score, 3),
        "news_score_day": round(day_score, 3),
    }
    return _cache_set(cache_key, result)
