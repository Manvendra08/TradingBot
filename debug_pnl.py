import sqlite3

DB_PATH = "data/nsebot.db"
LOT_SIZES = {
    "NIFTY": 25, "BANKNIFTY": 15, "NATURALGAS": 1250, "CRUDEOIL": 100,
}
BULLISH = {"Long Buildup", "Put Writing", "OI Bias Bullish", "Short Covering"}
BEARISH = {"Short Buildup", "Call Writing", "OI Bias Bearish", "Long Unwinding"}

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, symbol, option_type, verdict_label, entry_premium, exit_premium, lots, pnl_rupees, status "
    "FROM paper_trades WHERE status != 'OPEN'"
).fetchall()

print(f"Found {len(rows)} closed trades\n")
for r in rows:
    r = dict(r)
    tid  = r["id"]
    sym  = (r["symbol"] or "").upper()
    ot   = (r["option_type"] or "").upper()
    verd = r["verdict_label"] or ""
    ep   = float(r["entry_premium"] or 0)
    xp   = float(r["exit_premium"] or 0)
    lots = int(r["lots"] or 1)
    old  = float(r["pnl_rupees"] or 0)
    lot_size = LOT_SIZES.get(sym, 1)

    is_bullish = verd in BULLISH
    is_bearish = verd in BEARISH

    if ep > 0 and xp > 0:
        if ot in ("CE", "PE"):
            if (ot == "CE" and is_bullish) or (ot == "PE" and is_bearish):
                pnl_pts = xp - ep
                direction = "LONG"
            else:
                pnl_pts = ep - xp
                direction = "SHORT"
        else:
            # Futures
            if is_bearish:
                pnl_pts = ep - xp
                direction = "SHORT"
            else:
                pnl_pts = xp - ep
                direction = "LONG"
    else:
        pnl_pts = 0
        direction = "?"

    pnl_rs = round(pnl_pts * lot_size * lots, 2)
    diff = pnl_rs - old
    flag = "WRONG" if abs(diff) > 0.01 else "ok"
    print(f"[{flag:5s}] #{tid:2d} {sym:12s} {ot} {direction:5s} [{verd:20s}]  ep={ep:8.2f} xp={xp:8.2f}  OLD={old:>10,.0f}  CORRECT={pnl_rs:>10,.0f}  diff={diff:>10,.0f}")

conn.close()
