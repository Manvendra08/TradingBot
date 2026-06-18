import requests
import urllib.request
import logging
import sys

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_dhan_variants")

url = "https://dhan.co/commodity/natural-gas-option-chain/"

# Test 1: urllib.request
try:
    log.info("Test 1: urllib.request...")
    req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        html = response.read()
        log.info(f"urllib success! Length: {len(html)}")
except Exception as e:
    log.warning(f"urllib failed: {e}")

# Test 2: requests with absolutely NO custom headers
try:
    log.info("Test 2: requests with default headers...")
    resp = requests.get(url, timeout=10)
    log.info(f"Default requests success! Status: {resp.status_code}, Length: {len(resp.text)}")
except Exception as e:
    log.warning(f"Default requests failed: {e}")

# Test 3: requests with Curl-like User-Agent
try:
    log.info("Test 3: requests with curl User-Agent...")
    resp = requests.get(url, headers={"User-Agent": "curl/8.4.0"}, timeout=10)
    log.info(f"Curl UA requests success! Status: {resp.status_code}, Length: {len(resp.text)}")
except Exception as e:
    log.warning(f"Curl UA requests failed: {e}")

# Test 4: requests with session and custom headers
try:
    log.info("Test 4: requests session with custom headers...")
    session = requests.Session()
    resp = session.get(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "*/*",
    }, timeout=10)
    log.info(f"Session requests success! Status: {resp.status_code}, Length: {len(resp.text)}")
except Exception as e:
    log.warning(f"Session requests failed: {e}")

# Test 5: requests head request
try:
    log.info("Test 5: requests HEAD method...")
    resp = requests.head(url, timeout=10)
    log.info(f"HEAD success! Status: {resp.status_code}")
except Exception as e:
    log.warning(f"HEAD failed: {e}")
