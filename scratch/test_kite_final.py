"""
Definitive test: Are SSL EOF retries succeeding or failing after all 5 attempts?
"""
import sys
sys.path.insert(0, '.')
import logging

# Set WARNING to see our adapter warnings
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')

from kiteconnect import KiteConnect
from src.utils.tls_adapter import mount_resilient_tls
from src.models.schema import get_broker_config
import time

config = get_broker_config()
kite = KiteConnect(api_key=config['api_key'])
kite.set_access_token(config['access_token'])
mount_resilient_tls(kite.reqsession)

print("BEFORE mount - Connection:", kite.reqsession.headers.get('Connection'))

print("\n=== Test: Single margins call (watch for retries and final result) ===")
t0 = time.time()
try:
    m = kite.margins()
    net = (m.get("equity") or {}).get("net", 0)
    print(f"FINAL RESULT: SUCCESS net={net:.0f} in {time.time()-t0:.2f}s")
except Exception as e:
    print(f"FINAL RESULT: FAIL after {time.time()-t0:.2f}s: {type(e).__name__}: {e}")

print("\n=== Test: positions call ===")
t0 = time.time()
try:
    pos = kite.positions()
    net_pos = pos.get("net", [])
    day_pos = pos.get("day", [])
    print(f"FINAL RESULT: SUCCESS {len(net_pos)} net / {len(day_pos)} day positions in {time.time()-t0:.2f}s")
    for p in net_pos:
        print(f"  {p.get('tradingsymbol')} qty={p.get('quantity')} avg={p.get('average_price')}")
except Exception as e:
    print(f"FINAL RESULT: FAIL after {time.time()-t0:.2f}s: {type(e).__name__}: {e}")

print("\n=== Test: orders call ===")
t0 = time.time()
try:
    orders = kite.orders()
    print(f"FINAL RESULT: SUCCESS {len(orders or [])} orders in {time.time()-t0:.2f}s")
except Exception as e:
    print(f"FINAL RESULT: FAIL after {time.time()-t0:.2f}s: {type(e).__name__}: {e}")
