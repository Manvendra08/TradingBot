import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.engine.live_trading import get_kite_client

kite = get_kite_client()
if not kite:
    print("Failed to get Kite client")
    sys.exit(1)

try:
    print("Fetching margins...")
    margins = kite.margins()
    print("Margins:")
    import pprint
    pprint.pprint(margins)
    
    print("\nFetching positions...")
    positions = kite.positions()
    print("Net positions:")
    pprint.pprint(positions.get("net", []))
except Exception as e:
    import traceback
    traceback.print_exc()
