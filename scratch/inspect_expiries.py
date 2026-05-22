import asyncio
from playwright.async_api import async_playwright

async def test_expiries():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900}
        )
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        # We can listen to console and network responses
        page.on("console", lambda msg: print(f"Browser Console [{msg.type}]: {msg.text}"))
        
        url = "https://www.moneycontrol.com/commodity/option-chain/naturalgas?exchange=mcx&optyp=CE"
        print(f"Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # Wait for the select option to be populated
        print("Waiting for select options to load...")
        await page.wait_for_selector("#sel_exp_date option", state="attached", timeout=10000)
        
        # Get expiry option values
        options_locator = page.locator("#sel_exp_date option")
        count = await options_locator.count()
        print(f"Found {count} expiry options:")
        options_info = []
        for i in range(count):
            val = await options_locator.nth(i).get_attribute("value")
            text = await options_locator.nth(i).inner_text()
            options_info.append((val, text))
            print(f"  {val} -> {text}")
            
        # Let's try to select each option, click Submit, and check table rows
        for idx, (val, text) in enumerate(options_info):
            print(f"\n--- Testing Expiry: {text} (value={val}) ---")
            await page.select_option("#sel_exp_date", val)
            
            # Click submit
            submit_btn = page.get_by_role("button", name="Submit")
            if await submit_btn.count() == 0:
                submit_btn = page.locator("input[value='Submit']")
            await submit_btn.first.click()
            
            # Wait for some time
            print("Waiting for data to load...")
            await page.wait_for_timeout(4000)
            
            # Count rows in option chain table (usually the second table or table with headers)
            tables = await page.query_selector_all("table")
            print(f"Tables count: {len(tables)}")
            for t_idx, tbl in enumerate(tables):
                rows = await tbl.query_selector_all("tr")
                print(f"  Table {t_idx} row count: {len(rows)}")
                if len(rows) > 1:
                    print(f"    First data row: {await rows[1].inner_text()}")
                    
            # Take screenshot
            sc_path = f"c:\\Users\\manve\\Downloads\\NSEBOT\\scratch\\mc_expiry_{idx}.png"
            await page.screenshot(path=sc_path)
            print(f"Saved screenshot to {sc_path}")
            
        await browser.close()

asyncio.run(test_expiries())
