import sys
import logging
logging.basicConfig(level=logging.INFO)

from src.models.schema import get_broker_config
from kiteconnect import KiteConnect

config = get_broker_config()
if not config or not config.get("api_key") or not config.get("access_token"):
    print("Error: Kite api_key or access_token not configured in DB.")
    sys.exit(1)

print(f"Loaded credentials from DB: api_key={config['api_key']}, access_token={config['access_token'][:5]}...")

kite = KiteConnect(api_key=config["api_key"])
kite.set_access_token(config["access_token"])

# First, test without mounting any TLS adapter
print("\n--- Test 1: Standard connection (no TLS adapter adjustment) ---")
try:
    margins = kite.margins()
    print("Margins fetched successfully:", margins.get("equity", {}).get("net"))
except Exception as e:
    print("Standard connection failed:", e)

# Mount resilient TLS adapter
print("\n--- Test 2: Mounting ResilientTLSAdapter ---")
try:
    from src.utils.tls_adapter import mount_resilient_tls
    mount_resilient_tls(kite.reqsession)
    margins = kite.margins()
    print("Margins fetched successfully with ResilientTLSAdapter:", margins.get("equity", {}).get("net"))
except Exception as e:
    print("ResilientTLSAdapter connection failed:", e)

# Force Connection: close
print("\n--- Test 3: ResilientTLSAdapter + Connection: close ---")
try:
    kite.reqsession.headers["Connection"] = "close"
    margins = kite.margins()
    print("Margins fetched successfully with Connection: close:", margins.get("equity", {}).get("net"))
except Exception as e:
    print("Connection: close failed:", e)
