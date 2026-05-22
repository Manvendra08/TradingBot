import re
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def main():
    print("Launching synchronous playwright...")
    with sync_playwright() as pw:
        browser = None
        for channel in ["chrome", "msedge", None]:
            try:
                browser = pw.chromium.launch(
                    headless=True,
                    channel=channel,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                print(f"Successfully launched browser with channel: {channel}")
                break
            except Exception as e:
                print(f"Failed to launch with channel {channel}: {e}")
                
        if not browser:
            print("Could not launch browser with any channel.")
            return

        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900}
        )
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        print("Navigating to Dhan Natural Gas public option chain page...")
        page.goto("https://dhan.co/commodity/natural-gas-option-chain/", wait_until="domcontentloaded", timeout=30000)
        
        print("Waiting 10 seconds for dynamic option chain table to fully render...")
        page.wait_for_timeout(10000)
        
        try:
            tbl_el = page.query_selector("table")
            if tbl_el:
                html = tbl_el.inner_html()
                with open("scratch/dhan_table.html", "w", encoding="utf-8") as f:
                    f.write(html)
                print("Successfully saved table inner HTML to scratch/dhan_table.html")
            else:
                print("Table not found in DOM!")
        except Exception as e:
            print(f"Error querying table: {e}")
            
        page.close()
        ctx.close()
        browser.close()

if __name__ == "__main__":
    main()
