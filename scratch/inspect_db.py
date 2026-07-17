import sys
sys.path.append('.')
from config.settings import DB_PATH
import sqlite3

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Get rows from eia_consensus table
rows = conn.execute("SELECT * FROM eia_consensus ORDER BY report_date DESC LIMIT 5").fetchall()
print("EIA Consensus rows:")
for r in rows:
    print(dict(r))
