"""
Indian exchange trading holiday configuration.
Handles full and session-specific holiday calendars for NSE, BSE, and MCX.

Integrates with official NSE Holiday Master API:
https://www.nseindia.com/api/holiday-master?type=trading
(referenced from https://www.nseindia.com/resources/exchange-communication-holidays)

Provides persistent local caching via data/nse_holidays_cache.json and
fail-safe static fallbacks for multi-year operations.
"""
from __future__ import annotations

import logging
from datetime import datetime, date, time as dt_time, timezone, timedelta
from typing import Any

log = logging.getLogger(__name__)
_WARNED_YEARS = set()

# ── 1. STATIC CURATED HOLIDAYS (Fallback & Historical) ─────────────────────────

# 2025 NSE Holidays (Capital Market & Equity Derivatives)
NSE_HOLIDAYS_2025: set[date] = {
    date(2025, 1, 26),   # Republic Day (Sunday)
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr (Ramadan Eid)
    date(2025, 4, 6),    # Shri Ram Navami (Sunday)
    date(2025, 4, 10),   # Shri Mahavir Jayanti
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 6, 7),    # Bakri Id (Saturday)
    date(2025, 7, 6),    # Muharram (Sunday)
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Mahatma Gandhi Jayanti / Dussehra
    date(2025, 10, 21),  # Diwali Laxmi Pujan (Muhurat Trading)
    date(2025, 10, 22),  # Diwali-Balipratipada
    date(2025, 11, 5),   # Prakash Gurpurb Sri Guru Nanak Dev
    date(2025, 12, 25),  # Christmas
}

# 2026 NSE/BSE Trading Holidays (Official NSE exchange communication schedule)
NSE_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 15),   # Municipal Corporation Election - Maharashtra
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 15),   # Mahashivratri (Sunday)
    date(2026, 3, 3),    # Holi
    date(2026, 3, 21),   # Id-Ul-Fitr (Ramadan Eid) (Saturday)
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 8, 15),   # Independence Day (Saturday)
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 8),   # Diwali Laxmi Pujan (Sunday - Muhurat Trading)
    date(2026, 11, 10),  # Diwali-Balipratipada
    date(2026, 24, 11) if False else date(2026, 11, 24),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}

# BSE SENSEX shares the identical national equity derivative holiday calendar
BSE_HOLIDAYS_2026: set[date] = set(NSE_HOLIDAYS_2026)

# 2026 MCX Trading Holidays (Full day closed)
MCX_FULL_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 12, 25),  # Christmas
}

# 2026 MCX Trading Holidays (Morning session closed, Evening session open from 17:00 IST)
MCX_PARTIAL_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 15),   # Municipal Corporation Election - Maharashtra
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali-Balipratipada
    date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
}

# MCX New Year's Day (Morning session open, Evening session closed after 17:00 IST)
MCX_NEW_YEAR_HOLIDAY_2026: set[date] = {
    date(2026, 1, 1),
}

NSE_STATIC_BY_YEAR: dict[int, set[date]] = {
    2025: NSE_HOLIDAYS_2025,
    2026: NSE_HOLIDAYS_2026,
}

STATIC_DESCRIPTIONS: dict[date, str] = {
    date(2026, 1, 15): "Municipal Corporation Election - Maharashtra",
    date(2026, 1, 26): "Republic Day",
    date(2026, 2, 15): "Mahashivratri",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 21): "Id-Ul-Fitr (Ramadan Eid)",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 8, 15): "Independence Day",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 8): "Diwali Laxmi Pujan (Muhurat Trading)",
    date(2026, 11, 10): "Diwali-Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}

# Maximum year configured statically
MAX_CONFIGURED_YEAR = 2026

# In-memory dynamic caches
_DYNAMIC_HOLIDAYS_LOADED = False
_DYNAMIC_NSE_HOLIDAYS: set[date] = set()
_DYNAMIC_DESCRIPTIONS: dict[date, str] = {}
_DYNAMIC_MCX_FULL: set[date] = set()
_DYNAMIC_MCX_MORNING_CLOSED: set[date] = set()
_DYNAMIC_MCX_EVENING_CLOSED: set[date] = set()


