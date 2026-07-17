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
CREATE UNIQUE INDEX IF NOT EXISTS uq_oc_snap
    ON option_chain_snapshots (fetched_at, symbol, expiry, strike, option_type);

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
    reason              TEXT,                -- entry reason (decision_reason at open time)
    exit_reason         TEXT,                -- exit reason (set at close)
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
    regime_score        INTEGER,
    entry_dev_pct       REAL
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
    regime_score        INTEGER,
    entry_dev_pct       REAL
);

CREATE INDEX IF NOT EXISTS idx_live_symbol_status
    ON live_trades (symbol, status, opened_at);

CREATE INDEX IF NOT EXISTS idx_live_trades_status
    ON live_trades (status);

CREATE INDEX IF NOT EXISTS idx_live_trades_status_setup_type
    ON live_trades (status, setup_type);

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

CREATE TABLE IF NOT EXISTS decision_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    engine          TEXT NOT NULL,       -- CORE_OI | TIMEFRAME
    symbol          TEXT NOT NULL,
    direction       TEXT,                -- LONG | SHORT | NULL
    action          TEXT NOT NULL,       -- TRADE | SKIP
    signal_score    REAL,
    rule_passed     INTEGER,
    ai_score        REAL,
    ai_agrees       INTEGER,
    entry_quality   REAL,
    trend_score     REAL,
    regime_score    REAL,
    risk_passed     INTEGER,
    risk_sub_check  TEXT,
    block_step      TEXT,                -- First failing step name (NULL if TRADE)
    block_reason    TEXT,
    trail_json      TEXT,                -- Full list[StepResult] as JSON
    trade_id        INTEGER,             -- FK to paper_trades.id (NULL if SKIP)
    bar_end_utc     TEXT,
    scan_fetched_at TEXT,
    -- TFSS v4 audit fields (plan §4.9)
    core_origin_verdict    TEXT,          -- Original Core verdict (e.g. GO_LONG, Long Buildup)
    core_execution_intent  TEXT,          -- TFSS resolved side (SELL_PE / SELL_CE / empty)
    primary_trigger        TEXT,          -- Selected exit trigger
    persistence_source     TEXT,          -- native_5scan / empty
    persistence_agreeing_count INTEGER    -- 3-5 agreeing scans
);

CREATE INDEX IF NOT EXISTS idx_da_symbol_ts ON decision_audit(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_da_action ON decision_audit(action, engine);

CREATE TABLE IF NOT EXISTS ng_parity_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,             -- IST ISO
    nymex_last REAL, usdinr REAL, fair_value REAL,
    mcx_last REAL, dev_pct REAL,
    nymex_age_sec INTEGER, fx_age_sec INTEGER, mcx_age_sec INTEGER,
    mcx_src TEXT, fx_src TEXT, nymex_src TEXT,
    regime TEXT,                  -- PARITY/MOMENTUM/EVENT/BLOCKED
    valid INTEGER
);

CREATE TABLE IF NOT EXISTS eia_consensus (
    report_date TEXT PRIMARY KEY, -- Thursday date
    consensus_bcf REAL,           -- expected build(+)/draw(-)
    actual_bcf REAL,              -- filled post-release
    surprise_bcf REAL,
    fetched_at TEXT, source TEXT
);

CREATE TABLE IF NOT EXISTS ng_weather_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                   -- IST ISO
    source TEXT NOT NULL,               -- open-meteo-gfs / open-meteo-ecmwf / nws
    hdd_15d REAL,                       -- population-weighted 15-day HDD sum
    cdd_15d REAL,                       -- population-weighted 15-day CDD sum
    delta_hdd REAL,                     -- revision vs previous run (same source)
    delta_cdd REAL,
    zscore REAL,                        -- revision z vs trailing 30 runs (seasonal-aware)
    gulf_storm_active INTEGER,          -- 1 = active Gulf tropical system
    valid INTEGER                       -- 1 = successful fetch
);

CREATE INDEX IF NOT EXISTS idx_nwr_ts ON ng_weather_runs(ts);
CREATE INDEX IF NOT EXISTS idx_nwr_source_ts ON ng_weather_runs(source, ts DESC);

CREATE TABLE IF NOT EXISTS fii_positioning (
    report_date TEXT PRIMARY KEY, -- YYYY-MM-DD
    fii_index_long INTEGER,
    fii_index_short INTEGER,
    client_index_long INTEGER,
    client_index_short INTEGER,
    dii_cash_net REAL,
    fii_cash_net REAL,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS multi_leg_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_ref INTEGER,
    symbol TEXT NOT NULL,
    structure TEXT NOT NULL,
    net_premium REAL,
    margin_req REAL,
    total_pnl REAL,
    opened_at TEXT,
    closed_at TEXT,
    status TEXT DEFAULT 'OPEN',
    reason TEXT,
    profit_factor REAL
);

