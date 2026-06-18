import sqlite3
import os

DB_PATH = "data/nsebot.db"
if not os.path.exists(DB_PATH):
    print("Database not found")
else:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    print("OPEN LIVE TRADES:")
    rows_trades = conn.execute("SELECT id, opened_at, symbol, option_type, strike, status, trade_status, pnl_rupees, side, lots FROM live_trades WHERE status='OPEN'").fetchall()
    for r in rows_trades:
        print(dict(r))
    conn.close()
