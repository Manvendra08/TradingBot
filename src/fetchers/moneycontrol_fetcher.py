"""
Moneycontrol Option Chain Fetcher — Playwright-based (CE+PE stitched).

Moneycontrol returns 403 to plain requests; requires full browser rendering.
Since both CE and PE details are presented in the same merged table, we only
need to fetch a single page to get the entire option chain.

Note: IV and Greeks are NOT available on Moneycontrol — those fields will be None.
This fetcher is a fallback; Dhan headless is primary for full data.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from config.settings import NSE_BASE_URL, NSE_HEADERS, STRIKES_AROUND_ATM

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Moneycontrol commodity symbol map
_MC_SYMBOL_MAP: dict[str, str] = {
    "NATURALGAS": "naturalgas",
    "CRUDEOIL": "crudeoil",
    "GOLD": "gold",
    "SILVER": "silver",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
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


def _fetch_nse_commodity_spot(symbol: str) -> Optional[float]:
    try:
        import requests

        session = requests.Session()
        session.headers.update(NSE_HEADERS)
        session.get(NSE_BASE_URL, timeout=10)
        session.get(f"{NSE_BASE_URL}/option-chain", timeout=10)
        res = session.get(f"{NSE_BASE_URL}/api/refrates?index=commodityspotrates", timeout=10)
        res.raise_for_status()
        for item in res.json().get("data", []):
            if str(item.get("symbol", "")).upper() != symbol:
                continue
            val = item.get("lastSpotPrice") or item.get("spotPrice")
            spot = _parse_number(str(val))
            if spot and spot > 0:
                log.info("[mc] NSE commodity spot for %s: %.2f", symbol, spot)
                return spot
    except Exception as exc:
        log.warning("[mc] NSE commodity spot fetch failed for %s: %s", symbol, exc)
    return None


def _scrape_moneycontrol_spot(page) -> Optional[float]:
    selectors = [
        ".stkUp", ".stkDn", ".stkFlat", ".stkUnch",
        "#last_price", ".price_dil", ".commodity-price", ".inprice1",
        "div.price", "span.price",
    ]
    for selector in selectors:
        try:
            for el in page.query_selector_all(selector):
                val = _parse_number(el.inner_text())
                if val and val > 0:
                    return val
        except Exception:
            continue

    try:
        text = page.inner_text("body", timeout=2000)
    except Exception:
        return None

    patterns = (
        r"(?:Spot|Underlying|Last(?:\s+Price)?|LTP)\s*[:\-]?\s*([\d,]+(?:\.\d+)?)",
        r"([\d,]+(?:\.\d+)?)\s*(?:Spot|Underlying|LTP)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            val = _parse_number(match.group(1))
            if val and val > 0:
                return val
    return None


def _fetch_side_sync(base_symbol: str, sym_slug: str, requested_expiry: Optional[str] = None) -> tuple[Optional[str], Optional[float], list[dict]]:
    """Fetch option chain and spot price from Moneycontrol using synchronous Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("[mc] playwright not installed — pip install playwright && playwright install chromium")
        return None, None, []

    url = f"https://www.moneycontrol.com/commodity/option-chain/{sym_slug}?exchange=mcx"
    parsed_strikes = []
    actual_expiry = None
    underlying_price = _fetch_nse_commodity_spot(base_symbol)
    keep_strikes: set[float] = set()

    with sync_playwright() as pw:
        # Try launching with chrome, then msedge, then default chromium
        browser = None
        for channel in ["chrome", "msedge", None]:
            try:
                browser = pw.chromium.launch(
                    headless=True,
                    channel=channel,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                break
            except Exception as e:
                log.warning("[mc] failed to launch browser with channel %s: %s", channel, e)

        if not browser:
            log.error("[mc] could not launch browser with any channel")
            return None, None, []

        ctx = browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        try:
            log.info("[mc] navigating to option chain: %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # Wait for select element container to load
            page.wait_for_selector("#sel_exp_date", state="attached", timeout=10000)
            
            # Poll robustly until child options are populated in select element
            options = page.locator("#sel_exp_date option")
            for _ in range(50):
                if options.count() > 0:
                    break
                page.wait_for_timeout(100)
            
            # Get available expiries
            count = options.count()
            available_expiries = []
            for i in range(count):
                val = options.nth(i).get_attribute("value")
                available_expiries.append(val)
                
            if not available_expiries:
                log.error("[mc] no expiries found in dropdown")
                browser.close()
                return None, None, []
                
            if not underlying_price:
                underlying_price = _scrape_moneycontrol_spot(page)
                if underlying_price:
                    log.info("[mc] Moneycontrol spot for %s: %.2f", base_symbol, underlying_price)
            if not underlying_price:
                log.error("[mc] no spot/underlying found for %s; cannot identify ATM", base_symbol)
                browser.close()
                return None, None, []

            # Select the best expiry that actually covers the spot price
            actual_expiry = None
            table_html = None
            
            expiries_to_test = available_expiries
            if requested_expiry and requested_expiry in available_expiries:
                # Prioritize requested expiry
                expiries_to_test = [requested_expiry] + [e for e in available_expiries if e != requested_expiry]

            from bs4 import BeautifulSoup

            for exp in expiries_to_test:
                try:
                    log.info("[mc] testing expiry candidate: %s", exp)
                    page.select_option("#sel_exp_date", exp)
                    
                    submit_btn = page.get_by_role("button", name="Submit")
                    if submit_btn.count() == 0:
                        submit_btn = page.locator("input[value='Submit']")
                    submit_btn.first.click()
                    
                    page.wait_for_timeout(1000)
                    
                    # Wait for table
                    target_table = None
                    for attempt in range(15):
                        tables = page.query_selector_all("table")
                        for tbl in tables:
                            try:
                                r_count = len(tbl.query_selector_all("tr"))
                                if r_count > 5:
                                    target_table = tbl
                                    break
                            except Exception:
                                continue
                        if target_table:
                            break
                        page.wait_for_timeout(100)
                    
                    if not target_table:
                        log.warning("[mc] table not found for expiry: %s", exp)
                        continue
                        
                    if not underlying_price:
                        underlying_price = _scrape_moneycontrol_spot(page)
                        if underlying_price:
                            log.info("[mc] Moneycontrol spot for %s: %.2f", base_symbol, underlying_price)

                    curr_html = target_table.inner_html()
                    soup = BeautifulSoup(curr_html, "html.parser")
                    trs = soup.find_all("tr")
                    strikes = []
                    for tr in trs:
                        cells = tr.find_all("td")
                        if len(cells) >= 11:
                            strike = _parse_number(cells[5].get_text(strip=True))
                            if strike:
                                strikes.append(strike)
                    
                    if not strikes:
                        log.warning("[mc] no strikes parsed for expiry: %s", exp)
                        continue
                        
                    min_s, max_s = min(strikes), max(strikes)
                    log.info("[mc] expiry %s range: %s to %s", exp, min_s, max_s)
                    
                    # If spot is covered, or if we don't know the spot price, or if it's the only option
                    if not underlying_price or (min_s <= underlying_price <= max_s) or len(expiries_to_test) == 1:
                        actual_expiry = exp
                        table_html = curr_html
                        strikes = sorted(set(strikes))
                        atm_strike = min(strikes, key=lambda x: abs(x - underlying_price))
                        idx = strikes.index(atm_strike)
                        start_idx = max(0, idx - STRIKES_AROUND_ATM)
                        end_idx = min(len(strikes), idx + STRIKES_AROUND_ATM + 1)
                        keep_strikes = set(strikes[start_idx:end_idx])
                        log.info("[mc] selected valid expiry: %s", actual_expiry)
                        log.info(
                            "[mc] ATM window for %s: spot %.2f, ATM %s, strikes %s-%s (%d)",
                            base_symbol,
                            underlying_price,
                            atm_strike,
                            min(keep_strikes),
                            max(keep_strikes),
                            len(keep_strikes),
                        )
                        break
                    else:
                        log.warning("[mc] expiry %s does not cover spot %.2f", exp, underlying_price)
                except Exception as e:
                    log.error("[mc] error testing expiry %s: %s", exp, e)
                    continue

            if not table_html:
                log.error("[mc] could not find any valid option chain table")
                browser.close()
                return None, None, []

            page.close()
            ctx.close()
            browser.close()
        except Exception as exc:
            log.error("[mc] page interaction failed: %s", exc)
            if browser:
                try: browser.close()
                except: pass
            return None, None, []

        soup = BeautifulSoup(table_html, "html.parser")
        trs = soup.find_all("tr")
        for tr in trs:
            cells = tr.find_all("td")
            if len(cells) < 11:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            
            strike = _parse_number(texts[5])
            if strike is None:
                continue
            if keep_strikes and strike not in keep_strikes:
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
                "bid": None,
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
                "bid": None,
                "ask": None
            })

    parsed_strikes.sort(key=lambda r: (r["strike"], r["option_type"]))
    log.info("[mc] parsed %d ATM-window strikes for %s", len(parsed_strikes) // 2, sym_slug)
    return actual_expiry, underlying_price, parsed_strikes


class MoneycontrolFetcher:
    """Playwright-based Moneycontrol option chain fetcher (MCX commodities)."""

    name = "moneycontrol"

    def fetch_option_chain(self, symbol: str) -> dict | None:
        base = symbol.upper().split()[0]
        slug = _MC_SYMBOL_MAP.get(base)
        if not slug:
            log.warning("[mc] unsupported symbol: %s", symbol)
            return None

        actual_expiry, underlying_price, strikes = _fetch_side_sync(base, slug)

        if not strikes:
            log.error("[mc] no data returned for %s", symbol)
            return None

        return {
            "symbol": base,
            "underlying_price": underlying_price,
            "expiry": actual_expiry,
            "strikes": strikes,
            "source": self.name,
            "fetched_at": datetime.now(IST).isoformat(),
        }
