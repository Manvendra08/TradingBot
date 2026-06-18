import requests
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_scanx_commodities")

url = "https://open-web-scanx.dhan.co/scanx/optchainactive"
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://dhan.co",
    "Referer": "https://dhan.co/",
}

def scan_symbol(symbol, sid):
    log.info(f"=== Scanning option expiries for {symbol} (sid={sid}) ===")
    # Get the futures list first
    payload = {"Data": {"Seg": 5, "Sid": sid, "Exp": 0}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        fl = resp.json().get("data", {}).get("fl", {})
    except Exception as e:
        log.error(f"Failed to get futures list for {symbol}: {e}")
        return
    
    expjs = sorted([int(k) for k in fl.keys()])
    if not expjs:
        log.warning(f"No futures found for {symbol}")
        return
        
    fut_exp = expjs[0]
    log.info(f"Nearest future expiry for {symbol}: {fut_exp} ({fl[str(fut_exp)].get('sym')})")
    
    seconds_per_day = 86400
    for days_before in range(0, 10):
        test_exp = fut_exp - (days_before * seconds_per_day)
        payload_oc = {"Data": {"Seg": 5, "Sid": sid, "Exp": test_exp}}
        try:
            # Low timeout to skip hung requests quickly
            resp_oc = requests.post(url, headers=headers, json=payload_oc, timeout=2.0)
            data_oc = resp_oc.json().get("data", {})
            oc = data_oc.get("oc", {})
            if oc:
                log.info(f"FOUND OPTIONS FOR {symbol}! Offset={days_before} days, Exp={test_exp}, strikes count={len(oc)}")
                return test_exp
            else:
                log.debug(f"{symbol} offset {days_before} -> 0 strikes")
        except requests.exceptions.ReadTimeout:
            log.debug(f"{symbol} offset {days_before} timed out")
        except Exception as e:
            log.debug(f"{symbol} offset {days_before} error: {e}")
            
    log.warning(f"No options found for {symbol} within 10 days before future expiry")
    return None

scan_symbol("NATURALGAS", 504265)
scan_symbol("CRUDEOIL", 499095)
