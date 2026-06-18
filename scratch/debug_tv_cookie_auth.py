import os
import re
import requests
import logging
from dotenv import load_dotenv
load_dotenv()

from config.settings import TV_SESSIONID
from tvDatafeed import TvDatafeed, Interval

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def extract_auth_token(session_id):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.tradingview.com/'
    }
    cookies = {
        'sessionid': session_id
    }
    
    # Try both domains
    urls = ['https://www.tradingview.com/', 'https://in.tradingview.com/']
    
    for url in urls:
        print(f"Requesting {url} with sessionid cookie...")
        try:
            resp = requests.get(url, cookies=cookies, headers=headers, timeout=10)
            print(f"Status Code: {resp.status_code}")
            
            # Let's search the HTML response for auth_token
            # TradingView typically stores it in a window.user or similar script tag:
            # "auth_token":"..."
            match = re.search(r'"auth_token"\s*:\s*"([^"]+)"', resp.text)
            if match:
                token = match.group(1)
                print(f"[SUCCESS] Found auth_token in HTML! First 10 chars: {token[:10]}...")
                return token
                
            # Try searching for user object or other patterns
            match_user = re.search(r'"user"\s*:\s*({[^}]+})', resp.text)
            if match_user:
                print("Found user block, but couldn't parse auth_token directly.")
                
        except Exception as e:
            print(f"Error requesting {url}: {e}")
            
    return None

def main():
    if not TV_SESSIONID:
        print("TV_SESSIONID is not set in env!")
        return
        
    print(f"TV_SESSIONID value: {TV_SESSIONID[:8]}...")
    
    # Extract auth_token using sessionid
    auth_token = extract_auth_token(TV_SESSIONID)
    if not auth_token:
        print("[FAILED] Could not extract auth_token using sessionid.")
        return
        
    print(f"\nInitializing TvDatafeed with extracted auth_token...")
    try:
        # Pass the extracted auth_token as the sessionid to TvDatafeed,
        # which will assign it to self.token, and send it to set_auth_token!
        tv = TvDatafeed(sessionid=auth_token)
        tv.ws_debug = True
        
        print("\nAttempting to fetch NATURALGAS from MCX...")
        df = tv.get_hist("NATURALGAS", "MCX", interval=Interval.in_daily, n_bars=5, fut_contract=1)
        if df is not None and not df.empty:
            print("\n[SUCCESS] Successfully fetched MCX data!")
            print(df)
        else:
            print("\n[FAILED] Fetch returned None/empty.")
    except Exception as e:
        print(f"\n[ERROR] Failed to run test: {e}")

if __name__ == "__main__":
    main()
