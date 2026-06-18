import sqlite3
conn = sqlite3.connect('data/nsebot.db')
conn.row_factory = sqlite3.Row

# Closed trade stats
row = conn.execute("""
    SELECT COUNT(*) as total,
           SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN pnl_rupees < 0 THEN 1 ELSE 0 END) as losses,
           SUM(CASE WHEN pnl_rupees = 0 THEN 1 ELSE 0 END) as breakeven,
           ROUND(SUM(pnl_rupees), 2) as total_pnl,
           ROUND(AVG(pnl_rupees), 2) as avg_pnl,
           ROUND(MIN(pnl_rupees), 2) as worst_trade,
           ROUND(MAX(pnl_rupees), 2) as best_trade
    FROM paper_trades WHERE status != 'OPEN'
""").fetchone()
print("=== CLOSED TRADE STATS ===")
print(dict(row))

# Open trades
opens = conn.execute("SELECT COUNT(*) as c FROM paper_trades WHERE status='OPEN'").fetchone()
print(f"\nOpen trades: {opens['c']}")

# By symbol
print("\n=== BY SYMBOL ===")
for r in conn.execute("""
    SELECT symbol, COUNT(*) as trades, 
           SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
           ROUND(SUM(pnl_rupees), 2) as pnl,
           ROUND(AVG(pnl_rupees), 2) as avg_pnl
    FROM paper_trades WHERE status != 'OPEN'
    GROUP BY symbol
""").fetchall():
    print(dict(r))

# By setup_type
print("\n=== BY SETUP TYPE ===")
for r in conn.execute("""
    SELECT setup_type, COUNT(*) as trades,
           SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
           ROUND(SUM(pnl_rupees), 2) as pnl
    FROM paper_trades WHERE status != 'OPEN'
    GROUP BY setup_type
""").fetchall():
    print(dict(r))

# By status
print("\n=== BY EXIT STATUS ===")
for r in conn.execute("""
    SELECT status, COUNT(*) as trades,
           ROUND(SUM(pnl_rupees), 2) as pnl
    FROM paper_trades WHERE status != 'OPEN'
    GROUP BY status
""").fetchall():
    print(dict(r))

# Longest-open trade still open
print("\n=== OLDEST OPEN TRADES ===")
for r in conn.execute("""
    SELECT id, symbol, option_type, strike, side, opened_at, verdict_label, setup_type
    FROM paper_trades WHERE status='OPEN'
    ORDER BY opened_at ASC LIMIT 5
""").fetchall():
    print(dict(r))

conn.close()
