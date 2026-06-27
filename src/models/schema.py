"""
SQLite schema + lightweight data-access helpers.
Tables:
  option_chain_snapshots  — raw per-strike OC data, 15-min cadence
  underlying_price        — spot price per symbol per snapshot
  anomaly_alerts          — fired alert log
  alert_dedup             — deduplication tracker

B8 fix: scan_summaries gains is_fallback column; stale-price rows tagged at insert.
B10 fix: insert_paper_trade uses INSERT OR IGNORE on signal_key to prevent duplicate
         trade rows on pipeline retry after crash.
P3 fix (#14): get_today_scan_count() now uses IST midnight (UTC+05:30) as the
  day boundary instead of UTC midnight. A trade at 00:30 UTC (06:00 IST) on a
  new IST calendar day was previously counted in the prior day's quota.

Autopsy fix 4: close_paper_trade() and close_live_trade() now deduct
  transaction costs before writing pnl_rupees. Costs are modelled per
  instrument class via _calc_transaction_costs():
    Options: STT 0.0625% of sell-side turnover + ₹20 brokerage + ₹5 exchange
    Futures: STT 0.01% of sell-side turnover  + ₹20 brokerage + ₹5 exchange
  gross_pnl_rupees (pre-cost) is preserved in memory so callers can log it;
  pnl_rupees written to DB is always the net (post-cost) figure.
  The schema does NOT add a gross_pnl_rupees column to avoid a migration — the
  column can be added later when historical comparison is required.
"""

import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import DB_PATH

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

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
    expiry              TEXT,
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
    pnl_rupees          REAL DEFAULT 0,     -- P&L in ₹ net of transaction costs
    status              TEXT NOT NULL,      -- OPEN | CLOSED_TARGET | CLOSED_SL | CLOSED_MANUAL
    reason              TEXT,
    digest_id           TEXT,
    signal_key          TEXT UNIQUE,
    pyramid_level       INTEGER DEFAULT 1,
    max_favorable_r     REAL DEFAULT 0,
    trade_status        TEXT DEFAULT 'TRIGGERED_CORE',
    setup_type          TEXT,
    decision_reason     TEXT,
    confidence_score    INTEGER,
    entry_quality_score INTEGER,
    trend_alignment_score INTEGER,
    regime_score        INTEGER
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
    is_fallback            INTEGER DEFAULT 0,  -- B8: 1 when underlying is a stale fallback value
    created_at             TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scan_summaries_symbol_time
    ON scan_summaries (symbol, fetched_at DESC);

CREATE TABLE IF NOT EXISTS broker_configs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key             TEXT,
    api_secret          TEXT,
    access_token        TEXT,
    request_token       TEXT,
    totp_secret         TEXT,
    kill_switch_active  INTEGER DEFAULT 0,
    last_login_date     TEXT
);

CREATE TABLE IF NOT EXISTS live_trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at           TEXT NOT NULL,
    closed_at           TEXT,
    symbol              TEXT NOT NULL,
    expiry              TEXT,
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
    pnl_points          REAL DEFAULT 0,
    pnl_rupees          REAL DEFAULT 0,     -- net of transaction costs
    status              TEXT NOT NULL,      -- OPEN | CLOSED_TARGET | CLOSED_SL | CLOSED_MANUAL | SHADOW | REJECTED
    reason              TEXT,
    digest_id           TEXT,
    signal_key          TEXT UNIQUE,
    pyramid_level       INTEGER DEFAULT 1,
    max_favorable_r     REAL DEFAULT 0,
    broker_order_id     TEXT,
    gtt_order_id        TEXT,
    broker_status       TEXT,               -- OPEN, REJECTED, COMPLETE, CANCELLED
    broker_message      TEXT,
    exit_mode           TEXT,               -- GTT | POLL
    trade_status        TEXT DEFAULT 'TRIGGERED_CORE',
    setup_type          TEXT,
    decision_reason     TEXT,
    confidence_score    INTEGER,
    entry_quality_score INTEGER,
    trend_alignment_score INTEGER,
    regime_score        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_live_symbol_status
    ON live_trades (symbol, status, opened_at);

CREATE TABLE IF NOT EXISTS daily_equity_peaks (
    date            TEXT NOT NULL,
    mode            TEXT NOT NULL,
    peak_equity     REAL NOT NULL,
    PRIMARY KEY (date, mode)
);

