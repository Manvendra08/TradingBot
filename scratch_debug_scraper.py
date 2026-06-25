"""Debug Dhan SENSEX scraper - test the actual data extraction."""

import json
import sys

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

url = "https://dhan.co/indices/bse-sensex-option-chain/"

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, timeout=30000)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        viewport={"width": 1280, "height": 900},
    )
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)

    # Same logic as dhan_sensex_fetcher
    extracted_data = page.evaluate("""() => {
        let spot = 0.0;
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

        let expDate = "";
        const lis = document.querySelectorAll('li');
        for (const li of lis) {
            const txt = li.innerText.trim();
            if (/^\\d{1,2}\\s+[A-Za-z]{3}\\s+\\d{4}$/.test(txt)) {
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
        return { spot, expDate, rows, trCount: trs.length };
    }""")

    print(f"Spot: {extracted_data['spot']}")
    print(f"Expiry: {extracted_data['expDate']}")
    print(f"Total TRs: {extracted_data['trCount']}")
    print(f"Rows with 7 cells: {len(extracted_data['rows'])}")

    if extracted_data["rows"]:
        # Print first row raw
        r0 = extracted_data["rows"][0]
        print("\n=== First row raw fields ===")
        for k, v in r0.items():
            print(f"  {k}: {repr(v)}")

        # Print a middle row raw
        mid = len(extracted_data["rows"]) // 2
        rm = extracted_data["rows"][mid]
        print(f"\n=== Middle row (index {mid}) raw fields ===")
        for k, v in rm.items():
            print(f"  {k}: {repr(v)}")

        # Check parse results
        import re

        def parse_num(t):
            if not t:
                return 0.0
            cleaned = re.sub(r"[^\d.\-]", "", t.strip())
            if not cleaned or cleaned in ("-", "."):
                return 0.0
            try:
                return float(cleaned)
            except:
                return 0.0

        def parse_int(t):
            return int(round(parse_num(t)))

        print("\n=== Parsed first row ===")
        r = extracted_data["rows"][0]
        strike = parse_num(r["strike"])
        c_oi = parse_int(r["c_oi"].split("(")[0])
        c_vol_str = (
            r["c_vol"]
            .replace("L", "00000")
            .replace("K", "000")
            .replace("Cr", "0000000")
        )
        c_vol = parse_int(c_vol_str)
        c_ltp = parse_num(r["c_ltp"].split("(")[0])
        p_oi = (
            parse_int(r["p_oi"].split(")")[-1])
            if ")" in r["p_oi"]
            else parse_int(r["p_oi"])
        )
        p_vol_str = (
            r["p_vol"]
            .replace("L", "00000")
            .replace("K", "000")
            .replace("Cr", "0000000")
        )
        p_vol = parse_int(p_vol_str)
        p_ltp = parse_num(r["p_ltp"].split("(")[0])
        print(f"  strike={strike} c_oi={c_oi} c_vol={c_vol} c_ltp={c_ltp}")
        print(f"  p_ltp={p_ltp} p_vol={p_vol} p_oi={p_oi}")
        print(f"  strike > 0: {strike > 0}")

        # Count valid rows
        valid = 0
        for r in extracted_data["rows"]:
            s = parse_num(r["strike"])
            if s > 0:
                valid += 1
        print(f"\nRows with strike > 0: {valid}/{len(extracted_data['rows'])}")

    else:
        # Show what's in the table
        print("\nNo rows with 7 cells found. Checking tables...")
        tables = page.evaluate("""() => {
            const tables = document.querySelectorAll('table');
            return Array.from(tables).map(t => ({
                classes: t.className,
                rows: t.querySelectorAll('tr').length,
                firstRowCells: t.querySelector('tr') ? t.querySelector('tr').querySelectorAll('td,th').length : 0
            }));
        }""")
        print(json.dumps(tables, indent=2))

    browser.close()