CREATE TABLE IF NOT EXISTS multi_leg_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    side TEXT NOT NULL,
    lots INTEGER NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL,
    entry_premium REAL,
    exit_premium REAL,
    FOREIGN KEY (trade_id) REFERENCES multi_leg_trades(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mll_trade_id ON multi_leg_legs (trade_id);

-- OPS Agent: health state tracking (separate read path for ops_agent.py)
CREATE TABLE IF NOT EXISTS health_state (
    key         TEXT PRIMARY KEY,       -- component name
    status      TEXT,                   -- OK | DEGRADED | DOWN
    detail      TEXT,
    updated_at  TEXT                    -- IST ISO
);

-- ── ADR-007 v2 Schema Tables ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT, engine TEXT,                 -- CORE_OI / TIMEFRAME
    rule_action TEXT, rule_block_reason TEXT,
    old_ai_would_boost INTEGER,               -- OLD rule: ai_agrees & conf>=80
    ai_bias TEXT, ai_conf INTEGER, ai_veto_flag INTEGER, ai_veto_reason TEXT,
    empirical_n INTEGER, empirical_winrate REAL, empirical_avg_pnl REAL,
    final_action TEXT, setup_type TEXT,
    outcome_pnl REAL, outcome_filled_at TEXT  -- backfilled on close/expiry
);
CREATE INDEX IF NOT EXISTS idx_shadow_decisions_symbol_ts ON shadow_decisions (symbol, ts);

CREATE TABLE IF NOT EXISTS shadow_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, symbol TEXT,
    model_version TEXT, p_success REAL,
    features_json TEXT,                        -- Phase-0 roadmap dependency
    decision_id INTEGER,                       -- FK -> shadow_decisions
    outcome INTEGER                            -- backfilled: 1 win / 0 loss
);
CREATE INDEX IF NOT EXISTS idx_shadow_predictions_symbol_ts ON shadow_predictions (symbol, ts);

CREATE TABLE IF NOT EXISTS trade_autopsies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER, ts TEXT,
    reasons_held INTEGER, primary_failure TEXT, note TEXT,
    llm_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_autopsies_trade_id ON trade_autopsies (trade_id);

CREATE TABLE IF NOT EXISTS pattern_stats_rollup (
    symbol TEXT, verdict_label TEXT, pcr_regime TEXT,
    n_trades INTEGER, win_rate REAL, avg_pnl REAL,
    computed_at TEXT,
    PRIMARY KEY (symbol, verdict_label, pcr_regime)
);

-- L5 FIX: Track applied schema migrations to avoid re-running every startup
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

