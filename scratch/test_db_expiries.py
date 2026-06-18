import sqlite3
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_db_expiries")

db_path = "data/nsebot.db"

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    log.info("=== NATURALGAS DISTINCT EXPIRIES ===")
    cursor.execute("""
        SELECT DISTINCT expiry, MIN(fetched_at), MAX(fetched_at) 
        FROM option_chain_snapshots 
        WHERE symbol='NATURALGAS' 
        GROUP BY expiry;
    """)
    for row in cursor.fetchall():
        log.info(f"Expiry: {row[0]}, first fetched: {row[1]}, last fetched: {row[2]}")
        
    log.info("=== CRUDEOIL DISTINCT EXPIRIES ===")
    cursor.execute("""
        SELECT DISTINCT expiry, MIN(fetched_at), MAX(fetched_at) 
        FROM option_chain_snapshots 
        WHERE symbol='CRUDEOIL' 
        GROUP BY expiry;
    """)
    for row in cursor.fetchall():
        log.info(f"Expiry: {row[0]}, first fetched: {row[1]}, last fetched: {row[2]}")
        
    conn.close()
except Exception as e:
    log.exception("Error querying expiries from DB")
