import sqlite3

conn = sqlite3.connect("data/nsebot.db")
conn.row_factory = sqlite3.Row

# Get latest fetched_at for CRUDEOIL
latest = conn.execute("SELECT MAX(fetched_at) FROM option_chain_snapshots WHERE symbol='CRUDEOIL'").fetchone()[0]
print(f"Latest fetched_at: {latest}")

# Print snapshots
snaps = conn.execute("SELECT strike, option_type, ltp, underlying_price FROM option_chain_snapshots WHERE symbol='CRUDEOIL' AND fetched_at=?", (latest,)).fetchall()
print(f"Total snapshots: {len(snaps)}")
for r in snaps:
    print(f"  Strike: {r['strike']}, OptionType: {r['option_type']}, LTP: {r['ltp']}, Underlying: {r['underlying_price']}")

conn.close()
