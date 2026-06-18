import sqlite3
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_db_schema")

db_path = "data/nsebot.db"

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    log.info(f"Tables in DB: {tables}")
    
    # Print schema for each table
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table});")
        info = cursor.fetchall()
        log.info(f"Table '{table}' columns: {[col[1] for col in info]}")
        
        # Count rows
        cursor.execute(f"SELECT COUNT(*) FROM {table};")
        count = cursor.fetchone()[0]
        log.info(f"Table '{table}' row count: {count}")
        
    conn.close()
except Exception as e:
    log.exception("Error checking DB schema")
