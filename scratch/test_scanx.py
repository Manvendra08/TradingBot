import requests
import logging

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("test_scanx")

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

# Example payload with Seg=5 (MCX/Commodity), Sid=504265 (NATURALGAS JUN FUT), Exp=0 (or valid Julian)
# Let's try sending a simple post to see if we get a response or a timeout.
payload = {"Data": {"Seg": 5, "Sid": 504265, "Exp": 0}}

try:
    log.info(f"Connecting to {url}...")
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    log.info(f"Status Code: {resp.status_code}")
    log.info(f"Response: {resp.text[:500]}")
except Exception as e:
    log.exception("Failed to connect to ScanX API")
