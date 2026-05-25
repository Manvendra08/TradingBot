"""
One-time fix: recalculate P&L for all closed paper trades.

Direction logic:
  Call Writing  = Bearish  → PE short  → profit = entry_prem - exit_prem
  Short Buildup = Bearish  → PE short  → profit = entry_prem - exit_prem
  OI Bias Bearish = Bearish → PE short → profit = entry_prem - exit_prem
  Long Unwinding = Bearish → PE short  → profit = entry_prem - exit_prem

  Put Writing   = Bullish  → CE long   → profit = exit_prem - entry_prem
  Long Buildup  = Bullish  → CE long   → profit = exit_prem - entry_prem
  OI Bias Bullish = Bullish → CE long  → profit = exit_prem - entry_prem
  Short Covering = Bullish → CE long   → profit = exit_prem - entry_prem

  CE + bullish = long CE  → profit = exit - entry
  PE + bearish = long PE  → profit = exit - entry
  CE + bearish = short CE → profit = entry - exit
  PE + bullish = short PE → profit = entry - exit
"""
import sqlite3

DB_PATH = "data/nsebot.db"

LOT_SIZES = {
    "NIFTY": 25, "BANKNIFTY": 15, "FINNIFTY": 25, "MIDCPNIFTY": 50,
    "NATURALGAS": 1250, "CRUDEOIL": 100, "GOLD": 100, "SILVER": 30,
}

BULLISH = {"Long Buildup", "Put Writing", "OI Bias Bullish", "Short Covering"}
BEARISH = {"Short Buildup", "Call Writing", "OI Bias Bearish", "Long Unwinding"}

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, symbol, option_type, verdict_label, "
    "entry_underlying, exit_underlying, entry_premium, exit_premium, "
    "lots, pnl_rupees, status FROM paper_trades WHERE status != 'OPEN'"
).fetchall()

updated = 0
for r in rows:
    r = dict(r)
    tid   = r["id"]
    sym   = (r["symbol"] or "").upper()
    ot    = (r["option_type"] or "").upper()
    verd  = r["verdict_label"] or ""
    ep    = float(r["entry_premium"] or 0)
    xp    = float(r["exit_premium"] or 0)
    eu    = float(r["entry_underlying"] or 0)
    xu    = float(r["exit_underlying"] or 0)
    lots  = int(r["lots"] or 1)
    old   = float(r["pnl_rupees"] or 0)
    lot_size = LOT_SIZES.get(sym, 1)

    is_bullish = verd in BULLISH
    is_bearish = verd in BEARISH

    if ot in ("CE", "PE"):
        if ep > 0 and xp > 0:
            # long position: profit when premium rises
            if (ot == "CE" and is_bullish) or (ot == "PE" and is_bearish):
                pnl_pts = xp - ep
            else:
                # short position: profit when premium falls
                pnl_pts = ep - xp
        else:
            pnl_pts = (xu - eu) if ot == "CE" else (eu - xu)
    else:
        pnl_pts = xu - eu

    pnl_rs = round(pnl_pts * lot_size * lots, 2)

    if abs(pnl_rs - old) > 0.01:
        conn.execute(
            "UPDATE paper_trades SET pnl_points=?, pnl_rupees=? WHERE id=?",
            (round(pnl_pts, 4), pnl_rs, tid)
        )
        direction = "LONG" if ((ot=="CE" and is_bullish) or (ot=="PE" and is_bearish)) else "SHORT"
        print(f"  #{tid:2d} {sym:12s} {ot} {direction:5s} [{verd}]  ep={ep:.2f} xp={xp:.2f}  OLD={old:>10,.0f}  NEW={pnl_rs:>10,.0f}")
        updated += 1

conn.commit()
conn.close()
print(f"\nUpdated {updated} / {len(rows)} trades.")
