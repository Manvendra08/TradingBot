import logging
import re
import time
from datetime import datetime, timedelta, timezone
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
        """Scrapes option chain for SENSEX index from Dhan public pages."""
        if symbol.upper().split()[0] != "SENSEX":
            log.warning(
                "[dhan_sensex] DhanSensexFetcher only supports SENSEX symbol. Got: %s",
                symbol,
            )
            return None

        url = "https://dhan.co/indices/bse-sensex-option-chain/"
        log.info("[dhan_sensex] Scraping option chain from: %s", url)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error(
                "[dhan_sensex] playwright is not installed. Run: pip install playwright && playwright install chromium"
            )
            return None

        # Retry loop: the page can be slow to hydrate its React tree
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            result = self._attempt_scrape(
                url, expiry, sync_playwright, attempt, max_attempts
            )
            if result is not None:
                return result

        log.error(
            "[dhan_sensex] All %d scrape attempts failed for SENSEX", max_attempts
        )
        return None

    def _attempt_scrape(
        self,
        url: str,
        expiry: str | None,
        sync_playwright,
        attempt: int,
        max_attempts: int,
    ) -> dict | None:
        """Single scrape attempt with its own browser session."""
        log.info("[dhan_sensex] Scrape attempt %d/%d", attempt, max_attempts)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    timeout=30000,
                    args=["--disable-gpu", "--no-sandbox"],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Wait for the data table to render (table-bod class)
                # This table has the actual option chain rows
                try:
                    page.wait_for_selector(
                        "table.table-bod tr td",
                        timeout=20000,
                    )
                    log.info("[dhan_sensex] Data table detected")
                except Exception:
                    log.warning(
                        "[dhan_sensex] Timeout waiting for data table on attempt %d",
                        attempt,
                    )
                    # Extra grace period in case it just loaded
                    page.wait_for_timeout(3000)

                # Expiry date selection
                target_expiry_text = None
                if expiry:
                    try:
                        dt = datetime.strptime(expiry, "%Y-%m-%d")
                        day = str(dt.day)
                        month_str = dt.strftime("%b")
                        year = str(dt.year)
                        target_expiry_text = f"{day} {month_str} {year}"
                    except Exception as e:
                        log.warning(
                            "[dhan_sensex] invalid expiry filter: %s, error: %s",
                            expiry,
                            e,
                        )

                if target_expiry_text:
                    clicked = page.evaluate(
                        """(targetText) => {
                            const lis = document.querySelectorAll('li');
                            for (const li of lis) {
                                const txt = li.innerText.trim().replace(/\\s+/g, ' ');
                                if (txt === targetText) {
                                    li.click();
                                    return true;
                                }
                            }
                            return false;
                        }""",
                        target_expiry_text,
                    )
                    if clicked:
                        page.wait_for_timeout(3000)
                        # Re-wait for table after expiry switch
                        try:
                            page.wait_for_selector(
                                "table.table-bod tr td",
                                timeout=15000,
                            )
                        except Exception:
                            pass
                    else:
                        log.warning(
                            "[dhan_sensex] Requested expiry %s not found, using default",
                            expiry,
                        )

                # Extract all data in one evaluate call
                extracted_data = page.evaluate("""() => {
                    // ---- 1. Spot price ----
                    let spot = 0.0;
                    // The spot is in a single-<td> row — look for it in the whole page
                    const tds = document.querySelectorAll('tr td');
                    for (const td of tds) {
                        if (td.parentElement.children.length === 1) {
                            const txt = td.innerText.trim();
                            const val = parseFloat(txt.replace(/[^\\d.]/g, ''));
                            if (val > 0) {
                                spot = val;
                                break;
                            }
                        }
                    }

                    // ---- 2. Expiry date ----
                    let expDate = "";
                    const lis = document.querySelectorAll('li');
                    for (const li of lis) {
                        const txt = li.innerText.trim();
                        if (/^\\d{1,2}\\s+[A-Za-z]{3}\\s+\\d{4}$/.test(txt)) {
                            // Prefer the active/selected tab (orange border)
                            if (li.className && li.className.includes('EF9309')) {
                                expDate = txt;
                                break;
                            }
                        }
                    }
                    // Fallback: first matching expiry if none was active
                    if (!expDate) {
                        for (const li of lis) {
                            const txt = li.innerText.trim();
                            if (/^\\d{1,2}\\s+[A-Za-z]{3}\\s+\\d{4}$/.test(txt)) {
                                expDate = txt;
                                break;
                            }
                        }
                    }

                    // ---- 3. Option chain table ----
                    // Target the data table (second table with class "table-bod ... tabele-auto")
                    const tables = document.querySelectorAll('table.table-bod');
                    let dataTable = null;
                    for (const t of tables) {
                        if (t.className.includes('tabele-auto') || t.className.includes('lg:table-fixed')) {
                            dataTable = t;
                            break;
                        }
                    }
                    // Fallback: any table-bod table that is not the first one (header)
                    if (!dataTable && tables.length >= 2) {
                        dataTable = tables[1];
                    }
                    // Last fallback: any table-bod at all
                    if (!dataTable && tables.length > 0) {
                        dataTable = tables[0];
                    }

                    const rows = [];
                    if (dataTable) {
                        const trs = dataTable.querySelectorAll('tr');
                        for (const tr of trs) {
                            const cells = tr.querySelectorAll('td');
                            // Data rows have exactly 7 cells
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
                    }

                    return { spot, expDate, rows, tableFound: !!dataTable };
                }""")

                browser.close()

        except Exception as exc:
            log.exception(
                "[dhan_sensex] scraping SENSEX option chain failed on attempt %d: %s",
                attempt,
                exc,
            )
            return None

        if not extracted_data:
            log.warning("[dhan_sensex] No data returned on attempt %d", attempt)
            return None

        if not extracted_data.get("tableFound"):
            log.warning("[dhan_sensex] Data table not found on attempt %d", attempt)

        spot = extracted_data.get("spot", 0.0)
        expiry_date = extracted_data.get("expiryDate") or extracted_data.get(
            "expDate", ""
        )
        rows_raw = extracted_data.get("rows", [])

        log.info(
            "[dhan_sensex] Attempt %d: spot=%s, expiry=%s, rows=%d",
            attempt,
            spot,
            expiry_date,
            len(rows_raw),
        )

        if not rows_raw:
            log.warning(
                "[dhan_sensex] Empty rows on attempt %d, will retry",
                attempt,
            )
            return None

        # Parse expiry
        actual_expiry = None
        if expiry_date:
            try:
                dt = datetime.strptime(expiry_date, "%d %b %Y")
                actual_expiry = dt.strftime("%Y-%m-%d")
            except Exception as e:
                log.warning(
                    "[dhan_sensex] failed to parse expiry '%s': %s",
                    expiry_date,
                    e,
                )

        # Parse rows into strikes
        parsed_strikes = []
        for row in rows_raw:
            strike = _parse_number(row["strike"])
            if strike <= 0:
                continue

            c_oi = _parse_int(row["c_oi"].split("(")[0])

            vol_str = row["c_vol"]
            for pattern, replacement in [
                ("L", "00000"),
                ("K", "000"),
                ("Cr", "0000000"),
            ]:
                vol_str = vol_str.replace(pattern, replacement)
            c_vol = _parse_int(vol_str)

            c_ltp = _parse_number(row["c_ltp"].split("(")[0])

            p_oi = (
                _parse_int(row["p_oi"].split(")")[-1])
                if ")" in row["p_oi"]
                else _parse_int(row["p_oi"])
            )

            vol_str = row["p_vol"]
            for pattern, replacement in [
                ("L", "00000"),
                ("K", "000"),
                ("Cr", "0000000"),
            ]:
                vol_str = vol_str.replace(pattern, replacement)
            p_vol = _parse_int(vol_str)

            p_ltp = _parse_number(row["p_ltp"].split("(")[0])

            parsed_strikes.append(
                {
                    "strike": strike,
                    "option_type": "CE",
                    "ltp": c_ltp,
                    "oi": c_oi,
                    "oi_change": 0,
                    "volume": c_vol,
                    "iv": 0.0,
                    "bid": 0.0,
                    "ask": 0.0,
                }
            )
            parsed_strikes.append(
                {
                    "strike": strike,
                    "option_type": "PE",
                    "ltp": p_ltp,
                    "oi": p_oi,
                    "oi_change": 0,
                    "volume": p_vol,
                    "iv": 0.0,
                    "bid": 0.0,
                    "ask": 0.0,
                }
            )

        if not parsed_strikes:
            log.warning(
                "[dhan_sensex] No valid strikes parsed on attempt %d",
                attempt,
            )
            return None

        parsed_strikes.sort(key=lambda r: (r["strike"], r["option_type"]))

        log.info(
            "[dhan_sensex] Successfully parsed %d strikes (%.0f CE + %.0f PE) for expiry %s",
            len(parsed_strikes),
            len(parsed_strikes) / 2,
            len(parsed_strikes) / 2,
            actual_expiry or "?",
        )

        return {
            "symbol": "SENSEX",
            "underlying_price": spot,
            "expiry": actual_expiry,
            "strikes": parsed_strikes,
            "source": self.name,
            "fetched_at": datetime.now(IST).isoformat(),
        }
