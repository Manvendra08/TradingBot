import logging
import re
import time
from datetime import datetime

log = logging.getLogger("nsebot.symbol_resolver")

import calendar

# Local instrument cache for the day (TTL-based to avoid repeated SSL failures spamming)
_INSTRUMENT_CACHE = {}
_TSYM_EXPIRY_CACHE = {}
_INSTRUMENT_CACHE_TS = 0.0
_INSTRUMENT_CACHE_TTL_SEC = 6 * 60 * 60  # 6 hours

_REFRESH_IN_PROGRESS = False
_REFRESH_IN_PROGRESS_TS = 0.0

# Rate-limit cache-miss warnings (per key, per TTL window)
_CACHE_MISS_WARNED: dict[tuple, float] = {}
_CACHE_MISS_WARN_TTL_SEC = 15 * 60  # 15 minutes


def _instrument_cache_is_ready() -> bool:
    global _INSTRUMENT_CACHE_TS
    if not _INSTRUMENT_CACHE:
        return False
    if _INSTRUMENT_CACHE_TTL_SEC <= 0:
        return True
    return (time.time() - float(_INSTRUMENT_CACHE_TS)) <= _INSTRUMENT_CACHE_TTL_SEC


def fetch_and_cache_instruments(
    kite_client,
    *,
    timeout_sec: float = 8.0,
    retries: int = 2,
    retry_backoff_sec: float = 0.5,
) -> None:
    """
    Fetch and cache all instruments for NFO and MCX.
    Must be failure-tolerant: on SSL/network issues, do not raise and do not poison the cache.
    """
    global _INSTRUMENT_CACHE, _TSYM_EXPIRY_CACHE, _INSTRUMENT_CACHE_TS
    global _REFRESH_IN_PROGRESS, _REFRESH_IN_PROGRESS_TS

    # Guard against concurrent refresh storms
    if _REFRESH_IN_PROGRESS and (time.time() - float(_REFRESH_IN_PROGRESS_TS)) < 60.0:
        return

    _REFRESH_IN_PROGRESS = True
    _REFRESH_IN_PROGRESS_TS = time.time()
    try:
        cache = {}
        tsym_expiry_cache = {}

        last_exc: Exception | None = None

        for attempt in range(retries + 1):
            try:
                # kiteconnect requests use kite.reqsession; we don't have a guaranteed per-call timeout.
                # Still, retries here help reduce transient failures. Any SSL errors will be handled below.
                if attempt > 0:
                    time.sleep(retry_backoff_sec * attempt)

                log.info("Fetching instruments from Kite (attempt %d)...", attempt + 1)

                nfo = kite_client.instruments("NFO")
                mcx = kite_client.instruments("MCX")

                for inst in nfo + mcx:
                    name = inst.get("name")
                    if not name:
                        continue
                    name = str(name).upper()

                    expiry = inst.get("expiry")
                    if expiry:
                        if isinstance(expiry, str):
                            exp_str = expiry
                        else:
                            exp_str = expiry.strftime("%Y-%m-%d")
                    else:
                        exp_str = ""

                    strike = float(inst.get("strike") or 0.0)
                    otype = str(inst.get("instrument_type") or "").upper()  # CE, PE, FUT
                    tsym = inst.get("tradingsymbol")
                    token = inst.get("instrument_token")
                    lot_size = inst.get("lot_size")

                    key = (name, exp_str, strike, otype)
                    cache[key] = {
                        "tradingsymbol": tsym,
                        "instrument_token": token,
                        "lot_size": lot_size,
                    }
                    if tsym:
                        tsym_expiry_cache[str(tsym).upper()] = exp_str

                _INSTRUMENT_CACHE = cache
                _TSYM_EXPIRY_CACHE = tsym_expiry_cache
                _INSTRUMENT_CACHE_TS = time.time()
                log.info(
                    "Successfully cached %d instruments and %d trading symbols.",
                    len(_INSTRUMENT_CACHE),
                    len(_TSYM_EXPIRY_CACHE),
                )
                return
            except Exception as exc:
                last_exc = exc
                continue

        # All attempts failed: keep existing cache as-is (do not clear)
        if not _instrument_cache_is_ready() or not _INSTRUMENT_CACHE:
            # Log only once per TTL window to avoid spamming.
            log.warning("Failed to fetch/cache instruments from Kite API (SSL/network). Keeping existing cache. err=%s", last_exc)
        else:
            log.info("Instrument refresh failed but existing cache is available. err=%s", last_exc)

    finally:
        _REFRESH_IN_PROGRESS = False


