import sys
import os
sys.path.append(os.getcwd())

import logging
logging.basicConfig(level=logging.INFO)

from src.engine.live_trading import get_kite_client
from dashboard_server import _fetch_real_kite_positions, get_live_trades

kite = get_kite_client()
print("KITE CLIENT:", kite)
if kite:
    try:
        raw_pos = kite.positions()
        print("RAW KITE NET POSITIONS:")
        for p in raw_pos.get("net", []):
            if p.get("quantity") != 0:
                print({k: p[k] for k in ["tradingsymbol", "exchange", "quantity", "average_price", "last_price", "pnl"]})
    except Exception as e:
        print("Failed to get raw positions:", e)

    parsed = _fetch_real_kite_positions(kite)
    print("\nPARSED REAL KITE POSITIONS:")
    for p in parsed:
        print(p)

    print("\nGET LIVE TRADES (status=OPEN):")
    lt = get_live_trades(symbol="", status="OPEN")
    for t in lt:
        print(t)
