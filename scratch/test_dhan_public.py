import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import re

async def main():
    print("Launching headless browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        print("Navigating to Dhan Natural Gas public option chain page...")
        url = "https://dhan.co/commodity/natural-gas-option-chain/"
        await page.goto(url, wait_until="networkidle", timeout=60000)
        
        # Wait for table or rows to load
        print("Waiting for option chain elements to load...")
        try:
            await page.wait_for_selector("table", timeout=15000)
            print("Table element found!")
        except Exception as e:
            print("Failed to find table element via selector:", e)
            
        # Get content and parse
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        
        # Save parsed table rows to verify
        tables = soup.find_all("table")
        print(f"Found {len(tables)} tables on the page.")
        
        for idx, table in enumerate(tables):
            rows = table.find_all("tr")
            print(f"\nTable {idx} has {len(rows)} rows.")
            # Print headers
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            print("Headers:", headers)
            
            # Print first few rows to show structure
            valid_rows = 0
            for r in rows:
                cells = [td.get_text(strip=True) for td in r.find_all("td")]
                if len(cells) >= 7:
                    valid_rows += 1
                    if valid_rows <= 10:
                        print(f"Row {valid_rows}: {cells}")
            print(f"Total valid data rows: {valid_rows}")
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
