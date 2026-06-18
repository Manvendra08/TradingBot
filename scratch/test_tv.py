import logging
from tvDatafeed import TvDatafeed
import os
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)

load_dotenv()
username = os.getenv("TV_USERNAME")
password = os.getenv("TV_PASSWORD")

print(f"Username: {username}")
print(f"Password: {password}")

print("Attempting authenticated login...")
try:
    tv = TvDatafeed(username=username, password=password)
    print("Login successful!")
    print("Fetching Natural Gas data...")
    df = tv.get_hist("NATURALGAS", "MCX", n_bars=10)
    print(df)
except Exception as e:
    print(f"Failed with exception: {e}")

print("\nAttempting anonymous login...")
try:
    tv = TvDatafeed()
    print("Anonymous login successful!")
    print("Fetching NIFTY data...")
    df = tv.get_hist("NIFTY", "NSE", n_bars=10)
    print(df)
except Exception as e:
    print(f"Failed with exception: {e}")
