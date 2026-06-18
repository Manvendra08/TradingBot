import logging
from tvDatafeed import TvDatafeed
import os
from dotenv import load_dotenv

# Set logging to DEBUG to see everything
logging.basicConfig(level=logging.DEBUG)

load_dotenv()
username = os.getenv("TV_USERNAME")
password = os.getenv("TV_PASSWORD")

print(f"Username: {username}")
print(f"Password: {password}")

tv = TvDatafeed(username=username, password=password)
tv.ws_debug = True

# Try fetching a completely free symbol like AAPL on NASDAQ first to see if it works without login or with login
print("--- Fetching AAPL from NASDAQ ---")
try:
    df = tv.get_hist("AAPL", "NASDAQ", n_bars=10)
    print("AAPL df:")
    print(df)
except Exception as e:
    print(f"AAPL failed: {e}")

# Try fetching NATURALGAS from MCX
print("--- Fetching NATURALGAS from MCX ---")
try:
    df = tv.get_hist("NATURALGAS", "MCX", n_bars=10)
    print("NATURALGAS df:")
    print(df)
except Exception as e:
    print(f"NATURALGAS failed: {e}")
