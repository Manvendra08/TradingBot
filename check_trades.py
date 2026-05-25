import sqlite3
conn = sqlite3.connect("data/nsebot.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, symbol, option_type, verdict_label, entry_premium, exit_premium, lots, pnl_rupees, status "
    "FROM paper_trades WHERE status != 'OPEN' ORDER BY id"
).fetchall()
for r in rows:
    r = dict(r)
    print(r["id"], r["symbol"], r["option_type"], repr(r["verdict_label"]),
          "ep=", r["entry_premium"], "xp=", r["exit_premium"], "pnl=", r["pnl_rupees"])
conn.close()
