import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_mc_single")

IST = timezone(timedelta(hours=5, minutes=30))

_MC_SYMBOL_MAP = {
    "NATURALGAS": "naturalgas",
    "CRUDEOIL": "crudeoil",
    "GOLD": "gold",
    "SILVER": "silver",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

def _parse_number(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", text.strip())
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def _parse_int(text: str) -> Optional[int]:
    val = _parse_number(text)
    return int(val) if val is not None else None

async def fetch_option_chain_single(symbol: str, requested_expiry: Optional[str] = None) -> dict | None:
    from playwright.async_api import async_playwright

    base = symbol.upper().split()[0]
    slug = _MC_SYMBOL_MAP.get(base)
    if not slug:
        log.warning(f"Symbol {symbol} not in Moneycontrol map")
        return None

    url = f"https://www.moneycontrol.com/commodity/option-chain/{slug}?exchange=mcx"
    parsed_strikes = []
    actual_expiry = None

    async with async_playwright() as pw:
        browser = None
        for channel in ["chrome", "msedge", None]:
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    channel=channel,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                break
            except Exception as e:
                log.warning(f"Failed to launch browser with channel {channel}: {e}")

        if not browser:
            log.error("Could not launch browser")
            return None

        ctx = await browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        try:
            print(f"Navigating to {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # Wait for select options
            await page.wait_for_selector("#sel_exp_date option", state="attached", timeout=10000)
            
            # Get available expiries
            options = page.locator("#sel_exp_date option")
            count = await options.count()
            available_expiries = []
            for i in range(count):
                val = await options.nth(i).get_attribute("value")
                available_expiries.append(val)
                
            if not available_expiries:
                log.error("No expiries found in dropdown")
                await browser.close()
                return None
                
            # Select expiry
            selected_val = available_expiries[0]
            if requested_expiry and requested_expiry in available_expiries:
                selected_val = requested_expiry
                
            actual_expiry = selected_val
            print(f"Selecting expiry: {actual_expiry}")
            await page.select_option("#sel_exp_date", actual_expiry)
            
            # Click Submit
            submit_btn = page.get_by_role("button", name="Submit")
            if await submit_btn.count() == 0:
                submit_btn = page.locator("input[value='Submit']")
            await submit_btn.first.click()
            
            # Wait
            print("Waiting for data table to load...")
            await page.wait_for_timeout(5000)
            
        except Exception as exc:
            log.error(f"Failed to load or submit page: {exc}")
            await browser.close()
            return None

        # Grab all tables
        tables = await page.query_selector_all("table")
        target_table = None
        for idx, tbl in enumerate(tables):
            rows = await tbl.query_selector_all("tr")
            if len(rows) > 5:
                target_table = tbl
                print(f"Found option chain table with {len(rows)} rows")
                break

        if not target_table:
            log.error("Option chain table not found")
            await browser.close()
            return None

        trs = await target_table.query_selector_all("tr")
        for tr in trs:
            cells = await tr.query_selector_all("td")
            if len(cells) < 11:
                continue
            texts = [await c.inner_text() for c in cells]
            
            # Strike is in cell index 5
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
                "iv": None,
                "bid": _parse_number(texts[3]),  # actually bid/ask are not standard here but we don't strictly need them
                "ask": None
            })

            # Add PE row
            parsed_strikes.append({
                "strike": strike,
                "option_type": "PE",
                "ltp": pe_ltp,
                "oi": pe_oi,
                "oi_change": pe_oi_change,
                "volume": pe_vol,
                "iv": None,
                "bid": _parse_number(texts[7]),
                "ask": None
            })

        await browser.close()

    parsed_strikes.sort(key=lambda r: (r["strike"], r["option_type"]))
    
    return {
        "symbol": base,
        "underlying_price": None,
        "expiry": actual_expiry,
        "strikes": parsed_strikes,
        "source": "moneycontrol",
        "fetched_at": datetime.now(IST).isoformat(),
    }

def main():
    loop = asyncio.new_event_loop()
    try:
        res = loop.run_until_complete(fetch_option_chain_single("NATURALGAS"))
        if res:
            print("SUCCESS!")
            print(f"Expiry: {res['expiry']}")
            print(f"Strikes count: {len(res['strikes'])}")
            print("First 10 strikes:")
            for s in res['strikes'][:10]:
                print(s)
        else:
            print("FAILED")
    finally:
        loop.close()

main()
