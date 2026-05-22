import asyncio
from playwright.async_api import async_playwright

async def inspect_page():
    async with async_playwright() as pw:
        # Launch using chrome channel
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
        
        # Load the base URL
        url = "https://www.moneycontrol.com/commodity/option-chain/naturalgas?exchange=mcx&optyp=CE"
        print(f"Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(f"Loaded URL: {page.url}")
        
        # Let's wait a bit for any dynamic scripts
        await page.wait_for_timeout(5000)
        
        # Let's find all tables on the page
        tables = await page.query_selector_all("table")
        print(f"Total tables found: {len(tables)}")
        for idx, tbl in enumerate(tables):
            cls = await tbl.get_attribute("class")
            id_attr = await tbl.get_attribute("id")
            rows = await tbl.query_selector_all("tr")
            print(f"Table {idx}: id={id_attr}, class={cls}, rows={len(rows)}")
            if len(rows) > 0:
                first_row_text = await rows[0].inner_text()
                print(f"  Row 0 text: {first_row_text.strip().replace('\n', ' | ')[:150]}")
                
        # Find all dropdown elements (select)
        selects = await page.query_selector_all("select")
        print(f"Dropdowns found: {len(selects)}")
        for idx, sel in enumerate(selects):
            id_attr = await sel.get_attribute("id")
            name = await sel.get_attribute("name")
            options = await sel.query_selector_all("option")
            opt_texts = [await opt.inner_text() for opt in options]
            print(f"Select {idx}: id={id_attr}, name={name}, option count={len(options)}")
            print(f"  Options: {opt_texts[:10]}")
            
        # Take a screenshot to visualize
        screenshot_path = "c:\\Users\\manve\\Downloads\\NSEBOT\\scratch\\moneycontrol_screenshot.png"
        await page.screenshot(path=screenshot_path)
        print(f"Saved screenshot to {screenshot_path}")
        
        await browser.close()

asyncio.run(inspect_page())
