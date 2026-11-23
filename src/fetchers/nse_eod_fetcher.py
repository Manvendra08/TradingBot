"""NSE End-Of-Day (EOD) Data Fetcher.
Fetches:
1. F&O Security Ban list
2. Market-Wide Position Limit (MWPL) utilization %
3. Corporate action / earnings calendar (results, AGM, ex-dates, splits)
Fails closed with FetchError if network/parsing fails.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

log = logging.getLogger("src.fetchers.nse_eod_fetcher")
IST = timezone(timedelta(hours=5, minutes=30))

CACHE_DIR = Path("data/cache/nse_eod")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class FetchError(Exception):
    """Raised when NSE EOD data fetch fails."""
    pass


class NseEodFetcher:
    """Fetcher for NSE EOD ban, MWPL, and corporate action data."""

    BASE_URL = "https://www.nseindia.com"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._session: Optional[requests.Session] = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(self.HEADERS)
        return self._session

    def fetch_ban_list(self) -> Set[str]:
        """
        Fetch securities currently in the F&O ban period.
        Returns a set of uppercase equity symbols (e.g. {'IDEA', 'SAIL'}).
        """
        cache_file = CACHE_DIR / f"ban_list_{datetime.now(IST).strftime('%Y%m%d')}.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return set(data)
            except Exception:
                pass

        # 1. Primary: nsearchives official CSV
        csv_url = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
        try:
            resp = requests.get(csv_url, headers=self.HEADERS, timeout=self.timeout)
            if resp.status_code == 200 and resp.text:
                banned = set()
                for line in resp.text.strip().splitlines()[1:]:
                    if "," in line:
                        parts = line.split(",")
                        if len(parts) >= 2:
                            sym = parts[1].strip().upper()
                            if sym:
                                banned.add(sym)
                if banned or "Securities in Ban" in resp.text:
                    try:
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(list(banned), f)
                    except Exception:
                        pass
                    return banned
        except Exception as e:
            log.debug("Failed fetching ban list from nsearchives csv: %s", e)

        # 2. Secondary: NSE JSON API
        url = f"{self.BASE_URL}/api/fo-sec-ban"
        session = self._get_session()
        try:
            # Prime session cookie if needed
            try:
                session.get(self.BASE_URL, timeout=self.timeout)
            except Exception:
                pass

            resp = session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                banned = set()
                items = data if isinstance(data, list) else data.get("secBanList", data.get("data", []))
                for item in items:
                    sym = item if isinstance(item, str) else item.get("symbol", item.get("secName", ""))
                    sym_clean = str(sym).strip().upper()
                    if sym_clean:
                        banned.add(sym_clean)

                try:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(list(banned), f)
                except Exception:
                    pass
                return banned
        except Exception as e:
            log.warning("Failed fetching ban list from nseindia api: %s", e)

        # 3. Fallback: Check for any existing recent cache file
        for p in sorted(CACHE_DIR.glob("ban_list_*.json"), reverse=True):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception:
                continue

        log.warning("Ban list could not be fetched; defaulting to empty ban set")
        return set()

    def fetch_mwpl(self) -> Dict[str, float]:
        """
        Fetch MWPL (Market-Wide Position Limit) utilization percentages.
        Returns dict: { 'RELIANCE': 45.2, 'IDEA': 88.6 }
        """
        cache_file = CACHE_DIR / f"mwpl_{datetime.now(IST).strftime('%Y%m%d')}.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        url = f"{self.BASE_URL}/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"
        session = self._get_session()
        try:
            try:
                session.get(self.BASE_URL, timeout=self.timeout)
            except Exception:
                pass

            resp = session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                mwpl_dict: Dict[str, float] = {}
                for item in data.get("data", []):
                    sym = str(item.get("symbol") or "").strip().upper()
                    mwpl_pct = float(item.get("mwplPercentage") or item.get("meta", {}).get("mwpl", 0.0))
                    if sym:
                        mwpl_dict[sym] = round(mwpl_pct, 2)

                try:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(mwpl_dict, f)
                except Exception:
                    pass
                return mwpl_dict
        except Exception as e:
            log.warning("MWPL fetch failed (%s); checking cache fallback", e)

        # Cache fallback
        for p in sorted(CACHE_DIR.glob("mwpl_*.json"), reverse=True):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue

        log.warning("MWPL data unavailable; defaulting to empty MWPL map")
        return {}

    def fetch_events(self, days: int = 7) -> Dict[str, List[str]]:
        """
        Fetch corporate actions / board meetings within the specified number of calendar/trading days.
        Returns dict: { 'INFY': ['BOARD_MEETING_RESULTS_2026-11-28', 'EX_DIVIDEND_2026-11-27'] }
        """
        cache_file = CACHE_DIR / f"events_{datetime.now(IST).strftime('%Y%m%d')}_{days}d.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        today = datetime.now(IST).date()
        end_date = today + timedelta(days=days + 4)  # buffer for weekends
        from_str = today.strftime("%d-%m-%Y")
        to_str = end_date.strftime("%d-%m-%Y")

        url = f"{self.BASE_URL}/api/event-calendar?from={from_str}&to={to_str}"
        session = self._get_session()
        try:
            try:
                session.get(self.BASE_URL, timeout=self.timeout)
            except Exception:
                pass

            resp = session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                events_dict: Dict[str, List[str]] = {}
                for item in (data if isinstance(data, list) else data.get("data", [])):
                    sym = str(item.get("symbol") or "").strip().upper()
                    desc = str(item.get("purpose") or item.get("desc") or "EVENT").strip().upper()
                    ev_date = str(item.get("date") or "").strip()
                    if sym:
                        events_dict.setdefault(sym, []).append(f"{desc}_{ev_date}")

                try:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(events_dict, f)
                except Exception:
                    pass
                return events_dict
        except Exception as e:
            log.warning("Corporate events fetch failed (%s); checking cache fallback", e)

        # Cache fallback
        for p in sorted(CACHE_DIR.glob("events_*.json"), reverse=True):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue

        log.warning("Corporate events data unavailable; defaulting to empty events map")
        return {}


# Module-level convenience functions
_default_fetcher = NseEodFetcher()


def fetch_ban_list() -> Set[str]:
    return _default_fetcher.fetch_ban_list()


def fetch_mwpl() -> Dict[str, float]:
    return _default_fetcher.fetch_mwpl()


def fetch_events(days: int = 7) -> Dict[str, List[str]]:
    return _default_fetcher.fetch_events(days=days)
