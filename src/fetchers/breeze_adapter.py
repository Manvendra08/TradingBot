from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from datetime import datetime

import pyotp
from breeze_connect import BreezeConnect
from playwright.sync_api import sync_playwright

# Replace this import with your actual framework base class path if necessary
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

class BreezeAdapter(BaseFetcher):
    name = "breeze"

    def __init__(self, api_key: str, api_secret: str, user_id: str, password: str, totp_secret: str):
        """
        Initializes a modular plug-and-play execution adapter for ICICIDirect Breeze.
        """
        super().__init__()
        self.api_key = api_key
        self.api_secret = api_secret
        self.user_id = user_id
        self.password = password
        self.totp_secret = totp_secret
        
        # Instantiate the official underlying SDK wrapper client
        self.breeze = BreezeConnect(api_key=self.api_key)
        self.session_token: str | None = None
        
        # Thread safety control primitives
        self._lock = threading.Lock()
        
        # Trigger automated headless authentication immediately on startup
        self.authenticate()

    def authenticate(self) -> bool:
        """
        Runs a headless automation workflow to process 2FA forms, bypasses manual
        logins, and registers a valid 24-hour runtime session context.
        """
        with self._lock:
            log.info("[breeze] Initializing automated headless login routine...")
            encoded_key = urllib.parse.quote(self.api_key)
            login_url = f"https://icicidirect.com{encoded_key}"

            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context()
                    page = context.new_page()

                    page.goto(login_url)
                    page.wait_for_load_state("networkidle")

                    # Input authentication parameters into forms
                    page.locator("input[name='txtUserId']").fill(self.user_id)
                    page.locator("input[name='txtPassword']").fill(self.password)
                    
                    # Generate dynamic time-based MFA pin
                    totp_pin = pyotp.TOTP(self.totp_secret).now()
                    page.locator("input[name='txtOTP']").fill(totp_pin)
                    
                    # Submit and listen for standard loopback callback redirects
                    with page.expect_navigation(timeout=15000):
                        page.locator("input[type='submit']").click()

                    final_url = page.url
                    browser.close()

                # Extract token from redirect URL string parameters
                parsed_url = urllib.parse.urlparse(final_url)
                query_params = urllib.parse.parse_qs(parsed_url.query)

                if "apisession" in query_params:
                    self.session_token = query_params["apisession"][0]
                    
                    # Feed the extracted session key directly into the Breeze SDK engine
                    self.breeze.generate_session(
                        api_secret=self.api_secret, 
                        session_token=self.session_token
                    )
                    log.info("[breeze] Plug-and-play adapter authenticated successfully.")
                    return True
                else:
                    log.error("[breeze] Authentication token absent from redirect string.")
                    return False

            except Exception as e:
                log.error("[breeze] Automated connection process failed: %s", e)
                return False

    def get_ltp(self, symbol: str, exchange: str = "NFO") -> float | None:
        """
        Plug-and-play uniform function to query the current Last Traded Price (LTP).
        """
        try:
            # ICICIDirect expects parameters mapping to generic or specific derivative products
            response = self.breeze.get_quotes(
                stock_code=symbol,
                exchange_code=exchange,
                product_type="futures" if exchange == "NFO" else "cash"
            )
            if response and response.get("Status") == 200:
                data_list = response.get("Success", [])
                if data_list:
                    return float(data_list[0].get("ltp", 0.0))
            return None
        except Exception as e:
            log.error("[breeze] Failed to retrieve spot quote for symbol %s: %s", symbol, e)
            return None

    def place_market_order(self, symbol: str, exchange: str, action: str, quantity: int) -> str | None:
        """
        Plug-and-play uniform function to place a market order.
        action: 'BUY' or 'SELL'
        """
        try:
            response = self.breeze.place_order(
                stock_code=symbol,
                exchange_code=exchange,
                action=action.lower(),
                order_type="market",
                quantity=str(quantity),
                price="0",
                validity="day",
                product="futures" if exchange == "NFO" else "cash"
            )
            if response and response.get("Status") == 200:
                order_id = response.get("Success", {}).get("order_id")
                log.info("[breeze] Order executed successfully. ID: %s", order_id)
                return order_id
            return None
        except Exception as e:
            log.error("[breeze] Market order transmission rejected: %s", e)
            return None