def load_dynamic_holidays() -> None:
    """Load cached NSE holiday master JSON into in-memory lookup sets."""
    global _DYNAMIC_HOLIDAYS_LOADED, _DYNAMIC_NSE_HOLIDAYS, _DYNAMIC_DESCRIPTIONS
    global _DYNAMIC_MCX_FULL, _DYNAMIC_MCX_MORNING_CLOSED, _DYNAMIC_MCX_EVENING_CLOSED

    try:
        from src.fetchers.nse_holiday_fetcher import get_or_sync_nse_holidays
        cache = get_or_sync_nse_holidays(force=False)
        segments = cache.get("segments", {})

        fo_records = segments.get("FO", []) or segments.get("CM", [])
        for item in fo_records:
            d_str = item.get("date")
            if d_str:
                try:
                    d = datetime.strptime(d_str, "%Y-%m-%d").date()
                    _DYNAMIC_NSE_HOLIDAYS.add(d)
                    if item.get("description"):
                        _DYNAMIC_DESCRIPTIONS[d] = item["description"].strip()
                except Exception:
                    continue

        com_records = segments.get("COM", [])
        for item in com_records:
            d_str = item.get("date")
            if not d_str:
                continue
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
                morning = (item.get("morning_session") or "").strip().lower()
                evening = (item.get("evening_session") or "").strip().lower()

                if morning == "closed" and evening == "closed":
                    _DYNAMIC_MCX_FULL.add(d)
                elif morning == "closed" and evening == "open":
                    _DYNAMIC_MCX_MORNING_CLOSED.add(d)
                elif morning == "open" and evening == "closed":
                    _DYNAMIC_MCX_EVENING_CLOSED.add(d)
                if item.get("description"):
                    _DYNAMIC_DESCRIPTIONS[d] = item["description"].strip()
            except Exception:
                continue

        _DYNAMIC_HOLIDAYS_LOADED = True
        log.debug("[holidays] Dynamic holiday calendar loaded (%d NSE dates, %d MCX dates)",
                  len(_DYNAMIC_NSE_HOLIDAYS), len(_DYNAMIC_MCX_FULL) + len(_DYNAMIC_MCX_MORNING_CLOSED))

    except Exception as exc:
        log.warning("[holidays] Failed initializing dynamic holidays from cache: %s", exc)
        _DYNAMIC_HOLIDAYS_LOADED = True


def get_nse_holidays(year: int | None = None) -> set[date]:
    """Return all NSE holidays for a given year (or all years if year is None)."""
    if not _DYNAMIC_HOLIDAYS_LOADED:
        load_dynamic_holidays()

    combined = set()
    if year is not None:
        combined.update(NSE_STATIC_BY_YEAR.get(year, set()))
        combined.update({d for d in _DYNAMIC_NSE_HOLIDAYS if d.year == year})
    else:
        for s in NSE_STATIC_BY_YEAR.values():
            combined.update(s)
        combined.update(_DYNAMIC_NSE_HOLIDAYS)

    return combined


def get_bse_holidays(year: int | None = None) -> set[date]:
    """Return BSE SENSEX holidays (matches NSE derivatives schedule)."""
    return get_nse_holidays(year)


def get_holiday_reason(symbol: str, dt: datetime | date) -> str | None:
    """Return the name/description of the holiday if today is a holiday."""
    d = dt.date() if isinstance(dt, datetime) else dt
    if not _DYNAMIC_HOLIDAYS_LOADED:
        load_dynamic_holidays()

    if d in _DYNAMIC_DESCRIPTIONS:
        return _DYNAMIC_DESCRIPTIONS[d]
    if d in STATIC_DESCRIPTIONS:
        return STATIC_DESCRIPTIONS[d]
    return None


def is_market_holiday(symbol: str, dt: datetime) -> bool:
    """
    Check if the market is closed for a given symbol and datetime due to holiday.
    `dt` should be timezone-aware (Asia/Kolkata) or local time representing the market clock.
    """
    from config.symbol_classes import get_symbol_class
    if not _DYNAMIC_HOLIDAYS_LOADED:
        load_dynamic_holidays()

    d = dt.date()
    class_key = get_symbol_class(symbol)

    if class_key in ("NSE_INDEX", "BSE_INDEX"):
        nse_holidays = get_nse_holidays(d.year)
        if d in nse_holidays:
            # Special Diwali Muhurat Trading Window check:
            # If description notes Laxmi Pujan / Muhurat, evening 18:00-19:15 session is open
            reason = get_holiday_reason(symbol, d) or ""
            if "muhurat" in reason.lower() or "laxmi pujan" in reason.lower():
                t = dt.time()
                if dt_time(18, 0) <= t <= dt_time(19, 30):
                    return False  # Muhurat trading active
            return True

    elif class_key in ("MCX_COMMODITY", "MCX_AGRI"):
        # Full day holidays
        mcx_full = set(MCX_FULL_HOLIDAYS_2026) if d.year == 2026 else set()
        mcx_full.update({x for x in _DYNAMIC_MCX_FULL if x.year == d.year})
        if d in mcx_full:
            return True

        # Morning closed (open from 17:00 IST)
        mcx_morning = set(MCX_PARTIAL_HOLIDAYS_2026) if d.year == 2026 else set()
        mcx_morning.update({x for x in _DYNAMIC_MCX_MORNING_CLOSED if x.year == d.year})
        if d in mcx_morning:
            if dt.time() < dt_time(17, 0):
                return True

        # Evening closed (closed after 17:00 IST)
        mcx_evening = set(MCX_NEW_YEAR_HOLIDAY_2026) if d.year == 2026 else set()
        mcx_evening.update({x for x in _DYNAMIC_MCX_EVENING_CLOSED if x.year == d.year})
        if d in mcx_evening:
            if dt.time() >= dt_time(17, 0):
                return True

    return False
