import logging
import asyncio
import time
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from src.fetchers.moneycontrol_fetcher import _MC_SYMBOL_MAP, _HEADERS, _parse_number, _parse_int

async def test_debug():
    sym_slug = "naturalgas"
    url = f"https://www.moneycontrol.com/commodity/option-chain/{sym_slug}?exchange=mcx"
    print("Launching playwright...")
    start = time.time()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        print(f"Navigating to {url}...")
        nav_start = time.time()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(f"Navigation took: {time.time() - nav_start:.2f} seconds")

        print("Waiting for select dropdown element...")
        wait_start = time.time()
        await page.wait_for_selector("#sel_exp_date", state="attached", timeout=10000)
        print(f"Dropdown element wait took: {time.time() - wait_start:.2f} seconds")

        options = page.locator("#sel_exp_date option")
        for _ in range(50):
            if await options.count() > 0:
                break
            await page.wait_for_timeout(100)
        
        count = await options.count()
        print(f"Options count: {count}")
        available_expiries = []
        for i in range(count):
            val = await options.nth(i).get_attribute("value")
            available_expiries.append(val)
        print(f"Available expiries: {available_expiries}")

        actual_expiry = available_expiries[0]
        print(f"Selecting expiry: {actual_expiry}...")
        await page.select_option("#sel_exp_date", actual_expiry)

        print("Clicking Submit...")
        click_start = time.time()
        submit_btn = page.get_by_role("button", name="Submit")
        if await submit_btn.count() == 0:
            submit_btn = page.locator("input[value='Submit']")
        await submit_btn.first.click()
        print(f"Click submit took: {time.time() - click_start:.2f} seconds")

        print("Waiting 5s for table load...")
        await page.wait_for_timeout(5000)

        print("Finding tables...")
        table_start = time.time()
        tables = await page.query_selector_all("table")
        target_table = None
        for idx, tbl in enumerate(tables):
            rows = await tbl.query_selector_all("tr")
            if len(rows) > 5:
                target_table = tbl
                break
        print(f"Table discovery took: {time.time() - table_start:.2f} seconds")

        if not target_table:
            print("Target table NOT found!")
            await browser.close()
            return

        print("Fetching inner HTML...")
        html_start = time.time()
        table_html = await target_table.inner_html()
        print(f"Fetching inner HTML took: {time.time() - html_start:.2f} seconds")

        print("Closing browser...")
        close_start = time.time()
        await browser.close()
        print(f"Browser close took: {time.time() - close_start:.2f} seconds")

        print("Parsing table via BeautifulSoup...")
        parse_start = time.time()
        soup = BeautifulSoup(table_html, "html.parser")
        trs = soup.find_all("tr")
        parsed_strikes = []
        for tr in trs:
            cells = tr.find_all("td")
            if len(cells) < 11:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            
            strike = _parse_number(texts[5])
            if strike is None:
                continue

            # Parse CE (left side)
            ce_ltp = _parse_number(texts[4]) or 0.0
            ce_oi = _parse_int(texts[0]) or 0
            ce_oi_change = _parse_int(texts[1]) or 0
            ce_vol = _parse_int(texts[2]) or 0

            # Parse PE (right side)
            pe_ltp = _parse_number(texts[6]) or 0.0
            pe_oi = _parse_int(texts[10]) or 0
            pe_oi_change = _parse_int(texts[9]) or 0
            pe_vol = _parse_int(texts[8]) or 0

            # Add CE row
            parsed_strikes.append({
                "strike": strike,
                "option_type": "CE",
                "ltp": ce_ltp,
                "oi": ce_oi,
                "oi_change": ce_oi_change,
                "volume": ce_vol,
            })

            # Add PE row
            parsed_strikes.append({
                "strike": strike,
                "option_type": "PE",
                "ltp": pe_ltp,
                "oi": pe_oi,
                "oi_change": pe_oi_change,
                "volume": pe_vol,
            })
        print(f"BeautifulSoup parsing took: {time.time() - parse_start:.2f} seconds")
        print(f"Total strikes parsed: {len(parsed_strikes)}")
        print(f"Total script run time: {time.time() - start:.2f} seconds")

asyncio.run(test_debug())
