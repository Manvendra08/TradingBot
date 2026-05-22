"""
SQLite schema + lightweight data-access helpers.
Tables:
  option_chain_snapshots  — raw per-strike OC data, 15-min cadence
  underlying_price        — spot price per symbol per snapshot
  anomaly_alerts          — fired alert log
  alert_dedup             — deduplication tracker
"""
import sqlite3
import contextlib
import logging
from pathlib import Path

from config.settings import DB_PATH

log = logging.getLogger(__name__)

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at      TEXT NOT NULL,          -- ISO8601 UTC
    symbol          TEXT NOT NULL,
    expiry          TEXT NOT NULL,          -- YYYY-MM-DD
    strike          REAL NOT NULL,
    option_type     TEXT NOT NULL,          -- CE | PE
    ltp             REAL,
    ltp_change_pct  REAL,
    oi              INTEGER,
    oi_change_pct   REAL,
    oi_change       INTEGER,
    volume          INTEGER,
    iv              REAL,
    bid             REAL,
    ask             REAL,
    delta           REAL,
    underlying_price REAL,
    fetcher_source  TEXT                    -- dhan | nse_public | upstox
);

CREATE INDEX IF NOT EXISTS idx_oc_symbol_time
    ON option_chain_snapshots (symbol, fetched_at);
CREATE INDEX IF NOT EXISTS idx_oc_strike_type
    ON option_chain_snapshots (symbol, strike, option_type, fetched_at);

CREATE TABLE IF NOT EXISTS underlying_price (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    price       REAL NOT NULL,
    pct_change  REAL                        -- vs previous snapshot
);

CREATE INDEX IF NOT EXISTS idx_up_symbol_time
    ON underlying_price (symbol, fetched_at);

CREATE TABLE IF NOT EXISTS anomaly_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    alert_type      TEXT NOT NULL,
    strike          REAL,
    option_type     TEXT,
    expiry          TEXT,
    detail_json     TEXT,
    telegram_sent   INTEGER DEFAULT 0,
    severity        TEXT    DEFAULT 'LOW',   -- HIGH | MEDIUM | LOW
    digest_id       TEXT                     -- groups alerts fired in same scan
);

CREATE TABLE IF NOT EXISTS alert_dedup (
    dedup_key       TEXT PRIMARY KEY,
    last_fired_at   TEXT NOT NULL,
    severity        TEXT DEFAULT 'LOW'
);

CREATE TABLE IF NOT EXISTS snapshot_baseline (
    symbol          TEXT PRIMARY KEY,
    last_symbol_at  TEXT NOT NULL           -- ISO UTC timestamp of last symbol switch
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at           TEXT NOT NULL,
    closed_at           TEXT,
    symbol              TEXT NOT NULL,
    verdict_label       TEXT,
    option_type         TEXT NOT NULL,      -- CE | PE
    strike              REAL,
    entry_underlying    REAL NOT NULL,
    exit_underlying     REAL,
    sl_underlying       REAL,
    target_underlying   REAL,
    pnl_points          REAL DEFAULT 0,
    status              TEXT NOT NULL,      -- OPEN | CLOSED_TARGET | CLOSED_SL | CLOSED_MANUAL
    reason              TEXT,
    digest_id           TEXT
);

CREATE INDEX IF NOT EXISTS idx_paper_symbol_status
    ON paper_trades (symbol, status, opened_at);
