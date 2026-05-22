import requests
import time

print("Testing direct request to Dhan API...")
start = time.time()
try:
    # Just a simple GET to the root or a subpath
    r = requests.get("https://api.dhan.co/v2", timeout=5)
    print(f"Status: {r.status_code}")
    print(f"Headers: {dict(r.headers)}")
    print(f"Body: {r.text[:200]}")
except Exception as e:
    print(f"Failed: {e}")
print(f"Time taken: {time.time() - start:.2f} seconds")
