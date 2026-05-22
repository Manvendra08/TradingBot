from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import re

def main():
    print("Launching playwright...")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900}
        )
        page = ctx.new_page()
        print("Navigating...")
        page.goto("https://dhan.co/commodity/natural-gas-option-chain/", wait_until="domcontentloaded")
        
        print("Waiting 10 seconds for dynamic content to render...")
        page.wait_for_timeout(10000)
        
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        
        # Let's search for elements containing the spot price. We know from screenshot the spot price was around 289.80
        # Let's search for text matching a number around 280-300
        print("\nSearching for spot price pattern (200.00 to 300.00) in text...")
        for element in soup.find_all(text=True):
            text = element.strip()
            if re.match(r"^\d+\.\d+$", text):
                val = float(text)
                if 200.0 <= val <= 350.0:
                    parent = element.parent
                    print(f"Found price candidate: '{text}' inside tag: <{parent.name} class='{parent.get('class', [])}' id='{parent.get('id', '')}'>")
                    
        browser.close()

if __name__ == "__main__":
    main()
