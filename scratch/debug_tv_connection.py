import websocket
import ssl
import json

url = "wss://data.tradingview.com/socket.io/websocket"

print("--- Test 1: JSON headers like tvDatafeed ---")
try:
    headers = json.dumps({"Origin": "https://data.tradingview.com"})
    ws = websocket.create_connection(url, headers=headers, timeout=5)
    print("Success with JSON headers!")
    ws.close()
except Exception as e:
    print(f"Failed with: {e}")

print("--- Test 2: Standard header (dict) ---")
try:
    # Notice websocket-client parameter is 'header' (singular) for custom headers,
    # or does it support headers? Let's check signature.
    # Actually, websocket-client create_connection signature accepts header as dict or list of strings.
    ws = websocket.create_connection(url, header={"Origin": "https://data.tradingview.com"}, timeout=5)
    print("Success with dict header parameter!")
    ws.close()
except Exception as e:
    print(f"Failed with: {e}")

print("--- Test 3: Standard header with User-Agent ---")
try:
    headers_dict = {
        "Origin": "https://data.tradingview.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    ws = websocket.create_connection(url, header=headers_dict, timeout=5)
    print("Success with User-Agent in header!")
    ws.close()
except Exception as e:
    print(f"Failed with: {e}")
