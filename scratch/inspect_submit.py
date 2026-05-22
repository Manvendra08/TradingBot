import asyncio
from playwright.async_api import async_playwright

async def inspect_submit():
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
        
        url = "https://www.moneycontrol.com/commodity/option-chain/naturalgas?exchange=mcx&optyp=CE"
        print(f"Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # Click the Submit button
        submit_btn = page.locator("input[type='submit'], button:has-text('Submit'), .btn_submit, a:has-text('Submit')")
        # Let's find any button/input/element containing 'Submit'
        # In the screenshot it's a blue button saying 'Submit'. Let's search for it.
        submit_locator = page.get_by_role("button", name="Submit")
        if await submit_locator.count() == 0:
            submit_locator = page.locator("input[value='Submit']")
        if await submit_locator.count() == 0:
            submit_locator = page.locator("a:has-text('Submit')")
            
        print(f"Submit buttons found: {await submit_locator.count()}")
        if await submit_locator.count() > 0:
            print("Clicking Submit...")
            await submit_locator.first.click()
            
        # Wait for the table to load
        print("Waiting 5 seconds for table to update...")
        await page.wait_for_timeout(5000)
        
        # Let's count table rows again
        tables = await page.query_selector_all("table")
        print(f"Total tables found: {len(tables)}")
        for idx, tbl in enumerate(tables):
            rows = await tbl.query_selector_all("tr")
            print(f"Table {idx}: rows={len(rows)}")
            if len(rows) > 1:
                print(f"  First 3 rows:")
                for r in rows[:3]:
                    text = await r.inner_text()
                    print(f"    {text.strip().replace('\n', ' | ')}")
                    
        # Let's also check for any network requests or responses that might contain option chain data
        screenshot_path = "c:\\Users\\manve\\Downloads\\NSEBOT\\scratch\\moneycontrol_screenshot_after_submit.png"
        await page.screenshot(path=screenshot_path)
        print(f"Saved screenshot after submit to {screenshot_path}")
        
        await browser.close()

asyncio.run(inspect_submit())
