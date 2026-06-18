import requests
import time
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_scanx_retry")

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

# Scan NATURALGAS (504265) 5 times
for attempt in range(1, 6):
    log.info(f"--- Attempt {attempt}/5 ---")
    start = time.time()
    try:
        # Get futures list
        fl_payload = {"Data": {"Seg": 5, "Sid": 504265, "Exp": 0}}
        resp = requests.post(url, headers=headers, json=fl_payload, timeout=5.0)
        data = resp.json().get("data", {})
        fl = data.get("fl", {})
        expjs_list = sorted([int(k) for k in fl.keys()])
        
        if expjs_list:
            fut_exp = expjs_list[0]
            # Try offset 2 days
            test_exp = fut_exp - (2 * 86400)
            payload_oc = {"Data": {"Seg": 5, "Sid": 504265, "Exp": test_exp}}
            resp_oc = requests.post(url, headers=headers, json=payload_oc, timeout=5.0)
            oc = resp_oc.json().get("data", {}).get("oc", {})
            log.info(f"Attempt {attempt} SUCCESS: strikes count = {len(oc)}, time = {time.time() - start:.2f}s")
        else:
            log.warning(f"Attempt {attempt} failed: no futures in fl")
    except Exception as e:
        log.warning(f"Attempt {attempt} failed: {e}")
        
    time.sleep(2.0)
