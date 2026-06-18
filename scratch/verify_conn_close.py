import sys
sys.path.insert(0, '.')
from kiteconnect import KiteConnect
from src.utils.tls_adapter import mount_resilient_tls
from src.models.schema import get_broker_config
import time, logging
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')

config = get_broker_config()
kite = KiteConnect(api_key=config['api_key'])
kite.set_access_token(config['access_token'])

print("BEFORE mount - Connection header:", kite.reqsession.headers.get('Connection'))
mount_resilient_tls(kite.reqsession)
print("AFTER mount  - Connection header:", kite.reqsession.headers.get('Connection'))

# Test 5 rapid consecutive calls (no sleep) to confirm no EOF on reuse
print("\nTesting 5 rapid consecutive margins calls:")
ok, fail = 0, 0
for i in range(5):
    t0 = time.time()
    try:
        m = kite.margins()
        elapsed = time.time() - t0
        net = (m.get("equity") or {}).get("net", 0)
        print(f"  [{i+1}] OK net={net:.0f} in {elapsed:.2f}s")
        ok += 1
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [{i+1}] FAIL {type(e).__name__}: {e} in {elapsed:.2f}s")
        fail += 1

print(f"\nResult: {ok} success / {fail} fail")

# Also test positions and orders
print("\nTesting positions + orders:")
try:
    pos = kite.positions()
    net_pos = pos.get("net", [])
    print(f"  positions: {len(net_pos)} net positions")
except Exception as e:
    print(f"  positions FAIL: {e}")

try:
    orders = kite.orders()
    print(f"  orders: {len(orders or [])} orders")
except Exception as e:
    print(f"  orders FAIL: {e}")
