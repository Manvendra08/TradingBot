"""
Fast SSL diagnostic - bypass the raw socket test, just test kite API directly.
"""
import sys
sys.path.insert(0, '.')
import logging
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')

import ssl
import time
import socket

HOST = "api.kite.trade"
PORT = 443

print(f"=== Raw TCP+SSL test to {HOST}:{PORT} ===")
for i in range(3):
    t0 = time.time()
    try:
        raw = socket.create_connection((HOST, PORT), timeout=10)
        ctx = ssl.create_default_context()
        ctx.options |= getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
        tls = ctx.wrap_socket(raw, server_hostname=HOST)
        tls.sendall(b"GET /api HTTP/1.1\r\nHost: api.kite.trade\r\nConnection: close\r\n\r\n")
        data = b""
        while True:
            chunk = tls.recv(4096)
            if not chunk:
                break
            data += chunk
        status = data.split(b"\r\n")[0].decode()
        print(f"  [{i+1}] OK {status!r} in {time.time()-t0:.2f}s")
        tls.close()
    except Exception as e:
        print(f"  [{i+1}] FAIL {type(e).__name__}: {e} in {time.time()-t0:.2f}s")
    time.sleep(0.5)
