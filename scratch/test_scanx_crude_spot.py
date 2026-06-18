import requests
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_crude_spot")

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

# Spot ID: 294
# June 16, 2026 (Julian: 1466101800)
# June 15, 2026 (Julian: 1466015400)

for exp in [1466101800, 1466015400]:
    payload = {"Data": {"Seg": 5, "Sid": 294, "Exp": exp}}
    try:
        log.info(f"Querying Sid=294 with Exp={exp}...")
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        data = resp.json().get("data", {})
        oc = data.get("oc", {})
        log.info(f"Exp={exp} -> Option Chain size: {len(oc)}")
        if oc:
            first_strike = list(oc.keys())[0]
            log.info(f"Sample: {oc[first_strike]}")
    except Exception as e:
        log.error(f"Failed for Exp={exp}: {e}")