"""


@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=30.0)
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
    ("M001_add_ltp_change_pct", "ALTER TABLE option_chain_snapshots ADD COLUMN ltp_change_pct REAL"),
    ("M002_add_oi_change_pct", "ALTER TABLE option_chain_snapshots ADD COLUMN oi_change_pct REAL"),
    ("M003_add_alert_severity", "ALTER TABLE anomaly_alerts ADD COLUMN severity TEXT DEFAULT 'LOW'"),
    ("M004_add_alert_digest_id", "ALTER TABLE anomaly_alerts ADD COLUMN digest_id TEXT"),
    ("M005_add_dedup_severity", "ALTER TABLE alert_dedup    ADD COLUMN severity TEXT DEFAULT 'LOW'"),
    ("M006_add_entry_premium", "ALTER TABLE paper_trades ADD COLUMN entry_premium REAL"),
    ("M007_add_exit_premium", "ALTER TABLE paper_trades ADD COLUMN exit_premium REAL"),
    ("M008_add_sl_premium", "ALTER TABLE paper_trades ADD COLUMN sl_premium REAL"),
    ("M009_add_target_premium", "ALTER TABLE paper_trades ADD COLUMN target_premium REAL"),
    ("M010_add_lots", "ALTER TABLE paper_trades ADD COLUMN lots INTEGER DEFAULT 1"),
    ("M011_add_lot_size", "ALTER TABLE paper_trades ADD COLUMN lot_size INTEGER DEFAULT 1"),
    ("M012_add_pnl_rupees", "ALTER TABLE paper_trades ADD COLUMN pnl_rupees REAL DEFAULT 0"),
    ("M013_add_trade_status", "ALTER TABLE paper_trades ADD COLUMN trade_status TEXT DEFAULT 'TRIGGERED_CORE'"),
    ("M014_add_setup_type", "ALTER TABLE paper_trades ADD COLUMN setup_type TEXT"),
    ("M015_add_decision_reason", "ALTER TABLE paper_trades ADD COLUMN decision_reason TEXT"),
    ("M016_add_confidence_score", "ALTER TABLE paper_trades ADD COLUMN confidence_score INTEGER"),
    ("M017_add_entry_quality_score", "ALTER TABLE paper_trades ADD COLUMN entry_quality_score INTEGER"),
    ("M018_add_trend_alignment_score", "ALTER TABLE paper_trades ADD COLUMN trend_alignment_score INTEGER"),
    ("M019_add_regime_score", "ALTER TABLE paper_trades ADD COLUMN regime_score INTEGER"),
    ("M020_add_signal_key", "ALTER TABLE paper_trades ADD COLUMN signal_key TEXT"),
    ("M021_add_pyramid_level", "ALTER TABLE paper_trades ADD COLUMN pyramid_level INTEGER DEFAULT 1"),
    ("M022_add_max_favorable_r", "ALTER TABLE paper_trades ADD COLUMN max_favorable_r REAL DEFAULT 0"),
    ("M023_add_side", "ALTER TABLE paper_trades ADD COLUMN side TEXT DEFAULT 'BUY'"),
    ("M024_add_is_fallback", "ALTER TABLE scan_summaries ADD COLUMN is_fallback INTEGER DEFAULT 0"),
    ("M025_add_live_trade_status", "ALTER TABLE live_trades ADD COLUMN trade_status TEXT DEFAULT 'TRIGGERED_CORE'"),
    ("M026_add_live_setup_type", "ALTER TABLE live_trades ADD COLUMN setup_type TEXT"),
    ("M027_add_live_decision_reason", "ALTER TABLE live_trades ADD COLUMN decision_reason TEXT"),
    ("M028_add_live_confidence_score", "ALTER TABLE live_trades ADD COLUMN confidence_score INTEGER"),
    ("M029_add_live_entry_quality_score", "ALTER TABLE live_trades ADD COLUMN entry_quality_score INTEGER"),
    ("M030_add_live_trend_alignment_score", "ALTER TABLE live_trades ADD COLUMN trend_alignment_score INTEGER"),
    ("M031_add_live_regime_score", "ALTER TABLE live_trades ADD COLUMN regime_score INTEGER"),
    ("M032_add_paper_expiry", "ALTER TABLE paper_trades ADD COLUMN expiry TEXT"),
    ("M033_add_live_expiry", "ALTER TABLE live_trades ADD COLUMN expiry TEXT"),
    ("M034_add_paper_price_change_pct", "ALTER TABLE paper_trades ADD COLUMN price_change_pct REAL"),
    ("M035_add_paper_pcr", "ALTER TABLE paper_trades ADD COLUMN pcr REAL"),
    ("M036_add_paper_ce_oi_change", "ALTER TABLE paper_trades ADD COLUMN ce_oi_change REAL"),
    ("M037_add_paper_pe_oi_change", "ALTER TABLE paper_trades ADD COLUMN pe_oi_change REAL"),
    ("M038_add_paper_underlying", "ALTER TABLE paper_trades ADD COLUMN underlying REAL"),
    ("M039_add_paper_support", "ALTER TABLE paper_trades ADD COLUMN support REAL"),
    ("M040_add_paper_resistance", "ALTER TABLE paper_trades ADD COLUMN resistance REAL"),
    ("M041_add_paper_max_pain", "ALTER TABLE paper_trades ADD COLUMN max_pain REAL"),
    ("M042_add_paper_days_to_expiry", "ALTER TABLE paper_trades ADD COLUMN days_to_expiry INTEGER"),
    ("M043_add_paper_chart_conflict", "ALTER TABLE paper_trades ADD COLUMN chart_conflict INTEGER"),
    ("M044_add_paper_rsi_1h", "ALTER TABLE paper_trades ADD COLUMN rsi_1h REAL"),
    ("M045_add_paper_rsi_3h", "ALTER TABLE paper_trades ADD COLUMN rsi_3h REAL"),
    ("M046_add_paper_regime", "ALTER TABLE paper_trades ADD COLUMN regime TEXT"),
    ("M047_add_live_price_change_pct", "ALTER TABLE live_trades ADD COLUMN price_change_pct REAL"),
    ("M048_add_live_pcr", "ALTER TABLE live_trades ADD COLUMN pcr REAL"),
    ("M049_add_live_ce_oi_change", "ALTER TABLE live_trades ADD COLUMN ce_oi_change REAL"),
    ("M050_add_live_pe_oi_change", "ALTER TABLE live_trades ADD COLUMN pe_oi_change REAL"),
    ("M051_add_live_underlying", "ALTER TABLE live_trades ADD COLUMN underlying REAL"),
    ("M052_add_live_support", "ALTER TABLE live_trades ADD COLUMN support REAL"),
    ("M053_add_live_resistance", "ALTER TABLE live_trades ADD COLUMN resistance REAL"),
    ("M054_add_live_max_pain", "ALTER TABLE live_trades ADD COLUMN max_pain REAL"),
    ("M055_add_live_days_to_expiry", "ALTER TABLE live_trades ADD COLUMN days_to_expiry INTEGER"),
    ("M056_add_live_chart_conflict", "ALTER TABLE live_trades ADD COLUMN chart_conflict INTEGER"),
    ("M057_add_live_rsi_1h", "ALTER TABLE live_trades ADD COLUMN rsi_1h REAL"),
    ("M058_add_live_rsi_3h", "ALTER TABLE live_trades ADD COLUMN rsi_3h REAL"),
    ("M059_add_live_regime", "ALTER TABLE live_trades ADD COLUMN regime TEXT"),
    ("M060_add_broker_user_id", "ALTER TABLE broker_configs ADD COLUMN user_id TEXT"),
    ("M061_add_broker_password", "ALTER TABLE broker_configs ADD COLUMN password TEXT"),
    ("M062_add_paper_entry_dev_pct", "ALTER TABLE paper_trades ADD COLUMN entry_dev_pct REAL"),
    ("M063_add_live_entry_dev_pct", "ALTER TABLE live_trades ADD COLUMN entry_dev_pct REAL"),
    ("M064_add_core_origin_verdict", "ALTER TABLE decision_audit ADD COLUMN core_origin_verdict TEXT"),
    ("M065_add_core_execution_intent", "ALTER TABLE decision_audit ADD COLUMN core_execution_intent TEXT"),
    ("M066_add_primary_trigger", "ALTER TABLE decision_audit ADD COLUMN primary_trigger TEXT"),
    ("M067_add_persistence_source", "ALTER TABLE decision_audit ADD COLUMN persistence_source TEXT"),
    ("M068_add_persistence_agreeing_count", "ALTER TABLE decision_audit ADD COLUMN persistence_agreeing_count INTEGER"),
    ("M069_add_live_lot_size", "ALTER TABLE live_trades ADD COLUMN lot_size INTEGER DEFAULT 1"),
    ("M070_add_paper_reason", "ALTER TABLE paper_trades ADD COLUMN reason TEXT"),
    ("M071_add_paper_exit_reason", "ALTER TABLE paper_trades ADD COLUMN exit_reason TEXT"),
    ("M072_add_live_exit_reason", "ALTER TABLE live_trades ADD COLUMN exit_reason TEXT"),
]


def init_db() -> None:
    """Create tables + run safe column migrations. Call on every startup."""
    with get_conn() as conn:
        conn.executescript(DDL)
        applied = {
            row["migration_id"]
            for row in conn.execute("SELECT migration_id FROM schema_migrations").fetchall()
        }
        for migration_id, sql in _MIGRATIONS:
            if migration_id not in applied:
                try:
                    conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                        (migration_id, datetime.now(timezone.utc).isoformat()),
                    )
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
        INSERT OR REPLACE INTO option_chain_snapshots
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
    from config.runtime_config import get_scan_frequency_mcx, get_scan_frequency_nse
    from config.symbol_classes import get_symbol_class

    class_key = get_symbol_class(symbol)
    if class_key == "MCX_COMMODITY":
        freq_min = get_scan_frequency_mcx()
    else:
        freq_min = get_scan_frequency_nse()

    # Parse fetched_at to UTC — always use this as reference, NOT datetime.now()
    try:
        ts = fetched_at.replace("Z", "+00:00")
        curr_dt = datetime.fromisoformat(ts)
        if curr_dt.tzinfo is None:
            curr_dt = curr_dt.replace(tzinfo=timezone.utc)
        else:
            curr_dt = curr_dt.astimezone(timezone.utc)
    except Exception:
        log = logging.getLogger(__name__)
        log.warning(
            "Cannot parse fetched_at=%s — falling back to current time", fetched_at
        )
        curr_dt = datetime.now(timezone.utc)

    target_time = curr_dt - timedelta(minutes=freq_min)
    # Allow ±2x frequency window to find the previous scan's underlying
    window_sec = freq_min * 2 * 60
    window_start = (target_time - timedelta(seconds=window_sec)).isoformat()
    window_end = curr_dt.isoformat()

    sql = """
        SELECT * FROM underlying_price
        WHERE symbol=? AND fetched_at >= ? AND fetched_at < ?
        ORDER BY fetched_at DESC
        LIMIT 20
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol, window_start, window_end)).fetchall()
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

        # Staleness guard: if the closest row is > 3x frequency away, reject
        if best_row and min_diff is not None and min_diff > freq_min * 3 * 60:
            return None

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


