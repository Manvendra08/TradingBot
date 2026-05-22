import requests
import time

url = "https://images.dhan.co/api-data/api-scrip-master.csv"
print("Sending request to Dhan Scrip Master...")
start = time.time()
try:
    # Use stream=True to avoid loading the whole huge file if we don't want to
    r = requests.get(url, stream=True, timeout=10)
    print(f"Status Code: {r.status_code}")
    print(f"Headers: {dict(r.headers)}")
    
    # Read the first 100 bytes
    first_chunk = next(r.iter_content(chunk_size=100))
    print(f"First 100 bytes: {first_chunk}")
    print(f"Time taken to get headers: {time.time() - start:.2f} seconds")
except Exception as e:
    print(f"Failed: {e}")
