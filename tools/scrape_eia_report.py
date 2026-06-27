import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

def scrape_eia():
    with sync_playwright() as p:
        # Launch browser headlessly
        browser = p.chromium.launch(headless=True)
        # Create a new context with a realistic user agent
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        try:
            # Navigate to the economic calendar page
            page.goto("https://www.investing.com/economic-calendar/natural-gas-storage-386", timeout=60000)
            
            # Wait for the historic events table
            page.wait_for_selector("table#historicEvents", timeout=15000)
            
            # Get the first row of data (latest release)
            row = page.locator("table#historicEvents tbody tr").first
            
            # Extract data
            release_date = row.locator("td.left").inner_text().strip()
            time_text = row.locator("td.time").inner_text().strip()
            actual = row.locator("td.noWrap").nth(0).inner_text().strip()
            forecast = row.locator("td.noWrap").nth(1).inner_text().strip()
            previous = row.locator("td.noWrap").nth(2).inner_text().strip()
            
            data = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "release_date": f"{release_date} {time_text}".strip(),
                "actual": actual,
                "forecast": forecast,
                "previous": previous
            }
            
            return data
            
        except Exception as e:
            return {"error": str(e)}
        finally:
            browser.close()

if __name__ == "__main__":
    result = scrape_eia()
    print(json.dumps(result))
    if "error" in result:
        sys.exit(1)
    sys.exit(0)
