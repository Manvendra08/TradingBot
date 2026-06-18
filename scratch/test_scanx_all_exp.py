import requests
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_scanx_all_exp")

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

# The futures expiry is 1466793000 (June 24, 2026).
# MCX options typically expire 3-5 days before futures.
# Let's check 1 to 15 days before 1466793000.
fut_exp = 1466793000
seconds_per_day = 86400

for days_before in range(0, 15):
    test_exp = fut_exp - (days_before * seconds_per_day)
    payload = {"Data": {"Seg": 5, "Sid": 504265, "Exp": test_exp}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=5)
        data = resp.json().get("data", {})
        oc = data.get("oc", {})
        if oc:
            log.info(f"SUCCESS! days_before={days_before}, exp={test_exp}, strikes count={len(oc)}")
            break
        else:
            log.info(f"days_before={days_before}, exp={test_exp} -> 0 strikes")
    except Exception as e:
        log.error(f"Failed for exp={test_exp}: {e}")
