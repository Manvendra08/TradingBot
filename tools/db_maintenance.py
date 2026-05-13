"""
DB Maintenance utility — prune old snapshots, vacuum, report stats.

Usage:
  python tools/db_maintenance.py            → report stats only
  python tools/db_maintenance.py --prune 30 → delete snapshots older than 30 days
  python tools/db_maintenance.py --vacuum    → VACUUM (reclaim disk space)
"""
import argparse
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH


def stats(conn):
    print("\n── DB Stats ──────────────────────────────────────────────────")
    tables = [
        "option_chain_snapshots",
        "underlying_price",
        "anomaly_alerts",
        "alert_dedup",
    ]
    for t in tables:
        row = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
        print(f"  {t:<35} {row[0]:>10,} rows")

    # Date range of snapshots
    row = conn.execute(
        "SELECT MIN(fetched_at), MAX(fetched_at) FROM option_chain_snapshots"
    ).fetchone()
    print(f"\n  Snapshot range: {row[0]} → {row[1]}")

    # DB file size
    size_mb = DB_PATH.stat().st_size / 1_048_576
    print(f"  DB file size  : {size_mb:.2f} MB")

    # Alerts by type
    print("\n── Alert Breakdown ───────────────────────────────────────────")
    rows = conn.execute(
        "SELECT alert_type, COUNT(*) FROM anomaly_alerts GROUP BY alert_type ORDER BY 2 DESC"
    ).fetchall()
    for r in rows:
        print(f"  {r[0]:<25} {r[1]:>8,}")
    print()


def prune(conn, days: int):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    tables = ["option_chain_snapshots", "underlying_price"]
    for t in tables:
        cur = conn.execute(f"DELETE FROM {t} WHERE fetched_at < ?", (cutoff,))
        print(f"  Pruned {cur.rowcount:,} rows from {t} (older than {days} days)")
    conn.commit()


def vacuum(conn):
    print("  Running VACUUM ...")
    conn.execute("VACUUM")
    print("  Done.")


def main():
    parser = argparse.ArgumentParser(description="NSEBOT DB Maintenance")
    parser.add_argument("--prune",  type=int, metavar="DAYS",
                        help="Delete snapshots older than N days")
    parser.add_argument("--vacuum", action="store_true",
                        help="VACUUM the database to reclaim disk space")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"[ERROR] DB not found at {DB_PATH}. Run main.py --now first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    stats(conn)

    if args.prune:
        print(f"\n── Pruning rows older than {args.prune} days ─────────────────")
        prune(conn, args.prune)
        stats(conn)   # show updated stats

    if args.vacuum:
        print("\n── Vacuum ────────────────────────────────────────────────────")
        vacuum(conn)
        size_mb = DB_PATH.stat().st_size / 1_048_576
        print(f"  DB file size after vacuum: {size_mb:.2f} MB")

    conn.close()


if __name__ == "__main__":
    main()