def get_expiry_for_tradingsymbol(tsym: str) -> str | None:
    """Resolve expiry YYYY-MM-DD for a given trading symbol, using cache or parsing fallback."""
    if not tsym:
        return None
    tsym = tsym.upper()
    if tsym in _TSYM_EXPIRY_CACHE:
        return _TSYM_EXPIRY_CACHE[tsym]

    # Regex fallback matching
    m_opt = re.match(r"^([A-Z\-]+)(\d{2})([A-Z]{3})(\d+)(CE|PE)$", tsym)
    m_opt_w = re.match(r"^([A-Z\-]+)(\d{2})([0-9OND])(\d{2})(\d+)(CE|PE)$", tsym)
    m_fut = re.match(r"^([A-Z\-]+)(\d{2})([A-Z]{3})(FUT)?$", tsym)
    m_fut_w = re.match(r"^([A-Z\-]+)(\d{2})([0-9OND])(\d{2})(FUT)?$", tsym)
    
    months = {"JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
              "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12"}
              
    m_chars = {"1": "01", "2": "02", "3": "03", "4": "04", "5": "05", "6": "06", "7": "07",
               "8": "08", "9": "09", "O": "10", "N": "11", "D": "12"}

    if m_opt_w:
        yy = m_opt_w.group(2)
        m_char = m_opt_w.group(3)
        dd = m_opt_w.group(4)
        return f"20{yy}-{m_chars.get(m_char, '01')}-{dd}"
    elif m_fut_w:
        yy = m_fut_w.group(2)
        m_char = m_fut_w.group(3)
        dd = m_fut_w.group(4)
        return f"20{yy}-{m_chars.get(m_char, '01')}-{dd}"
    elif m_opt:
        yy = m_opt.group(2)
        mon = m_opt.group(3).upper()
        mm = months.get(mon)
        if mm:
            year = 2000 + int(yy)
            month = int(mm)
            month_days = calendar.monthcalendar(year, month)
            thursdays = []
            for week in month_days:
                day = week[3]
                if day > 0:
                    thursdays.append(day)
            last_thurs = thursdays[-1]
            return f"{year}-{mm}-{last_thurs:02d}"
    elif m_fut:
        yy = m_fut.group(2)
        mon = m_fut.group(3).upper()
        mm = months.get(mon)
        if mm:
            year = 2000 + int(yy)
            month = int(mm)
            month_days = calendar.monthcalendar(year, month)
            thursdays = []
            for week in month_days:
                day = week[3]
                if day > 0:
                    thursdays.append(day)
            last_thurs = thursdays[-1]
            return f"{year}-{mm}-{last_thurs:02d}"
            
    return None


def resolve_instrument(symbol: str, expiry: str, strike: float, option_type: str) -> dict | None:
    """
    Lookup instrument details from cache.
    expiry: YYYY-MM-DD
    """
    symbol = symbol.upper()
    option_type = option_type.upper()
    key = (symbol, expiry, float(strike), option_type)

    res = _INSTRUMENT_CACHE.get(key)
    if res:
        return res

    now = time.time()
    last = _CACHE_MISS_WARNED.get(key) or 0.0
    if (now - float(last)) > _CACHE_MISS_WARN_TTL_SEC:
        log.warning("Instrument not found in cache for %s. Generating fallback tradingsymbol...", key)
        _CACHE_MISS_WARNED[key] = now

    fallback_tsym = generate_fallback_tradingsymbol(symbol, expiry, strike, option_type)
    return {
        "tradingsymbol": fallback_tsym,
        "instrument_token": None,
        "lot_size": None,
    }

def generate_fallback_tradingsymbol(symbol: str, expiry_str: str, strike: float, option_type: str) -> str:
    """
    Generate offline fallback tradingsymbol based on standard NFO and MCX naming rules.
    expiry_str: YYYY-MM-DD
    """
    symbol = symbol.upper()
    option_type = option_type.upper()
    try:
        exp_date = datetime.strptime(expiry_str, "%Y-%m-%d")
    except ValueError:
        return f"{symbol}_{expiry_str}_{strike}_{option_type}"
    
    yy = exp_date.strftime("%y") # 2 digits
    
    if symbol in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"):
        mon_letters = exp_date.strftime("%b").upper() # e.g. JUN
        if option_type == "FUT":
            return f"{symbol}{yy}{mon_letters}FUT"
        else:
            strike_str = str(int(strike))
            return f"{symbol}{yy}{mon_letters}{strike_str}{option_type}"
    
    import calendar
    month_days = calendar.monthcalendar(exp_date.year, exp_date.month)
    thursdays = []
    for week in month_days:
        day = week[3]
        if day > 0:
            thursdays.append(day)
    last_thurs = thursdays[-1]
    
    is_monthly = (exp_date.day == last_thurs)
    
    if option_type == "FUT":
        mon_letters = exp_date.strftime("%b").upper()
        return f"{symbol}{yy}{mon_letters}FUT"
    
    if is_monthly:
        mon_letters = exp_date.strftime("%b").upper()
        strike_str = str(int(strike))
        return f"{symbol}{yy}{mon_letters}{strike_str}{option_type}"
    else:
        m_chars = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D"}
        m_char = m_chars[exp_date.month]
        dd = exp_date.strftime("%d")
        strike_str = str(int(strike))
        return f"{symbol}{yy}{m_char}{dd}{strike_str}{option_type}"
