import requests
import time

t0 = time.time()
try:
    r = requests.get("http://localhost:8080/api/live_trades?status=OPEN", timeout=60)
    elapsed = time.time() - t0
    print(f"Status: {r.status_code} in {elapsed:.2f}s")
    data = r.json()
    print(f"Count: {len(data)}")
    for p in (data or [])[:10]:
        sym = p.get("symbol") or p.get("tradingsymbol", "")
        side = p.get("side", "")
        qty = p.get("quantity", p.get("lots", 0))
        print(f"  {sym} {side} qty={qty}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
