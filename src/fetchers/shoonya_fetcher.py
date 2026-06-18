import logging
import pyotp
from NorenRestApiPy.NorenApi import NorenApi
from datetime import datetime
import os

log = logging.getLogger(__name__)

class ShoonyaFetcher:
    def __init__(self):
        self.api = NorenApi()
        self.is_logged_in = False
        self.user_id = os.environ.get("SHOONYA_USER_ID")
        self.password = os.environ.get("SHOONYA_PASSWORD")
        self.totp_key = os.environ.get("SHOONYA_TOTP_KEY")
        self.vendor_code = os.environ.get("SHOONYA_VENDOR_CODE")
        self.api_key = os.environ.get("SHOONYA_API_KEY")
        self.imei = os.environ.get("SHOONYA_IMEI")

    def login(self):
        if self.is_logged_in:
            return True
        try:
            token = pyotp.TOTP(self.totp_key).now()
            res = self.api.login(
                userid=self.user_id,
                password=self.password,
                twoFA=token,
                vendor_code=self.vendor_code,
                api_secret=self.api_key,
                imei=self.imei
            )
            if res and res.get('stat') == 'Ok':
                self.is_logged_in = True
                log.info("Shoonya login successful")
                return True
            else:
                log.error(f"Shoonya login failed: {res}")
        except Exception:
            log.exception("Shoonya login exception")
        return False

    def fetch(self, symbol: str, expiry: str = None):
        """
        Fetches option chain data and maps it to the internal NSEBOT format.
        """
        if not self.login():
            return None

        try:
            # 1. Resolve Exchange and Instrument
            # Shoonya uses NIFTY, BANKNIFTY for Indices on NSE exchange
            exch = 'NSE' if symbol in ['NIFTY', 'BANKNIFTY', 'FINNIFTY'] else 'MCX'
            
            # Search for the underlying to get LTP
            search_res = self.api.search_scrip(exchange=exch, searchtext=symbol)
            if not search_res or search_res.get('stat') != 'Ok':
                return None
            
            # Find the exact match for Index/Future
            token = search_res['values'][0]['token']
            quote = self.api.get_quotes(exchange=exch, token=token)
            underlying_price = float(quote.get('lp', 0))

            # 2. Fetch Option Chain
            # Shoonya api.get_option_chain returns strikes around ATM
            chain = self.api.get_option_chain(exchange='NFO', tradingsymbol=symbol, strikeprice=underlying_price, count=10)
            if not chain or chain.get('stat') != 'Ok':
                return None

            expiry_list = chain.get('values', [])
            # Filter by specific expiry if provided, else take the first one
            if expiry:
                target_expiry = expiry
            else:
                # Shoonya format usually matches what we expect or needs conversion
                target_expiry = expiry_list[0].get('expiry')

            strikes_data = []
            # Shoonya returns a list of scripts for the chain
            tokens = [item['token'] for item in expiry_list if item.get('expiry') == target_expiry]
            
            # Bulk fetch quotes for all tokens in the chain
            quotes = self.api.get_quotes(exchange='NFO', token=','.join(tokens))
            # Shoonya returns a list if multiple tokens, or dict if one. Standardize.
            quote_list = quotes if isinstance(quotes, list) else [quotes]

            for q in quote_list:
                # Find original item to get strike and type
                orig = next(i for i in expiry_list if i['token'] == q['token'])
                
                # Map to internal format
                strikes_data.append({
                    "strike": float(orig['strprc']),
                    "option_type": orig['optt'],
                    "ltp": float(q.get('lp', 0)),
                    "ltp_change_pct": float(q.get('pc', 0)),
                    "oi": int(q.get('oi', 0)),
                    "oi_change": int(q.get('oichg', 0)),
                    "volume": int(q.get('v', 0)),
                    "iv": float(q.get('iv', 0)) if 'iv' in q else 0.0,
                    "bid": float(q.get('bp1', 0)),
                    "ask": float(q.get('sp1', 0)),
                    "delta": 0.0, # Shoonya might not provide this directly in get_quotes
                })

            return {
                "symbol": symbol,
                "underlying_price": underlying_price,
                "expiry": target_expiry,
                "source": "shoonya",
                "strikes": strikes_data
            }

        except Exception:
            log.exception(f"Shoonya fetch failed for {symbol}")
            return None