def get_prev_snapshots_bulk(
    symbol: str, expiry: str, fetched_at: str | None = None
) -> dict[tuple, dict]:
    """
    Fetch the previous bulk snapshot (option chain) for a symbol/expiry pair.

    BUG-007 FIX: Now accepts an optional `fetched_at` parameter to use as the
    reference time instead of `datetime.now()`. This aligns with the pattern
    in `get_previous_underlying_before()` and prevents noise-suppression
    failures on delayed scan cycles where `now()` drifts from the actual
    scan timestamp.

    Args:
        symbol: Trading symbol (e.g., "NIFTY")
        expiry: Expiry date string
        fetched_at: Optional ISO timestamp to use as reference time.
                   If None, uses datetime.now(timezone.utc).

    Returns:
        Dict mapping (strike, option_type) tuples to snapshot row dicts.
    """
    from config.runtime_config import get_scan_frequency_mcx, get_scan_frequency_nse
    from config.symbol_classes import get_symbol_class

    class_key = get_symbol_class(symbol)
    if class_key == "MCX_COMMODITY":
        freq_min = get_scan_frequency_mcx()
    else:
        freq_min = get_scan_frequency_nse()

    # BUG-007 FIX: Use fetched_at parameter if provided, else fall back to now()
    if fetched_at:
        try:
            ts = fetched_at.replace("Z", "+00:00")
            now_utc = datetime.fromisoformat(ts)
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)
            else:
                now_utc = now_utc.astimezone(timezone.utc)
        except Exception:
            log.warning(
                "get_prev_snapshots_bulk: cannot parse fetched_at=%s, falling back to now()",
                fetched_at,
            )
            now_utc = datetime.now(timezone.utc)
    else:
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
            # All stored snapshots were inserted in the last 5 seconds — no valid baseline.
            return {}

        # Find the fetched_at closest to target_time
        best_fetched_at_str = None
        best_dt = None
        min_diff = None
        for fs, dt in fetched_ats:
            diff = abs((dt - target_time).total_seconds())
            if min_diff is None or diff < min_diff:
                min_diff = diff
                best_fetched_at_str = fs
                best_dt = dt

        # ── Staleness guard ────────────────────────────────────────────────────
        # If the best candidate is older than max_age_minutes, it belongs to a
        # different trading session (e.g. Thursday close vs Monday open).
        # Returning stale baselines causes every OI value to look like a massive
        # spike (+91000%) which floods anomaly detection with false positives.
        # NSE: 4h cap   MCX: 6h cap
        if class_key == "MCX_COMMODITY":
            max_age_minutes = 360  # 6 hours
        else:
            max_age_minutes = 240  # 4 hours

        if best_dt is not None:
            age_minutes = (now_utc - best_dt).total_seconds() / 60
            # Handle cross-session: if best_dt is in the future relative to now_utc
            # (e.g., yesterday's 10:00 UTC vs today's 04:00 UTC), the age is negative
            # which means the baseline belongs to a different trading session.
            if age_minutes < 0 or age_minutes > max_age_minutes:
                import logging as _log

                _log.getLogger(__name__).debug(
                    "[schema] get_prev_snapshots_bulk: %s prev snapshot age=%.0f min "
                    "(> %d min cap or cross-session) — returning empty baseline to suppress cross-session noise",
                    symbol,
                    age_minutes,
                    max_age_minutes,
                )
                return {}

            # Session boundary check: if previous scan and current scan are on different
            # IST calendar days, treat as cross-session to avoid false OI spikes
            # at market open (e.g., yesterday 15:30 vs today 09:30 = different trading day).
            try:
                ist = timezone(timedelta(hours=5, minutes=30))
                now_ist = now_utc.astimezone(ist)
                prev_ist = best_dt.astimezone(ist)
                if now_ist.date() != prev_ist.date():
                    import logging as _log

                    _log.getLogger(__name__).debug(
                        "[schema] get_prev_snapshots_bulk: %s cross-session (prev=%s, now=%s) — "
                        "returning empty baseline",
                        symbol,
                        prev_ist.strftime("%Y-%m-%d %H:%M IST"),
                        now_ist.strftime("%Y-%m-%d %H:%M IST"),
                    )
                    return {}
            except Exception:
                pass

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


