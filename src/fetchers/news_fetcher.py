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
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

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

_POS_WORDS = ["rally", "rises", "rise", "gain", "surge", "jump", "bullish",
              "tight", "demand", "up", "climb", "soar", "recover", "rebound"]
_NEG_WORDS = ["fall", "falls", "drop", "retreat", "decline", "slump", "bearish",
              "oversupply", "cools", "down", "crash", "plunge", "weak", "tumble"]


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
                "User-Agent": "Mozilla/5.0",
                "Connection": "close"  # Prevent keep-alive issues
            }
        )
        log.debug("[news] Received response status %s from TradingView for %s", res.status_code, symbol)
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
            rows.append({
                "title": title,
                "provider": provider,
                "published": pub,
                "published_at": datetime.fromtimestamp(pub, timezone.utc).isoformat(),
                "url": story_url,
                "score": _news_sentiment_score(title),
            })
        rows.sort(key=lambda x: x["published"], reverse=True)
        current_items = rows[:5]
        current_score = (sum(r["score"] for r in current_items) / len(current_items)) if current_items else 0.0
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


# ── Commentary Scrapers ───────────────────────────────────────────────────

def _fetch_icici_commentary() -> list[dict]:
    url = "https://www.icicidirect.com/share-market-today/market-news-commentary"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close"
    }
    rows = []
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

        res = session.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            elements = soup.find_all(["p", "div", "span", "li"])
            for el in elements:
                t = el.text.strip().replace("\xa0", " ").replace("\u200b", "")
                if len(t) > 60 and any(w in t for w in ["Nifty", "Sensex", "market", "GIFT", "benchmark", "index", "indices"]):
                    if any(skip in t.lower() for skip in ["relationship manager", "kra", "kyc", "cheque", "disclaimer", "open an account"]):
                        continue
                    t = " ".join(t.split())
                    title = t[:200] + "..." if len(t) > 200 else t
                    if not any(r["title"] == title for r in rows):
                        rows.append({
                            "title": title,
                            "provider": "ICICIDirect",
                            "published": int(time.time()),
                            "published_at": datetime.now(timezone.utc).isoformat(),
                            "url": url,
                            "score": _news_sentiment_score(t),
                        })
    except Exception as e:
        log.warning("ICICIDirect fetch failed: %s", e)
    return rows


def _fetch_way2wealth_commentary() -> list[dict]:
    url = "https://www.way2wealth.com/market/marketcommentry/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Connection": "close",
    }
    rows = []
    try:
        import urllib3
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

        res = session.get(url, headers=headers, verify=False, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for el in soup.find_all(["p", "div", "td", "span"]):
                t = el.text.strip().replace("\xa0", " ").replace("\u200b", "")
                if len(t) > 60 and any(w in t.lower() for w in ["nifty", "sensex", "benchmark", "expected to open", "outlook", "support", "resistance"]):
                    if any(skip in t.lower() for skip in ["kra", "kyc", "relationship manager", "complaint", "cheque", "scores", "backoffice", "antara"]):
                        continue
                    lines = [line.strip() for line in t.split("\n") if len(line.strip()) > 50]
                    for line in lines:
                        if any(w in line.lower() for w in ["nifty", "sensex", "benchmark", "global", "open", "cautious", "expected", "outlook"]):
                            if any(skip in line.lower() for skip in ["kra", "kyc", "complaint", "cheque", "scores"]):
                                continue
                            line = " ".join(line.split())
                            title = line[:200] + "..." if len(line) > 200 else line
                            if not any(r["title"] == title for r in rows):
                                rows.append({
                                    "title": title,
                                    "provider": "Way2Wealth",
                                    "published": int(time.time()),
                                    "published_at": datetime.now(timezone.utc).isoformat(),
                                    "url": url,
                                    "score": _news_sentiment_score(line),
                                })
    except Exception as e:
        log.warning("Way2Wealth fetch failed: %s", e)
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

    # Commodities: TradingView API
    if sym in ("NATURALGAS", "CRUDEOIL"):
        result = _fetch_tv_commodity_news(sym)
        return _cache_set(cache_key, result)

    # Indices: Scrape ICICIDirect and Way2Wealth
    if sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        icici_items = _fetch_icici_commentary()
        w2w_items = _fetch_way2wealth_commentary()
        all_items = icici_items + w2w_items
        
        filtered_items = []
        for item in all_items:
            t = item["title"].lower()
            if sym == "BANKNIFTY":
                # For BANKNIFTY, prioritize banking, banks, banknifty, and broad index commentary
                if any(w in t for w in ["bank", "nifty", "index", "benchmark", "indices"]):
                    filtered_items.append(item)
            else:
                filtered_items.append(item)
                
        # Deduplicate
        seen_titles = set()
        final_items = []
        for item in filtered_items:
            if item["title"] not in seen_titles:
                seen_titles.add(item["title"])
                final_items.append(item)
                
        final_items.sort(key=lambda x: x["published"], reverse=True)
        
        current_items = final_items[:5]
        current_score = (sum(r["score"] for r in current_items) / len(current_items)) if current_items else 0.0
        day_score = (sum(r["score"] for r in final_items) / len(final_items)) if final_items else 0.0
        
        result = {
            "items": final_items[:10],
            "count_24h": len(final_items),
            "current_news_direction": _dir_label(current_score),
            "news_score_current": round(current_score, 3),
            "news_score_day": round(day_score, 3),
        }
        return _cache_set(cache_key, result)

    # Fallback to empty news
    return _cache_set(cache_key, _empty_news())
