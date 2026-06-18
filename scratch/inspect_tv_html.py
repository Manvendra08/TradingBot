import os
import requests
from dotenv import load_dotenv
load_dotenv()

from config.settings import TV_SESSIONID

def main():
    if not TV_SESSIONID:
        print("TV_SESSIONID not set.")
        return
        
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.tradingview.com/'
    }
    cookies = {
        'sessionid': TV_SESSIONID
    }
    
    url = 'https://www.tradingview.com/'
    print(f"Requesting {url}...")
    resp = requests.get(url, cookies=cookies, headers=headers, timeout=10)
    print(f"Status Code: {resp.status_code}")
    
    with open('scratch/tv_page.html', 'w', encoding='utf-8') as f:
        f.write(resp.text)
        
    print("Saved response to scratch/tv_page.html")
    
    # Search for any occurrences of "auth_token" (case-insensitive)
    matches = re_find_all_around("auth_token", resp.text)
    print(f"\nFound {len(matches)} occurrences of 'auth_token':")
    for m in matches:
        print("---")
        print(m)

def re_find_all_around(pattern, text, context=100):
    matches = []
    for m in re.finditer(pattern, text, re.IGNORECASE):
        start = max(0, m.start() - context)
        end = min(len(text), m.end() + context)
        matches.append(text[start:end])
    return matches

import re
if __name__ == "__main__":
    main()