def get_open_timeframe_trades(symbol: str, table: str = "paper_trades") -> list[dict]:
    if table not in ("paper_trades", "live_trades"):
        table = "paper_trades"
    sql = f"""
        SELECT * FROM {table}
        WHERE symbol=? AND status='OPEN' AND setup_type='TIMEFRAME'
        ORDER BY opened_at DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol,)).fetchall()
        return [dict(r) for r in rows]


def get_scan_summary_at_least_1h_old(
    symbol: str, current_fetched_at: str
) -> dict | None:
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
    """
    Return the scan_summary row at OFFSET n (0-indexed).
    n=0 returns the most recent summary, n=1 returns the 2nd most recent, etc.
    """
    sql = """
        SELECT total_ce_oi, total_pe_oi, fetched_at FROM scan_summaries
        WHERE symbol=?
        ORDER BY fetched_at DESC
        LIMIT 1 OFFSET ?
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol, n)).fetchone()
        return dict(row) if row else None


def get_recent_alerts_for_symbol(symbol: str, limit: int = 50) -> list[dict]:
    # Flaw #8: Stale Alert Persistence Vulnerability
    sql = """
        SELECT verdict_label FROM scan_summaries
        WHERE symbol = ?
          AND verdict_label IS NOT NULL
          AND fetched_at >= datetime('now', '-24 hours')
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
             rsi_1h, rsi_3h, regime, entry_dev_pct)
        VALUES
            (:opened_at, :symbol, :expiry, :verdict_label, :side, :option_type, :strike, :entry_underlying,
             :entry_premium, :sl_underlying, :sl_premium, :target_underlying, :target_premium,
             :lots, :lot_size, :status, :reason, :digest_id,
             :trade_status, :setup_type, :decision_reason,
             :confidence_score, :entry_quality_score, :trend_alignment_score, :regime_score,
             :signal_key, :pyramid_level, :max_favorable_r,
             :price_change_pct, :pcr, :ce_oi_change, :pe_oi_change, :underlying,
             :support, :resistance, :max_pain, :days_to_expiry, :chart_conflict,
             :rsi_1h, :rsi_3h, :regime, :entry_dev_pct)
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
        "lot_size": trade["lot_size"]
        if "lot_size" in trade
        else LOT_SIZES.get(trade.get("symbol", "").upper(), 1),
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
        "entry_dev_pct": trade.get("entry_dev_pct"),
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
    symbol: str = "",
) -> float:
    """
    Return total round-trip transaction costs in ₹ for one closed trade.

    Modelled as entry leg + exit leg where:
      - Options turnover = premium × lot_size × lots
      - Futures turnover = underlying × lot_size × lots

    STT rules (India):
      - Index options (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY): STT on BOTH legs (0.0625% each)
      - Stock options: STT on sell leg only (0.0625%)
      - Futures: STT on sell leg only (0.02%)

    P0-04 FIX: Index options now charge STT on both legs.
    """
    from config.settings import LOT_SIZES

    flat_per_leg = 20.0 + 5.0
    round_trip_flat = flat_per_leg * 2

    # Extract base symbol for index detection
    base_symbol = symbol.upper().split()[0] if symbol else ""
    index_symbols = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
    is_index_option = base_symbol in index_symbols and option_type in ("CE", "PE")

    if option_type in ("CE", "PE"):
        entry_turnover = float(entry_premium or 0.0) * lot_size * lots
        exit_turnover = float(exit_premium or 0.0) * lot_size * lots

        if is_index_option:
            # P0-04 FIX: Index options — STT on BOTH legs
            stt = (entry_turnover + exit_turnover) * 0.000625
        else:
            # Stock options — STT on sell leg only
            is_sell_side = side == "SELL"
            sell_premium = entry_premium if is_sell_side else exit_premium
            sell_turnover = float(sell_premium or 0.0) * lot_size * lots
            stt = sell_turnover * 0.000625
    else:
        # BUG-029 FIX: MCX commodity futures use CTT (Commodity Transaction Tax)
        # at 0.01% (0.0001), not STT at 0.02% (0.0002). NSE index futures use STT.
        # Previously all futures used 0.0002, overestimating MCX costs by 2x.
        mcx_commodity_symbols = {
            "NATURALGAS",
            "CRUDEOIL",
            "GOLD",
            "SILVER",
            "COPPER",
            "ZINC",
            "ALUMINIUM",
            "LEAD",
            "NICKEL",
            "MENTHA",
            "COTTON",
            "CPO",
        }
        is_mcx_commodity_future = base_symbol in mcx_commodity_symbols

        # Futures — STT on sell leg only
        # NSE Index Futures (NIFTY, BANKNIFTY): 0.01% (0.0001) - actually STT was reduced
        # MCX Commodity Futures: 0.01% (0.0001) - CTT
        # Stock Futures: 0.02% (0.0002) - STT
        if is_mcx_commodity_future:
            stt_rate = 0.0001  # CTT rate for MCX commodities
        elif base_symbol in index_symbols:
            # BUG-H04 FIX: NSE index futures STT is 0.01% (0.0001) — this is the
            # correct current rate, not a reduction. The previous comment was misleading.
            stt_rate = 0.0001  # STT rate for NSE index futures (0.01%)
        else:
            stt_rate = 0.0002  # STT rate for stock futures

        is_sell_side = side == "SELL"
        sell_price = entry_underlying if is_sell_side else exit_underlying
        sell_turnover = float(sell_price or 0.0) * lot_size * lots
        stt = sell_turnover * stt_rate

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
        # P0-05 FIX: Extract base symbol for LOT_SIZES lookup (handles "NIFTY 22000 CE 25Jun" format)
        base_symbol = symbol.upper().split()[0] if symbol else symbol.upper()
        lot_size = (
            int(stored_lot_size)
            if stored_lot_size is not None
            else LOT_SIZES.get(base_symbol, 1)
        )

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
            # BUG-006 FIX: Use only `side` to determine futures P&L direction.
            # Previously, the OR chain `side == "SELL" or verdict_label == "SHORT"
            # or is_bearish(verdict_label)` could cause P&L inversion if side
            # disagreed with verdict_label. The `side` field is the authoritative
            # source of position direction (BUY = long, SELL = short).
            if side == "SELL":
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
            symbol=symbol,  # P0-04: Pass symbol for index option STT detection
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
            SET closed_at=?, exit_underlying=?, exit_premium=?, pnl_points=?, pnl_rupees=?, status=?, exit_reason=?
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
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%Y-%m-%d"
    )
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
        cur2 = conn.execute(
            "DELETE FROM scan_summaries WHERE fetched_at < ?", (thirty_days_ago,)
        )
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
             rsi_1h, rsi_3h, regime, entry_dev_pct)
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
             :rsi_1h, :rsi_3h, :regime, :entry_dev_pct)
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
        "entry_dev_pct": trade.get("entry_dev_pct"),
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
        # BUG-H04 FIX: Also select lot_size from the database for accurate PnL
        row = conn.execute(
            "SELECT symbol, expiry, option_type, verdict_label, entry_underlying, entry_premium, lots, lot_size, strike, side FROM live_trades WHERE id=? AND status='OPEN'",
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

        # BUG-H04 FIX: Use stored lot_size from database if available, otherwise fall back to LOT_SIZES
        stored_lot_size = row["lot_size"]
        # P0-05 FIX: Extract base symbol for LOT_SIZES lookup
        base_symbol = symbol.upper().split()[0] if symbol else symbol.upper()
        lot_size = (
            int(stored_lot_size)
            if stored_lot_size is not None
            else LOT_SIZES.get(base_symbol, 1)
        )

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
            # BUG-006 FIX: Use only `side` to determine futures P&L direction.
            # Previously, the OR chain `side == "SELL" or verdict_label == "SHORT"
            # or is_bearish(verdict_label)` could cause P&L inversion if side
            # disagreed with verdict_label. The `side` field is the authoritative
            # source of position direction (BUY = long, SELL = short).
            if side == "SELL":
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
            symbol=symbol,  # P0-04: Pass symbol for index option STT detection
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

        # Decrypt api_secret and access_token
        # P0-03 FIX: Raise error if decryption fails — never return encrypted ciphertext as plaintext
        try:
            from src.services.zerodha_auth import _get_fernet

            f = _get_fernet()
            if config.get("api_secret"):
                try:
                    config["api_secret"] = f.decrypt(
                        config["api_secret"].encode("utf-8")
                    ).decode("utf-8")
                except Exception as e:
                    log.error("get_broker_config: api_secret decryption failed — %s", e)
                    return None  # P0-03: Return None instead of encrypted ciphertext
            if config.get("access_token"):
                try:
                    config["access_token"] = f.decrypt(
                        config["access_token"].encode("utf-8")
                    ).decode("utf-8")
                except Exception as e:
                    log.error(
                        "get_broker_config: access_token decryption failed — %s", e
                    )
                    return None  # P0-03: Return None instead of encrypted ciphertext
        except ImportError:
            log.error("get_broker_config: zerodha_auth module not available")
            return None
        except Exception as e:
            log.error("get_broker_config: Fernet initialization failed — %s", e)
            return None
        return config


def update_broker_config(**kwargs) -> None:
    from src.services.zerodha_auth import encrypt_secret

    kwargs_copy = kwargs.copy()
    for encrypt_key in ("api_secret", "access_token", "password", "totp_secret"):
        if encrypt_key in kwargs_copy and kwargs_copy[encrypt_key]:
            kwargs_copy[encrypt_key] = encrypt_secret(kwargs_copy[encrypt_key])

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


def insert_ng_parity_log(log_data: dict) -> None:
    """Insert a row into ng_parity_log."""
    db_data = {
        "ts": log_data.get("timestamp") or log_data.get("ts"),
        "nymex_last": log_data.get("nymex_last"),
        "usdinr": log_data.get("usdinr"),
        "fair_value": log_data.get("fair_value"),
        "mcx_last": log_data.get("mcx_last"),
        "dev_pct": log_data.get("dev_pct"),
        "nymex_age_sec": log_data.get("nymex_age_sec"),
        "fx_age_sec": log_data.get("fx_age_sec"),
        "mcx_age_sec": log_data.get("mcx_age_sec"),
        "mcx_src": log_data.get("mcx_src"),
        "fx_src": log_data.get("fx_src"),
        "nymex_src": log_data.get("nymex_src"),
        "valid": 1 if log_data.get("valid") else 0,
        "regime": log_data.get("ng_regime") or log_data.get("regime"),
    }
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ng_parity_log (
                ts, nymex_last, usdinr, fair_value, mcx_last, dev_pct,
                nymex_age_sec, fx_age_sec, mcx_age_sec, mcx_src, fx_src, nymex_src, valid, regime
            ) VALUES (
                :ts, :nymex_last, :usdinr, :fair_value, :mcx_last, :dev_pct,
                :nymex_age_sec, :fx_age_sec, :mcx_age_sec, :mcx_src, :fx_src, :nymex_src, :valid, :regime
            )
            """,
            db_data,
        )


