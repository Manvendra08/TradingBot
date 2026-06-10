"""
Indian exchange trading holiday configuration for 2026.
Handles full and session-specific holiday calendars for NSE and MCX.
"""
from datetime import datetime, date

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


def is_market_holiday(symbol: str, dt: datetime) -> bool:
    """
    Check if the market is closed for a given symbol and datetime due to holiday.
    `dt` should be timezone-aware (Asia/Kolkata) or local time representing the market clock.
    """
    from config.symbol_classes import get_symbol_class
    
    d = dt.date()
    class_key = get_symbol_class(symbol)
    
    if class_key == "NSE_INDEX":
        if d in NSE_HOLIDAYS_2026:
            return True
            
    elif class_key == "MCX_COMMODITY":
        if d in MCX_FULL_HOLIDAYS_2026:
            return True
            
        if d in MCX_PARTIAL_HOLIDAYS_2026:
            t = dt.strftime("%H:%M")
            if t < "17:00":
                return True
                
        if d in MCX_NEW_YEAR_HOLIDAY_2026:
            t = dt.strftime("%H:%M")
            if t >= "17:00":
                return True
                
    return False
