import requests
import logging
import time

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_crude_seq")

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

fut_exp = 1466188200 # CRUDEOIL JUN FUT
seconds_per_day = 86400
test_exp = fut_exp - (2 * seconds_per_day) # 2 days before

payload = {"Data": {"Seg": 5, "Sid": 499095, "Exp": test_exp}}

try:
    log.info(f"Querying CRUDEOIL option chain for Exp={test_exp} with 30s timeout...")
    start_time = time.time()
    resp = requests.post(url, headers=headers, json=payload, timeout=30.0)
    elapsed = time.time() - start_time
    data = resp.json().get("data", {})
    oc = data.get("oc", {})
    log.info(f"Response received in {elapsed:.2f} seconds. Option Chain size: {len(oc)}")
    if oc:
        first_strike = list(oc.keys())[0]
        log.info(f"Sample strike: {oc[first_strike]}")
except Exception as e:
    log.exception("Query failed")
