import logging
import re
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

def _parse_number(text: str) -> float:
    if not text:
        return 0.0
    cleaned = re.sub(r"[^\d.\-]", "", text.strip())
    if not cleaned or cleaned in ("-", "."):
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def _parse_int(text: str) -> int:
    val = _parse_number(text)
    return int(round(val))

class DhanSensexFetcher(BaseFetcher):
    name = "dhan_sensex"

    def fetch_option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
        """
        Scrapes option chain for SENSEX index from Dhan public pages.
        """
        # Ensure we only fetch for SENSEX
        if symbol.upper().split()[0] != "SENSEX":
            log.warning("[dhan_sensex] DhanSensexFetcher only supports SENSEX symbol. Got: %s", symbol)
            return None

        url = "https://dhan.co/indices/bse-sensex-option-chain/"
        log.info("[dhan_sensex] Scraping option chain from: %s", url)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error("[dhan_sensex] playwright is not installed. Run: pip install playwright && playwright install chromium")
            return None

        actual_expiry = None
        underlying_price = 0.0
        parsed_strikes = []

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, timeout=10000)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900}
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(8000)  # wait for client-side rendering

                # 1. Spot price resolution (Find 1-cell td row)
                rows = page.locator("tr").all()
                for row in rows:
                    cells = row.locator("td").all()
                    if len(cells) == 1:
                        txt = cells[0].inner_text()
                        val = _parse_number(txt)
                        if val > 0:
                            underlying_price = val
                            break

                # 2. Expiry date resolution (Find LI tags with date pattern)
                expiry_date = ""
                li_elements = page.locator("li").all()

                target_expiry_text = None
                if expiry:
                    try:
                        dt = datetime.strptime(expiry, "%Y-%m-%d")
                        day = str(dt.day)
                        month_str = dt.strftime("%b")
                        year = str(dt.year)
                        target_expiry_text = f"{day} {month_str} {year}"
                    except Exception as e:
                        log.warning("[dhan_sensex] invalid expiry filter passed: %s, error: %s", expiry, e)

                if target_expiry_text:
                    clicked = False
                    for li in li_elements:
                        txt = li.inner_text().strip()
                        txt_clean = " ".join(txt.split())
                        if txt_clean.replace(" 0", " ") == target_expiry_text.replace(" 0", " "):
                            log.info("[dhan_sensex] Clicking target expiry tab: %s", txt)
                            li.click()
                            page.wait_for_timeout(3000)
                            # Refresh rows after click
                            rows = page.locator("tr").all()
                            expiry_date = txt
                            clicked = True
                            break
                    if not clicked:
                        log.warning("[dhan_sensex] Requested expiry %s not found on page, using default near-month", expiry)

                if not expiry_date:
                    for li in li_elements:
                        txt = li.inner_text().strip()
                        if re.match(r'^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$', txt):
                            expiry_date = txt
                            break

                if expiry_date:
                    try:
                        dt = datetime.strptime(expiry_date, "%d %b %Y")
                        actual_expiry = dt.strftime("%Y-%m-%d")
                    except Exception as e:
                        log.warning("[dhan_sensex] failed to parse expiry date string '%s': %s", expiry_date, e)

                # 3. Parse strikes
                for row in rows:
                    cells = row.locator("td").all()
                    if len(cells) == 7:
                        c_oi_raw = cells[0].inner_text()
                        c_vol_raw = cells[1].inner_text()
                        c_ltp_raw = cells[2].inner_text()
                        strike_raw = cells[3].inner_text()
                        p_ltp_raw = cells[4].inner_text()
                        p_vol_raw = cells[5].inner_text()
                        p_oi_raw = cells[6].inner_text()

                        strike = _parse_number(strike_raw)
                        if strike <= 0:
                            continue

                        # Call conversions
                        c_oi = _parse_int(c_oi_raw.split("(")[0])
                        c_vol = _parse_int(c_vol_raw.replace("L", "00000").replace("K", "000").replace("Cr", "0000000"))
                        c_ltp = _parse_number(c_ltp_raw.split("\n")[0])

                        # Put conversions
                        p_oi = _parse_int(p_oi_raw.split(")")[-1]) if ")" in p_oi_raw else _parse_int(p_oi_raw)
                        p_vol = _parse_int(p_vol_raw.replace("L", "00000").replace("K", "000").replace("Cr", "0000000"))
                        p_ltp = _parse_number(p_ltp_raw.split("\n")[0])

                        parsed_strikes.append({
                            "strike": strike,
                            "option_type": "CE",
                            "ltp": c_ltp,
                            "oi": c_oi,
                            "oi_change": 0,
                            "volume": c_vol,
                            "iv": 0.0,
                            "bid": 0.0,
                            "ask": 0.0
                        })
                        parsed_strikes.append({
                            "strike": strike,
                            "option_type": "PE",
                            "ltp": p_ltp,
                            "oi": p_oi,
                            "oi_change": 0,
                            "volume": p_vol,
                            "iv": 0.0,
                            "bid": 0.0,
                            "ask": 0.0
                        })

                browser.close()
        except Exception as exc:
            log.exception("[dhan_sensex] scraping SENSEX option chain failed: %s", exc)
            return None

        if not parsed_strikes:
            log.error("[dhan_sensex] No strikes parsed for SENSEX")
            return None

        parsed_strikes.sort(key=lambda r: (r["strike"], r["option_type"]))

        return {
            "symbol": "SENSEX",
            "underlying_price": underlying_price,
            "expiry": actual_expiry,
            "strikes": parsed_strikes,
            "source": self.name,
            "fetched_at": datetime.now(IST).isoformat(),
        }
