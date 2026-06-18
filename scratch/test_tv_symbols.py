import os
import logging
from dotenv import load_dotenv
load_dotenv()

from config.settings import TV_SESSIONID
from tvDatafeed import TvDatafeed, Interval

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

def test_symbol(tv, symbol, exchange, fut_contract=None):
    print(f"\n--- Testing symbol {exchange}:{symbol} (fut={fut_contract}) ---")
    try:
        df = tv.get_hist(symbol, exchange, interval=Interval.in_daily, n_bars=5, fut_contract=fut_contract)
        if df is not None and not df.empty:
            print(f"[SUCCESS] Got {len(df)} bars for {exchange}:{symbol}")
            print(df.head(2))
        else:
            print(f"[FAILED] Returned empty/None for {exchange}:{symbol}")
    except Exception as e:
        print(f"[ERROR] Failed for {exchange}:{symbol}: {e}")

def main():
    if not TV_SESSIONID:
        print("TV_SESSIONID is not set in env!")
        return
        
    print(f"Initializing with session ID: {TV_SESSIONID[:8]}...")
    tv = TvDatafeed(sessionid=TV_SESSIONID)
    tv.ws_debug = False  # Turn off raw messages to see cleanly
    
    # 1. Test NASDAQ:AAPL
    test_symbol(tv, "AAPL", "NASDAQ")
    
    # 2. Test NSE:NIFTY
    test_symbol(tv, "NIFTY", "NSE")
    
    # 3. Test NSE:RELIANCE
    test_symbol(tv, "RELIANCE", "NSE")
    
    # 4. Test MCX:NATURALGAS1!
    test_symbol(tv, "NATURALGAS", "MCX", fut_contract=1)
    
    # 5. Test MCX:CRUDEOIL1!
    test_symbol(tv, "CRUDEOIL", "MCX", fut_contract=1)

if __name__ == "__main__":
    main()
