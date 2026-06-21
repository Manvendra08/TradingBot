import os
import requests

# Load .env
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    k, v = parts
                    v = v.strip("\"'")
                    os.environ[k] = v

client_id = os.environ.get("DHAN_CLIENT_ID")
access_token = os.environ.get("DHAN_ACCESS_TOKEN")
# Try with /v2 suffix
base_url = "https://api.dhan.co/v2"

print(f"client_id: {client_id}")
print(f"access_token prefix: {access_token[:15]}..." if access_token else "No access token")
print(f"base_url: {base_url}")

headers = {
    "access-token": access_token,
    "client-id": client_id,
    "Content-Type": "application/json"
}

# Payload for BANKNIFTY
payload = {
    "UnderlyingScrip": 25,
    "UnderlyingSeg": "IDX_I"
}

url = f"{base_url}/optionchain/expirylist"
print(f"Calling: {url} with {payload}...")
try:
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    print(f"Status Code: {r.status_code}")
    print(f"Headers: {r.headers}")
    print(f"Response text: {r.text}")
    if r.status_code == 200:
        print(f"JSON: {r.json()}")
except Exception as e:
    print(f"Request failed: {e}")
