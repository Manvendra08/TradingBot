"""
Deep SSL/TLS diagnostic for api.kite.trade
Runs raw socket + ssl handshake to identify the actual EOF source.
"""
import sys
import socket
import ssl
import time
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

HOST = "api.kite.trade"
PORT = 443

def raw_ssl_test(label: str, ctx: ssl.SSLContext, num_requests: int = 3):
    """Open SSL connection, send HTTP/1.1 requests, measure EOF behavior."""
    print(f"\n=== {label} ===")
    for i in range(num_requests):
        t0 = time.time()
        try:
            raw_sock = socket.create_connection((HOST, PORT), timeout=15)
            ssl_sock = ctx.wrap_socket(raw_sock, server_hostname=HOST)
            # Send HTTP/1.1 GET with Connection: close
            request = (
                f"GET /user/margins HTTP/1.1\r\n"
                f"Host: {HOST}\r\n"
                f"X-Kite-Version: 3\r\n"
                f"User-Agent: python-kiteconnect\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )
            ssl_sock.sendall(request.encode())
            response = b""
            while True:
                chunk = ssl_sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            status_line = response.split(b"\r\n")[0].decode(errors="replace")
            elapsed = time.time() - t0
            print(f"  [{i+1}] SUCCESS {status_line!r} in {elapsed:.2f}s")
            ssl_sock.close()
        except ssl.SSLError as e:
            print(f"  [{i+1}] SSL ERROR: {e}")
        except Exception as e:
            print(f"  [{i+1}] ERROR: {type(e).__name__}: {e}")

# Test 1: Default SSL context (what happens without our adapter)
ctx_default = ssl.create_default_context()
raw_ssl_test("Default SSL context (no OP_IGNORE)", ctx_default, 3)

# Test 2: With OP_IGNORE_UNEXPECTED_EOF
ctx_ignore = ssl.create_default_context()
if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
    ctx_ignore.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
raw_ssl_test("With OP_IGNORE_UNEXPECTED_EOF", ctx_ignore, 3)

# Test 3: TLS 1.2 min + OP_IGNORE
ctx_tls12 = ssl.create_default_context()
ctx_tls12.minimum_version = ssl.TLSVersion.TLSv1_2
if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
    ctx_tls12.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
raw_ssl_test("TLS 1.2 min + OP_IGNORE_UNEXPECTED_EOF", ctx_tls12, 3)

# Also test via requests to see the full adapter flow
print("\n=== requests via ResilientTLSAdapter (real kite calls) ===")
import sys
sys.path.insert(0, '.')
from src.models.schema import get_broker_config
from src.utils.tls_adapter import mount_resilient_tls
from kiteconnect import KiteConnect

config = get_broker_config()
kite = KiteConnect(api_key=config["api_key"])
kite.set_access_token(config["access_token"])
mount_resilient_tls(kite.reqsession)

for i in range(5):
    try:
        t0 = time.time()
        m = kite.margins()
        elapsed = time.time() - t0
        print(f"  [{i+1}] SUCCESS margin={m.get('equity',{}).get('net','?')} in {elapsed:.2f}s")
    except Exception as e:
        print(f"  [{i+1}] FAIL: {e}")
    time.sleep(0.2)
