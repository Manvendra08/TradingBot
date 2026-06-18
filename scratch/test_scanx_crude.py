import requests
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_scanx_crude")

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

# Payload with Seg=5 (MCX), Sid=499095 (CRUDEOIL FUT), Exp=0
payload = {"Data": {"Seg": 5, "Sid": 499095, "Exp": 0}}

try:
    log.info("Querying CRUDEOIL Exp=0...")
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    data = resp.json().get("data", {})
    fl = data.get("fl", {})
    
    log.info(f"Crude Oil Futures count: {len(fl)}")
    expjs = sorted([int(k) for k in fl.keys()])
    if expjs:
        fut_exp = expjs[0]
        log.info(f"Nearest Crude Oil Future expiry: {fut_exp} ({fl[str(fut_exp)].get('sym')})")
        
        # Scan daily steps up to 15 days before the future expiry
        seconds_per_day = 86400
        for days_before in range(0, 15):
            test_exp = fut_exp - (days_before * seconds_per_day)
            payload_oc = {"Data": {"Seg": 5, "Sid": 499095, "Exp": test_exp}}
            resp_oc = requests.post(url, headers=headers, json=payload_oc, timeout=15)
            oc = resp_oc.json().get("data", {}).get("oc", {})
            if oc:
                log.info(f"SUCCESS CRUDEOIL! days_before={days_before}, exp={test_exp}, strikes count={len(oc)}")
                break
            else:
                log.info(f"days_before={days_before}, exp={test_exp} -> 0 strikes")
    else:
        log.warning("No Crude Oil futures found.")
        
except Exception as e:
    log.exception("Error during Crude Oil query")
