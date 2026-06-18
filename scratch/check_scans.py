import sqlite3

conn = sqlite3.connect("data/nsebot.db")
conn.row_factory = sqlite3.Row
count = conn.execute("SELECT COUNT(*) AS c FROM scan_summaries").fetchone()["c"]
print("Total scan summaries:", count)
if count > 0:
    rows = conn.execute("SELECT id, symbol, fetched_at, total_ce_oi, total_pe_oi FROM scan_summaries ORDER BY fetched_at DESC LIMIT 10").fetchall()
    for r in rows:
        print(dict(r))
conn.close()
