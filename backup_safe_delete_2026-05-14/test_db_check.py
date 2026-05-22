#!/usr/bin/env python3
"""Check database structure and alert records"""
import sqlite3
from config.settings import DB_PATH

db = sqlite3.connect(str(DB_PATH))
cursor = db.cursor()

# Get all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print("✓ Tables:", [t[0] for t in tables])

# Get recent alerts
cursor.execute("SELECT COUNT(*) FROM anomaly_alerts")
alert_count = cursor.fetchone()[0]
print(f"✓ Total alerts in DB: {alert_count}")

# Get alerts
cursor.execute("SELECT alert_type, symbol, fired_at FROM anomaly_alerts ORDER BY fired_at DESC LIMIT 5")
recent = cursor.fetchall()
if recent:
    print("\n✓ Recent alerts:")
    for alert_type, symbol, ts in recent:
        print(f"  - {alert_type} on {symbol} at {ts}")
else:
    print("\n✓ No alerts yet (expected on fresh run)")

# Check snapshots
cursor.execute("SELECT COUNT(*) FROM option_chain_snapshots")
snapshot_count = cursor.fetchone()[0]
print(f"\n✓ Total snapshots in DB: {snapshot_count}")

db.close()
print("\n✅ Database check complete")
