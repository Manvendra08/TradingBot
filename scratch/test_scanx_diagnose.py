import requests
import logging

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("test_scanx_diagnose")

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
    log.info(f"=== Scanning {symbol} (sid={sid}) ===")
    payload = {"Data": {"Seg": 5, "Sid": sid, "Exp": 0}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        fl = resp.json().get("data", {}).get("fl", {})
    except Exception as e:
        log.error(f"Failed to get futures list for {symbol}: {e}")
        return
    
    expjs = sorted([int(k) for k in fl.keys()])
    if not expjs:
        return
        
    fut_exp = expjs[0]
    seconds_per_day = 86400
    for days_before in range(0, 10):
        test_exp = fut_exp - (days_before * seconds_per_day)
        payload_oc = {"Data": {"Seg": 5, "Sid": sid, "Exp": test_exp}}
        try:
            resp_oc = requests.post(url, headers=headers, json=payload_oc, timeout=4.0)
            data_oc = resp_oc.json().get("data", {})
            oc = data_oc.get("oc", {})
            if oc:
                log.info(f"--> FOUND! {symbol} offset {days_before} days, Exp {test_exp}, strikes {len(oc)}")
            else:
                log.info(f"--> {symbol} offset {days_before} days -> 0 strikes")
        except requests.exceptions.ReadTimeout:
            log.info(f"--> {symbol} offset {days_before} days -> TIMEOUT")
        except Exception as e:
            log.info(f"--> {symbol} offset {days_before} days -> ERROR: {e}")

scan_symbol("NATURALGAS", 504265)
scan_symbol("CRUDEOIL", 499095)
