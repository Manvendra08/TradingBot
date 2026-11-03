#!/usr/bin/env python3
"""
CLI script to trigger clearing of old completed rows & ledgers across Google Sheets.
Sends action: 'clear_completed' to the Google Apps Script Web App Bridge.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
import json

from src.engine.gemini_spark_feed import APPS_SCRIPT_FEED_URL

def clear_sheets():
    print(f"Triggering Google Sheets cleanup via Apps Script Bridge...")
    print(f"URL: {APPS_SCRIPT_FEED_URL}")
    payload = {
        "action": "clear_completed",
    }
    try:
        resp = requests.post(APPS_SCRIPT_FEED_URL, json=payload, timeout=45.0, allow_redirects=True)
        print(f"Status Code: {resp.status_code}")
        try:
            data = resp.json()
            print("Response:", json.dumps(data, indent=2))
            if data.get("status") == "success":
                print("\n✅ Successfully cleared/archived old completed rows on Google Sheets!")
                return 0
            elif data.get("status") == "ignored":
                print("\n⚠️ Note: The deployed Google Apps Script revision has not yet been updated with action 'clear_completed'.")
                print("Please update the script at script.google.com with src/engine/google_apps_script_bridge.js and deploy or run 'clearCompletedTradesAndLedger'.")
                return 1
        except Exception:
            print("Raw Response:", resp.text[:500])
    except Exception as e:
        print(f"Error calling Apps Script Web App: {e}")
        return 2

if __name__ == "__main__":
    sys.exit(clear_sheets() or 0)
