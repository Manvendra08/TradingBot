import requests
import ssl
from urllib3.util import create_urllib3_context

class TLSAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context(ssl_version=ssl.PROTOCOL_TLS_CLIENT)
        # Keep SSL verification on!
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)

print("Testing requests with secure custom TLS context...")
s = requests.Session()
s.mount("https://", TLSAdapter())

try:
    r = s.get("https://api.dhan.co/v2", timeout=5)
    print(f"Success! Status: {r.status_code}")
except Exception as e:
    print(f"Failed with custom TLS: {e}")