-- AI Intelligence: pattern insights cache (Phase 1)
CREATE TABLE IF NOT EXISTS ai_pattern_insights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name    TEXT NOT NULL,
    pattern_type    TEXT NOT NULL,
    sample_size     INTEGER,
    win_rate        REAL,
    avg_pnl         REAL,
    best_conditions TEXT,
    recommendation  TEXT,
    discovered_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_pattern_insights_name
    ON ai_pattern_insights (pattern_name);

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
    "ALTER TABLE paper_trades ADD COLUMN lot_size INTEGER DEFAULT 1",
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
    # B8: stale fallback tagging for regime isolation
    "ALTER TABLE scan_summaries ADD COLUMN is_fallback INTEGER DEFAULT 0",
    # Live trades decision metadata
    "ALTER TABLE live_trades ADD COLUMN trade_status TEXT DEFAULT 'TRIGGERED_CORE'",
    "ALTER TABLE live_trades ADD COLUMN setup_type TEXT",
    "ALTER TABLE live_trades ADD COLUMN decision_reason TEXT",
    "ALTER TABLE live_trades ADD COLUMN confidence_score INTEGER",
    "ALTER TABLE live_trades ADD COLUMN entry_quality_score INTEGER",
    "ALTER TABLE live_trades ADD COLUMN trend_alignment_score INTEGER",
    "ALTER TABLE live_trades ADD COLUMN regime_score INTEGER",
    "ALTER TABLE paper_trades ADD COLUMN expiry TEXT",
    "ALTER TABLE live_trades ADD COLUMN expiry TEXT",
    # ── Phase 0: ML feature columns (AI_INTELLIGENCE_ROADMAP_v3.0) ──────────
    # These columns capture scan context at trade open time for ML training.
    # All nullable — historical rows remain NULL, excluded from training.
    "ALTER TABLE paper_trades ADD COLUMN price_change_pct REAL",
    "ALTER TABLE paper_trades ADD COLUMN pcr REAL",
    "ALTER TABLE paper_trades ADD COLUMN ce_oi_change REAL",
    "ALTER TABLE paper_trades ADD COLUMN pe_oi_change REAL",
    "ALTER TABLE paper_trades ADD COLUMN underlying REAL",
    "ALTER TABLE paper_trades ADD COLUMN support REAL",
    "ALTER TABLE paper_trades ADD COLUMN resistance REAL",
    "ALTER TABLE paper_trades ADD COLUMN max_pain REAL",
    "ALTER TABLE paper_trades ADD COLUMN days_to_expiry INTEGER",
    "ALTER TABLE paper_trades ADD COLUMN chart_conflict INTEGER",
    "ALTER TABLE paper_trades ADD COLUMN rsi_1h REAL",
    "ALTER TABLE paper_trades ADD COLUMN rsi_3h REAL",
    "ALTER TABLE paper_trades ADD COLUMN regime TEXT",
    "ALTER TABLE live_trades ADD COLUMN price_change_pct REAL",
    "ALTER TABLE live_trades ADD COLUMN pcr REAL",
    "ALTER TABLE live_trades ADD COLUMN ce_oi_change REAL",
    "ALTER TABLE live_trades ADD COLUMN pe_oi_change REAL",
    "ALTER TABLE live_trades ADD COLUMN underlying REAL",
    "ALTER TABLE live_trades ADD COLUMN support REAL",
    "ALTER TABLE live_trades ADD COLUMN resistance REAL",
    "ALTER TABLE live_trades ADD COLUMN max_pain REAL",
    "ALTER TABLE live_trades ADD COLUMN days_to_expiry INTEGER",
    "ALTER TABLE live_trades ADD COLUMN chart_conflict INTEGER",
    "ALTER TABLE live_trades ADD COLUMN rsi_1h REAL",
    "ALTER TABLE live_trades ADD COLUMN rsi_3h REAL",
    "ALTER TABLE live_trades ADD COLUMN regime TEXT",
]


def init_db() -> None:
    """Create tables + run safe column migrations. Call on every startup."""
    with get_conn() as conn:
        conn.executescript(DDL)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_signal_key ON paper_trades (signal_key) WHERE signal_key IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass
    log.info("DB initialised at %s", DB_PATH)


def insert_snapshots(rows: list[dict]) -> int:
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


def insert_underlying_price(
    symbol: str, price: float, pct_change: float | None, fetched_at: str
) -> None:
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
        "severity": alert.get("severity", "LOW"),
        "digest_id": alert.get("digest_id"),
    }
    with get_conn() as conn:
        row = conn.execute(sql, row_data).fetchone()
        return row["id"]


def mark_telegram_sent(alert_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE anomaly_alerts SET telegram_sent=1 WHERE id=?", (alert_id,)
        )


