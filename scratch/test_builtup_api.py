import urllib.request
import json

payload = {
    "Data": {
        "Exch": "MCX",
        "Seg": "M",
        "Inst": "FUTCOM",
        "Timeinterval": "15",
        "Secid": 504265,
    }
}
req = urllib.request.Request(
    "https://openweb-ticks.dhan.co/builtup",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    },
    method="POST"
)
try:
    with urllib.request.urlopen(req, timeout=10) as res:
        data = json.loads(res.read().decode("utf-8"))
        print("Status code:", res.status)
        print("Data keys:", data.keys() if isinstance(data, dict) else type(data))
        if isinstance(data, dict) and "data" in data:
            rows = data["data"]
            print("Number of rows:", len(rows))
            if rows:
                print("First row:", rows[0])
                print("Last row:", rows[-1])
except Exception as e:
    print("Error:", e)
