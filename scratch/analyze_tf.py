import sqlite3, sys, io, re, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

conn = sqlite3.connect('data/nsebot.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM paper_trades WHERE setup_type='TIMEFRAME' ORDER BY id").fetchall()
trades = [dict(r) for r in rows]

print("=== ENTRY DISTANCE FROM BREAKOUT LEVEL ===")
print(f"{'ID':>4} {'Symbol':>12} {'Dir':>5} {'Entry':>9} {'Breakout':>9} {'Distance':>9} {'Dist%':>6} {'PnL':>10} {'MaxR':>5}")
for t in trades:
    reason = t.get('reason') or ''
    m = re.search(r'3H close ([\d.]+) [<>] p3H_(?:high|low) ([\d.]+)', reason)
    if m:
        close_3h = float(m.group(1))
        breakout_level = float(m.group(2))
        entry = t['entry_underlying']
        if t['verdict_label'] == 'LONG':
            dist = entry - breakout_level
        else:
            dist = breakout_level - entry
        dist_pct = (dist / entry) * 100
        pnl = t['pnl_rupees'] or 0
        maxr = t['max_favorable_r'] or 0
        print(f"{t['id']:>4} {t['symbol']:>12} {t['verdict_label']:>5} {entry:>9.1f} {breakout_level:>9.1f} {dist:>9.1f} {dist_pct:>5.2f}% {pnl:>10,.0f} {maxr:>5.2f}")

# Also check consecutive same-direction trades per symbol (trend exhaustion)
print()
print("=== TREND EXHAUSTION: Consecutive same-direction entries ===")
by_sym = {}
for t in trades:
    by_sym.setdefault(t['symbol'], []).append(t)

for sym, tl in sorted(by_sym.items()):
    streak = 1
    for i in range(1, len(tl)):
        if tl[i]['verdict_label'] == tl[i-1]['verdict_label']:
            streak += 1
            if streak >= 3:
                print(f"  {sym}: {streak}-trade streak of {tl[i]['verdict_label']}s ending at ID={tl[i]['id']} | "
                      f"entry1={tl[i-streak+1]['entry_underlying']:.0f} -> entry{streak}={tl[i]['entry_underlying']:.0f} | "
                      f"PnLs: {[int(tl[j]['pnl_rupees'] or 0) for j in range(i-streak+1, i+1)]}")
        else:
            streak = 1

# Key insight: how many 3H candles were same direction before entry?
print()
print("=== SCAN HISTORY: Consecutive same-direction candle_3h before each trade ===")
for t in trades:
    sym = t['symbol']
    opened = t['opened_at']
    # Get candle_3h history before this trade
    candles = conn.execute("""
        SELECT candle_3h, fetched_at, underlying FROM scan_summaries
        WHERE symbol=? AND fetched_at < ? ORDER BY fetched_at DESC LIMIT 10
    """, (sym, opened)).fetchall()
    direction = t['verdict_label']
    target_candle = 'BULLISH' if direction == 'LONG' else 'BEARISH'
    streak = 0
    for c in candles:
        if c['candle_3h'] == target_candle:
            streak += 1
        else:
            break
    pnl = t['pnl_rupees'] or 0
    print(f"  ID={t['id']:>3} {sym:>12} {direction:>5} | Prior same-dir 3H candles: {streak:>2} | PnL: {pnl:>10,.0f}")

conn.close()
