import sqlite3
from pathlib import Path

DB_PATH = Path("data/nsebot.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

print("Checking database rows for NATURALGAS...")
rows = conn.execute(
    "SELECT symbol, fetched_at, COUNT(*) as cnt, MIN(strike) as min_strike, MAX(strike) as max_strike, MAX(underlying_price) as spot "
    "FROM option_chain_snapshots WHERE symbol='NATURALGAS' "
    "GROUP BY fetched_at ORDER BY fetched_at DESC LIMIT 5"
).fetchall()

for r in rows:
    print(f"Time: {r['fetched_at']} | Rows: {r['cnt']} | Strike Range: {r['min_strike']} - {r['max_strike']} | Spot: {r['spot']}")

conn.close()