def has_recent_scan_summary(symbol: str, max_age_minutes: int) -> bool:
    """
    Returns True if the most recent scan_summary for ``symbol`` is at most
    ``max_age_minutes`` old (compared to now in UTC).

    Used by ``main.py --now`` to decide whether to run a dry run (fresh data)
    or a full scan (stale / missing data).
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    cutoff_str = cutoff.isoformat()

    sql = """
        SELECT 1 FROM scan_summaries
        WHERE symbol = ? AND fetched_at >= ?
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (symbol, cutoff_str)).fetchone()
        return row is not None


# ── Multi-leg Trades (Iron Condors, etc.) ────────────────────────────────


def insert_multi_leg_trade(trade: dict) -> int:
    """Insert a multi-leg trade and return its id."""
    sql = """
        INSERT INTO multi_leg_trades
            (trade_ref, symbol, structure, net_premium, margin_req, total_pnl,
             opened_at, closed_at, status, reason, profit_factor)
        VALUES
            (:trade_ref, :symbol, :structure, :net_premium, :margin_req, :total_pnl,
             :opened_at, :closed_at, :status, :reason, :profit_factor)
        RETURNING id
    """
    with get_conn() as conn:
        row = conn.execute(sql, trade).fetchone()
        return int(row["id"]) if row else 0