"""


@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_MIGRATIONS = [
    "ALTER TABLE option_chain_snapshots ADD COLUMN ltp_change_pct REAL",
    "ALTER TABLE option_chain_snapshots ADD COLUMN oi_change_pct REAL",
    "ALTER TABLE anomaly_alerts ADD COLUMN severity TEXT DEFAULT 'LOW'",
    "ALTER TABLE anomaly_alerts ADD COLUMN digest_id TEXT",
    "ALTER TABLE alert_dedup    ADD COLUMN severity TEXT DEFAULT 'LOW'",
]


def init_db() -> None:
    """Create tables + run safe column migrations. Call on every startup."""
    with get_conn() as conn:
        conn.executescript(DDL)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as e:
                # Idempotent: column already exists is the only expected error.
                if "duplicate column" not in str(e).lower():
                    raise
    log.info("DB initialised at %s", DB_PATH)


def insert_snapshots(rows: list[dict]) -> int:
    """Bulk-insert option chain snapshot rows. Returns count inserted."""
    if not rows:
        return 0
    sql = """
        INSERT INTO option_chain_snapshots
            (fetched_at, symbol, expiry, strike, option_type, ltp, ltp_change_pct, oi,
             oi_change_pct, oi_change, volume, iv, bid, ask, delta, underlying_price, fetcher_source)
        VALUES
            (:fetched_at, :symbol, :expiry, :strike, :option_type, :ltp, :ltp_change_pct, :oi,
             :oi_change_pct, :oi_change, :volume, :iv, :bid, :ask, :delta, :underlying_price, :fetcher_source)
    """
    with get_conn() as conn:
        conn.executemany(sql, rows)
    return len(rows)


def insert_underlying_price(symbol: str, price: float, pct_change: float | None, fetched_at: str) -> None:
    sql = """
        INSERT INTO underlying_price (fetched_at, symbol, price, pct_change)
        VALUES (?, ?, ?, ?)
    """
    with get_conn() as conn:
        conn.execute(sql, (fetched_at, symbol, price, pct_change))


def insert_alert(alert: dict) -> int:
    sql = """
        INSERT INTO anomaly_alerts
            (fired_at, symbol, alert_type, strike, option_type, expiry,
             detail_json, telegram_sent, severity, digest_id)
        VALUES
            (:fired_at, :symbol, :alert_type, :strike, :option_type, :expiry,
             :detail_json, :telegram_sent,
             :severity, :digest_id)
        RETURNING id
    """
    row_data = {
        **alert,
        "severity":  alert.get("severity", "LOW"),
        "digest_id": alert.get("digest_id"),
    }
    with get_conn() as conn:
        row = conn.execute(sql, row_data).fetchone()
        return row["id"]


def mark_telegram_sent(alert_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE anomaly_alerts SET telegram_sent=1 WHERE id=?", (alert_id,))


def get_previous_snapshot(symbol: str, expiry: str, strike: float, option_type: str) -> dict | None:
    """Fetch the immediately preceding snapshot for delta calculations."""
    sql = """
        SELECT * FROM option_chain_snapshots
        WHERE symbol=? AND expiry=? AND strike=? AND option_type=?
        ORDER BY fetched_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol, expiry, strike, option_type)).fetchone()
        return dict(row) if row else None


def get_previous_underlying(symbol: str) -> dict | None:
    sql = """
        SELECT * FROM underlying_price
        WHERE symbol=?
        ORDER BY fetched_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol,)).fetchone()
        return dict(row) if row else None


def get_latest_snapshots_for_symbol(symbol: str, expiry: str) -> list[dict]:
    """All strikes for the latest fetch timestamp for a symbol/expiry."""
    sql = """
        SELECT * FROM option_chain_snapshots
        WHERE symbol=? AND expiry=? AND fetched_at=(
            SELECT MAX(fetched_at) FROM option_chain_snapshots WHERE symbol=? AND expiry=?
        )
        ORDER BY strike
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol, expiry, symbol, expiry)).fetchall()
        return [dict(r) for r in rows]


def get_prev_snapshots_bulk(symbol: str, expiry: str) -> dict[tuple, dict]:
    """
    Single-query bulk fetch of the latest snapshot row per (strike, option_type).
    Returns a dict keyed by (strike, option_type) for O(1) detection lookups.
    Replaces per-strike get_previous_snapshot() calls in detection loops.
    """
    sql = """
        SELECT * FROM option_chain_snapshots
        WHERE symbol=? AND expiry=? AND fetched_at=(
            SELECT MAX(fetched_at) FROM option_chain_snapshots
            WHERE symbol=? AND expiry=?
        )
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol, expiry, symbol, expiry)).fetchall()
    return {(r["strike"], r["option_type"]): dict(r) for r in rows}


def get_latest_n_underlying(symbol: str, n: int = 4) -> list[dict]:
    """Return last N underlying price rows for PCR velocity calc."""
    sql = """
        SELECT * FROM underlying_price WHERE symbol=?
        ORDER BY fetched_at DESC LIMIT ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol, n)).fetchall()
        return [dict(r) for r in rows]


