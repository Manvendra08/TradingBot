import urllib.request
import re
import json

url = "https://dhan.co/commodity/natural-gas-option-chain/"
try:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        html = res.read().decode("utf-8")
    
    print("HTML length:", len(html))
    match_next = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
    if match_next:
        print("__NEXT_DATA__ match found!")
        data = json.loads(match_next.group(1))
        props = data.get("props", {}).get("pageProps", {})
        scrip_info = props.get("scripData", {}) or props.get("optionChainData", {}).get("scripData", {})
        print("scripData:", scrip_info)
        
    match_sid = re.search(r'"scripId"\s*:\s*(\d+)', html)
    if match_sid:
        print("Regex match scripId:", match_sid.group(1))
        
    sids = re.findall(r'"scripId"\s*:\s*(\d+)', html)
    print("All scripId matches:", sids)

except Exception as e:
    print("Error:", e)
