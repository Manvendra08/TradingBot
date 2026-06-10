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
    side                TEXT DEFAULT 'BUY',
    option_type         TEXT NOT NULL,      -- CE | PE | FUT
    strike              REAL,
    entry_underlying    REAL NOT NULL,      -- spot/futures price at entry
    exit_underlying     REAL,               -- spot/futures price at exit
    entry_premium       REAL,               -- option premium at entry (for options)
    exit_premium        REAL,               -- option premium at exit (for options)
    sl_underlying       REAL,               -- SL in underlying terms
    target_underlying   REAL,               -- Target in underlying terms
    sl_premium          REAL,               -- SL in premium terms (for options)
    target_premium      REAL,               -- Target in premium terms (for options)
    lots                INTEGER DEFAULT 1,  -- number of lots traded
    pnl_points          REAL DEFAULT 0,     -- P&L in points (legacy)
    pnl_rupees          REAL DEFAULT 0,     -- P&L in ₹ (lot size adjusted)
    status              TEXT NOT NULL,      -- OPEN | CLOSED_TARGET | CLOSED_SL | CLOSED_MANUAL
    reason              TEXT,
    digest_id           TEXT,
    signal_key          TEXT,
    pyramid_level       INTEGER DEFAULT 1,
    max_favorable_r     REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_paper_symbol_status
    ON paper_trades (symbol, status, opened_at);

CREATE TABLE IF NOT EXISTS scan_summaries (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                 TEXT NOT NULL,
    expiry                 TEXT,
    fetched_at             TEXT NOT NULL,
    digest_id              TEXT,
    underlying             REAL,
    atm_strike             REAL,
    total_ce_oi            INTEGER,
    total_pe_oi            INTEGER,
    ce_oi_change           INTEGER,
    pe_oi_change           INTEGER,
    pcr                    REAL,
    max_pain               REAL,
    support                REAL,
    resistance             REAL,
    verdict_label          TEXT,
    confidence             INTEGER,
    candle_1h              TEXT,
    candle_3h              TEXT,
    top_signal_type        TEXT,
    top_signal_strike      REAL,
    top_signal_option_type TEXT,
    top_signal_severity    TEXT,
    top_signal_oi_pct      REAL,
    trend_bias             TEXT,
    trend_strength         INTEGER,
    market_regime          TEXT,
    created_at             TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scan_summaries_symbol_time
    ON scan_summaries (symbol, fetched_at DESC);
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
    # Paper trading enhancements for realistic P&L
    "ALTER TABLE paper_trades ADD COLUMN entry_premium REAL",
    "ALTER TABLE paper_trades ADD COLUMN exit_premium REAL",
    "ALTER TABLE paper_trades ADD COLUMN sl_premium REAL",
    "ALTER TABLE paper_trades ADD COLUMN target_premium REAL",
    "ALTER TABLE paper_trades ADD COLUMN lots INTEGER DEFAULT 1",
    "ALTER TABLE paper_trades ADD COLUMN pnl_rupees REAL DEFAULT 0",
    # V2.2: trade decision metadata
    "ALTER TABLE paper_trades ADD COLUMN trade_status TEXT DEFAULT 'TRIGGERED_CORE'",
    "ALTER TABLE paper_trades ADD COLUMN setup_type TEXT",
    "ALTER TABLE paper_trades ADD COLUMN decision_reason TEXT",
    "ALTER TABLE paper_trades ADD COLUMN confidence_score INTEGER",
    "ALTER TABLE paper_trades ADD COLUMN entry_quality_score INTEGER",
    "ALTER TABLE paper_trades ADD COLUMN trend_alignment_score INTEGER",
    "ALTER TABLE paper_trades ADD COLUMN regime_score INTEGER",
    # Timeframe breakout enhancements
    "ALTER TABLE paper_trades ADD COLUMN signal_key TEXT",
    "ALTER TABLE paper_trades ADD COLUMN pyramid_level INTEGER DEFAULT 1",
    "ALTER TABLE paper_trades ADD COLUMN max_favorable_r REAL DEFAULT 0",
    "ALTER TABLE paper_trades ADD COLUMN side TEXT DEFAULT 'BUY'",
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
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_signal_key ON paper_trades (signal_key)")
        except sqlite3.OperationalError:
            pass
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


def get_previous_underlying_before(symbol: str, fetched_at: str) -> dict | None:
    """Fetch the latest underlying row strictly before the current scan timestamp."""
    sql = """
        SELECT * FROM underlying_price
        WHERE symbol=? AND fetched_at < ?
        ORDER BY fetched_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol, fetched_at)).fetchone()
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
        WHERE symbol=? AND status='OPEN' AND (setup_type IS NULL OR setup_type != 'TIMEFRAME')
        ORDER BY opened_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol,)).fetchone()
        return dict(row) if row else None


def get_open_timeframe_trades(symbol: str) -> list[dict]:
    sql = """
        SELECT * FROM paper_trades
        WHERE symbol=? AND status='OPEN' AND setup_type='TIMEFRAME'
        ORDER BY opened_at DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol,)).fetchall()
        return [dict(r) for r in rows]


