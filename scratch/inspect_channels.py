import asyncio
from playwright.async_api import async_playwright

async def inspect_channel(channel):
    print(f"=== Testing channel: {channel} ===")
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                headless=True,
                channel=channel,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900}
            )
            page = await ctx.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            url = "https://www.moneycontrol.com/commodity/option-chain/naturalgas?exchange=mcx&optyp=CE"
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            print(f"Loaded URL: {page.url}")
            
            table_exists = await page.locator("table.mctable1, table#opttbldata, .opt-chain-tbl table").count()
            print(f"Table exists: {table_exists > 0}")
            if not table_exists:
                body_text = await page.inner_text("body")
                print(f"Body snippet: {body_text[:200].replace('\n', ' ')}")
            else:
                # print some table rows
                rows = await page.locator("table.mctable1 tr, table#opttbldata tr, .opt-chain-tbl table tr").all_inner_texts()
                print(f"Rows count: {len(rows)}")
                print(f"First row: {rows[0] if rows else 'None'}")
                
            await browser.close()
        except Exception as e:
            print(f"Failed with channel {channel}: {e}")

async def main():
    await inspect_channel("chrome")
    print()
    await inspect_channel("msedge")

asyncio.run(main())
