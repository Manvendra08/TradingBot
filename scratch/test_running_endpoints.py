import requests

base_url = "http://localhost:8080"
endpoints = [
    ("/api/symbols", {}),
    ("/api/meta", {"symbol": "NATURALGAS"}),
    ("/api/meta", {"symbol": "CRUDEOIL"}),
    ("/api/meta", {"symbol": "NIFTY"}),
    ("/api/meta", {"symbol": "BANKNIFTY"}),
    ("/api/price", {"symbol": "NATURALGAS"}),
    ("/api/oi", {"symbol": "NATURALGAS"}),
    ("/api/pcr", {"symbol": "NATURALGAS"}),
    ("/api/alerts", {"symbol": "NATURALGAS"}),
    ("/api/intelligence_summary", {"symbol": "NATURALGAS"}),
    ("/api/intelligence_summary", {"symbol": "CRUDEOIL"}),
    ("/api/intelligence_summary", {"symbol": "NIFTY"}),
    ("/api/intelligence_summary", {"symbol": "BANKNIFTY"}),
    ("/api/expiries", {"symbol": "NATURALGAS"}),
    ("/api/runtime", {}),
    ("/api/paper_trades", {"symbol": "NATURALGAS"}),
    ("/api/paper_trades", {}),
    ("/api/paper_summary", {"symbol": "NATURALGAS"}),
    ("/api/paper_summary", {}),
    ("/api/paper_equity", {"symbol": "NATURALGAS"}),
    ("/api/paper_equity", {}),
]

print("=== Querying running server ===")
for path, params in endpoints:
    url = base_url + path
    try:
        r = requests.get(url, params=params, timeout=10)
        print(f"GET {path} {params} -> STATUS {r.status_code}")
        if r.status_code != 200:
            print("Response:", r.text[:500])
    except Exception as e:
        print(f"GET {path} {params} -> EXCEPTION: {e}")
