import sqlite3
from config.settings import DB_PATH

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row

tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", [t[0] for t in tables])
print()

for tbl in [t[0] for t in tables]:
    count = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    print(f"  {tbl}: {count} rows")

print()

# Sample latest snapshots
try:
    rows = con.execute("""
        SELECT * FROM snapshots
        ORDER BY rowid DESC LIMIT 5
    """).fetchall()
    print("Latest snapshots:")
    for r in rows:
        print(" ", dict(r))
except Exception as e:
    print("snapshots table error:", e)

# Run fetcher manually
print()
print("=== Manual fetch test ===")
from src.fetchers.router import fetch_option_chain
import logging
logging.basicConfig(level=logging.INFO)
result = fetch_option_chain("NATURALGAS")
if result:
    strikes = result.get("strikes", [])
    print(f"Source: {result.get('source')}")
    print(f"Underlying: {result.get('underlying_price')}")
    print(f"Expiry: {result.get('expiry')}")
    print(f"Strikes count: {len(strikes)}")
    # Show first non-zero strike
    for s in strikes[:5]:
        print(" ", s)
else:
    print("fetch_option_chain returned None — ALL fetchers failed")

con.close()
