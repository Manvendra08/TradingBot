import sqlite3
conn = sqlite3.connect('data/nsebot.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, opened_at, closed_at, symbol, verdict_label, option_type, strike,
           entry_underlying, exit_underlying, sl_underlying,
           entry_premium, exit_premium, sl_premium, pnl_rupees, status,
           reason, setup_type, max_favorable_r, side
    FROM paper_trades WHERE setup_type='TIMEFRAME' ORDER BY id
""").fetchall()
for r in rows:
    d = dict(r)
    print(f"ID={d['id']} | {d['symbol']} {d['verdict_label']} {d['option_type']} {d['strike']} | "
          f"Entry={d['entry_underlying']:.0f} Prem={d['entry_premium']:.1f} SL={d['sl_premium']} | "
          f"Status={d['status']} PnL={d['pnl_rupees']:.0f} MaxR={d['max_favorable_r']:.2f} | "
          f"Opened={d['opened_at'][:16]} Closed={str(d['closed_at'] or '')[:16]} | "
          f"Reason={d['reason'][:80]}")
conn.close()