def get_latest_n_snapshots(symbol: str, expiry: str, n: int = 3) -> list[list[dict]]:
    """
    Return last N distinct fetch timestamps' snapshots (for PCR velocity).
    Uses a single query with IN clause instead of N+1 round trips.
    """
    times_sql = """
        SELECT DISTINCT fetched_at FROM option_chain_snapshots
        WHERE symbol=? AND expiry=?
        ORDER BY fetched_at DESC LIMIT ?
    """
    with get_conn() as conn:
        times = [r[0] for r in conn.execute(times_sql, (symbol, expiry, n)).fetchall()]
        if not times:
            return []
        placeholders = ",".join("?" * len(times))
        all_rows = conn.execute(
            f"SELECT * FROM option_chain_snapshots "
            f"WHERE symbol=? AND expiry=? AND fetched_at IN ({placeholders}) "
            f"ORDER BY fetched_at DESC, strike",
            (symbol, expiry, *times),
        ).fetchall()

    # Group rows back by timestamp, preserving order
    grouped: dict[str, list[dict]] = {t: [] for t in times}
    for row in all_rows:
        t = row["fetched_at"]
        if t in grouped:
            grouped[t].append(dict(row))
    return [grouped[t] for t in times if grouped[t]]


def get_alert_history(symbol: str | None = None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM anomaly_alerts"
    params: list = []
    if symbol:
        sql += " WHERE symbol=?"
        params.append(symbol)
    sql += " ORDER BY fired_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def delete_alerts(symbol: str | None = None) -> int:
    """Delete alerts (and their dedup records). If symbol is None, wipes all rows."""
    with get_conn() as conn:
        if symbol:
            cur = conn.execute("DELETE FROM anomaly_alerts WHERE symbol=?", (symbol,))
            conn.execute("DELETE FROM alert_dedup WHERE symbol=?", (symbol,))
        else:
            cur = conn.execute("DELETE FROM anomaly_alerts")
            conn.execute("DELETE FROM alert_dedup")
        return cur.rowcount


def get_open_paper_trade(symbol: str) -> dict | None:
    sql = """
        SELECT * FROM paper_trades
        WHERE symbol=? AND status='OPEN'
        ORDER BY opened_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol,)).fetchone()
        return dict(row) if row else None


def insert_paper_trade(trade: dict) -> int:
    sql = """
        INSERT INTO paper_trades
            (opened_at, symbol, verdict_label, option_type, strike, entry_underlying,
             sl_underlying, target_underlying, status, reason, digest_id)
        VALUES
            (:opened_at, :symbol, :verdict_label, :option_type, :strike, :entry_underlying,
             :sl_underlying, :target_underlying, :status, :reason, :digest_id)
        RETURNING id
    """
    with get_conn() as conn:
        row = conn.execute(sql, trade).fetchone()
        return int(row["id"])


def close_paper_trade(trade_id: int, closed_at: str, exit_underlying: float, status: str, reason: str = "") -> None:
    with get_conn() as conn:
        row = conn.execute("SELECT option_type, entry_underlying FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return
        option_type = row["option_type"]
        entry = float(row["entry_underlying"] or 0.0)
        if option_type == "CE":
            pnl = float(exit_underlying) - entry
        else:
            pnl = entry - float(exit_underlying)
        conn.execute(
            """
            UPDATE paper_trades
            SET closed_at=?, exit_underlying=?, pnl_points=?, status=?, reason=?
            WHERE id=?
            """,
            (closed_at, exit_underlying, round(pnl, 4), status, reason, trade_id),
        )


def list_paper_trades(symbol: str | None = None, limit: int = 300) -> list[dict]:
    sql = "SELECT * FROM paper_trades"
    params: list = []
    if symbol:
        sql += " WHERE symbol=?"
        params.append(symbol)
    sql += " ORDER BY opened_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
