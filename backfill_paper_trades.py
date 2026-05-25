"""
One-shot backfill script for paper_trades:
  1. Sets entry_premium / sl_premium / target_premium on OPEN trades
     using the closest snapshot at or before opened_at.
  2. Recalculates pnl_rupees for all CLOSED trades where it is 0.
Run once: python backfill_paper_trades.py
"""
import sqlite3

LOT_SIZES = {
    "NIFTY": 25,
    "BANKNIFTY": 15,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    "NATURALGAS": 1250,
    "CRUDEOIL": 100,
    "GOLD": 100,
    "SILVER": 30,
}

DB_PATH = "data/nsebot.db"
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# ── 1. Backfill entry_premium for OPEN trades ─────────────────────────────
open_trades = conn.execute(
    "SELECT id, symbol, option_type, strike, opened_at FROM paper_trades WHERE status='OPEN'"
).fetchall()

for t in open_trades:
    # Try snapshot closest to (but not after) opened_at
    snap = conn.execute(
        """SELECT ltp FROM option_chain_snapshots
           WHERE symbol=? AND strike=? AND option_type=? AND fetched_at <= ?
           ORDER BY fetched_at DESC LIMIT 1""",
        (t["symbol"], t["strike"], t["option_type"], t["opened_at"]),
    ).fetchone()

    if not snap:
        # Fallback: earliest snapshot for this strike regardless of time
        snap = conn.execute(
            """SELECT ltp FROM option_chain_snapshots
               WHERE symbol=? AND strike=? AND option_type=?
               ORDER BY fetched_at ASC LIMIT 1""",
            (t["symbol"], t["strike"], t["option_type"]),
        ).fetchone()

    if snap and snap["ltp"]:
        ltp = float(snap["ltp"])
        # PE bearish (Call Writing verdict) → long PE: SL -30%, Target +50%
        sl_p  = round(ltp * 0.70, 2)
        tgt_p = round(ltp * 1.50, 2)
        conn.execute(
            "UPDATE paper_trades SET entry_premium=?, sl_premium=?, target_premium=? WHERE id=?",
            (ltp, sl_p, tgt_p, t["id"]),
        )
        print(f"  OPEN  id={t['id']} {t['symbol']} {t['option_type']} strike={t['strike']}: "
              f"entry_premium={ltp}  sl={sl_p}  target={tgt_p}")
    else:
        print(f"  OPEN  id={t['id']} {t['symbol']} {t['option_type']} strike={t['strike']}: NO SNAPSHOT FOUND")

# ── 2. Backfill pnl_rupees for CLOSED trades ─────────────────────────────
closed_trades = conn.execute(
    "SELECT id, symbol, option_type, pnl_points, lots FROM paper_trades "
    "WHERE status LIKE 'CLOSED_%' AND (pnl_rupees IS NULL OR pnl_rupees=0) AND pnl_points!=0"
).fetchall()

for t in closed_trades:
    lot_size = LOT_SIZES.get(t["symbol"], 1)
    lots = int(t["lots"] or 1)
    pnl_rs = round(float(t["pnl_points"]) * lot_size * lots, 2)
    conn.execute("UPDATE paper_trades SET pnl_rupees=? WHERE id=?", (pnl_rs, t["id"]))
    print(f"  CLOSED id={t['id']} {t['symbol']}: "
          f"pnl_points={t['pnl_points']} x lot_size={lot_size} x lots={lots} = pnl_rupees=Rs {pnl_rs}")

conn.commit()
conn.close()
print("\nBackfill complete.")
