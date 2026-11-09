"""
NSE India Exchange Communication & Trading Holiday Fetcher.
Fetches official trading holidays dynamically from:
https://www.nseindia.com/api/holiday-master?type=trading
(referenced from https://www.nseindia.com/resources/exchange-communication-holidays)

Provides persistent local caching in data/nse_holidays_cache.json with
graceful offline fallback.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Any

from config.settings import DATA_DIR, NSE_BASE_URL, NSE_HEADERS

log = logging.getLogger(__name__)

CACHE_FILE = DATA_DIR / "nse_holidays_cache.json"
IST = timezone(timedelta(hours=5, minutes=30))
NSE_HOLIDAYS_API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
CACHE_TTL_HOURS = 24


def _parse_nse_date(date_str: str) -> date | None:
    """Tolerantly parse NSE date formats like '15-Jan-2026' or '2026-01-15'."""
    s = str(date_str).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def fetch_nse_holidays_from_api(timeout: int = 12) -> dict[str, Any] | None:
    """
    Fetch live trading holidays directly from NSE holiday-master API.
    Uses NSEPublicFetcher to establish required session cookies.
    """
    try:
        from src.fetchers.nse_fetcher import NSEPublicFetcher
        fetcher = NSEPublicFetcher()
        fetcher._warm_session()

        headers = dict(fetcher.session.headers)
        headers["Referer"] = "https://www.nseindia.com/resources/exchange-communication-holidays"
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"

        resp = fetcher.session.get(NSE_HOLIDAYS_API_URL, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            log.warning("[nse_holiday_fetcher] HTTP %d fetching holidays: %s", resp.status_code, resp.text[:120])
            return None

        raw_data = resp.json()
        if not isinstance(raw_data, dict):
            log.warning("[nse_holiday_fetcher] Unexpected API payload format: %s", type(raw_data))
            return None

        now_ist = datetime.now(IST)
        parsed_payload: dict[str, Any] = {
            "fetched_at": now_ist.isoformat(),
            "source": NSE_HOLIDAYS_API_URL,
            "segments": {},
        }

        for segment, records in raw_data.items():
            if not isinstance(records, list):
                continue
            seg_list = []
            for item in records:
                if not isinstance(item, dict):
                    continue
                d = _parse_nse_date(item.get("tradingDate", ""))
                if not d:
                    continue
                seg_list.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "description": item.get("description", "").strip(),
                    "weekday": item.get("weekDay", "").strip(),
                    "morning_session": item.get("morning_session"),
                    "evening_session": item.get("evening_session"),
                    "sr_no": item.get("Sr_no"),
                })
            parsed_payload["segments"][segment] = seg_list

        log.info(
            "[nse_holiday_fetcher] Successfully fetched holidays from NSE API (%d FO, %d CM, %d COM records)",
            len(parsed_payload["segments"].get("FO", [])),
            len(parsed_payload["segments"].get("CM", [])),
            len(parsed_payload["segments"].get("COM", [])),
        )
        return parsed_payload

    except Exception as exc:
        log.warning("[nse_holiday_fetcher] Error fetching holidays from NSE API: %s", exc)
        return None


def save_holiday_cache(payload: dict[str, Any], path: Path = CACHE_FILE) -> bool:
    """Save parsed holiday payload to JSON cache atomically."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as exc:
        log.error("[nse_holiday_fetcher] Failed to write holiday cache to %s: %s", path, exc)
        return False


def load_holiday_cache(path: Path = CACHE_FILE) -> dict[str, Any] | None:
    """Load cached holiday data from disk."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "segments" in data:
            return data
    except Exception as exc:
        log.warning("[nse_holiday_fetcher] Failed reading holiday cache from %s: %s", path, exc)
    return None


def get_or_sync_nse_holidays(force: bool = False, max_age_hours: int = CACHE_TTL_HOURS) -> dict[str, Any]:
    """
    Retrieve holiday data. Returns cached data if fresh; otherwise attempts
    a live sync against NSE API. Falls back gracefully to existing cache or empty.
    """
    cached = load_holiday_cache()
    if cached and not force:
        fetched_at_str = cached.get("fetched_at")
        if fetched_at_str:
            try:
                fetched_at = datetime.fromisoformat(fetched_at_str)
                age = datetime.now(IST) - fetched_at
                if age < timedelta(hours=max_age_hours):
                    return cached
            except Exception:
                pass

    # Needs refresh
    live = fetch_nse_holidays_from_api()
    if live and live.get("segments"):
        save_holiday_cache(live)
        return live

    if cached:
        log.info("[nse_holiday_fetcher] Using existing cached holiday data (live sync failed/unavailable)")
        return cached

    return {"fetched_at": None, "segments": {}}