def get_previous_snapshot(
    symbol: str, expiry: str, strike: float, option_type: str
) -> dict | None:
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
    from datetime import datetime, timedelta, timezone

    from config.runtime_config import get_scan_frequency_mcx, get_scan_frequency_nse
    from config.symbol_classes import get_symbol_class

    class_key = get_symbol_class(symbol)
    if class_key == "MCX_COMMODITY":
        freq_min = get_scan_frequency_mcx()
    else:
        freq_min = get_scan_frequency_nse()

    try:
        ts = fetched_at.replace("Z", "+00:00")
        curr_dt = datetime.fromisoformat(ts)
        if curr_dt.tzinfo is None:
            curr_dt = curr_dt.replace(tzinfo=timezone.utc)
        else:
            curr_dt = curr_dt.astimezone(timezone.utc)
    except Exception:
        curr_dt = datetime.now(timezone.utc)

    target_time = curr_dt - timedelta(minutes=freq_min)

    sql = """
        SELECT * FROM underlying_price
        WHERE symbol=? AND fetched_at < ?
        ORDER BY fetched_at DESC
        LIMIT 50
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol, fetched_at)).fetchall()
        if not rows:
            return None

        best_row = None
        min_diff = None
        for r in rows:
            try:
                row_str = r["fetched_at"].replace("Z", "+00:00")
                row_dt = datetime.fromisoformat(row_str)
                if row_dt.tzinfo is None:
                    row_dt = row_dt.replace(tzinfo=timezone.utc)
                else:
                    row_dt = row_dt.astimezone(timezone.utc)

                diff = abs((row_dt - target_time).total_seconds())
                if min_diff is None or diff < min_diff:
                    min_diff = diff
                    best_row = r
            except Exception:
                continue

        return dict(best_row) if best_row else None


def get_latest_snapshots_for_symbol(symbol: str, expiry: str) -> list[dict]:
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
    from datetime import datetime, timedelta, timezone

    from config.runtime_config import get_scan_frequency_mcx, get_scan_frequency_nse
    from config.symbol_classes import get_symbol_class

    class_key = get_symbol_class(symbol)
    if class_key == "MCX_COMMODITY":
        freq_min = get_scan_frequency_mcx()
    else:
        freq_min = get_scan_frequency_nse()

    now_utc = datetime.now(timezone.utc)
    target_time = now_utc - timedelta(minutes=freq_min)

    # Fetch distinct fetched_at values to find the one closest to target_time
    sql_fetched_ats = """
        SELECT DISTINCT fetched_at FROM option_chain_snapshots
        WHERE symbol=? AND expiry=?
        ORDER BY fetched_at DESC
        LIMIT 50
    """
    with get_conn() as conn:
        rows = conn.execute(sql_fetched_ats, (symbol, expiry)).fetchall()
        if not rows:
            return {}

        fetched_ats = []
        for r in rows:
            fetched_at_str = r["fetched_at"]
            try:
                fs = fetched_at_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(fs)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)

                # Ignore snapshots that are too close to 'now' (e.g. less than 5 seconds ago)
                # to prevent picking the current scan if it was already inserted.
                if (now_utc - dt).total_seconds() > 5:
                    fetched_ats.append((fetched_at_str, dt))
            except Exception:
                continue

        if not fetched_ats:
            # Fallback: if all snapshots were excluded, just pick the absolute latest one
            sql_fallback = """
                SELECT * FROM option_chain_snapshots
                WHERE symbol=? AND expiry=? AND fetched_at=(
                    SELECT MAX(fetched_at) FROM option_chain_snapshots
                    WHERE symbol=? AND expiry=?
                )
            """
            fallback_rows = conn.execute(
                sql_fallback, (symbol, expiry, symbol, expiry)
            ).fetchall()
            return {(r["strike"], r["option_type"]): dict(r) for r in fallback_rows}

        # Find the fetched_at closest to target_time
        best_fetched_at_str = None
        min_diff = None
        for fs, dt in fetched_ats:
            diff = abs((dt - target_time).total_seconds())
            if min_diff is None or diff < min_diff:
                min_diff = diff
                best_fetched_at_str = fs

        # Fetch all snapshots matching this best_fetched_at_str
        sql_select = """
            SELECT * FROM option_chain_snapshots
            WHERE symbol=? AND expiry=? AND fetched_at=?
        """
        result_rows = conn.execute(
            sql_select, (symbol, expiry, best_fetched_at_str)
        ).fetchall()
        return {(r["strike"], r["option_type"]): dict(r) for r in result_rows}


def get_latest_n_underlying(symbol: str, n: int = 4) -> list[dict]:
    sql = """
        SELECT * FROM underlying_price WHERE symbol=?
        ORDER BY fetched_at DESC LIMIT ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol, n)).fetchall()
        return [dict(r) for r in rows]


def get_latest_n_snapshots(symbol: str, expiry: str, n: int = 3) -> list[list[dict]]:
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


def get_scan_summary_at_least_1h_old(
    symbol: str, current_fetched_at: str
) -> dict | None:
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
                row_dt = datetime.fromisoformat(
                    row["fetched_at"].replace("Z", "+00:00")
                )
                if curr_dt - row_dt >= timedelta(hours=1):
                    return dict(row)
            except Exception:
                continue
    return None


