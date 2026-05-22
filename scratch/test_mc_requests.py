import requests
from bs4 import BeautifulSoup

url = "https://www.moneycontrol.com/commodity/option-chain/naturalgas?exchange=mcx"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

print(f"Fetching {url} using requests...")
r = requests.get(url, headers=headers)
print(f"Status Code: {r.status_code}")
if r.status_code == 200:
    soup = BeautifulSoup(r.text, "html.parser")
    # Check if select element sel_exp_date is present
    select = soup.find(id="sel_exp_date")
    if select:
        print("Success! sel_exp_date select element found!")
        options = select.find_all("option")
        print(f"Available expiries in raw HTML: {[o.get('value') for o in options]}")
    else:
        print("sel_exp_date select element NOT found in raw HTML.")
    
    # Check if any tables are present
    tables = soup.find_all("table")
    print(f"Found {len(tables)} tables in raw HTML.")
    for idx, tbl in enumerate(tables):
        trs = tbl.find_all("tr")
        if len(trs) > 5:
            print(f"Table {idx} has {len(trs)} rows!")
else:
    print("Failed to fetch.")
