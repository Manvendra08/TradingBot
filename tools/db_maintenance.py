"""
DB maintenance utility.

Usage:
  python tools/db_maintenance.py             -> report stats only
  python tools/db_maintenance.py --prune 30  -> delete snapshots older than 30 days
  python tools/db_maintenance.py --purge-all -> delete all runtime data rows
  python tools/db_maintenance.py --vacuum     -> VACUUM (reclaim disk space)
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CANONICAL_SYMBOLS = [
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NATURALGAS",
    "CRUDEOIL",
    "GOLD",
    "SILVER",
    "COPPER",
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
]
_CANONICAL_SET = set(CANONICAL_SYMBOLS)
_ALIASES = {
    "NIFTY 50": "NIFTY",
    "NIFTY50": "NIFTY",
    "HDFC BANK": "HDFCBANK",
    "NATURALGASM": "NATURALGAS",
}


def _canonical_symbol(symbol: str | None) -> str:
    s = str(symbol or "").upper().strip()
    if not s:
        return ""
    if s in _ALIASES:
        return _ALIASES[s]
    first = s.split()[0]
    if first in _CANONICAL_SET:
        return first
    return s


def stats(conn):
    print("\n-- DB Stats --")
    for t in [
        "option_chain_snapshots",
        "underlying_price",
        "anomaly_alerts",
        "alert_dedup",
    ]:
        row = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
        print(f"  {t:<35} {row[0]:>10,} rows")

    row = conn.execute(
        "SELECT MIN(fetched_at), MAX(fetched_at) FROM option_chain_snapshots"
    ).fetchone()
    print(f"\n  Snapshot range: {row[0]} -> {row[1]}")
    print(f"  DB file size  : {DB_PATH.stat().st_size / 1_048_576:.2f} MB")

    print("\n-- Alert Breakdown --")
    rows = conn.execute(
        "SELECT alert_type, COUNT(*) FROM anomaly_alerts GROUP BY alert_type ORDER BY 2 DESC"
    ).fetchall()
    for r in rows:
        print(f"  {r[0]:<25} {r[1]:>8,}")

    rows = conn.execute(
        "SELECT symbol, COUNT(*) FROM option_chain_snapshots GROUP BY symbol ORDER BY 2 DESC, 1"
    ).fetchall()
    print("\n-- Snapshot Symbols --")
    for r in rows[:20]:
        print(f"  {r[0]:<25} {r[1]:>8,}")
    if len(rows) > 20:
        print(f"  ... {len(rows) - 20} more")


def prune(conn, days: int):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    for t in ["option_chain_snapshots", "underlying_price"]:
        cur = conn.execute(f"DELETE FROM {t} WHERE fetched_at < ?", (cutoff,))
        print(f"  Pruned {cur.rowcount:,} rows from {t} (older than {days} days)")
    conn.commit()


def purge_all(conn):
    tables = [
        "option_chain_snapshots",
        "underlying_price",
        "anomaly_alerts",
        "alert_dedup",
        "snapshot_baseline",
    ]
    for table in tables:
        cur = conn.execute(f"DELETE FROM {table}")
        print(f"  Purged {cur.rowcount:,} rows from {table}")

    conn.execute(
        "DELETE FROM sqlite_sequence WHERE name IN (?, ?, ?, ?, ?)",
        tuple(tables),
    )
    conn.commit()
    with sqlite3.connect(DB_PATH) as checkpoint_conn:
        checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def vacuum(conn):
    print("  Running VACUUM ...")
    conn.execute("VACUUM")
    print("  Done.")


def normalize_symbols(conn):
    tables = [
        "option_chain_snapshots",
        "underlying_price",
        "anomaly_alerts",
        "snapshot_baseline",
    ]
    for table in tables:
        rows = conn.execute(f"SELECT DISTINCT symbol FROM {table}").fetchall()
        updated = 0
        deleted = 0
        for (symbol,) in rows:
            canonical = _canonical_symbol(symbol)
            if canonical not in _CANONICAL_SET:
                cur = conn.execute(f"DELETE FROM {table} WHERE symbol=?", (symbol,))
                deleted += cur.rowcount
                continue
            if canonical != symbol:
                cur = conn.execute(
                    f"UPDATE {table} SET symbol=? WHERE symbol=?",
                    (canonical, symbol),
                )
                updated += cur.rowcount
        print(f"  Normalized {table:<24} updated={updated:,} deleted={deleted:,}")
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="NSEBOT DB Maintenance")
    parser.add_argument("--prune", type=int, metavar="DAYS",
                        help="Delete snapshots older than N days")
    parser.add_argument("--normalize-symbols", action="store_true",
                        help="Collapse symbol aliases and drop junk symbols")
    parser.add_argument("--purge-all", action="store_true",
                        help="Delete all runtime data rows from the DB")
    parser.add_argument("--vacuum", action="store_true",
                        help="VACUUM the database to reclaim disk space")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"[ERROR] DB not found at {DB_PATH}. Run main.py --now first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    stats(conn)

    if args.purge_all:
        print("\n-- Purging all runtime data --")
        purge_all(conn)
        stats(conn)

    if args.normalize_symbols:
        print("\n-- Normalizing symbols --")
        normalize_symbols(conn)
        stats(conn)

    if args.prune:
        print(f"\n-- Pruning rows older than {args.prune} days --")
        prune(conn, args.prune)
        stats(conn)

    if args.vacuum:
        print("\n-- Vacuum --")
        vacuum(conn)
        print(f"  DB file size after vacuum: {DB_PATH.stat().st_size / 1_048_576:.2f} MB")

    conn.close()


if __name__ == "__main__":
    main()
