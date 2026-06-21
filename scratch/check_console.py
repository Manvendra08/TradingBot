import asyncio
import sys
from playwright.async_api import async_playwright

# Set stdout encoding to utf-8 just in case
sys.stdout.reconfigure(encoding='utf-8')

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        console_logs = []
        page_errors = []
        
        # Listen for console events
        page.on("console", lambda msg: console_logs.append(f"CONSOLE {msg.type}: {msg.text}"))
        page.on("pageerror", lambda exc: page_errors.append(f"PAGE ERROR: {exc}"))
        
        print("Navigating to http://localhost:8080/paper ...")
        await page.goto("http://localhost:8080/paper")
        
        # Wait for some time to allow API calls to complete
        await asyncio.sleep(5)
        
        # Check the open-body html content
        open_body = await page.eval_on_selector("#open-body", "el => el.innerHTML")
        
        with open("scratch/browser_debug.txt", "w", encoding="utf-8") as f:
            f.write("=== CONSOLE LOGS ===\n")
            for log in console_logs:
                f.write(log + "\n")
            f.write("\n=== PAGE ERRORS ===\n")
            for err in page_errors:
                f.write(err + "\n")
            f.write("\n=== HTML OF #OPEN-BODY ===\n")
            f.write(open_body + "\n")
            
        print("Successfully wrote logs and HTML to scratch/browser_debug.txt")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