def get_today_scan_count(symbol: str, current_fetched_at: str) -> int:
    """
    Return count of scan summaries saved for the current IST calendar day.

    P3 fix (#14): Uses IST midnight as the day boundary (UTC+05:30) instead
    of UTC midnight. Previously a scan at 00:30 UTC (06:00 IST) on a new
    IST calendar day was counted against the prior day's quota because the
    UTC date string was used directly for the LIKE comparison.
    """
    try:
        # Parse fetched_at (may be UTC ISO string with or without Z/offset)
        ts = current_fetched_at.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(ts)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        dt_ist = dt_utc.astimezone(_IST)
        ist_date_str = dt_ist.strftime("%Y-%m-%d")
    except Exception:
        # Fallback: strip date prefix from raw string (UTC, best-effort)
        ist_date_str = current_fetched_at.split("T")[0]

    # fetched_at is stored as UTC ISO strings; compute IST day window in UTC
    # and query with a BETWEEN so the index on fetched_at is used.
    try:
        ist_midnight = datetime(
            int(ist_date_str[:4]),
            int(ist_date_str[5:7]),
            int(ist_date_str[8:10]),
            tzinfo=_IST,
        )
        window_start_utc = ist_midnight.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        window_end_ist = ist_midnight + timedelta(days=1)
        window_end_utc = window_end_ist.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
    except Exception:
        # Fallback to UTC date LIKE (pre-fix behaviour)
        date_str = current_fetched_at.split("T")[0]
        sql = "SELECT COUNT(*) FROM scan_summaries WHERE symbol=? AND fetched_at LIKE ?"
        with get_conn() as conn:
            row = conn.execute(sql, (symbol, f"{date_str}%")).fetchone()
        return row[0] if row else 0

    sql = """
        SELECT COUNT(*) FROM scan_summaries
        WHERE symbol = ?
          AND fetched_at >= ?
          AND fetched_at <  ?
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol, window_start_utc, window_end_utc)).fetchone()
    return int(row[0]) if row else 0


def get_scan_summary_n_scans_ago(symbol: str, n: int) -> dict | None:
    sql = """
        SELECT total_ce_oi, total_pe_oi, fetched_at FROM scan_summaries
        WHERE symbol=?
        ORDER BY fetched_at DESC
        LIMIT 1 OFFSET ?
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol, n - 1)).fetchone()
        return dict(row) if row else None


