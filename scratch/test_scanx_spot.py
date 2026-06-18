import requests
import json
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_scanx_spot")

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

# Payload with Seg=5 (MCX), Sid=401 (NATURALGAS Spot), Exp=0
payload = {"Data": {"Seg": 5, "Sid": 401, "Exp": 0}}

try:
    log.info("Test 1: Querying Sid=401 (spot) with Exp=0...")
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    data = resp.json().get("data", {})
    log.info(f"Keys: {list(data.keys())}")
    explst = data.get("explst", [])
    log.info(f"Expiry list for Spot 401: {explst}")
    
    if explst:
        # Query with first expiry in explst
        expj = explst[0]
        payload_oc = {"Data": {"Seg": 5, "Sid": 401, "Exp": int(expj)}}
        log.info(f"Querying Sid=401 with Exp={expj}...")
        resp_oc = requests.post(url, headers=headers, json=payload_oc, timeout=10)
        data_oc = resp_oc.json().get("data", {})
        oc = data_oc.get("oc", {})
        log.info(f"Option Chain size for Spot 401, Exp {expj}: {len(oc)}")
        if oc:
            first_strike = list(oc.keys())[0]
            log.info(f"Sample strike: {oc[first_strike]}")
            
except Exception as e:
    log.exception("Error during Spot 401 query")
