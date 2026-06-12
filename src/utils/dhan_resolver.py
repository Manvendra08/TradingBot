"""
Dynamic Dhan monthly contract scrip/security ID resolver.
Fetches the current active contract ID from Dhan's public web pages for MCX symbols,
caching results to prevent repeated network overhead.
"""
import re
import urllib.request
import json
import logging
from config.settings import DHAN_SECURITY_IDS

log = logging.getLogger(__name__)

_SYMBOL_SLUGS = {
    "NATURALGAS": "natural-gas",
    "CRUDEOIL": "crude-oil",
    "GOLD": "gold",
    "SILVER": "silver",
}

_CACHE = {}


def get_dhan_security_id(symbol: str) -> int | None:
    """
    Dynamically resolve the current front-month Dhan security ID for commodities,
    falling back to config.settings.DHAN_SECURITY_IDS if resolution fails.
    """
    symbol = symbol.upper().split()[0]
    
    # Non-commodities (indices) are static and never change
    if symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        return DHAN_SECURITY_IDS.get(symbol)
        
    if symbol in _CACHE:
        return _CACHE[symbol]
        
    slug = _SYMBOL_SLUGS.get(symbol)
    if not slug:
        return DHAN_SECURITY_IDS.get(symbol)
        
    url = f"https://dhan.co/commodity/{slug}-option-chain/"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as res:
            html = res.read().decode("utf-8")
            
        # Try NextData JSON extraction
        match_next = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if match_next:
            data = json.loads(match_next.group(1))
            props = data.get("props", {}).get("pageProps", {})
            scrip_info = props.get("scripData", {}) or props.get("optionChainData", {}).get("scripData", {})
            if scrip_info and scrip_info.get("scripId"):
                sec_id = int(scrip_info["scripId"])
                _CACHE[symbol] = sec_id
                log.info("[resolver] Dynamically resolved Dhan security ID for %s: %d", symbol, sec_id)
                return sec_id
                
        # Regex fallback
        match_sid = re.search(r'"scripId"\s*:\s*(\d+)', html)
        if match_sid:
            sec_id = int(match_sid.group(1))
            _CACHE[symbol] = sec_id
            log.info("[resolver] Dynamically resolved Dhan security ID (regex fallback) for %s: %d", symbol, sec_id)
            return sec_id
            
    except Exception as e:
        log.warning("[resolver] Dynamic Dhan security ID resolution failed for %s: %s. Using fallback.", symbol, e)
        
    # Return hardcoded fallback
    return DHAN_SECURITY_IDS.get(symbol)
