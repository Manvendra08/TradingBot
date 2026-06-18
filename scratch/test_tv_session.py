import os
import logging
from dotenv import load_dotenv
load_dotenv()

from config.settings import TV_SESSIONID, TV_USERNAME
from tvDatafeed import TvDatafeed, Interval

# Configure logs
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def main():
    print("Environments loaded.")
    
    print(f"TV_SESSIONID loaded: {TV_SESSIONID is not None and len(TV_SESSIONID) > 0}")
    if TV_SESSIONID:
        print(f"Session ID first 8 chars: {TV_SESSIONID[:8]}...")
    print(f"TV_USERNAME: {TV_USERNAME}")
    
    if not TV_SESSIONID:
        print("\n[WARNING] TV_SESSIONID is not set in .env!")
        print("To fetch MCX commodities (e.g. NATURALGAS), please follow the instructions in the implementation plan or .env.example to set TV_SESSIONID.")
        print("Skipping session-based test.\n")
        return
        
    print("\nInitializing TvDatafeed with sessionid...")
    try:
        tv = TvDatafeed(sessionid=TV_SESSIONID)
        tv.ws_debug = True
        print("Initialization successful!")
        
        print("\nFetching NATURALGAS MCX data with fut_contract=1...")
        df = tv.get_hist("NATURALGAS", "MCX", interval=Interval.in_3_hour, n_bars=5, fut_contract=1)
        if df is not None and not df.empty:
            print("\n[SUCCESS] Successfully fetched MCX NATURALGAS data using sessionid!")
            print(df)
        else:
            print("\n[FAILURE] tv.get_hist returned None or empty DataFrame!")
    except Exception as e:
        print(f"\n[ERROR] Failed to fetch MCX data with session ID: {e}")

if __name__ == "__main__":
    main()