def get_scan_summary_at_least_1h_old(symbol: str, current_fetched_at: str) -> dict | None:
    from datetime import datetime, timedelta
    try:
        curr_dt = datetime.fromisoformat(current_fetched_at.replace("Z", "+00:00"))
    except Exception:
        return None

    sql = """
        SELECT total_ce_oi, total_pe_oi, fetched_at FROM scan_summaries
        WHERE symbol=?
        ORDER BY fetched_at DESC
        LIMIT 50
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol,)).fetchall()
        for row in rows:
            try:
                row_dt = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
                if curr_dt - row_dt >= timedelta(hours=1):
                    return dict(row)
            except Exception:
                continue
    return None


def insert_paper_trade(trade: dict) -> int:
    sql = """
        INSERT INTO paper_trades
            (opened_at, symbol, verdict_label, side, option_type, strike, entry_underlying,
             entry_premium, sl_underlying, sl_premium, target_underlying, target_premium,
             lots, status, reason, digest_id,
             trade_status, setup_type, decision_reason,
             confidence_score, entry_quality_score, trend_alignment_score, regime_score,
             signal_key, pyramid_level, max_favorable_r)
        VALUES
            (:opened_at, :symbol, :verdict_label, :side, :option_type, :strike, :entry_underlying,
             :entry_premium, :sl_underlying, :sl_premium, :target_underlying, :target_premium,
             :lots, :status, :reason, :digest_id,
             :trade_status, :setup_type, :decision_reason,
             :confidence_score, :entry_quality_score, :trend_alignment_score, :regime_score,
             :signal_key, :pyramid_level, :max_favorable_r)
        RETURNING id
    """
    row_data = {
        "side":                  trade.get("side", "BUY"),
        "trade_status":          trade.get("trade_status", "TRIGGERED_CORE"),
        "setup_type":            trade.get("setup_type"),
        "decision_reason":       trade.get("decision_reason"),
        "confidence_score":      trade.get("confidence_score"),
        "entry_quality_score":   trade.get("entry_quality_score"),
        "trend_alignment_score": trade.get("trend_alignment_score"),
        "regime_score":          trade.get("regime_score"),
        "signal_key":            trade.get("signal_key"),
        "pyramid_level":         trade.get("pyramid_level", 1),
        "max_favorable_r":       trade.get("max_favorable_r", 0.0),
        **{k: trade.get(k) for k in (
            "opened_at", "symbol", "verdict_label", "option_type", "strike",
            "entry_underlying", "entry_premium", "sl_underlying", "sl_premium",
            "target_underlying", "target_premium", "lots", "status", "reason", "digest_id",
        )},
    }
    with get_conn() as conn:
        row = conn.execute(sql, row_data).fetchone()
        return int(row["id"])


def close_paper_trade(trade_id: int, closed_at: str, exit_underlying: float, exit_premium: float | None, status: str, reason: str = "") -> None:
    """Close a paper trade and calculate P&L in both points and rupees."""
    from config.settings import LOT_SIZES

    with get_conn() as conn:
        row = conn.execute(
            "SELECT symbol, option_type, verdict_label, entry_underlying, entry_premium, lots, strike, side FROM paper_trades WHERE id=?",
            (trade_id,)
        ).fetchone()
        if not row:
            return

        symbol = row["symbol"]
        option_type = row["option_type"]
        verdict_label = row["verdict_label"] or ""
        entry_underlying = float(row["entry_underlying"] or 0.0)
        entry_premium = float(row["entry_premium"] or 0.0)
        lots = int(row["lots"] or 1)
        side = row["side"] or "BUY"

        lot_size = LOT_SIZES.get(symbol.upper(), LOT_SIZES.get(symbol, 1))

        # P&L calculation: inverted for SELL option trades.
        if option_type in ("CE", "PE"):
            if entry_premium and entry_premium > 0 and exit_premium and exit_premium > 0:
                if side == "SELL":
                    pnl_points = entry_premium - exit_premium
                else:
                    pnl_points = exit_premium - entry_premium
            elif entry_premium and entry_premium > 0:
                # Fallback: Estimate exit premium using last known snapshot LTP to preserve time value
                strike = float(row["strike"] or 0.0)
                snap_row = conn.execute(
                    "SELECT ltp FROM option_chain_snapshots WHERE symbol=? AND strike=? AND option_type=? AND ltp IS NOT NULL AND ltp > 0 ORDER BY fetched_at DESC LIMIT 1",
                    (symbol.upper(), strike, option_type)
                ).fetchone()
                if snap_row:
                    estimated_exit = float(snap_row["ltp"])
                else:
                    # Fallback to intrinsic value at exit if no snapshot exists
                    if option_type == "CE":
                        estimated_exit = max(0.0, float(exit_underlying) - strike)
                    else:
                        estimated_exit = max(0.0, strike - float(exit_underlying))
                if side == "SELL":
                    pnl_points = entry_premium - estimated_exit
                else:
                    pnl_points = estimated_exit - entry_premium
                exit_premium = estimated_exit
            else:
                pnl_points = 0.0
        else:
            # Futures
            from src.engine.verdict_sets import is_bearish
            if side == "SELL" or verdict_label == "SHORT" or is_bearish(verdict_label):
                pnl_points = entry_underlying - float(exit_underlying)
            else:
                pnl_points = float(exit_underlying) - entry_underlying

        pnl_rupees = pnl_points * lot_size * lots

        conn.execute(
            """
            UPDATE paper_trades
            SET closed_at=?, exit_underlying=?, exit_premium=?, pnl_points=?, pnl_rupees=?, status=?, reason=?
            WHERE id=?
            """,
            (closed_at, exit_underlying, exit_premium, round(pnl_points, 4), round(pnl_rupees, 2), status, reason, trade_id),
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

def delete_expired_contracts() -> int:
    """Delete expired contract OI data from DB."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_conn() as conn:
        cur1 = conn.execute("DELETE FROM option_chain_snapshots WHERE expiry < ?", (today,))
        cur2 = conn.execute("DELETE FROM scan_summaries WHERE expiry < ?", (today,))
        log.info("[db] Deleted %d expired snapshots and %d expired scan summaries.", cur1.rowcount, cur2.rowcount)
        return cur1.rowcount + cur2.rowcount

