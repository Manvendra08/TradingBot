import logging
import sys

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_dhan_tls")

url = "https://dhan.co/commodity/natural-gas-option-chain/"

# Test 1: httpx (if installed)
try:
    import httpx
    log.info("Test 1: httpx GET...")
    with httpx.Client(http2=True, timeout=10) as client:
        resp = client.get(url)
        log.info(f"httpx HTTP/2 success! Status: {resp.status_code}, Length: {len(resp.text)}, Version: {resp.http_version}")
except ImportError:
    log.warning("httpx is not installed.")
except Exception as e:
    log.warning(f"httpx failed: {e}")

# Test 2: urllib.request with ssl context disabling SNI / renegotiation tweaks
try:
    import urllib.request
    import ssl
    log.info("Test 2: urllib with custom SSL context...")
    ctx = ssl.create_default_context()
    # Let's disable SNI or tweak ciphers to see if it avoids renegotiation
    ctx.set_ciphers('DEFAULT@SECLEVEL=1')
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
        html = response.read()
        log.info(f"urllib SSL tweak success! Length: {len(html)}")
except Exception as e:
    log.warning(f"urllib SSL tweak failed: {e}")