def close_multi_leg_trade(
    trade_id: int, closed_at: str, status: str, reason: str, total_pnl: float
) -> None:
    """Close a multi-leg trade."""
    sql = "UPDATE multi_leg_trades SET closed_at=?, status=?, reason=?, total_pnl=? WHERE id=?"
    with get_conn() as conn:
        conn.execute(sql, (closed_at, status, reason, total_pnl, trade_id))


def list_multi_leg_trades() -> list[dict]:
    """List all multi-leg trades with their legs (Disabled for NSEBOT)."""
    return []


def delete_multi_leg_trade(trade_ref: int) -> None:
    """Delete a multi-leg trade by trade_ref (cascade deletes legs)."""
    sql = "DELETE FROM multi_leg_trades WHERE trade_ref=?"
    with get_conn() as conn:
        conn.execute(sql, (trade_ref,))


def update_multi_leg_leg_exit_premium(leg_id: int, exit_premium: float) -> None:
    """Update exit premium for a specific multi-leg leg."""
    sql = "UPDATE multi_leg_legs SET exit_premium=? WHERE id=?"
    with get_conn() as conn:
        conn.execute(sql, (exit_premium, leg_id))


# ── OPS Agent: Health State Stamps ──────────────────────────────────────────

def stamp_health(key: str, status: str, detail: str = "") -> None:
    """Write a health state row for ops_agent.py to read."""
    now_ist = datetime.now(_IST).isoformat()
    sql = """
        INSERT INTO health_state (key, status, detail, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            status=excluded.status,
            detail=excluded.detail,
            updated_at=excluded.updated_at
    """
    with get_conn() as conn:
        conn.execute(sql, (key, status, detail, now_ist))


