"""Inspect Dhan SENSEX page for available expiry tabs"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__) or ".")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, timeout=30000)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page.goto(
        "https://dhan.co/indices/bse-sensex-option-chain/",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    page.wait_for_timeout(5000)

    # Get all expiry tabs
    tabs_info = page.evaluate("""() => {
        const lis = document.querySelectorAll('li');
        const results = [];
        for (const li of lis) {
            const txt = li.innerText.trim().replace(/\\s+/g, ' ');
            if (/^\\d{1,2}\\s+[A-Za-z]{3}\\s+\\d{4}$/.test(txt)) {
                results.push({
                    text: txt,
                    isActive: li.className && li.className.includes('EF9309'),
                    className: li.className
                });
            }
        }
        return results;
    }""")

    print(f"Found {len(tabs_info)} expiry tabs:")
    for t in tabs_info:
        print(
            f"  {'[ACTIVE]' if t['isActive'] else '         '} {t['text']}  class={t['className']}"
        )

    # Get spot price too
    spot = page.evaluate("""() => {
        const tds = document.querySelectorAll('tr td');
        for (const td of tds) {
            if (td.parentElement.children.length === 1) {
                const txt = td.innerText.trim();
                const val = parseFloat(txt.replace(/[^\\d.]/g, ''));
                if (val > 0) return val;
            }
        }
        return null;
    }""")
    print(f"\nSpot price: {spot}")

    # Get the table data count
    rows_count = page.evaluate("""() => {
        const table = document.querySelector('table.table-bod');
        if (!table) return 0;
        return table.querySelectorAll('tr').length;
    }""")
    print(f"Data rows in table: {rows_count}")

    browser.close()
    print("\nDone.")
