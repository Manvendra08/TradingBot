import asyncio
from playwright.async_api import async_playwright

async def inspect_mc():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # Open base commodity option chain page
        url = "https://www.moneycontrol.com/commodity/option-chain/naturalgas?exchange=mcx&optyp=CE"
        print(f"Navigating to {url}")
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            
            # Let's check what the URL redirected to, or current URL
            print(f"Loaded URL: {page.url}")
            
            # Let's check if the table exists
            table_exists = await page.locator("table.mctable1, table#opttbldata, .opt-chain-tbl table").count()
            print(f"Table exists: {table_exists > 0}")
            
            # Let's inspect any dropdown menu with expiry dates
            # Usually there is a select element for expiry, e.g., name="expiry" or similar
            selects = await page.query_selector_all("select")
            print(f"Found {len(selects)} select dropdowns")
            for i, select in enumerate(selects):
                name = await select.get_attribute("name")
                id_attr = await select.get_attribute("id")
                options = await select.query_selector_all("option")
                opt_values = [await opt.get_attribute("value") for opt in options]
                opt_texts = [await opt.inner_text() for opt in options]
                print(f"Dropdown {i}: name={name}, id={id_attr}")
                print(f"  Options: {list(zip(opt_values, opt_texts))[:10]}")
                
            # If no table, let's grab some body text to see what is on the page
            if not table_exists:
                body_text = await page.inner_text("body")
                print("Body text snippet:")
                print(body_text[:1000])
                
        except Exception as e:
            print(f"Error: {e}")
        finally:
            await browser.close()

asyncio.run(inspect_mc())
