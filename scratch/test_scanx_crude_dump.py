import requests
import json
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_scanx_crude_dump")

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

payload = {"Data": {"Seg": 5, "Sid": 499095, "Exp": 0}}

try:
    log.info("Querying Crude Oil Sid=499095 with Exp=0...")
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    data = resp.json().get("data", {})
    
    log.info(f"s_sid: {data.get('s_sid')}")
    log.info(f"u_id: {data.get('u_id')}")
    log.info(f"sinst: {data.get('sinst')}")
    log.info(f"finst: {data.get('finst')}")
    log.info(f"explst: {data.get('explst')}")
    
except Exception as e:
    log.exception("Error in crude dump")
