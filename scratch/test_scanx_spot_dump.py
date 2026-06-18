import requests
import json
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_scanx_spot_dump")

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

payload = {"Data": {"Seg": 5, "Sid": 401, "Exp": 0}}

try:
    log.info("Querying Spot Sid=401...")
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    data = resp.json().get("data", {})
    
    with open("scratch/scanx_spot_dump.json", "w") as f:
        json.dump(data, f, indent=2)
    log.info("Dumped scanx_spot_dump.json successfully.")
    
    fl = data.get("fl", {})
    log.info(f"Number of future contracts: {len(fl)}")
    for k, v in fl.items():
        log.info(f"Future contract expiry key {k}: name={v.get('sym')}, sid={v.get('sid')}")
        
except Exception as e:
    log.exception("Error in spot dump")
