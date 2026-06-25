import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Load .env
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("paytm_token")

def main():
    api_key = os.environ.get("PAYTM_API_KEY")
    api_secret = os.environ.get("PAYTM_API_SECRET")
    
    if not api_key or not api_secret:
        log.error("PAYTM_API_KEY and PAYTM_API_SECRET must be set in your .env file.")
        print("\nPlease add the following to your .env file first:")
        print("PAYTM_API_KEY=your_paytm_api_key")
        print("PAYTM_API_SECRET=your_paytm_api_secret")
        return
        
    print("=== Paytm Money JWT Token Generator ===")
    print(f"Using API Key: {api_key}")
    
    if len(sys.argv) > 1:
        request_token = sys.argv[1]
    else:
        print("\nTo obtain a request token:")
        print(f"1. Open your browser and navigate to: https://login.paytmmoney.com/merchant-login?apiKey={api_key}")
        print("2. Log in with your credentials.")
        print("3. You will be redirected to your redirect URL. Copy the 'request_token' parameter from the URL query string.")
        try:
            request_token = input("\nEnter the copied request_token: ").strip()
        except KeyboardInterrupt:
            print("\nCancelled.")
            return

    if not request_token:
        log.error("Request token cannot be empty.")
        return

    from src.fetchers.paytm_fetcher import PaytmFetcher
    fetcher = PaytmFetcher()
    log.info("Requesting JWT token from Paytm Money...")
    success = fetcher._refresh_token(request_token)
    
    if success and fetcher._jwt_token:
        print("\n" + "="*50)
        print("SUCCESS! Paytm Money JWT Token generated:")
        print("="*50)
        print(fetcher._jwt_token)
        print("="*50)
        
        # Automatically update .env file
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            try:
                content = env_path.read_text(encoding="utf-8")
                lines = content.splitlines()
                updated = False
                for idx, line in enumerate(lines):
                    if line.strip().startswith("PAYTM_JWT_TOKEN="):
                        lines[idx] = f"PAYTM_JWT_TOKEN={fetcher._jwt_token}"
                        updated = True
                        break
                if not updated:
                    lines.append(f"PAYTM_JWT_TOKEN={fetcher._jwt_token}")
                env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                log.info("Automatically updated PAYTM_JWT_TOKEN in .env file!")
            except Exception as e:
                log.error("Failed to automatically update .env file: %s", e)
        else:
            print("\nAdd this line to your .env file:")
            print(f"PAYTM_JWT_TOKEN={fetcher._jwt_token}")
            print("="*50)
    else:
        log.error("Failed to generate JWT token. Please check your credentials and make sure the request token is fresh/not expired.")

if __name__ == "__main__":
    main()
