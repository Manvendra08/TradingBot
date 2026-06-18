import requests
import logging
import sys

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("test_dhan")

url = "https://dhan.co/commodity/natural-gas-option-chain/"
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

try:
    log.info(f"Connecting to {url}...")
    resp = requests.get(url, headers=headers, timeout=15)
    log.info(f"Status Code: {resp.status_code}")
    log.info(f"Content Length: {len(resp.text)}")
    log.info("Success!")
except Exception as e:
    log.exception("Failed to connect")
    sys.exit(1)
