import sqlite3
from config.settings import DB_PATH

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

print("--- RECENT NATURALGAS PAPER TRADES ---")
trades = conn.execute("SELECT * FROM paper_trades WHERE symbol LIKE 'NATURALGAS%' ORDER BY opened_at DESC LIMIT 5").fetchall()
for t in trades:
    print(dict(t))

print("\n--- RECENT SCAN SUMMARIES FOR NATURALGAS ---")
scans = conn.execute("SELECT * FROM scan_summaries WHERE symbol LIKE 'NATURALGAS%' ORDER BY fetched_at DESC LIMIT 5").fetchall()
for s in scans:
    print(dict(s))

print("\n--- RECENT ALERTS FOR NATURALGAS ---")
alerts = conn.execute("SELECT * FROM anomaly_alerts WHERE symbol LIKE 'NATURALGAS%' ORDER BY fired_at DESC LIMIT 10").fetchall()
for a in alerts:
    print(dict(a))

conn.close()
