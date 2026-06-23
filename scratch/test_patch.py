import sqlite3
import re

class PatchedCursor(sqlite3.Cursor):
    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and re.match(r'(?i)^\s*(select|with)\b', sql) and re.search(r'(?i)\bfrom\s+paper_trades\b', sql) and not re.search(r'(?i)\bfrom\s+live_trades\b', sql):
            subquery = """(
                SELECT id, symbol, status, pnl_rupees
                FROM paper_trades
                UNION ALL
                SELECT id, symbol, status, pnl_rupees
                FROM live_trades
                WHERE status = 'CLOSED_SHADOW' OR trade_status = 'SHADOW' OR broker_status = 'SHADOW'
            )"""
            sql = re.sub(r'(?i)\bfrom\s+paper_trades\b', f'FROM {subquery}', sql)
        return super().execute(sql, *args, **kwargs)

class PatchedConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and re.match(r'(?i)^\s*(select|with)\b', sql) and re.search(r'(?i)\bfrom\s+paper_trades\b', sql) and not re.search(r'(?i)\bfrom\s+live_trades\b', sql):
            subquery = """(
                SELECT id, symbol, status, pnl_rupees
                FROM paper_trades
                UNION ALL
                SELECT id, symbol, status, pnl_rupees
                FROM live_trades
                WHERE status = 'CLOSED_SHADOW' OR trade_status = 'SHADOW' OR broker_status = 'SHADOW'
            )"""
            sql = re.sub(r'(?i)\bfrom\s+paper_trades\b', f'FROM {subquery}', sql)
        return super().execute(sql, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return super().cursor(factory=PatchedCursor, *args, **kwargs)

_orig_connect = sqlite3.connect
def _patched_connect(*args, **kwargs):
    if "factory" not in kwargs:
        kwargs["factory"] = PatchedConnection
    return _orig_connect(*args, **kwargs)

sqlite3.connect = _patched_connect

# Try connection
conn = sqlite3.connect("data/nsebot.db")
conn.row_factory = sqlite3.Row
try:
    rows = conn.execute("SELECT symbol, count(*) from paper_trades group by symbol").fetchall()
    print("Result:")
    for r in rows:
        print(dict(r))
finally:
    conn.close()
