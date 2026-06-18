import sqlite3
from pathlib import Path

db_path = Path("data/nsebot.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== OPEN PAPER TRADES ===")
open_trades = conn.execute("SELECT * FROM paper_trades WHERE status='OPEN'").fetchall()
for t in open_trades:
    td = dict(t)
    print(f"ID: {td['id']}, Symbol: {td['symbol']}, Side: {td['side']}, OptionType: {td['option_type']}, Strike: {td['strike']} (type: {type(td['strike'])}), EntryPremium: {td['entry_premium']}, EntryUnderlying: {td['entry_underlying']}")
    
    symbol = td['symbol']
    option_type = td['option_type']
    strike = td['strike']
    
    # 1. Check underlying price query
    res_underlying = conn.execute("SELECT price, fetched_at FROM underlying_price WHERE symbol=? ORDER BY fetched_at DESC LIMIT 1", (symbol,)).fetchone()
    print(f"  Underlying Price Query: {dict(res_underlying) if res_underlying else None}")
    
    if option_type and option_type != "FUT" and strike is not None:
        try:
            strike_val = float(strike)
        except Exception as e:
            strike_val = 0.0
            print(f"  Failed to parse strike: {e}")
        
        # 2. Check exact option chain snapshot query
        res_exact = conn.execute(
            "SELECT ltp, fetched_at FROM option_chain_snapshots WHERE symbol=? AND strike=? AND option_type=? ORDER BY fetched_at DESC LIMIT 1",
            (symbol, strike_val, option_type)
        ).fetchone()
        print(f"  Exact Snapshot Query (strike={strike_val}): {dict(res_exact) if res_exact else None}")
        
        # 3. Check what strikes exist in DB for this symbol/option_type
        print("  Available strikes for symbol and option_type:")
        strikes = conn.execute(
            "SELECT DISTINCT strike FROM option_chain_snapshots WHERE symbol=? AND option_type=? LIMIT 10",
            (symbol, option_type)
        ).fetchall()
        print(f"    {[s['strike'] for s in strikes]}")
        
        # 4. Try fuzzy query if exact is None
        res_fuzzy = conn.execute(
            "SELECT ltp, strike, fetched_at FROM option_chain_snapshots WHERE symbol=? AND ABS(strike - ?) < 0.01 AND option_type=? ORDER BY fetched_at DESC LIMIT 1",
            (symbol, strike_val, option_type)
        ).fetchone()
        print(f"  Fuzzy Snapshot Query (strike ~ {strike_val}): {dict(res_fuzzy) if res_fuzzy else None}")

        # 5. Check expiries for this symbol
        print("  Available expiries for this symbol:")
        expiries = conn.execute(
            "SELECT DISTINCT expiry FROM option_chain_snapshots WHERE symbol=?",
            (symbol,)
        ).fetchall()
        print(f"    {[e['expiry'] for e in expiries]}")

        # 6. Check if there are multiple expiries for the same fetched_at
        print("  Expiries per fetched_at:")
        exp_per_fetch = conn.execute(
            "SELECT fetched_at, expiry, count(*) FROM option_chain_snapshots WHERE symbol=? GROUP BY fetched_at, expiry ORDER BY fetched_at DESC LIMIT 5",
            (symbol,)
        ).fetchall()
        for epf in exp_per_fetch:
            print(f"    FetchedAt: {epf['fetched_at']}, Expiry: {epf['expiry']}, Count: {epf['count(*)']}")


conn.close()
