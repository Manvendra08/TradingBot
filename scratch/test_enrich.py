import sqlite3
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard_server import _enrich_open_trades_with_live_pnl, _q

# Load open trades from DB
rows = _q("SELECT * FROM paper_trades WHERE status='OPEN'")
print("--- RUNNING REAL ENRICH TEST ---")
for r in rows:
    print(f"BEFORE: {dict(r)}")

_enrich_open_trades_with_live_pnl(rows)

for r in rows:
    print(f"AFTER: {r}")
