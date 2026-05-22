import sqlite3
from pathlib import Path

db_path = Path("data/nsebot.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("--- Source and fields for NATURALGAS snapshots ---")
for r in conn.execute("SELECT DISTINCT fetched_at, fetcher_source, count(*) FROM option_chain_snapshots WHERE symbol='NATURALGAS' GROUP BY fetched_at, fetcher_source"):
    print(dict(r))

print("\n--- Example row for NATURALGAS ---")
for r in conn.execute("SELECT * FROM option_chain_snapshots WHERE symbol='NATURALGAS' LIMIT 2"):
    print(dict(r))

conn.close()
