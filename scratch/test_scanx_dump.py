import requests
import json
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_scanx_dump")

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

# Payload with Seg=5 (MCX), Sid=504265, Exp=0
payload = {"Data": {"Seg": 5, "Sid": 504265, "Exp": 0}}

try:
    log.info(f"Connecting to {url}...")
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    log.info(f"Status Code: {resp.status_code}")
    data = resp.json().get("data", {})
    
    # Print keys and structure of 'data'
    log.info(f"Data keys: {list(data.keys())}")
    
    # Dump formatted JSON to file for full inspection
    with open("scratch/scanx_dump_exp0.json", "w") as f:
        json.dump(data, f, indent=2)
    log.info("Dumped scanx_dump_exp0.json successfully.")
    
    # Print the futures list details
    fl = data.get("fl", {})
    log.info(f"Number of future contracts: {len(fl)}")
    for k, v in fl.items():
        log.info(f"Future contract expiry key {k}: name={v.get('sym')}, sid={v.get('sid')}, seg={v.get('seg')}, underlying_sid={v.get('poi') or v.get('u_id') or v.get('sltp')}")
        
except Exception as e:
    log.exception("Error during ScanX dump")
