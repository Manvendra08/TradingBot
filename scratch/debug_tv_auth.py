import requests
import os
import json
from dotenv import load_dotenv

load_dotenv()
username = os.getenv("TV_USERNAME")
password = os.getenv("TV_PASSWORD")

sign_in_url = 'https://www.tradingview.com/accounts/signin/'
signin_headers = {
    'Referer': 'https://www.tradingview.com',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

data = {
    "username": username,
    "password": password,
    "remember": "on"
}

print(f"URL: {sign_in_url}")
print(f"Data: {data}")
print("Sending signin request...")

try:
    response = requests.post(url=sign_in_url, data=data, headers=signin_headers, timeout=10)
    print(f"Status Code: {response.status_code}")
    print(f"Response headers: {response.headers}")
    print(f"Response text (first 1000 chars): {response.text[:1000]}")
    try:
        res_json = response.json()
        print("JSON response keys:", list(res_json.keys()))
        if 'user' in res_json:
            print("Auth token:", res_json['user'].get('auth_token'))
    except Exception as e:
        print("Failed to decode JSON:", e)
except Exception as e:
    print("Request failed:", e)
