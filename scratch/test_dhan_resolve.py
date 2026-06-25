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
    
    match_next = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
    if match_next:
        data = json.loads(match_next.group(1))
        props = data.get("props", {}).get("pageProps", {})
        fnoData = props.get("fnoData", {})
        # Remove flst as it's large
        fno_clean = {k: v for k, v in fnoData.items() if k != "flst"}
        print("fnoData details:")
        print(json.dumps(fno_clean, indent=2))
        
except Exception as e:
    print("Error:", e)