def get_recent_alerts_for_symbol(symbol: str, limit: int = 50) -> list[dict]:
    sql = """
        SELECT verdict_label FROM scan_summaries
        WHERE symbol = ?
          AND verdict_label IS NOT NULL
        ORDER BY fetched_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol, limit)).fetchall()
        return [dict(r) for r in rows]


def insert_paper_trade(trade: dict) -> int:
    """
    Insert a new paper trade row.
    B10: Uses INSERT OR IGNORE when signal_key is present — safe against
    duplicate rows on pipeline retry after crash. Returns 0 if deduped.
    """
    from config.settings import LOT_SIZES
    signal_key = trade.get("signal_key")
    verb = "INSERT OR IGNORE" if signal_key else "INSERT"

    sql = f"""
        {verb} INTO paper_trades
            (opened_at, symbol, expiry, verdict_label, side, option_type, strike, entry_underlying,
             entry_premium, sl_underlying, sl_premium, target_underlying, target_premium,
             lots, lot_size, status, reason, digest_id,
             trade_status, setup_type, decision_reason,
             confidence_score, entry_quality_score, trend_alignment_score, regime_score,
             signal_key, pyramid_level, max_favorable_r,
             price_change_pct, pcr, ce_oi_change, pe_oi_change, underlying,
             support, resistance, max_pain, days_to_expiry, chart_conflict,
             rsi_1h, rsi_3h, regime)
        VALUES
            (:opened_at, :symbol, :expiry, :verdict_label, :side, :option_type, :strike, :entry_underlying,
             :entry_premium, :sl_underlying, :sl_premium, :target_underlying, :target_premium,
             :lots, :lot_size, :status, :reason, :digest_id,
             :trade_status, :setup_type, :decision_reason,
             :confidence_score, :entry_quality_score, :trend_alignment_score, :regime_score,
             :signal_key, :pyramid_level, :max_favorable_r,
             :price_change_pct, :pcr, :ce_oi_change, :pe_oi_change, :underlying,
             :support, :resistance, :max_pain, :days_to_expiry, :chart_conflict,
             :rsi_1h, :rsi_3h, :regime)
        RETURNING id
    """
    row_data = {
        "side": trade.get("side", "BUY"),
        "trade_status": trade.get("trade_status", "TRIGGERED_CORE"),
        "setup_type": trade.get("setup_type"),
        "decision_reason": trade.get("decision_reason"),
        "confidence_score": trade.get("confidence_score"),
        "entry_quality_score": trade.get("entry_quality_score"),
        "trend_alignment_score": trade.get("trend_alignment_score"),
        "regime_score": trade.get("regime_score"),
        "signal_key": signal_key,
        "pyramid_level": trade.get("pyramid_level", 1),
        "max_favorable_r": trade.get("max_favorable_r", 0.0),
        "lot_size": trade["lot_size"] if "lot_size" in trade else LOT_SIZES.get(trade.get("symbol", "").upper(), 1),
        # Phase 0: ML feature columns (captured at trade open time)
        "price_change_pct": trade.get("price_change_pct"),
        "pcr": trade.get("pcr"),
        "ce_oi_change": trade.get("ce_oi_change"),
        "pe_oi_change": trade.get("pe_oi_change"),
        "underlying": trade.get("underlying"),
        "support": trade.get("support"),
        "resistance": trade.get("resistance"),
        "max_pain": trade.get("max_pain"),
        "days_to_expiry": trade.get("days_to_expiry"),
        "chart_conflict": trade.get("chart_conflict"),
        "rsi_1h": trade.get("rsi_1h"),
        "rsi_3h": trade.get("rsi_3h"),
        "regime": trade.get("regime"),
        **{
            k: trade.get(k)
            for k in (
                "opened_at",
                "symbol",
                "expiry",
                "verdict_label",
                "option_type",
                "strike",
                "entry_underlying",
                "entry_premium",
                "sl_underlying",
                "sl_premium",
                "target_underlying",
                "target_premium",
                "lots",
                "status",
                "reason",
                "digest_id",
            )
        },
    }
    with get_conn() as conn:
        row = conn.execute(sql, row_data).fetchone()
        return int(row["id"]) if row else 0


def insert_scan_summary(summary: dict, is_fallback: bool = False) -> None:
    sql = """
        INSERT INTO scan_summaries
            (symbol, expiry, fetched_at, digest_id, underlying, atm_strike,
             total_ce_oi, total_pe_oi, ce_oi_change, pe_oi_change, pcr, max_pain,
             support, resistance, verdict_label, confidence,
             candle_1h, candle_3h, top_signal_type, top_signal_strike,
             top_signal_option_type, top_signal_severity, top_signal_oi_pct,
             trend_bias, trend_strength, market_regime, is_fallback)
        VALUES
            (:symbol, :expiry, :fetched_at, :digest_id, :underlying, :atm_strike,
             :total_ce_oi, :total_pe_oi, :ce_oi_change, :pe_oi_change, :pcr, :max_pain,
             :support, :resistance, :verdict_label, :confidence,
             :candle_1h, :candle_3h, :top_signal_type, :top_signal_strike,
             :top_signal_option_type, :top_signal_severity, :top_signal_oi_pct,
             :trend_bias, :trend_strength, :market_regime, :is_fallback)
    """
    row_data = {**summary, "is_fallback": 1 if is_fallback else 0}
    with get_conn() as conn:
        conn.execute(sql, row_data)


# ── Transaction cost model (Autopsy fix 4) ─────────────────────────────────
# Costs are approximate but directionally correct for NSE/MCX retail accounts:
#   Options: STT 0.0625% on sell-side premium turnover + ₹20 brokerage + ₹5 exchange
#   Futures: STT 0.01%   on sell-side notional turnover + ₹20 brokerage + ₹5 exchange
# These are per-leg figures. Round-trip (entry + exit) costs are 2× this amount.
# Override this function to plug in broker-specific actuals (e.g. Zerodha / Dhan).


def _calc_transaction_costs(
    option_type: str,
    side: str,
    entry_premium: float,
    entry_underlying: float,
    exit_premium: float,
    exit_underlying: float,
    lot_size: int,
    lots: int,
) -> float:
    """
    Return total round-trip transaction costs in ₹ for one closed trade.

    Modelled as entry leg + exit leg where:
      - Options turnover = premium × lot_size × lots
      - Futures turnover = underlying × lot_size × lots
    STT is only on the sell-side leg.  For SELL-to-open trades the sell
    leg is the *entry* price; for BUY-to-open it is the *exit* price.
    Brokerage (₹20) and exchange charges (₹5) apply to each leg.
    """
    flat_per_leg = 20.0 + 5.0
    round_trip_flat = flat_per_leg * 2

    is_sell_side = side == "SELL"

    if option_type in ("CE", "PE"):
        sell_premium = (entry_premium if is_sell_side else exit_premium)
        sell_turnover = float(sell_premium or 0.0) * lot_size * lots
        stt = sell_turnover * 0.000625
    else:
        sell_price = (entry_underlying if is_sell_side else exit_underlying)
        sell_turnover = float(sell_price or 0.0) * lot_size * lots
        stt = sell_turnover * 0.0001

    return round(stt + round_trip_flat, 2)


def close_paper_trade(
    trade_id: int,
    closed_at: str,
    exit_underlying: float,
    exit_premium: float | None,
    status: str,
    reason: str = "",
) -> None:
    """Close a paper trade and calculate net P&L (post transaction costs)."""
    from config.settings import LOT_SIZES

    with get_conn() as conn:
        row = conn.execute(
            "SELECT symbol, expiry, option_type, verdict_label, entry_underlying, entry_premium, lots, lot_size, strike, side FROM paper_trades WHERE id=? AND status='OPEN'",
            (trade_id,),
        ).fetchone()
        if not row:
            log.debug(
                "close_paper_trade: trade %s not found or already closed, skipping",
                trade_id,
            )
            return

        symbol = row["symbol"]
        expiry = row["expiry"]
        option_type = row["option_type"]
        verdict_label = row["verdict_label"] or ""
        entry_underlying = float(row["entry_underlying"] or 0.0)
        entry_premium = float(row["entry_premium"] or 0.0)
        lots = int(row["lots"] or 1)
        side = row["side"] or "BUY"

        stored_lot_size = row["lot_size"]
        lot_size = int(stored_lot_size) if stored_lot_size is not None else LOT_SIZES.get(symbol.upper(), LOT_SIZES.get(symbol, 1))

        if option_type in ("CE", "PE"):
            if (
                entry_premium
                and entry_premium > 0
                and exit_premium
                and exit_premium > 0
            ):
                if side == "SELL":
                    pnl_points = entry_premium - exit_premium
                else:
                    pnl_points = exit_premium - entry_premium
            elif entry_premium and entry_premium > 0:
                strike = float(row["strike"] or 0.0)
                snap_row = conn.execute(
                    "SELECT ltp FROM option_chain_snapshots WHERE symbol=? AND expiry=? AND strike=? AND option_type=? AND ltp IS NOT NULL AND ltp > 0 ORDER BY fetched_at DESC LIMIT 1",
                    (symbol.upper(), expiry, strike, option_type),
                ).fetchone()
                if snap_row:
                    estimated_exit = float(snap_row["ltp"])
                else:
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
            from src.engine.verdict_sets import is_bearish

            if side == "SELL" or verdict_label == "SHORT" or is_bearish(verdict_label):
                pnl_points = entry_underlying - float(exit_underlying)
            else:
                pnl_points = float(exit_underlying) - entry_underlying

        gross_pnl_rupees = pnl_points * lot_size * lots

        # Autopsy fix 4: deduct transaction costs so pnl_rupees is net.
        tx_cost = _calc_transaction_costs(
            option_type,
            side,
            entry_premium,
            entry_underlying,
            exit_premium if exit_premium is not None else 0.0,
            exit_underlying,
            lot_size,
            lots,
        )
        pnl_rupees = gross_pnl_rupees - tx_cost
        log.debug(
            "close_paper_trade id=%s: gross=%.2f tx_cost=%.2f net=%.2f",
            trade_id,
            gross_pnl_rupees,
            tx_cost,
            pnl_rupees,
        )

        conn.execute(
            """
            UPDATE paper_trades
            SET closed_at=?, exit_underlying=?, exit_premium=?, pnl_points=?, pnl_rupees=?, status=?, reason=?
            WHERE id=? AND status='OPEN'
            """,
            (
                closed_at,
                exit_underlying,
                exit_premium,
                round(pnl_points, 4),
                round(pnl_rupees, 2),
                status,
                reason,
                trade_id,
            ),
        )

    # Phase 1: Invalidate pattern cache after trade close
    try:
        from src.engine.paper_trading import _invalidate_pattern_cache

        _invalidate_pattern_cache()
    except Exception:
        pass


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
        # Log per-symbol/per-expiry breakdown before deleting
        before = conn.execute(
            "SELECT symbol, expiry, COUNT(*) FROM option_chain_snapshots "
            "WHERE expiry < ? GROUP BY symbol, expiry ORDER BY symbol, expiry",
            (today,),
        ).fetchall()
        for sym, exp, cnt in before:
            log.info("[db]  Expired: %-15s expiry %s  (%d rows)", sym, exp, cnt)

        cur1 = conn.execute(
            "DELETE FROM option_chain_snapshots WHERE expiry < ?", (today,)
        )
        cur2 = conn.execute("DELETE FROM scan_summaries WHERE expiry < ?", (today,))
        log.info(
            "[db] Deleted %d expired snapshots and %d expired scan summaries.",
            cur1.rowcount,
            cur2.rowcount,
        )
        return cur1.rowcount + cur2.rowcount


def get_open_live_trade(symbol: str) -> dict | None:
    sql = """
        SELECT * FROM live_trades
        WHERE symbol=? AND status='OPEN' AND (setup_type IS NULL OR setup_type != 'TIMEFRAME')
        ORDER BY opened_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol,)).fetchone()
        return dict(row) if row else None


