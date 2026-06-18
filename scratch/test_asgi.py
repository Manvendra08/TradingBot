import asyncio
import sys
from pathlib import Path

# Resolve project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.basicConfig(level=logging.INFO)

from dashboard_server import app

endpoints = [
    ("/", ""),
    ("/paper", ""),
    ("/api/symbols", ""),
    ("/api/meta", "symbol=NATURALGAS"),
    ("/api/price", "symbol=NATURALGAS"),
    ("/api/oi", "symbol=NATURALGAS"),
    ("/api/pcr", "symbol=NATURALGAS"),
    ("/api/alerts", "symbol=NATURALGAS"),
    ("/api/intelligence_summary", "symbol=NATURALGAS"),
    ("/api/expiries", "symbol=NATURALGAS"),
    ("/api/runtime", ""),
    ("/api/paper_trades", "symbol=NATURALGAS"),
    ("/api/paper_summary", "symbol=NATURALGAS"),
    ("/api/paper_equity", "symbol=NATURALGAS"),
]

async def dummy_receive():
    return {"type": "http.request"}

async def dummy_send(message):
    pass

async def test_path(path, query_string):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query_string.encode("utf-8"),
        "headers": [],
    }
    try:
        print(f"Testing GET {path}?{query_string} ...", end=" ")
        await app(scope, dummy_receive, dummy_send)
        print("SUCCESS")
    except Exception as e:
        print("\nEXCEPTION RAISED:")
        import traceback
        traceback.print_exc()
        print("-" * 50)

async def main():
    for path, qs in endpoints:
        await test_path(path, qs)

if __name__ == "__main__":
    asyncio.run(main())
