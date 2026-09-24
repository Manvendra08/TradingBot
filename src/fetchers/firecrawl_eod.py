"""
Firecrawl EOD Fetcher — fetch market close data via Firecrawl API v2.
Runs off-market (e.g. 15:45 IST) for market-close macro reporting.

Uses multiple targeted searches to gather:
  1. Market closing prices (Nifty, Sensex, BankNifty)
  2. F&O / OI derivative flows
  3. Macro news & events
"""

import logging
import os
import requests
import time

log = logging.getLogger(__name__)

FIRECRAWL_SEARCH_URL_V2 = "https://api.firecrawl.dev/v2/search"

# Per-query timeout for each Firecrawl search
_SEARCH_TIMEOUT = 20


def _firecrawl_search(api_key: str, query: str, limit: int = 3, country: str = "in") -> list[dict]:
    """Single Firecrawl search. Returns raw items list or empty on failure."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "limit": limit,
        "country": country,
    }
    try:
        resp = requests.post(
            FIRECRAWL_SEARCH_URL_V2, json=payload, headers=headers, timeout=_SEARCH_TIMEOUT
        )
        if resp.status_code != 200:
            log.warning("[firecrawl] HTTP %d for query=%.60s", resp.status_code, query)
            return []

        data = resp.json()
        if not data.get("success", False) and "data" not in data:
            return []

        raw_data = data.get("data")
        raw_items = []
        if isinstance(raw_data, dict):
            raw_items = raw_data.get("web") or raw_data.get("results") or raw_data.get("items") or []
        elif isinstance(raw_data, list):
            raw_items = raw_data
        elif isinstance(data.get("web"), list):
            raw_items = data.get("web")

        items = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            desc = item.get("description") or item.get("snippet") or ""
            items.append({
                "title": item.get("title") or item.get("name") or "Untitled",
                "url": item.get("url") or "",
                "description": desc,
                "snippet": (item.get("markdown") or desc)[:1200],
            })
        return items

    except Exception as exc:
        log.warning("[firecrawl] Search failed: %s", exc)
        return []


def fetch_firecrawl_eod_data(report_date: str | None = None) -> dict:
    """Fetch full EOD market data via multiple Firecrawl searches with burst pacing.

    Returns:
        {
            "ok": bool,
            "closing_bell": {"items": list, "count": int, "error": str|None},
            "fii_dii_flows": {"items": list, "count": int, "error": str|None},
            "macro_news":    {"items": list, "count": int, "error": str|None},
        }
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        log.warning("[firecrawl] FIRECRAWL_API_KEY not set — skipping all EOD searches")
        empty = {"items": [], "count": 0, "error": "FIRECRAWL_API_KEY missing"}
        return {"ok": False, "closing_bell": empty, "fii_dii_flows": empty, "macro_news": empty}

    date_hint = f" {report_date}" if report_date else ""

    # Include the explicit report date because "today" search results can lag
    # by a session around the Indian market close.
    closing = _firecrawl_search(
        api_key,
        f"Indian stock market closing bell Sensex Nifty{date_hint}",
        limit=3,
    )
    log.info("[firecrawl] Closing bell: %d items", len(closing))
    time.sleep(1.5)  # Pace queries to avoid Firecrawl burst rate limits

    # ── Query 2: FII/DII Institutional flows ───────────────────────────────
    fii_dii = _firecrawl_search(
        api_key,
        f"Indian stock market closing FII DII flow Sensex Nifty{date_hint}",
        limit=2,
    )
    log.info("[firecrawl] FII/DII flows: %d items", len(fii_dii))
    time.sleep(1.5)

    # ── Query 3: Market wrap & macro commentary ────────────────────────────
    news = _firecrawl_search(
        api_key,
        f"Sensex Nifty closing news market wrap Moneycontrol Livemint{date_hint}",
        limit=2,
    )
    log.info("[firecrawl] Market wrap: %d items", len(news))

    has_data = bool(closing or fii_dii or news)
    return {
        "ok": has_data,
        "closing_bell": {"items": closing, "count": len(closing), "error": None if closing else "No results"},
        "fii_dii_flows": {"items": fii_dii, "count": len(fii_dii), "error": None if fii_dii else "No results"},
        "macro_news":    {"items": news, "count": len(news), "error": None if news else "No results"},
    }


# ── Legacy wrapper (backward compat) ───────────────────────────────────────

def fetch_firecrawl_macro_news(
    query: str = "Indian stock market Sensex Nifty close news today",
    limit: int = 5,
    country: str = "in",
) -> dict:
    """Backward-compatible single-query wrapper. Prefer fetch_firecrawl_eod_data()."""
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        log.warning("[firecrawl] FIRECRAWL_API_KEY not set in environment — skipping search")
        return {"ok": False, "items": [], "count": 0, "error": "FIRECRAWL_API_KEY missing"}

    items = _firecrawl_search(api_key, query, limit, country=country)
    return {"ok": bool(items), "items": items, "count": len(items), "error": None if items else "No results"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = fetch_firecrawl_eod_data()
    for section, res in data.items():
        if isinstance(res, dict) and "items" in res:
            print(f"\n{section}: {res['count']} items")
            for i in res["items"][:2]:
                print(f"  - {i['title'][:80]}")