def get_open_live_timeframe_trades(symbol: str) -> list[dict]:
    sql = """
        SELECT * FROM live_trades
        WHERE symbol=? AND status='OPEN' AND setup_type='TIMEFRAME'
        ORDER BY opened_at DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol,)).fetchall()
        return [dict(r) for r in rows]


def insert_live_trade(trade: dict) -> int:
    signal_key = trade.get("signal_key")
    verb = "INSERT OR IGNORE" if signal_key else "INSERT"

    sql = f"""
        {verb} INTO live_trades
            (opened_at, symbol, expiry, verdict_label, side, option_type, strike, entry_underlying,
             entry_premium, sl_underlying, sl_premium, target_underlying, target_premium,
             lots, status, reason, digest_id,
             trade_status, setup_type, decision_reason,
             confidence_score, entry_quality_score, trend_alignment_score, regime_score,
             signal_key, pyramid_level, max_favorable_r,
             broker_order_id, gtt_order_id, broker_status, broker_message, exit_mode,
             price_change_pct, pcr, ce_oi_change, pe_oi_change, underlying,
             support, resistance, max_pain, days_to_expiry, chart_conflict,
             rsi_1h, rsi_3h, regime)
        VALUES
            (:opened_at, :symbol, :expiry, :verdict_label, :side, :option_type, :strike, :entry_underlying,
             :entry_premium, :sl_underlying, :sl_premium, :target_underlying, :target_premium,
             :lots, :status, :reason, :digest_id,
             :trade_status, :setup_type, :decision_reason,
             :confidence_score, :entry_quality_score, :trend_alignment_score, :regime_score,
             :signal_key, :pyramid_level, :max_favorable_r,
             :broker_order_id, :gtt_order_id, :broker_status, :broker_message, :exit_mode,
             :price_change_pct, :pcr, :ce_oi_change, :pe_oi_change, :underlying,
             :support, :resistance, :max_pain, :days_to_expiry, :chart_conflict,
             :rsi_1h, :rsi_3h, :regime)
        RETURNING id
    """
    row_data = {
        "side": trade.get("side", "BUY"),
        "trade_status": trade.get("trade_status", "TRIGGERED_CORE"),
        "setup_type": trade.get("setup_type"),
        "decision_reason": trade.get("decision_reason"),
        "confidence_score": trade.get("confidence_score"),
        "entry_quality_score": trade.get("entry_quality_score"),
        "trend_alignment_score": trade.get("trend_alignment_score"),
        "regime_score": trade.get("regime_score"),
        "signal_key": signal_key,
        "pyramid_level": trade.get("pyramid_level", 1),
        "max_favorable_r": trade.get("max_favorable_r", 0.0),
        "broker_order_id": trade.get("broker_order_id"),
        "gtt_order_id": trade.get("gtt_order_id"),
        "broker_status": trade.get("broker_status", "OPEN"),
        "broker_message": trade.get("broker_message"),
        "exit_mode": trade.get("exit_mode"),
        # Phase 0: ML feature columns (captured at trade open time)
        "price_change_pct": trade.get("price_change_pct"),
        "pcr": trade.get("pcr"),
        "ce_oi_change": trade.get("ce_oi_change"),
        "pe_oi_change": trade.get("pe_oi_change"),
        "underlying": trade.get("underlying"),
        "support": trade.get("support"),
        "resistance": trade.get("resistance"),
        "max_pain": trade.get("max_pain"),
        "days_to_expiry": trade.get("days_to_expiry"),
        "chart_conflict": trade.get("chart_conflict"),
        "rsi_1h": trade.get("rsi_1h"),
        "rsi_3h": trade.get("rsi_3h"),
        "regime": trade.get("regime"),
        **{
            k: trade.get(k)
            for k in (
                "opened_at",
                "symbol",
                "expiry",
                "verdict_label",
                "option_type",
                "strike",
                "entry_underlying",
                "entry_premium",
                "sl_underlying",
                "sl_premium",
                "target_underlying",
                "target_premium",
                "lots",
                "status",
                "reason",
                "digest_id",
            )
        },
    }
    with get_conn() as conn:
        row = conn.execute(sql, row_data).fetchone()
        return int(row["id"]) if row else 0


def update_live_trade_entry(
    trade_id: int,
    *,
    broker_order_id: str | None = None,
    gtt_order_id: str | None = None,
    broker_status: str | None = None,
    broker_message: str | None = None,
    exit_mode: str | None = None,
    status: str | None = None,
    reason: str | None = None,
) -> None:
    updates: list[str] = []
    params: list = []
    for column, value in (
        ("broker_order_id", broker_order_id),
        ("gtt_order_id", gtt_order_id),
        ("broker_status", broker_status),
        ("broker_message", broker_message),
        ("exit_mode", exit_mode),
        ("status", status),
        ("reason", reason),
    ):
        if value is not None:
            updates.append(f"{column}=?")
            params.append(value)

    if not updates:
        return

    params.append(trade_id)
    sql = f"UPDATE live_trades SET {', '.join(updates)} WHERE id=?"
    with get_conn() as conn:
        conn.execute(sql, tuple(params))


def close_live_trade(
    trade_id: int,
    closed_at: str,
    exit_underlying: float,
    exit_premium: float | None,
    status: str,
    reason: str = "",
) -> None:
    """Close a live trade and calculate net P&L (post transaction costs)."""
    from config.settings import LOT_SIZES

    with get_conn() as conn:
        row = conn.execute(
            "SELECT symbol, expiry, option_type, verdict_label, entry_underlying, entry_premium, lots, strike, side FROM live_trades WHERE id=? AND status='OPEN'",
            (trade_id,),
        ).fetchone()
        if not row:
            log.debug(
                "close_live_trade: trade %s not found or already closed, skipping",
                trade_id,
            )
            return

        symbol = row["symbol"]
        expiry = row["expiry"]
        option_type = row["option_type"]
        verdict_label = row["verdict_label"] or ""
        entry_underlying = float(row["entry_underlying"] or 0.0)
        entry_premium = float(row["entry_premium"] or 0.0)
        lots = int(row["lots"] or 1)
        side = row["side"] or "BUY"

        lot_size = LOT_SIZES.get(symbol.upper(), LOT_SIZES.get(symbol, 1))

        if option_type in ("CE", "PE"):
            if (
                entry_premium
                and entry_premium > 0
                and exit_premium
                and exit_premium > 0
            ):
                if side == "SELL":
                    pnl_points = entry_premium - exit_premium
                else:
                    pnl_points = exit_premium - entry_premium
            elif entry_premium and entry_premium > 0:
                strike = float(row["strike"] or 0.0)
                snap_row = conn.execute(
                    "SELECT ltp FROM option_chain_snapshots WHERE symbol=? AND expiry=? AND strike=? AND option_type=? AND ltp IS NOT NULL AND ltp > 0 ORDER BY fetched_at DESC LIMIT 1",
                    (symbol.upper(), expiry, strike, option_type),
                ).fetchone()
                if snap_row:
                    estimated_exit = float(snap_row["ltp"])
                else:
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
            from src.engine.verdict_sets import is_bearish

            if side == "SELL" or verdict_label == "SHORT" or is_bearish(verdict_label):
                pnl_points = entry_underlying - float(exit_underlying)
            else:
                pnl_points = float(exit_underlying) - entry_underlying

        gross_pnl_rupees = pnl_points * lot_size * lots

        # Autopsy fix 4: deduct transaction costs so pnl_rupees is net.
        tx_cost = _calc_transaction_costs(
            option_type,
            side,
            entry_premium,
            entry_underlying,
            exit_premium if exit_premium is not None else 0.0,
            exit_underlying,
            lot_size,
            lots,
        )
        pnl_rupees = gross_pnl_rupees - tx_cost
        log.debug(
            "close_live_trade id=%s: gross=%.2f tx_cost=%.2f net=%.2f",
            trade_id,
            gross_pnl_rupees,
            tx_cost,
            pnl_rupees,
        )

        conn.execute(
            """
            UPDATE live_trades
            SET closed_at=?, exit_underlying=?, exit_premium=?, pnl_points=?, pnl_rupees=?, status=?, reason=?
            WHERE id=? AND status='OPEN'
            """,
            (
                closed_at,
                exit_underlying,
                exit_premium,
                round(pnl_points, 4),
                round(pnl_rupees, 2),
                status,
                reason,
                trade_id,
            ),
        )


def list_live_trades(symbol: str | None = None, limit: int = 300) -> list[dict]:
    sql = "SELECT * FROM live_trades"
    params: list = []
    if symbol:
        sql += " WHERE symbol=?"
        params.append(symbol)
    sql += " ORDER BY opened_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_broker_config() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM broker_configs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        config = dict(row)

        # Decrypt api_secret and access_token with plaintext fallback
        try:
            from src.services.zerodha_auth import _get_fernet

            f = _get_fernet()
            if config.get("api_secret"):
                try:
                    config["api_secret"] = f.decrypt(
                        config["api_secret"].encode("utf-8")
                    ).decode("utf-8")
                except Exception:
                    pass
            if config.get("access_token"):
                try:
                    config["access_token"] = f.decrypt(
                        config["access_token"].encode("utf-8")
                    ).decode("utf-8")
                except Exception:
                    pass
        except Exception:
            pass
        return config


def update_broker_config(**kwargs) -> None:
    from src.services.zerodha_auth import encrypt_secret

    kwargs_copy = kwargs.copy()
    if "api_secret" in kwargs_copy and kwargs_copy["api_secret"]:
        kwargs_copy["api_secret"] = encrypt_secret(kwargs_copy["api_secret"])
    if "access_token" in kwargs_copy and kwargs_copy["access_token"]:
        kwargs_copy["access_token"] = encrypt_secret(kwargs_copy["access_token"])

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM broker_configs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            broker_id = row["id"]
            sets = []
            vals = []
            for k, v in kwargs_copy.items():
                sets.append(f"{k}=?")
                vals.append(v)
            vals.append(broker_id)
            sql = f"UPDATE broker_configs SET {', '.join(sets)} WHERE id=?"
            conn.execute(sql, tuple(vals))
        else:
            cols = list(kwargs_copy.keys())
            vals = list(kwargs_copy.values())
            placeholders = ", ".join(["?"] * len(cols))
            sql = f"INSERT INTO broker_configs ({', '.join(cols)}) VALUES ({placeholders})"
            conn.execute(sql, tuple(vals))


def set_kill_switch(active: bool) -> None:
    val = 1 if active else 0
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM broker_configs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE broker_configs SET kill_switch_active=? WHERE id=?",
                (val, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO broker_configs (kill_switch_active) VALUES (?)", (val,)
            )
