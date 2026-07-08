"""
Parity Engine for Natural Gas.
Calculates fair value of MCX Natural Gas based on NYMEX NG=F and USDINR.
"""

from dataclasses import dataclass
from datetime import datetime
import time
import logging
import yfinance as yf

try:
    from src.models.schema import stamp_health
except ImportError:
    # For standalone testing
    def stamp_health(key: str, status: str, detail: str = "") -> None:
        pass

log = logging.getLogger(__name__)

@dataclass
class ParityState:
    nymex_last: float          # NG=F last (USD/mmBtu)
    usdinr: float              # USDINR rate
    fair_value: float          # nymex_last * usdinr
    mcx_last: float            # Shoonya MCX real-time tick
    dev_pct: float             # (mcx_last - fair_value) / fair_value * 100
    nymex_age_sec: int         
    fx_age_sec: int
    mcx_age_sec: int
    mcx_src: str               
    fx_src: str                
    nymex_src: str
    valid: bool                # False if any leg stale > PARITY_MAX_STALENESS_SEC


_YF_CACHE = {}
_YF_CACHE_TTL_SEC = 60

def _get_yf_quote(ticker: str) -> tuple[float, int]:
    """Returns (last_price, age_sec). Uses 60s cache."""
    now = time.time()
    if ticker in _YF_CACHE:
        cached_price, cached_time = _YF_CACHE[ticker]
        age = int(now - cached_time)
        if age < _YF_CACHE_TTL_SEC:
            return cached_price, age
            
    try:
        tkr = yf.Ticker(ticker)
        # Fast fetch current price
        info = tkr.fast_info
        price = info.last_price
        _YF_CACHE[ticker] = (price, now)
        return price, 0
    except Exception as e:
        log.warning("Failed to fetch %s from yfinance: %s", ticker, e)
        # return stale cached if available, else 0
        if ticker in _YF_CACHE:
            cached_price, cached_time = _YF_CACHE[ticker]
            return cached_price, int(now - cached_time)
        return 0.0, 99999

def _get_shoonya_usdinr() -> tuple[float, int, str]:
    """
    Attempts to fetch USDINR near-month futures from Shoonya CDS.
    Returns (price, age_sec, source_name).
    On failure, returns (0.0, 99999, "shoonya").
    """
    try:
        from src.fetchers.shoonya_fetcher import get_shoonya_fetcher
        f = get_shoonya_fetcher()
        if not f.login():
            return 0.0, 99999, "shoonya"
            
        search_res = f._search_scrip("CDS", "USDINR")
        if not search_res or search_res.get("stat") != "Ok" or not search_res.get("values"):
            return 0.0, 99999, "shoonya"
            
        # Filter for FUTCUR (currency futures)
        futures = []
        for val in search_res["values"]:
            if val.get("instname") == "FUTCUR" and val.get("symname") == "USDINR":
                exd_str = val.get("exd")
                if exd_str:
                    try:
                        exd_dt = datetime.strptime(exd_str, "%d-%b-%Y").date()
                        futures.append((exd_dt, val.get("token"), val.get("tsym")))
                    except Exception:
                        continue
                        
        if not futures:
            return 0.0, 99999, "shoonya"
            
        # Sort by expiry date ascending to get nearest
        futures.sort(key=lambda x: x[0])
        nearest_expiry, token, tsym = futures[0]
        
        quote = f._get_quotes("CDS", token)
        if not quote or quote.get("stat") != "Ok":
            return 0.0, 99999, "shoonya"
            
        lp_str = quote.get("lp")
        if lp_str:
            price = float(lp_str)
            if price > 0:
                log.info("Fetched USDINR from Shoonya CDS (%s): %.4f", tsym, price)
                return price, 0, "shoonya"
    except Exception as e:
        log.warning("Failed to fetch USDINR from Shoonya CDS: %s", e)
        
    return 0.0, 99999, "shoonya"

def get_parity_state(mcx_last: float, mcx_age_sec: int = 0) -> ParityState:
    """Computes parity state."""
    from config.settings import PARITY_MAX_STALENESS_SEC
    
    # NYMEX
    nymex_last, nymex_age = _get_yf_quote("NG=F")
    
    # USDINR (Shoonya CDS with yfinance fallback)
    usdinr, fx_age, fx_src = _get_shoonya_usdinr()
    if usdinr == 0.0:
        usdinr, fx_age = _get_yf_quote("INR=X")
        fx_src = "yfinance"
    
    if usdinr == 0.0 or nymex_last == 0.0 or mcx_last == 0.0:
        return ParityState(
            nymex_last=nymex_last, usdinr=usdinr, fair_value=0.0,
            mcx_last=mcx_last, dev_pct=0.0,
            nymex_age_sec=nymex_age, fx_age_sec=fx_age, mcx_age_sec=mcx_age_sec,
            mcx_src="shoonya", fx_src=fx_src, nymex_src="yfinance",
            valid=False
        )
        
    fair_value = nymex_last * usdinr
    dev_pct = ((mcx_last - fair_value) / fair_value) * 100.0
    
    valid = (
        nymex_age <= PARITY_MAX_STALENESS_SEC and 
        fx_age <= PARITY_MAX_STALENESS_SEC and 
        mcx_age_sec <= PARITY_MAX_STALENESS_SEC
    )
    
    # Health stamp for ops_agent
    stamp_health("parity_feed", "OK" if valid else "DOWN", 
                 f"nymex_age={nymex_age}s fx_age={fx_age}s mcx_age={mcx_age_sec}s valid={valid}")
    
    return ParityState(
        nymex_last=nymex_last,
        usdinr=usdinr,
        fair_value=fair_value,
        mcx_last=mcx_last,
        dev_pct=dev_pct,
        nymex_age_sec=nymex_age,
        fx_age_sec=fx_age,
        mcx_age_sec=mcx_age_sec,
        mcx_src="shoonya",
        fx_src=fx_src,
        nymex_src="yfinance",
        valid=valid
    )
