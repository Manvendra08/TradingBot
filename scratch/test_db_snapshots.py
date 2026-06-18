import sqlite3
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_db_snapshots")

db_path = "data/nsebot.db"

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT DISTINCT fetcher_source, symbol, expiry, COUNT(*), MIN(fetched_at), MAX(fetched_at)
        FROM option_chain_snapshots 
        GROUP BY fetcher_source, symbol, expiry;
    """)
    rows = cursor.fetchall()
    log.info("Distinct sources/symbols/expiries in DB:")
    for r in rows:
        log.info(f"Source: {r[0]}, Symbol: {r[1]}, Expiry: {r[2]}, Count: {r[3]}, Min Date: {r[4]}, Max Date: {r[5]}")
        
    conn.close()
except Exception as e:
    log.exception("Error checking snapshots")
