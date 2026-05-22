import asyncio
from playwright.async_api import async_playwright

async def inspect_default_chromium():
    async with async_playwright() as pw:
        # Launch standard playwright chromium (no channel parameter!)
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900}
        )
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        url = "https://www.moneycontrol.com/commodity/option-chain/naturalgas?exchange=mcx&optyp=CE"
        print(f"Navigating to {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_selector("#sel_exp_date option", state="attached", timeout=10000)
            
            # Select first option
            options = page.locator("#sel_exp_date option")
            val = await options.first.get_attribute("value")
            text = await options.first.inner_text()
            print(f"Selected option: {text} ({val})")
            
            await page.select_option("#sel_exp_date", val)
            
            # Click submit
            submit_btn = page.get_by_role("button", name="Submit")
            if await submit_btn.count() == 0:
                submit_btn = page.locator("input[value='Submit']")
            await submit_btn.first.click()
            
            # Wait for second row in table
            print("Waiting for data table to load...")
            await page.wait_for_selector("table.mctable1 tr:nth-child(2), table#opttbldata tr:nth-child(2), .opt-chain-tbl table tr:nth-child(2)", timeout=15000)
            
            tables = await page.query_selector_all("table")
            print(f"Tables found: {len(tables)}")
            for idx, tbl in enumerate(tables):
                rows = await tbl.query_selector_all("tr")
                print(f"  Table {idx} row count: {len(rows)}")
                if len(rows) > 1:
                    print(f"    Row 1: {await rows[1].inner_text()}")
                    
        except Exception as e:
            print(f"Error: {e}")
        finally:
            await browser.close()

asyncio.run(inspect_default_chromium())
