import time
print("Importing sync_playwright...")
from playwright.sync_api import sync_playwright
print("Starting sync_playwright context manager...")
with sync_playwright() as pw:
    print("Launching browser...")
    browser = pw.chromium.launch(headless=True)
    print("Browser launched successfully!")
    browser.close()
print("Done!")