def read_health_state() -> list[dict]:
    """Read all health state rows (for /health endpoint and ops_agent)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM health_state").fetchall()
    return [dict(r) for r in rows]


def read_health_state_ro() -> list[dict]:
    """Read-only health state for ops_agent (separate connection, no WAL lock)."""
    try:
        db_uri = Path(DB_PATH).as_uri() + "?mode=ro"
        conn = sqlite3.connect(
            db_uri,
            uri=True,
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM health_state").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_open_positions_count() -> int:
    """Count open paper + live positions."""
    with get_conn() as conn:
        paper = conn.execute(
            "SELECT COUNT(*) as c FROM paper_trades WHERE status='OPEN'"
        ).fetchone()["c"]
        live = conn.execute(
            "SELECT COUNT(*) as c FROM live_trades WHERE status='OPEN'"
        ).fetchone()["c"]
    return paper + live


def get_oldest_open_position_age_min() -> float | None:
    """Return age in minutes of the oldest open position, or None if none open."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT MIN(opened_at) as oldest FROM (
                SELECT opened_at FROM paper_trades WHERE status='OPEN'
                UNION ALL
                SELECT opened_at FROM live_trades WHERE status='OPEN'
            )
            """
        ).fetchone()
    if not row or not row["oldest"]:
        return None
    try:
        opened = datetime.fromisoformat(row["oldest"].replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
    except Exception:
        return None


def stamp_open_positions() -> None:
    """Stamp open_positions health row with count and oldest age."""
    count = get_open_positions_count()
    age = get_oldest_open_position_age_min()
    detail = f"count={count}"
    if age is not None:
        detail += f" oldest_age={age:.0f}m"
    stamp_health("open_positions", "OK" if count == 0 else "OK", detail)
