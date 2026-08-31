"""
Indian exchange trading holiday configuration.
Handles full and session-specific holiday calendars for NSE and MCX.

TODO (L1): Implement dynamic holiday fetching from NSE/MCX APIs.
Hardcoded dates will become stale after MAX_CONFIGURED_YEAR, causing is_market_holiday() 
to incorrectly return False.
"""
from datetime import datetime, date

# Maximum year for which holiday data is configured.
# Update this and the holiday sets when new year data becomes available.
MAX_CONFIGURED_YEAR = 2026

# 2026 NSE/BSE Trading Holidays (all day closed)
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali-Balipratipada
    date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25),  # Christmas
}

# 2026 MCX Trading Holidays (Full day closed)
MCX_FULL_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 12, 25),  # Christmas
}

# 2026 MCX Trading Holidays (Morning session closed, Evening session open from 17:00 IST)
MCX_PARTIAL_HOLIDAYS_2026 = {
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
MCX_NEW_YEAR_HOLIDAY_2026 = {
    date(2026, 1, 1),
}


import logging
from datetime import time as dt_time

log = logging.getLogger(__name__)
_WARNED_YEARS = set()


def _check_year_supported(d: date) -> bool:
    """Check if the holiday calendar supports the given year.
    Returns True if supported, False if year is beyond MAX_CONFIGURED_YEAR.
    Logs a critical warning once per unsupported year."""
    if d.year > MAX_CONFIGURED_YEAR:
        if d.year not in _WARNED_YEARS:
            _WARNED_YEARS.add(d.year)
            log.critical(
                "Holiday calendar only configured up to year %d. Date %s (year %d) encountered; "
                "update config/holidays.py with holiday data for year %d. "
                "Returning True (fail-closed) to prevent trading on unknown holidays.",
                MAX_CONFIGURED_YEAR, d, d.year, d.year
            )
        return False
    return True


def is_market_holiday(symbol: str, dt: datetime) -> bool:
    """
    Check if the market is closed for a given symbol and datetime due to holiday.
    `dt` should be timezone-aware (Asia/Kolkata) or local time representing the market clock.
    """
    from config.symbol_classes import get_symbol_class
    
    d = dt.date()
    
    # Fail-closed: if year not supported, assume holiday to prevent trading
    if not _check_year_supported(d):
        return True
    
    class_key = get_symbol_class(symbol)
    
    if class_key in ("NSE_INDEX", "BSE_INDEX"):
        if d in NSE_HOLIDAYS_2026:
            return True
            
    elif class_key == "MCX_COMMODITY":
        if d in MCX_FULL_HOLIDAYS_2026:
            return True
            
        if d in MCX_PARTIAL_HOLIDAYS_2026:
            if dt.time() < dt_time(17, 0):
                return True
                
        if d in MCX_NEW_YEAR_HOLIDAY_2026:
            if dt.time() >= dt_time(17, 0):
                return True
                
    return False
