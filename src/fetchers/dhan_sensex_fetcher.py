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
                browser = pw.chromium.launch(headless=True, timeout=15000)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900}
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(5000)  # wait for client-side rendering

                # Expiry date selection via target tab if requested
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
                    clicked = page.evaluate(r"""(targetText) => {
                        const lis = document.querySelectorAll('li');
                        for (const li of lis) {
                            const txt = li.innerText.trim().replace(/\s+/g, ' ');
                            if (txt === targetText) {
                                li.click();
                                return true;
                            }
                        }
                        return false;
                    }""", target_expiry_text)
                    if clicked:
                        page.wait_for_timeout(3000)
                    else:
                        log.warning("[dhan_sensex] Requested expiry %s not found on page, using default near-month", expiry)

                # Fetch all page data in a single page.evaluate call to avoid costly Playwright sync IPC loops
                extracted_data = page.evaluate(r"""() => {
                    let spot = 0.0;
                    const tds = document.querySelectorAll('tr td');
                    for (const td of tds) {
                        if (td.parentElement.children.length === 1) {
                            const txt = td.innerText.trim();
                            const val = parseFloat(txt.replace(/[^\d.]/g, ''));
                            if (val > 0) {
                                spot = val;
                                break;
                            }
                        }
                    }

                    let expDate = "";
                    const lis = document.querySelectorAll('li');
                    for (const li of lis) {
                        const txt = li.innerText.trim();
                        if (/^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$/.test(txt)) {
                            // If there is an active class/state or if we want the first match
                            // Note: Dhan active expiry tab has a distinct style/class, but the first match is generally the selected one or near-month
                            expDate = txt;
                            break;
                        }
                    }

                    const trs = document.querySelectorAll('tr');
                    const rows = [];
                    for (const tr of trs) {
                        const cells = tr.querySelectorAll('td');
                        if (cells.length === 7) {
                            rows.push({
                                c_oi: cells[0].innerText.trim(),
                                c_vol: cells[1].innerText.trim(),
                                c_ltp: cells[2].innerText.trim(),
                                strike: cells[3].innerText.trim(),
                                p_ltp: cells[4].innerText.trim(),
                                p_vol: cells[5].innerText.trim(),
                                p_oi: cells[6].innerText.trim(),
                            });
                        }
                    }

                    return { spot, expDate, rows };
                }""")

                browser.close()
        except Exception as exc:
            log.exception("[dhan_sensex] scraping SENSEX option chain failed: %s", exc)
            return None

        if not extracted_data:
            log.error("[dhan_sensex] Failed to extract SENSEX page data")
            return None

        underlying_price = extracted_data["spot"]
        expiry_date = extracted_data["expiryDate"] if extracted_data.get("expiryDate") else extracted_data.get("expDate")

        if expiry_date:
            try:
                dt = datetime.strptime(expiry_date, "%d %b %Y")
                actual_expiry = dt.strftime("%Y-%m-%d")
            except Exception as e:
                log.warning("[dhan_sensex] failed to parse expiry date string '%s': %s", expiry_date, e)

        for row in extracted_data.get("rows", []):
            strike = _parse_number(row["strike"])
            if strike <= 0:
                continue

            # Call conversions
            c_oi = _parse_int(row["c_oi"].split("(")[0])
            c_vol = _parse_int(row["c_vol"].replace("L", "00000").replace("K", "000").replace("Cr", "0000000"))
            c_ltp = _parse_number(row["c_ltp"].split("\n")[0])

            # Put conversions
            p_oi = _parse_int(row["p_oi"].split(")")[-1]) if ")" in row["p_oi"] else _parse_int(row["p_oi"])
            p_vol = _parse_int(row["p_vol"].replace("L", "00000").replace("K", "000").replace("Cr", "0000000"))
            p_ltp = _parse_number(row["p_ltp"].split("\n")[0])

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
