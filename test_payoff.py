import sqlite3
import math

DB_PATH = "c:/Users/manve/Downloads/NSEBOT/data/nsebot.db"

def test_payoff_calculation():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Check option_chain_snapshots
    c.execute("SELECT * FROM option_chain_snapshots ORDER BY id DESC LIMIT 5")
    rows = c.fetchall()
    print("Latest option_chain_snapshots:")
    for r in rows:
        print(dict(r))

    conn.close()

if __name__ == "__main__":
    test_payoff_calculation()
