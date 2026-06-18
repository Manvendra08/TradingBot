import requests
import logging

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("test_scanx_oc")

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

# Let's request Exp = 1466793000
payload = {"Data": {"Seg": 5, "Sid": 504265, "Exp": 1466793000}}

try:
    log.info(f"Connecting to {url} with Exp=1466793000...")
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    log.info(f"Status Code: {resp.status_code}")
    data = resp.json().get("data", {})
    oc = data.get("oc", {})
    log.info(f"Option Chain size: {len(oc)}")
    if oc:
        first_strike = list(oc.keys())[0]
        log.info(f"Sample strike {first_strike}: {oc[first_strike]}")
except Exception as e:
    log.exception("Failed to connect to ScanX API")
