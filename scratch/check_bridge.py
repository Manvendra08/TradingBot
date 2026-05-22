import requests

try:
    r = requests.get("http://localhost:8765/health", timeout=2)
    print(f"Bridge is running! Status: {r.status_code}, Body: {r.text}")
except Exception as e:
    print(f"Bridge is NOT running: {e}")
