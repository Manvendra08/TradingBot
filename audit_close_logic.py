"""
Verify which trades were closed by the WRONG trigger condition.
Old bug: PE target = exit_premium <= target_premium
         But target_premium = entry * 1.5 (higher than entry)
         So condition fires immediately on first scan after open.
"""
import sqlite3

conn = sqlite3.connect("data/nsebot.db")
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, symbol, option_type, verdict_label, entry_premium, exit_premium, "
    "sl_premium, target_premium, pnl_rupees, status, opened_at, closed_at "
    "FROM paper_trades WHERE status != 'OPEN' ORDER BY id"
).fetchall()

print("=== CLOSE TRIGGER AUDIT ===\n")
print(f"{'ID':>3} {'SYM':12} {'OT':3} {'ep':>8} {'xp':>8} {'sl':>8} {'tgt':>8} {'status':16} {'verdict':20} {'note'}")
print("-" * 120)

fake_trades = []
for r in rows:
    r = dict(r)
    tid  = r["id"]
    ot   = r["option_type"]
    ep   = float(r["entry_premium"] or 0)
    xp   = float(r["exit_premium"] or 0)
    sl   = float(r["sl_premium"] or 0)
    tgt  = float(r["target_premium"] or 0)
    pnl  = float(r["pnl_rupees"] or 0)
    stat = r["status"]
    verd = r["verdict_label"] or ""

    note = ""
    is_fake = False

    if stat == "CLOSED_TARGET" and ep > 0 and xp > 0 and tgt > 0:
        # Correct logic: target hit when exit >= target (long position)
        if xp < tgt:
            note = f"FAKE TARGET: exit {xp:.2f} < target {tgt:.2f} (old bug fired too early)"
            is_fake = True
        else:
            note = f"REAL TARGET: exit {xp:.2f} >= target {tgt:.2f}"

    elif stat == "CLOSED_SL" and ep > 0 and xp > 0 and sl > 0:
        if xp > sl:
            note = f"FAKE SL: exit {xp:.2f} > sl {sl:.2f}"
            is_fake = True
        else:
            note = f"REAL SL: exit {xp:.2f} <= sl {sl:.2f}"

    flag = "FAKE" if is_fake else "    "
    print(f"[{flag}] #{tid:2d} {r['symbol']:12} {ot:3} ep={ep:8.2f} xp={xp:8.2f} sl={sl:8.2f} tgt={tgt:8.2f} {stat:16} [{verd:20}] {note}")
    if is_fake:
        fake_trades.append(tid)

print(f"\n=== {len(fake_trades)} FAKE trades (closed by wrong trigger): IDs {fake_trades} ===")
print("Recommendation: Delete these trades using the date range delete feature.")
conn.close()
