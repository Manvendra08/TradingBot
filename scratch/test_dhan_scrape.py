import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

async def main():
    print("Launching browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        page = await browser.new_page()
        print("Navigating to Dhan Natural Gas public option chain...")
        await page.goto("https://dhan.co/commodity/natural-gas-option-chain/", wait_until="networkidle", timeout=60000)
        
        # Save HTML
        html = await page.content()
        with open("scratch/dhan_ng_page.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Saved page HTML to scratch/dhan_ng_page.html")
        
        # Parse tables
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        print(f"Found {len(tables)} tables on the page.")
        for idx, table in enumerate(tables):
            print(f"\n--- TABLE {idx} ---")
            # Print headers
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            print("Headers:", headers)
            # Print first 3 rows
            rows = table.find_all("tr")
            print(f"Total rows: {len(rows)}")
            for r in rows[1:4]:
                cells = [td.get_text(strip=True) for td in r.find_all("td")]
                print("Row:", cells)
                
        # Look for custom components or selectors
        print("\nSearching for potential option-chain table container...")
        selectors = [
            ".option-chain", ".opt-chain", "#optionchain", "#optchain",
            "[class*='option-chain' i]", "[class*='optchain' i]", "[id*='optionchain' i]"
        ]
        for sel in selectors:
            elements = await page.query_selector_all(sel)
            if elements:
                print(f"Found match for selector '{sel}': {len(elements)} elements")
                for el in elements:
                    tag_name = await el.evaluate("e => e.tagName")
                    classes = await el.evaluate("e => e.className")
                    print(f"  Tag: {tag_name}, Class: {classes}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
