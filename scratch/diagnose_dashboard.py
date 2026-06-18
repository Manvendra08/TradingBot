import sys
from pathlib import Path

# Resolve project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.basicConfig(level=logging.INFO)

from fastapi.testclient import TestClient
from dashboard_server import app

client = TestClient(app)

endpoints = [
    ("/", {}),
    ("/paper", {}),
    ("/api/symbols", {}),
    ("/api/meta", {"symbol": "NATURALGAS"}),
    ("/api/price", {"symbol": "NATURALGAS"}),
    ("/api/oi", {"symbol": "NATURALGAS"}),
    ("/api/pcr", {"symbol": "NATURALGAS"}),
    ("/api/alerts", {"symbol": "NATURALGAS"}),
    ("/api/intelligence_summary", {"symbol": "NATURALGAS"}),
    ("/api/expiries", {"symbol": "NATURALGAS"}),
    ("/api/runtime", {}),
    ("/api/paper_trades", {"symbol": "NATURALGAS"}),
    ("/api/paper_summary", {"symbol": "NATURALGAS"}),
    ("/api/paper_equity", {"symbol": "NATURALGAS"}),
]

print("=== Testing FastAPI endpoints ===")
for path, params in endpoints:
    try:
        print(f"Testing GET {path} with params {params} ...", end=" ")
        resp = client.get(path, params=params)
        print(f"STATUS {resp.status_code}")
        if resp.status_code >= 500:
            print("Response:", resp.text)
    except Exception as e:
        print("EXCEPTION RAISED:")
        import traceback
        traceback.print_exc()
        print("-" * 50)
