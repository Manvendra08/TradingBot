import asyncio
from playwright.async_api import async_playwright

async def inspect_mc():
    async with async_playwright() as pw:
        # Launch headed or headless, but let's pass arguments to disable automation flags
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream"
            ]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="Asia/Kolkata",
        )
        page = await ctx.new_page()
        
        # Inject script to bypass navigator.webdriver detection
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        url = "https://www.moneycontrol.com/commodity/option-chain/naturalgas?exchange=mcx&optyp=CE"
        print(f"Navigating to {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            print(f"Loaded URL: {page.url}")
            
            # Let's wait a bit
            await page.wait_for_timeout(3000)
            
            table_exists = await page.locator("table.mctable1, table#opttbldata, .opt-chain-tbl table").count()
            print(f"Table exists: {table_exists > 0}")
            
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
                
            if not table_exists:
                body_text = await page.inner_text("body")
                print("Body text snippet:")
                print(body_text[:1000])
                
        except Exception as e:
            print(f"Error: {e}")
        finally:
            await browser.close()

asyncio.run(inspect_mc())
