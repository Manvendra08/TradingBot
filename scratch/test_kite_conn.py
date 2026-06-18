import sys
import os
import requests
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from kiteconnect import KiteConnect

print("Testing KiteConnect generate_session with dummy keys...")
try:
    kite = KiteConnect(api_key="dummy_api_key")
    # Let's try calling generate_session. It will raise an exception.
    # We want to see if the exception is SSL-related or API-related (e.g. 403 Forbidden / Invalid API key).
    res = kite.generate_session("dummy_request_token", api_secret="dummy_api_secret")
    print(f"Success! (Unexpected): {res}")
except Exception as e:
    print(f"KiteConnect failed. Exception type: {type(e)}")
    print(f"Exception message: {e}")
    import traceback
    traceback.print_exc()

print("\nTesting TLSAdapter with KiteConnect...")
try:
    class TLSAdapter(HTTPAdapter):
        def __init__(self, *args, **kwargs):
            self.ssl_context = ssl.create_default_context()
            self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            super().__init__(*args, **kwargs)
        
        def init_poolmanager(self, *args, **kwargs):
            kwargs['ssl_context'] = self.ssl_context
            return super().init_poolmanager(*args, **kwargs)
        
        def proxy_manager_for(self, *args, **kwargs):
            kwargs['ssl_context'] = self.ssl_context
            return super().proxy_manager_for(*args, **kwargs)

    kite = KiteConnect(api_key="dummy_api_key")
    retries = Retry(
        total=3,
        backoff_factor=0.2,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = TLSAdapter(max_retries=retries)
    kite.reqsession.mount("https://", adapter)
    
    res = kite.generate_session("dummy_request_token", api_secret="dummy_api_secret")
    print(f"Success! (Unexpected): {res}")
except Exception as e:
    print(f"KiteConnect with TLSAdapter failed. Exception type: {type(e)}")
    print(f"Exception message: {e}")
    import traceback
    traceback.print_exc()

