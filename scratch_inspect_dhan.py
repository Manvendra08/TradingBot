"""Inspect Dhan SENSEX option chain page HTML structure."""

import json

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, timeout=30000)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
    )
    page = context.new_page()
    page.goto(
        "https://dhan.co/indices/bse-sensex-option-chain/",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    page.wait_for_timeout(5000)

    # Check expiry tabs
    expiries = page.evaluate("""() => {
        const lis = document.querySelectorAll('li');
        const results = [];
        for (const li of lis) {
            const txt = li.innerText.trim();
            if (/^\\d{1,2}\\s+[A-Za-z]{3}\\s+\\d{4}$/.test(txt)) {
                results.push({text: txt, html: li.innerHTML.substring(0, 200), className: li.className});
            }
        }
        return results;
    }""")
    print("=== EXPIRY TABS ===")
    print(json.dumps(expiries, indent=2)[:1000])

    # Check table structure
    table_info = page.evaluate("""() => {
        const tables = document.querySelectorAll('table');
        const info = [];
        for (const t of tables) {
            const rows = t.querySelectorAll('tr');
            const cols = rows.length > 0 ? rows[0].querySelectorAll('th,td').length : 0;
            info.push({
                id: t.id,
                className: t.className,
                rows: rows.length,
                cols: cols,
                headerText: rows.length > 0 ? Array.from(rows[0].querySelectorAll('th,td')).map(c => c.innerText.trim()).join(' | ') : ''
            });
        }
        return info;
    }""")
    print("\n=== TABLES ===")
    print(json.dumps(table_info, indent=2)[:2000])

    # Check all rows with cell counts
    rows_info = page.evaluate("""() => {
        const trs = document.querySelectorAll('tr');
        const results = [];
        for (const tr of trs) {
            const cells = tr.querySelectorAll('td');
            const ths = tr.querySelectorAll('th');
            results.push({
                cellCount: cells.length,
                thCount: ths.length,
                sampleCell0: cells.length > 0 ? cells[0].innerText.trim().substring(0, 50) : '',
                sampleCell1: cells.length > 1 ? cells[1].innerText.trim().substring(0, 50) : '',
                sampleCell2: cells.length > 2 ? cells[2].innerText.trim().substring(0, 50) : '',
            });
        }
        return results;
    }""")
    print("\n=== UNIQUE CELL COUNTS ===")
    from collections import Counter

    counts = Counter(r["cellCount"] for r in rows_info)
    print(f"Distribution: {dict(counts)}")

    # For each cell count, show the first 3 data rows
    for cell_count in sorted(set(r["cellCount"] for r in rows_info)):
        samples = [r for r in rows_info if r["cellCount"] == cell_count][:5]
        non_header = [r for r in samples if r["thCount"] == 0]
        print(f"\n--- Rows with {cell_count} cells (non-header) ---")
        for s in non_header[:3]:
            print(
                f"  [{', '.join(s.get(f'sampleCell{i}', '') for i in range(min(4, cell_count)))}]"
            )

    # Check for option chain table specifically
    oc_table = page.evaluate("""() => {
        // Look for option chain table - try common patterns
        const selectors = [
            'table.table', 'table.options-chain', 'table.dataTable',
            'div[class*="option"] table', 'div[class*="chain"] table',
            'div.option-chain-container table', 'table option-chain-table'
        ];
        for (const sel of selectors) {
            const t = document.querySelector(sel);
            if (t) return {selector: sel, rows: t.querySelectorAll('tr').length};
        }
        return null;
    }""")
    print(f"\n=== OPTION CHAIN TABLE SEARCH: {oc_table} ===")

    # Check if table is inside a div with option chain data
    div_info = page.evaluate("""() => {
        const divs = document.querySelectorAll('div[class*="option"], div[class*="chain"], div[class*="strike"]');
        const results = [];
        for (const d of divs) {
            results.push({
                className: d.className.substring(0, 100),
                id: d.id,
                tables: d.querySelectorAll('table').length
            });
        }
        return results.slice(0, 10);
    }""")
    print(f"\n=== OPTION-RELATED DIVS ===")
    print(json.dumps(div_info, indent=2)[:2000])

    # Try to get full HTML of the option chain section
    body_html = page.evaluate("""() => {
        return document.body.innerHTML.substring(0, 5000);
    }""")
    # Write HTML to file for analysis
    with open("dhan_page_html.txt", "w", encoding="utf-8") as f:
        f.write(page.content())
    print("\n=== Full page HTML saved to dhan_page_html.txt ===")
    print(f"Page title: {page.title()}")
    print(f"Page URL: {page.url}")

    browser.close()
