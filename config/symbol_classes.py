"""
Symbol class metadata — maps each watched symbol to its exchange class,
strike step size, and market window key.
"""
from __future__ import annotations

from config.settings import MARKET_WINDOWS


# symbol → (class_key, strike_step_points)
# strike_step_points is used for cluster-dedup width calculation.
_SYMBOL_META: dict[str, tuple[str, int]] = {
    "NIFTY":       ("NSE_INDEX", 50),
    "BANKNIFTY":   ("NSE_INDEX", 100),
    "FINNIFTY":    ("NSE_INDEX", 50),
    "MIDCPNIFTY":  ("NSE_INDEX", 25),
    "SENSEX":      ("BSE_INDEX", 100),
    "NATURALGAS":  ("MCX_COMMODITY", 5),
    "CRUDEOIL":    ("MCX_COMMODITY", 100),
    "GOLD":        ("MCX_COMMODITY", 100),
    "SILVER":      ("MCX_COMMODITY", 500),
}

_DEFAULT_CLASS       = "NSE_INDEX"
_DEFAULT_STRIKE_STEP = 50


# Hardcoded 2026 MCX Futures Expiry dates to match official Dhan/MCX schedules
_MCX_2026_EXPIRIES: dict[str, dict[int, tuple[int, int, int]]] = {
    "NATURALGAS": {
        1: (2026, 1, 27),
        2: (2026, 2, 24),
        3: (2026, 3, 26),
        4: (2026, 4, 27),
        5: (2026, 5, 26),
        6: (2026, 6, 25),
        7: (2026, 7, 28),
        8: (2026, 8, 26),
        9: (2026, 9, 25),
        10: (2026, 10, 27),
        11: (2026, 11, 24),
        12: (2026, 12, 28),
    },
    "CRUDEOIL": {
        1: (2026, 1, 16),
        2: (2026, 2, 19),
        3: (2026, 3, 19),
        4: (2026, 4, 20),
        5: (2026, 5, 18),
        6: (2026, 6, 18),
        7: (2026, 7, 20),
        8: (2026, 8, 19),
        9: (2026, 9, 21),
        10: (2026, 10, 19),
        11: (2026, 11, 19),
        12: (2026, 12, 18),
    }
}


def _base_symbol(symbol: str) -> str:
    """Normalize expiry/month variants like 'NATURALGAS MAY FUT'."""
    return str(symbol or "").upper().strip().split()[0]


def get_symbol_class(symbol: str) -> str:
    """Return the market-window class key for a symbol."""
    return _SYMBOL_META.get(_base_symbol(symbol), (_DEFAULT_CLASS, _DEFAULT_STRIKE_STEP))[0]


def get_strike_step(symbol: str) -> int:
    """
    Return the canonical strike step (in points) for a symbol.
    Used by dedup cluster-width logic so suppression radius is
    expressed in number-of-strikes, not raw points.
    Example: NIFTY → 50, BANKNIFTY → 100
    """
    return _SYMBOL_META.get(_base_symbol(symbol), (_DEFAULT_CLASS, _DEFAULT_STRIKE_STEP))[1]


def market_window(symbol: str) -> tuple[str, str, list[int]]:
    """Return (open, close, weekdays) for the symbol's configured market class."""
    class_key = get_symbol_class(symbol)
    return MARKET_WINDOWS.get(class_key, MARKET_WINDOWS[_DEFAULT_CLASS])


def is_market_open(symbol: str, dt=None) -> bool:
    """Check if the market is currently open for the given symbol."""
    from datetime import datetime
    import pytz
    if dt is None:
        dt = datetime.now(pytz.timezone("Asia/Kolkata"))
    open_t, close_t, days = market_window(symbol)
    if dt.weekday() not in days:
        return False
    from config.holidays import is_market_holiday
    if is_market_holiday(symbol, dt):
        return False
    t = dt.strftime("%H:%M")
    return open_t <= t <= close_t


