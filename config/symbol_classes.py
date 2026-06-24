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


# ---------------------------------------------------------------------------
# Futures expiry calculation (separate from option chain expiry)
# ---------------------------------------------------------------------------
def _prev_working_day(d: "date", holidays: "set[date] | None" = None) -> "date":
    """Return d itself if it is a working day, else step back until one is found."""
    from datetime import timedelta
    holidays = holidays or set()
    while d.weekday() >= 5 or d in holidays:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def _last_weekday_of_month(year: int, month: int, weekday: int) -> "date":
    """Return the last occurrence of `weekday` (0=Mon … 6=Sun) in the given month."""
    import calendar
    from datetime import date as _date
    last_day = calendar.monthrange(year, month)[1]
    d = _date(year, month, last_day)
    while d.weekday() != weekday:
        d = d.replace(day=d.day - 1)
    return d


def get_futures_expiry(symbol: str, ref_date: "date | None" = None) -> "str | None":
    """
    Return the active futures contract expiry date (YYYY-MM-DD) for *symbol*.

    MCX expiry rules (approximate — MCX may shift for holidays):
      NATURALGAS : last Wednesday of the month
      CRUDEOIL   : 15th of the month (prev working day if weekend/holiday)
      GOLD       : 5th of the month  (prev working day if weekend/holiday)
      SILVER     : 5th of the month  (prev working day if weekend/holiday)

    NSE index futures: last Thursday of the month.

    If `ref_date` is None, today (IST) is used.
    If the computed expiry for the current month has already passed, the next
    month's expiry is returned.

    Returns None for symbols with no known futures contract (e.g., pure equity options).
    """
    import calendar
    from datetime import date as _date, timedelta
    try:
        from config.holidays import MCX_FULL_HOLIDAYS_2026, NSE_HOLIDAYS_2026
        mcx_holidays: set[_date] = MCX_FULL_HOLIDAYS_2026   # type: ignore[assignment]
        nse_holidays: set[_date] = NSE_HOLIDAYS_2026          # type: ignore[assignment]
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
            if base == "NATURALGAS":
                # Last Wednesday (weekday 2)
                raw = _last_weekday_of_month(y, m, 2)
                return _prev_working_day(raw, mcx_holidays)
            elif base == "CRUDEOIL":
                # 15th, adjusted backward
                raw = _date(y, m, 15)
                return _prev_working_day(raw, mcx_holidays)
            else:  # GOLD, SILVER
                # 5th, adjusted backward
                raw = _date(y, m, 5)
                return _prev_working_day(raw, mcx_holidays)

        expiry = _compute(year, month)
        if expiry < ref_date:
            # Current month's expiry has passed → next month
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
            expiry = _compute(year, month)
        return expiry.strftime("%Y-%m-%d")

    elif class_key in ("NSE_INDEX", "BSE_INDEX"):
        year, month = ref_date.year, ref_date.month

        def _compute_nse(y: int, m: int) -> "_date":
            # Last Thursday (weekday 3)
            raw = _last_weekday_of_month(y, m, 3)
            return _prev_working_day(raw, nse_holidays)

        expiry = _compute_nse(year, month)
        if expiry < ref_date:
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
            expiry = _compute_nse(year, month)
        return expiry.strftime("%Y-%m-%d")

    return None
