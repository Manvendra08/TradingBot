import asyncio
import logging
import re
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_mc_full")

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

async def _fetch_side_async(sym_slug: str, opt_type: str, requested_expiry: Optional[str] = None) -> tuple[Optional[str], list[dict]]:
    from playwright.async_api import async_playwright

    url = f"https://www.moneycontrol.com/commodity/option-chain/{sym_slug}?exchange=mcx&optyp={opt_type}"
    rows: list[dict] = []
    actual_expiry = None

    async with async_playwright() as pw:
        # Try launching with chrome, then msedge, then default
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
            log.error("Could not launch browser with any channel")
            return None, []

        ctx = await browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        try:
            print(f"Navigating to {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # Wait for select options to load
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
                return None, []
                
            # Select target expiry
            selected_val = available_expiries[0]
            if requested_expiry and requested_expiry in available_expiries:
                selected_val = requested_expiry
                
            actual_expiry = selected_val
            print(f"Selecting expiry value: {actual_expiry}")
            await page.select_option("#sel_exp_date", actual_expiry)
            
            # Click Submit
            submit_btn = page.get_by_role("button", name="Submit")
            if await submit_btn.count() == 0:
                submit_btn = page.locator("input[value='Submit']")
            await submit_btn.first.click()
            
            # Wait for data table to load
            print("Waiting for data table to load...")
            await page.wait_for_timeout(5000)
            
        except Exception as exc:
            log.warning(f"Page load/submit failed for {opt_type}: {exc}")
            await browser.close()
            return None, []

        # Grab all tables
        tables = await page.query_selector_all("table")
        print(f"[{opt_type}] Found {len(tables)} tables")
        
        trs = []
        for idx, tbl in enumerate(tables):
            rows = await tbl.query_selector_all("tr")
            print(f"[{opt_type}] Table {idx} has {len(rows)} rows")
            # If a table has more than 5 rows, it's likely our option chain table
            if len(rows) > 5:
                trs = rows
                break

        for tr_idx, tr in enumerate(trs):
            cells = await tr.query_selector_all("td")
            if len(cells) < 5:
                continue
            texts = [await c.inner_text() for c in cells]
            if tr_idx < 10:
                print(f"Row {tr_idx} cells ({opt_type}): {texts}")

            if opt_type == "CE":
                strike = _parse_number(texts[4] if len(texts) > 4 else "")
                ltp = _parse_number(texts[3] if len(texts) > 3 else "")
                oi = _parse_int(texts[0] if len(texts) > 0 else "")
                oi_chg = _parse_int(texts[1] if len(texts) > 1 else "")
                volume = _parse_int(texts[2] if len(texts) > 2 else "")
                bid = _parse_number(texts[5] if len(texts) > 5 else "")
                ask = _parse_number(texts[6] if len(texts) > 6 else "")
            else:
                strike = _parse_number(texts[0] if len(texts) > 0 else "")
                ltp = _parse_number(texts[1] if len(texts) > 1 else "")
                volume = _parse_int(texts[2] if len(texts) > 2 else "")
                oi_chg = _parse_int(texts[3] if len(texts) > 3 else "")
                oi = _parse_int(texts[4] if len(texts) > 4 else "")
                bid = _parse_number(texts[5] if len(texts) > 5 else "")
                ask = _parse_number(texts[6] if len(texts) > 6 else "")

            if strike is None:
                continue

            rows.append({
                "strike": strike,
                "option_type": opt_type,
                "ltp": ltp or 0.0,
                "oi": oi or 0,
                "oi_change": oi_chg or 0,
                "volume": volume or 0,
                "iv": None,
                "bid": bid,
                "ask": ask,
            })

        await browser.close()

    log.info(f"Fetched {len(rows)} {opt_type} rows for expiry {actual_expiry}")
    return actual_expiry, rows

def fetch_option_chain_sync(symbol: str) -> dict | None:
    base = symbol.upper().split()[0]
    slug = _MC_SYMBOL_MAP.get(base)
    if not slug:
        return None

    ce_expiry, ce_rows = None, []
    pe_expiry, pe_rows = None, []

    def _fetch_ce():
        nonlocal ce_expiry, ce_rows
        loop = asyncio.new_event_loop()
        try:
            ce_expiry, ce_rows = loop.run_until_complete(_fetch_side_async(slug, "CE"))
        finally:
            loop.close()

    def _fetch_pe():
        nonlocal pe_expiry, pe_rows
        loop = asyncio.new_event_loop()
        try:
            pe_expiry, pe_rows = loop.run_until_complete(_fetch_side_async(slug, "PE"))
        finally:
            loop.close()

    t_ce = threading.Thread(target=_fetch_ce, daemon=True)
    t_pe = threading.Thread(target=_fetch_pe, daemon=True)
    t_ce.start()
    t_pe.start()
    t_ce.join(timeout=90)
    t_pe.join(timeout=90)

    expiry = ce_expiry or pe_expiry
    all_strikes = ce_rows + pe_rows
    if not all_strikes:
        return None

    seen = set()
    unique = []
    for row in all_strikes:
        key = (row["strike"], row["option_type"])
        if key not in seen:
            seen.add(key)
            unique.append(row)

    unique.sort(key=lambda r: (r["strike"], r["option_type"]))

    return {
        "symbol": base,
        "underlying_price": None,
        "expiry": expiry,
        "strikes": unique,
        "source": "moneycontrol",
        "fetched_at": datetime.now(IST).isoformat(),
    }

print("Running test_mc_full...")
res = fetch_option_chain_sync("NATURALGAS")
if res:
    print("SUCCESS!")
    print(f"Expiry: {res.get('expiry')}")
    print(f"Strikes count: {len(res.get('strikes'))}")
    print("First 5 strikes:")
    for s in res.get('strikes')[:5]:
        print(s)
else:
    print("FAILED")