def get_kite_exchange(symbol: str) -> str:
    """Return Zerodha Kite exchange code for order/instrument resolution."""
    base = _base_symbol(symbol)
    if base in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"):
        return "MCX"
    if base == "SENSEX":
        return "BFO"
    return "NFO"


# ---------------------------------------------------------------------------
# Futures/Options expiry resolution — DYNAMIC FETCH FROM EXCHANGE
# ---------------------------------------------------------------------------
from functools import lru_cache

@lru_cache(maxsize=32)
def _fetch_nse_expiry_calendar() -> dict:
    """Fetch NSE option expiry calendar from NSE API. Returns dict with symbol -> list of expiry dates."""
    import requests
    from datetime import date
    try:
        # NSE option chain API for expiry calendar
        url = "https://www.nseindia.com/api/option-chain-indices"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("records", {})
        return records
    except Exception as e:
        log.warning(f"Failed to fetch NSE expiry calendar: {e}")
        return {}

@lru_cache(maxsize=32)
def _fetch_mcx_expiry_calendar() -> dict:
    """Fetch MCX option expiry calendar from MCX API."""
    import requests
    try:
        # MCX doesn't have a public expiry calendar API, use Dhan instruments as fallback
        # This will be populated by the fetcher when it runs
        return {}
    except Exception as e:
        log.warning(f"Failed to fetch MCX expiry calendar: {e}")
        return {}


def _get_nse_weekly_expiry(ref_date: "date", base: str) -> "str | None":
    """
    For NSE indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY):
    - Weekly expiry on TUESDAY (not Thursday!)
    - Returns the next Tuesday's expiry date
    """
    from datetime import timedelta
    try:
        from config.holidays import NSE_HOLIDAYS_2026
        nse_holidays = NSE_HOLIDAYS_2026
    except Exception:
        nse_holidays = set()
    
    # Tuesday = weekday 1
    days_ahead = (1 - ref_date.weekday() + 7) % 7
    raw = ref_date + timedelta(days=days_ahead)
    
    # Adjust for holidays - if Tuesday is holiday, move to previous working day
    while raw.weekday() >= 5 or raw in nse_holidays:
        raw -= timedelta(days=1)
    
    expiry = raw
    if expiry < ref_date:
        # If this week's expiry passed, get next week's
        raw += timedelta(days=7)
        while raw.weekday() >= 5 or raw in nse_holidays:
            raw -= timedelta(days=1)
        expiry = raw
    return expiry.strftime("%Y-%m-%d")


def _get_bse_weekly_expiry(ref_date: "date", base: str) -> "str | None":
    """
    For BSE SENSEX: Weekly expiry on THURSDAY
    - Returns the next Thursday's expiry date
    - Adjusts for BSE holidays
    """
    from datetime import timedelta
    try:
        from config.holidays import BSE_HOLIDAYS_2026
        bse_holidays = BSE_HOLIDAYS_2026
    except Exception:
        bse_holidays = set()
    
    # Thursday = weekday 3
    days_ahead = (3 - ref_date.weekday() + 7) % 7
    raw = ref_date + timedelta(days=days_ahead)
    
    # Adjust for holidays - if Thursday is holiday, move to previous working day
    while raw.weekday() >= 5 or raw in bse_holidays:
        raw -= timedelta(days=1)
    
    expiry = raw
    if expiry < ref_date:
        # If this week's expiry passed, get next week's
        raw += timedelta(days=7)
        while raw.weekday() >= 5 or raw in bse_holidays:
            raw -= timedelta(days=1)
        expiry = raw
    return expiry.strftime("%Y-%m-%d")


def _get_nse_monthly_expiry(ref_date: "date", base: str) -> "str | None":
    from datetime import date as _date, timedelta
    try:
        from config.holidays import NSE_HOLIDAYS_2026
        nse_holidays = NSE_HOLIDAYS_2026
    except Exception:
        nse_holidays = set()
    
    import calendar
    
    year, month = ref_date.year, ref_date.month
    last_day = calendar.monthrange(year, month)[1]
    d = _date(year, month, last_day)
    while d.weekday() != 1:
        d = d.replace(day=d.day - 1)
    while d.weekday() >= 5 or d in nse_holidays:
        d -= timedelta(days=1)
    expiry = d
    
    if expiry < ref_date:
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        last_day = calendar.monthrange(year, month)[1]
        d = _date(year, month, last_day)
        while d.weekday() != 1:
            d = d.replace(day=d.day - 1)
        while d.weekday() >= 5 or d in nse_holidays:
            d -= timedelta(days=1)
        expiry = d
    
    return expiry.strftime("%Y-%m-%d")


