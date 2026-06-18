import requests
import json
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_scanx_nifty")

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

# Payload with Seg=1 (NSE), Sid=13 (NIFTY), Exp=0
payload = {"Data": {"Seg": 1, "Sid": 13, "Exp": 0}}

try:
    log.info("Querying NIFTY...")
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    data = resp.json().get("data", {})
    
    log.info(f"Keys: {list(data.keys())}")
    log.info(f"explst: {data.get('explst')}")
    log.info(f"fl size: {len(data.get('fl', {}))}")
    
except Exception as e:
    log.exception("Error during NIFTY query")
