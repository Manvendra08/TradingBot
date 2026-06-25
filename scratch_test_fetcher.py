"""Test the improved dhan_sensex_fetcher with Python 3.12."""

import sys

sys.path.insert(0, ".")
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from src.fetchers.dhan_sensex_fetcher import DhanSensexFetcher

fetcher = DhanSensexFetcher()
result = fetcher.fetch_option_chain("SENSEX")

if result:
    print(f"\n✅ SUCCESS")
    print(f"  Symbol: {result['symbol']}")
    print(f"  Underlying: {result['underlying_price']}")
    print(f"  Expiry: {result['expiry']}")
    print(f"  Strikes: {len(result['strikes'])}")
    print(f"  Unique strikes: {len(set(s['strike'] for s in result['strikes']))}")
    print(f"  Source: {result['source']}")

    # Show a few samples
    unique_strikes = sorted(set(s["strike"] for s in result["strikes"]))
    print(f"\n  Strike range: {unique_strikes[0]:.2f} - {unique_strikes[-1]:.2f}")

    # Sample ATM region
    atm = min(unique_strikes, key=lambda x: abs(x - result["underlying_price"]))
    print(f"  ATM strike: {atm:.2f}")
    for s in result["strikes"]:
        if s["strike"] == atm:
            print(
                f"    {s['option_type']}: LTP={s['ltp']}, OI={s['oi']}, Vol={s['volume']}"
            )
else:
    print("\n❌ FAILED - No result returned")