def get_futures_expiry(symbol: str, ref_date: "date | None" = None) -> "str | None":
    """
    Return the active futures/options contract expiry date (YYYY-MM-DD) for *symbol*.
    
    For NSE indices:
      - NIFTY: Weekly TUESDAY
      - SENSEX: Weekly THURSDAY (BSE)
      - BANKNIFTY, FINNIFTY, MIDCPNIFTY: Monthly last TUESDAY
     
    For MCX commodities:
      - Monthly expiry per official schedule
     
    If `ref_date` is None, today (IST) is used.
    If the computed expiry for the current period has already passed, the next
    period's expiry is returned.
     
    Returns None for symbols with no known contract.
    """
    import calendar
    from datetime import date as _date, timedelta
    try:
        from config.holidays import MCX_FULL_HOLIDAYS_2026, NSE_HOLIDAYS_2026
        mcx_holidays: set[_date] = MCX_FULL_HOLIDAYS_2026
        nse_holidays: set[_date] = NSE_HOLIDAYS_2026
    except Exception:
        mcx_holidays = set()
        nse_holidays = set()

    if ref_date is None:
        from datetime import datetime, timezone, timedelta as _td
        IST = timezone(_td(hours=5, minutes=30))
        ref_date = datetime.now(IST).date()

    base = _base_symbol(symbol)
    class_key = get_symbol_class(symbol)

    if class_key == "MCX_COMMODITY":
        year, month = ref_date.year, ref_date.month

        def _compute(y: int, m: int) -> "_date":
            # 1. Try exact hardcoded 2026 schedule first
            if y == 2026 and base in _MCX_2026_EXPIRIES and m in _MCX_2026_EXPIRIES[base]:
                dt_tuple = _MCX_2026_EXPIRIES[base][m]
                return _date(*dt_tuple)

            # 2. Fallbacks for other years
            if base == "NATURALGAS":
                # 4 business days before the first calendar day of the next month
                if m == 12:
                    next_month_first = _date(y + 1, 1, 1)
                else:
                    next_month_first = _date(y, m + 1, 1)
                curr = next_month_first - timedelta(days=1)
                business_days_found = 0
                while business_days_found < 4:
                    if curr.weekday() < 5 and curr not in mcx_holidays:
                        business_days_found += 1
                        if business_days_found == 4:
                            return curr
                    curr -= timedelta(days=1)
                return next_month_first - timedelta(days=4)
            elif base == "CRUDEOIL":
                # 19th, adjusted backward to previous working day
                raw = _date(y, m, 19)
                return _prev_working_day(raw, mcx_holidays)
            else:  # GOLD, SILVER
                # 5th, adjusted backward to previous working day
                raw = _date(y, m, 5)
                return _prev_working_day(raw, mcx_holidays)

        expiry = _compute(year, month)
        if expiry < ref_date:
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
            expiry = _compute(year, month)
        return expiry.strftime("%Y-%m-%d")

    elif class_key in ("NSE_INDEX", "BSE_INDEX"):
        if base == "NIFTY":
            # Weekly Tuesday (weekday 1)
            return _get_nse_weekly_expiry(ref_date, base)
        elif base == "SENSEX":
            # Weekly Thursday for BSE SENSEX (BSE follows its own schedule)
            return _get_bse_weekly_expiry(ref_date, base)
        elif base in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            # Monthly last Tuesday
            return _get_nse_monthly_expiry(ref_date, base)
        else:
            # Fallback: monthly last Tuesday
            return _get_nse_monthly_expiry(ref_date, base)

    return None
