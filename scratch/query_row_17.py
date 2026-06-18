import sqlite3
import os

DB_PATH = "data/nsebot.db"
if not os.path.exists(DB_PATH):
    print("Database not found")
else:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM live_trades WHERE id=17").fetchone()
    print("ROW 17 details:")
    if row:
        for k in row.keys():
            print(f"{k}: {row[k]}")
    conn.close()
