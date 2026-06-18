import requests
import logging
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_crude_options")

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

def check_offset(days_before):
    fut_exp = 1466188200 # CRUDEOIL JUN FUT
    seconds_per_day = 86400
    test_exp = fut_exp - (days_before * seconds_per_day)
    payload = {"Data": {"Seg": 5, "Sid": 499095, "Exp": test_exp}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15.0)
        data = resp.json().get("data", {})
        oc = data.get("oc", {})
        if oc:
            log.info(f"SUCCESS CRUDEOIL! Offset={days_before} days, Exp={test_exp}, strikes count={len(oc)}")
            return (days_before, test_exp, len(oc))
        else:
            log.info(f"Offset={days_before} days -> 0 strikes")
    except Exception as e:
        log.warning(f"Offset={days_before} days failed/timeout: {e}")
    return None

with ThreadPoolExecutor(max_workers=10) as executor:
    executor.map(check_offset, range(0, 10))
