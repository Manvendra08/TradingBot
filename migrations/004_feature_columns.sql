-- Migration 004: ML Feature Columns for AI Intelligence System
-- AI_INTELLIGENCE_ROADMAP_v3.0 Phase 0 (BLOCKER)
--
-- Adds ML feature columns to paper_trades AND live_trades tables.
-- These columns capture the scan context at TRADE OPEN time so the Phase 2
-- ML model can train on actual feature data instead of zeros.
--
-- All columns are nullable — historical rows remain NULL and are excluded
-- from training by the NOT NULL guard in Phase 2's train() function.
--
-- Features are captured at OPEN time (not close) to prevent feature leakage.

-- ── paper_trades: ML feature columns ──────────────────────────────────────
ALTER TABLE paper_trades ADD COLUMN price_change_pct REAL;
ALTER TABLE paper_trades ADD COLUMN pcr REAL;
ALTER TABLE paper_trades ADD COLUMN ce_oi_change REAL;
ALTER TABLE paper_trades ADD COLUMN pe_oi_change REAL;
ALTER TABLE paper_trades ADD COLUMN underlying REAL;
ALTER TABLE paper_trades ADD COLUMN support REAL;
ALTER TABLE paper_trades ADD COLUMN resistance REAL;
ALTER TABLE paper_trades ADD COLUMN max_pain REAL;
ALTER TABLE paper_trades ADD COLUMN days_to_expiry INTEGER;
ALTER TABLE paper_trades ADD COLUMN chart_conflict INTEGER;  -- 0/1
ALTER TABLE paper_trades ADD COLUMN rsi_1h REAL;
ALTER TABLE paper_trades ADD COLUMN rsi_3h REAL;
ALTER TABLE paper_trades ADD COLUMN regime TEXT;

-- ── live_trades: mirror the same columns ──────────────────────────────────
ALTER TABLE live_trades ADD COLUMN price_change_pct REAL;
ALTER TABLE live_trades ADD COLUMN pcr REAL;
ALTER TABLE live_trades ADD COLUMN ce_oi_change REAL;
ALTER TABLE live_trades ADD COLUMN pe_oi_change REAL;
ALTER TABLE live_trades ADD COLUMN underlying REAL;
ALTER TABLE live_trades ADD COLUMN support REAL;
ALTER TABLE live_trades ADD COLUMN resistance REAL;
ALTER TABLE live_trades ADD COLUMN max_pain REAL;
ALTER TABLE live_trades ADD COLUMN days_to_expiry INTEGER;
ALTER TABLE live_trades ADD COLUMN chart_conflict INTEGER;
ALTER TABLE live_trades ADD COLUMN rsi_1h REAL;
ALTER TABLE live_trades ADD COLUMN rsi_3h REAL;
ALTER TABLE live_trades ADD COLUMN regime TEXT;

-- ── AI pattern insights cache table (Phase 1) ────────────────────────────
CREATE TABLE IF NOT EXISTS ai_pattern_insights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name    TEXT NOT NULL,
    pattern_type    TEXT NOT NULL,   -- 'symbol_verdict', 'session', 'confidence', 'regime'
    sample_size     INTEGER,
    win_rate        REAL,
    avg_pnl         REAL,
    best_conditions TEXT,            -- JSON
    recommendation  TEXT,
    discovered_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_pattern_insights_name
    ON ai_pattern_insights (pattern_name);
