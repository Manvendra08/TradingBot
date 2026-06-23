# 🧠 AI Intelligence System - Optimized Roadmap (v3.0)
## Building on Existing NSEBOT Architecture

**Date:** June 21, 2026  
**Version:** 3.0 (Revised after fourth-pass code review — correctness + UI overhaul)  
**Status:** Ready for Implementation  
**Estimated Effort:** 10-13 weeks (incremental delivery)

> ⚠️ **v3.0 BLOCKER — read before Phase 2:** The ML model reads ~25 feature
> columns (`pcr`, `ce_oi_change`, `support`, `resistance`, `max_pain`,
> `rsi_1h`, `regime`, etc.) from the trade tables at training time. **These
> columns are NOT in the current `paper_trades` schema.** Without persisting
> them at trade-close time, `_extract_features()` falls back to zeros and the
> "28-feature" model silently degrades to a confidence + verdict classifier.
> **Phase 0 (schema migration + close-time feature logging) is now mandatory
> and must ship before any ML training.** See Section 0.

---

## 📊 Executive Summary

This roadmap extends the **already functional NSEBOT** with an AI Intelligence System that learns from trade history, provides actionable coaching, and continuously self-improves. Unlike the theoretical plan, this is **practical and builds on existing code**.

### What Already Exists ✅

| Component | Location | Status |
|-----------|----------|--------|
| Intelligence Engine | `src/engine/intelligence.py` | ✅ Production-ready |
| LLM Integration | `src/engine/llm_enrichment.py` | ✅ Multi-provider (OpenRouter/Gemini/Groq) |
| Paper Trading | `src/engine/paper_trading.py` | ✅ Full lifecycle management |
| Database | `data/bot.db` | ✅ SQLite with trade history |
| Dashboard | `src/dashboard/` | ✅ Flask web UI |
| Signal Detection | `src/engine/anomaly_detector.py` | ✅ 15+ alert types |
| Risk Management | `src/engine/risk_engine.py` | ✅ Position sizing, limits |
| Trade Decision Gates | `src/engine/trade_decision.py` | ✅ Confidence/regime/scan gates |

### What We're Building 🚀

1. **Trade History Analyzer** (Weeks 1-2) - Pattern discovery & performance analytics
2. **ML Success Predictor** (Weeks 3-5) - XGBoost model predicting P(success)
3. **Edge Decay Monitor** (Weeks 6-7) - Strategy performance tracking
4. **AI Dashboard UI** (Weeks 8-10) - Visual insights & recommendations

> ⚠️ **v2.0 Change:** Behavioral Coach (Phase 3 in v1.0) has been **removed**. The bot is automated — FOMO/overtrading are human problems already handled by existing gates in `trade_decision.py` (`MIN_CONFIDENCE_CORE`, `TREND_MIN_SCANS`, regime checks). Adding a redundant behavioral layer creates conflicting control points with `risk_engine.py`.

---

## 🎯 Design Philosophy

### Core Principles

1. **Extend, Don't Replace** - Add AI as a layer on top of existing intelligence
2. **Transparent Confidence** - Show uncertainty based on sample size
3. **Actionable Insights** - Every metric has a "what to do" recommendation
4. **Trader Control** - AI advises, trader decides (no autopilot)
5. **Incremental Value** - Each phase delivers usable features
6. **IST-Aware** - All time-based analysis uses IST, not UTC (database stores UTC)
7. **Single Source of Truth** - Risk limits live in `risk_engine.py`, not duplicated

### Integration Strategy

```
Existing Flow:
  scan → alerts → intelligence → paper_trading → database

Enhanced Flow:
  scan → alerts → intelligence → [AI ANALYZER] → paper_trading → database
                                      ↓
                              [ML MODEL] → predictions
                                      ↓
                              [EDGE MONITOR] → health alerts
                                      ↓
                              [DASHBOARD UI] → visual insights
```

### Critical Fixes Applied (from Technical Review)

| Issue | Fix Applied | Phase |
|-------|-------------|-------|
| UTC timezone bug in session bucketing | IST conversion via `datetime(opened_at, '+5 hours', '+30 minutes')` | Phase 1 |
| Confidence bands too tight (±10) | Widened to ±20 for early-stage bots | Phase 1 |
| HAVING COUNT >= 3 too loose | Raised to >= 10 minimum | Phase 1 |
| Feature leakage (`datetime.now()`) | Use `opened_at` from trade record | Phase 2 |
| Class imbalance (loss-heavy data) | `scale_pos_weight = n_neg/n_pos` | Phase 2 |
| Feature ordering (`sorted()` fragile) | Explicit `FEATURE_ORDER` constant | Phase 2 |
| Weekly-only retraining too slow | Event-driven: edge health < 60 OR 20+ new trades | Phase 2 |
| No model rollback | Version comparison: deploy only if AUC improves ≥2% | Phase 2 |
| Paper trades only for training | UNION with `live_trades` when available | Phase 2 |
| Biased edge decay windows | Guard: return INSUFFICIENT_HISTORY if hist count < 5 | Phase 3 |
| Flawed health score formula | Include absolute performance, not just deltas | Phase 3 |
| N+1 queries in strategy health | Single GROUP BY query | Phase 3 |
| Missing API params in JS | Pass verdict + confidence to ML endpoint | Phase 4 |
| No error handling in fetch() | `.catch()` on all API calls | Phase 4 |
| WebSocket scope creep | Removed — use existing polling | Phase 4 |

### v2.1 Additional Fixes (Second-Pass Code Review)

| Issue | Fix Applied | Phase |
|-------|-------------|-------|
| `get_all_strategies_health()` hidden N+1 | Pass pre-fetched row data directly to scoring; no re-query | Phase 3 |
| `_trades_since_last_train` not thread-safe | Wrapped with `threading.Lock()` for APScheduler compatibility | Phase 2 |
| AUC gate startup blind spot | Floor: `baseline = max(self.current_auc, 0.55)` prevents locking out better models | Phase 2 |
| SHAP TreeExplainer created per prediction | Cached as `self._shap_explainer`; invalidated only on retrain | Phase 2 |
| Session boundary overlap (Afternoon/Closing) | Rewritten using total-minutes comparison; sessions are now mutually exclusive | Phase 1 |

### v2.2 Additional Fixes (Third-Pass Code Review)

| Issue | Fix Applied | Phase |
|-------|-------------|-------|
| `get_all_strategies_health()` calls undefined `_classify_trend()` and `EdgeHealthReport` | Use `self._classify_trend(change, baseline)` + `EdgeHealth(...)` dataclass; inline scoring now delegates to `_calculate_health_score_absolute()` for consistency | Phase 3 |
| `TradeHistoryAnalyzer` re-instantiated per API request | Module-level singleton + 5-min in-memory cache; `ai_pattern_insights` DB cache table now wired up | Phase 1 |
| `_analyze_by_session()` runs 5 separate queries in loop | Single `CASE WHEN` query bucketing all sessions at once | Phase 1 |
| `FEATURE_ORDER` missing 4 verdict encodings (Call/Put Writing, OI Bias) | Added all 8 verdict one-hot features; `_extract_features()` updated to match | Phase 2 |
| `TradeSuccessPredictor` not a singleton (disk load per scan) | Module-level `_predictor` singleton with lazy init; pipeline + API routes updated | Phase 2 |

### v3.0 Additional Fixes (Fourth-Pass Code Review)

| # | Issue | Severity | Fix Applied | Phase |
|---|-------|----------|-------------|-------|
| 1 | Feature columns (`pcr`, `ce_oi_change`, `support`, `rsi_1h`, `regime`…) never persisted to trade tables → model trains on zeros | 🔴 Critical | New **Phase 0**: schema migration adds feature columns; `paper_trading.close_trade()` logs full context at close | Phase 0 |
| 2 | Hour-match SQL compared `strftime('%H')` (`"09"`) against `str(hour)` (`"9"`) → morning session silently dropped | 🔴 Critical | Zero-padded numeric compare: cast both sides to INTEGER (`CAST(strftime('%H',…) AS INTEGER)`) | Phase 1 |
| 3 | Saved model could load with stale `FEATURE_ORDER` (v2.2 added 4 verdicts) → shape/column mismatch, silent miscolumn | 🔴 Critical | `_load_model()` validates `meta["feature_names"] == FEATURE_ORDER`; discards + flags retrain on mismatch | Phase 2 |
| 4 | Dashboard ML endpoint passed only symbol/verdict/confidence → near-empty vector, different probability than pipeline | 🔴 Critical | Endpoint hydrates full feature context from latest scan snapshot before predicting | Phase 4 |
| 5 | `HAVING >= 10` surfaced patterns but recommendation gated on `min_trades=30` → contradictory "insufficient" labels | 🟡 High | Single source of truth: both use `MIN_PATTERN_TRADES`; `min_trades` param removed | Phase 1 |
| 6 | Edge monitor retrained on every early close (insufficient-data sentinel score 50 < 60 threshold) | 🟡 High | Retrain trigger ignores `INSUFFICIENT_*` trends and `count < MIN_HISTORICAL_TRADES` | Phase 3 |
| 7 | `train_test_split` not stratified → single-class test fold on small data → AUC=0.5, model never deploys | 🟡 High | `stratify=y` + stratified K-fold CV used for the deploy gate (holdout too noisy at n≈30) | Phase 2 |
| 8 | SHAP `shap_values()[0]` assumed single ndarray; per-class list (older SHAP) → wrong top factors | 🟡 High | Shape-normalizing helper handles both list and ndarray returns | Phase 2 |
| 9 | `net_oi_change = ce + pe` mislabeled (that's *total*, not net) and collinear | 🟠 Medium | Redefined as directional `pe_oi_change - ce_oi_change` | Phase 2 |
| 10 | Sentinel mismatch: `"INSUFFICIENT_DATA"` vs `"INSUFFICIENT_HISTORY"` | 🟠 Medium | Unified to `INSUFFICIENT_HISTORY` everywhere | Phase 3 |
| 11 | `get_all_strategies_health()` mixed two scoring methods + fake "trend" in one sorted list | 🟠 Medium | Per-strategy rows carry `win_rate_trend=INSUFFICIENT_HISTORY`; sorted within method-consistent groups | Phase 3 |
| 12 | `pnl_trend` divided by ~0 historical baseline → unstable classification | 🟠 Medium | Min-baseline floor (`max(abs(hist_pnl), 100)`) before ratio | Phase 3 |
| 13 | Singleton swallowed constructor args silently | 🔵 Minor | `min_trades` arg removed; constants only | Phase 1 |
| 14 | `idx_trades_similarity` comment claimed IST-hour indexing (non-sargable) | 🔵 Minor | Comment corrected; added covering index note | Phase 1 |
| 15 | `_persist_patterns_to_db` DELETE+INSERT not atomic | 🔵 Minor | Wrapped in single `BEGIN…COMMIT` transaction | Phase 1 |
| 16 | `model_version = datetime.now()` (naive local) vs UTC/IST elsewhere | 🔵 Minor | Uses UTC ISO timestamp | Phase 2 |
| 17 | Confidence level keyed only on training sample count, not prediction uncertainty | 🔵 Minor | Blends sample count with predicted-probability margin | Phase 2 |
| — | Dashboard UI: flat card grid, no hierarchy, inline-styled, low contrast, no states | 🟡 High | Full Phase 4 redesign: design tokens, semantic color, responsive grid, loading/empty/error states, WCAG AA | Phase 4 |

---

## 🧱 Phase 0: Feature Persistence Migration (Week 0 — BLOCKER)

**Goal:** Guarantee every feature in `FEATURE_ORDER` is physically stored on each
closed trade. Without this, Phase 2 trains on zeros.

### 0.1 Schema Migration

```sql
-- migrations/004_feature_columns.sql
-- Adds ML feature columns to paper_trades AND live_trades.
-- All nullable — historical rows stay NULL and are excluded from training
-- by the NOT NULL guard in train() (see Phase 2).

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
-- symbol, verdict_label, confidence_score, opened_at, closed_at, pnl_rupees
-- already exist.

-- Mirror on live_trades so the Phase 2 UNION ALL has identical columns.
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
```

### 0.2 Persist Features at Trade Open

**File:** `src/engine/paper_trading.py` — when a trade is opened, snapshot the
scan context onto the trade row (features must reflect entry-time state, never
close-time — that would be leakage).

```python
def open_trade(self, symbol, verdict_label, confidence, scan_context, ...):
    """v3.0: Persist the full feature snapshot at OPEN time (no leakage)."""
    self.db.execute("""
        INSERT INTO paper_trades (
            symbol, verdict_label, confidence_score, opened_at, status,
            price_change_pct, pcr, ce_oi_change, pe_oi_change, underlying,
            support, resistance, max_pain, days_to_expiry, chart_conflict,
            rsi_1h, rsi_3h, regime
        ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        symbol, verdict_label, confidence, _utc_now_iso(),
        scan_context.get("price_change_pct"), scan_context.get("pcr"),
        scan_context.get("ce_oi_change"), scan_context.get("pe_oi_change"),
        scan_context.get("underlying"), scan_context.get("support"),
        scan_context.get("resistance"), scan_context.get("max_pain"),
        scan_context.get("days_to_expiry"), 1 if scan_context.get("chart_conflict") else 0,
        scan_context.get("rsi_1h"), scan_context.get("rsi_3h"),
        scan_context.get("regime"),
    ))
```

### 0.3 Validation Gate

Before enabling Phase 2 training, assert coverage:

```python
def assert_feature_coverage(min_pct=0.90):
    """Refuse to train if too many closed trades lack feature data."""
    from src.models.schema import get_conn
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN pcr IS NOT NULL AND rsi_1h IS NOT NULL
                       THEN 1 ELSE 0 END) AS with_features
            FROM paper_trades
            WHERE status != 'OPEN' AND closed_at IS NOT NULL
        """).fetchone()
    if row["total"] == 0:
        return False
    coverage = row["with_features"] / row["total"]
    if coverage < min_pct:
        log.warning(f"Feature coverage {coverage:.0%} < {min_pct:.0%}. "
                    f"Train on more instrumented trades first.")
        return False
    return True
```

### 0.4 Phase 0 Deliverables

- ✅ Migration `004_feature_columns.sql` (paper + live, identical columns)
- ✅ `open_trade()` snapshots entry-time feature context (leakage-safe)
- ✅ `assert_feature_coverage()` gate wired before Phase 2 training
- ✅ Backfill note: pre-migration trades stay NULL and are excluded from training

**Estimated Effort:** 3-5 hours  
**Impact:** Unblocks all of Phase 2 — without it the ML model is a placebo

---

## 📅 Phase 1: Trade History Analyzer (Weeks 1-2)

**Goal:** Extract patterns from existing trade data without ML

### 1.1 New Module: `src/intelligence/history_analyzer.py`

```python
"""
Analyzes closed paper trades to discover winning/losing patterns.
No ML required - pure statistical aggregation.

v2.0 FIXES:
- IST timezone conversion for session bucketing
- Minimum 10 trades per pattern (was 3)
- Confidence bands widened to ±20 (was ±10)
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# IST offset constant (matches risk_engine.py)
IST_OFFSET = timedelta(hours=5, minutes=30)

@dataclass
class PatternInsight:
    """A discovered pattern with actionable recommendation."""
    pattern_name: str              # e.g., "BANKNIFTY Long Buildup"
    sample_size: int               # number of trades
    win_rate: float                # 0.0-1.0
    avg_pnl: float                 # average PnL in rupees
    best_time: str                 # e.g., "09:30-11:00"
    best_conditions: dict          # e.g., {"min_confidence": 75, "pcr_range": [1.1, 1.5]}
    recommendation: str            # actionable advice
    

# ── Singleton + Cache (v2.2 FIX) ──────────────────────────────────────────
# Previously, every API call and pipeline cycle instantiated a new
# TradeHistoryAnalyzer and ran 5 full SQL aggregation queries. With 30s
# dashboard polling, that's ~2,880 unnecessary query sets per day.
# 
# Fix: Module-level singleton + 5-minute in-memory cache.
# Cache is invalidated when a trade closes (via invalidate_cache()).
# The ai_pattern_insights DB table (Section 1.2) is also populated for
# cross-process cache sharing and persistence across restarts.
import threading
from time import time as _time

_analyzer: "TradeHistoryAnalyzer | None" = None
_analyzer_lock = threading.Lock()

def get_analyzer() -> "TradeHistoryAnalyzer":
    """
    Return the module-level TradeHistoryAnalyzer singleton.

    v3.0 FIX: Removed the `min_trades` parameter. As a singleton it was only
    honored on the first call and silently ignored thereafter — a footgun.
    The single threshold now lives in MIN_PATTERN_TRADES.
    """
    global _analyzer
    if _analyzer is None:
        with _analyzer_lock:
            if _analyzer is None:  # double-checked locking
                _analyzer = TradeHistoryAnalyzer()
    return _analyzer


class TradeHistoryAnalyzer:
    """Analyzes closed trades to find patterns."""
    
    MIN_PATTERN_TRADES = 10  # v2.0: raised from 3 — 3 trades is noise.
                             # v3.0: SINGLE SOURCE OF TRUTH. Used by both the
                             # HAVING clause AND _generate_recommendation, so a
                             # pattern can never surface yet be labelled
                             # "insufficient" (the old min_trades=30 conflict).
    CACHE_TTL_SECONDS = 300  # v2.2: 5-minute cache TTL
    
    def __init__(self):
        # v3.0: min_trades param removed — see MIN_PATTERN_TRADES.
        # v2.2: In-memory pattern cache
        self._patterns_cache: list[PatternInsight] | None = None
        self._patterns_cache_ts: float = 0.0
    
    def invalidate_cache(self):
        """v2.2: Call after a trade closes to force re-computation."""
        self._patterns_cache = None
        self._patterns_cache_ts = 0.0
    
    def get_cached_patterns(self) -> list[PatternInsight]:
        """
        v2.2: Return cached patterns if fresh (< TTL), else recompute.
        Persists to ai_pattern_insights table for cross-process sharing.
        Reads from DB table if in-memory cache is empty before computing.
        """
        now = _time()
        if (self._patterns_cache is not None 
                and (now - self._patterns_cache_ts) < self.CACHE_TTL_SECONDS):
            return self._patterns_cache
        
        # Load from DB first if in-memory cache is empty (e.g. on restart)
        if self._patterns_cache is None:
            db_patterns = self._load_patterns_from_db()
            if db_patterns:
                self._patterns_cache = db_patterns
                self._patterns_cache_ts = now
                return db_patterns
        
        patterns = self.analyze_all_patterns()
        self._patterns_cache = patterns
        self._patterns_cache_ts = now
        
        # Persist to DB cache table (best-effort — non-blocking)
        self._persist_patterns_to_db(patterns)
        return patterns
    
    def _load_patterns_from_db(self) -> list[PatternInsight]:
        """Read patterns from ai_pattern_insights table for cache population."""
        try:
            from src.models.schema import get_conn
            import json
            with get_conn() as conn:
                rows = conn.execute("""
                    SELECT pattern_name, sample_size, win_rate, avg_pnl, 
                           best_conditions, recommendation
                    FROM ai_pattern_insights
                    ORDER BY win_rate * sample_size DESC
                """).fetchall()
                
                patterns = []
                for row in rows:
                    patterns.append(PatternInsight(
                        pattern_name=row["pattern_name"],
                        sample_size=row["sample_size"],
                        win_rate=row["win_rate"],
                        avg_pnl=row["avg_pnl"],
                        best_time="All day",
                        best_conditions=json.loads(row["best_conditions"]) if row["best_conditions"] else {},
                        recommendation=row["recommendation"]
                    ))
                return patterns
        except Exception as e:
            log.warning(f"Failed to load patterns from DB cache: {e}")
            return []
    
    def _persist_patterns_to_db(self, patterns: list[PatternInsight]):
        """Write patterns to ai_pattern_insights table for cross-process sharing."""
        try:
            from src.models.schema import get_conn
            import json
            with get_conn() as conn:
                # v3.0 FIX: Atomic refresh. Previously DELETE then loop-INSERT
                # ran un-transactioned — a crash mid-loop wiped the cache table
                # and left it empty. Wrap both in one transaction so the table
                # is never observed empty.
                conn.execute("BEGIN")
                try:
                    conn.execute("DELETE FROM ai_pattern_insights")
                    for p in patterns:
                        conn.execute("""
                            INSERT INTO ai_pattern_insights 
                            (pattern_name, pattern_type, sample_size, win_rate, avg_pnl, 
                             best_conditions, recommendation, discovered_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """, (p.pattern_name, "auto", p.sample_size, p.win_rate,
                              p.avg_pnl, json.dumps(p.best_conditions), p.recommendation))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        except Exception as e:
            log.warning(f"Failed to persist patterns to DB cache: {e}")
    
    def analyze_all_patterns(self) -> list[PatternInsight]:
        """Discover patterns across multiple dimensions."""
        patterns = []
        
        # 1. By Symbol + Verdict
        patterns.extend(self._analyze_by_symbol_verdict())
        
        # 2. By Time of Day (IST-corrected)
        patterns.extend(self._analyze_by_session())
        
        # 3. By Confidence Range
        patterns.extend(self._analyze_by_confidence())
        
        # 4. By Setup Type
        patterns.extend(self._analyze_by_setup_type())
        
        # 5. By Market Regime
        patterns.extend(self._analyze_by_regime())
        
        return sorted(patterns, key=lambda p: p.win_rate * p.sample_size, reverse=True)
    
    def _analyze_by_symbol_verdict(self) -> list[PatternInsight]:
        """Analyze performance by symbol and verdict label."""
        from src.models.schema import get_conn
        
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT 
                    symbol,
                    verdict_label,
                    COUNT(*) as count,
                    AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                    AVG(pnl_rupees) as avg_pnl,
                    AVG(confidence_score) as avg_confidence
                FROM paper_trades
                WHERE status != 'OPEN'
                  AND closed_at IS NOT NULL
                GROUP BY symbol, verdict_label
                HAVING COUNT(*) >= ?
            """, (self.MIN_PATTERN_TRADES,)).fetchall()
        
        insights = []
        for row in rows:
            recommendation = self._generate_recommendation(
                row["win_rate"], row["avg_pnl"], row["count"]
            )
            insights.append(PatternInsight(
                pattern_name=f"{row['symbol']} {row['verdict_label']}",
                sample_size=row["count"],
                win_rate=row["win_rate"],
                avg_pnl=row["avg_pnl"],
                best_time="All day",
                best_conditions={"avg_confidence": row["avg_confidence"]},
                recommendation=recommendation
            ))
        return insights
    
    def _analyze_by_session(self) -> list[PatternInsight]:
        """
        Analyze performance by time of day (IST sessions).
        
        v2.0 FIX: Database stores UTC timestamps. Must convert to IST
        before bucketing. A 9:15 AM IST trade is 03:45 UTC.
        
        v2.1 FIX: Previous hour/minute string comparison caused overlapping
        sessions (e.g., 15:00 matched both Afternoon and Closing). Now uses
        total-minutes-since-midnight comparison for mutually exclusive buckets.
        Sessions are defined as [start_minutes, end_minutes) — inclusive start,
        exclusive end — so no trade can belong to two sessions.
        
        v2.2 FIX: Runs a single CASE WHEN query instead of 5 separate queries
        in a loop. Each trade is bucketed exactly once (or into NULL if outside
        all sessions), and aggregation is computed in one pass.
        """
        from src.models.schema import get_conn
        
        # v2.2 FIX: Single CASE WHEN query — buckets all sessions in one pass.
        # Each branch is mutually exclusive because CASE evaluates top-to-bottom
        # and stops at the first match. Trades outside all session windows get
        # session = NULL and are excluded by the HAVING clause.
        query = """
            SELECT 
                session,
                COUNT(*) as count,
                AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                AVG(pnl_rupees) as avg_pnl
            FROM (
                SELECT 
                    pnl_rupees,
                    CASE
                        WHEN total_min >= 555  AND total_min < 630  THEN 'Market Open (09:15-10:30)'
                        WHEN total_min >= 630  AND total_min < 720  THEN 'Mid-Morning (10:30-12:00)'
                        WHEN total_min >= 720  AND total_min < 840  THEN 'Post-Lunch (12:00-14:00)'
                        WHEN total_min >= 840  AND total_min < 900  THEN 'Afternoon (14:00-15:00)'
                        WHEN total_min >= 900  AND total_min < 930  THEN 'Closing (15:00-15:30)'
                        ELSE NULL
                    END as session
                FROM (
                    SELECT 
                        pnl_rupees,
                        CAST(strftime('%H', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) * 60
                        + CAST(strftime('%M', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) as total_min
                    FROM paper_trades
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                )
            )
            WHERE session IS NOT NULL
            GROUP BY session
            HAVING COUNT(*) >= ?
        """
        
        # Session names in display order (for sorting results)
        SESSION_ORDER = [
            "Market Open (09:15-10:30)",
            "Mid-Morning (10:30-12:00)",
            "Post-Lunch (12:00-14:00)",
            "Afternoon (14:00-15:00)",
            "Closing (15:00-15:30)",
        ]
        
        insights = []
        with get_conn() as conn:
            rows = conn.execute(query, (self.MIN_PATTERN_TRADES,)).fetchall()
        
        # Build lookup for O(1) access by session name
        row_by_session = {row["session"]: row for row in rows}
        
        for session_name in SESSION_ORDER:
            row = row_by_session.get(session_name)
            if row:
                recommendation = self._generate_recommendation(
                    row["win_rate"], row["avg_pnl"], row["count"]
                )
                insights.append(PatternInsight(
                    pattern_name=f"Session: {session_name}",
                    sample_size=row["count"],
                    win_rate=row["win_rate"],
                    avg_pnl=row["avg_pnl"],
                    best_time=session_name,
                    best_conditions={},
                    recommendation=recommendation
                ))
        return insights


    def _generate_recommendation(self, win_rate: float, avg_pnl: float, count: int) -> str:
        """Generate actionable recommendation based on performance."""
        # v3.0 FIX: Gate on the same constant as the HAVING clause. Previously
        # this used self.min_trades (30) while HAVING used MIN_PATTERN_TRADES
        # (10), so 10–29-trade patterns surfaced then displayed "insufficient".
        if count < self.MIN_PATTERN_TRADES:
            return f"⚠️ Insufficient data ({count}/{self.MIN_PATTERN_TRADES} trades needed)"
        
        if win_rate >= 0.70 and avg_pnl > 1000:
            return "🟢 STRONG EDGE - Increase position size or frequency"
        elif win_rate >= 0.60 and avg_pnl > 0:
            return "🟡 MODERATE EDGE - Trade with standard size, look for confluence"
        elif win_rate >= 0.50 and avg_pnl >= 0:
            return "🟠 WEAK EDGE - Reduce size or wait for higher confidence"
        elif win_rate < 0.50:
            return "🔴 NEGATIVE EDGE - Avoid this setup until performance improves"
        else:
            return "⚪ NEUTRAL - Monitor for more data"
    
    def get_trade_dna_match(self, current_trade_context: dict) -> dict:
        """
        Find similar historical trades and show success probability.
        
        v2.0 FIXES:
        - Confidence band widened to ±20 (was ±10) for early-stage bots
        - IST timezone conversion for hour matching
        - Uses opened_at from trade record, not datetime.now()
        """
        from src.models.schema import get_conn
        
        symbol = current_trade_context.get("symbol")
        verdict = current_trade_context.get("verdict_label")
        confidence = current_trade_context.get("confidence", 0)
        # v2.0 FIX: Use IST hour from context, not datetime.now()
        ist_hour = current_trade_context.get("ist_hour", 
            (datetime.now(timezone.utc) + IST_OFFSET).hour)
        
        with get_conn() as conn:
            # v3.0 FIX: The old query compared strftime('%H') (zero-padded
            # text like "09") against str(ist_hour-1) ("8"). Lexically
            # "09" >= "8" is FALSE, so the entire 09:00–15:00 IST session was
            # silently dropped. Cast BOTH sides to INTEGER so the comparison
            # is numeric, not lexicographic.
            similar_trades = conn.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    AVG(pnl_rupees) as avg_pnl,
                    AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as avg_win,
                    AVG(CASE WHEN pnl_rupees <= 0 THEN pnl_rupees ELSE 0 END) as avg_loss
                FROM paper_trades
                WHERE status != 'OPEN'
                  AND symbol = ?
                  AND verdict_label = ?
                  AND confidence_score BETWEEN ? AND ?
                  AND CAST(strftime('%H', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER)
                      BETWEEN ? AND ?
            """, (
                symbol, verdict,
                max(0, confidence - 20), min(100, confidence + 20),
                max(0, ist_hour - 1), min(23, ist_hour + 1)   # v3.0: ints, not str()
            )).fetchone()
        
        if not similar_trades or similar_trades["total"] == 0:
            return {"match_found": False, "message": "No similar historical trades"}
        
        win_rate = similar_trades["wins"] / similar_trades["total"]
        
        return {
            "match_found": True,
            "similar_trades": similar_trades["total"],
            "historical_win_rate": win_rate,
            "avg_pnl": similar_trades["avg_pnl"],
            "avg_win": similar_trades["avg_win"],
            "avg_loss": similar_trades["avg_loss"],
            "confidence_note": f"Based on {similar_trades['total']} similar trades (±20 confidence band)"
        }
```

### 1.2 Database Schema Extension

```sql
-- Add to bot.db

-- Pattern insights cache (refreshed daily)
CREATE TABLE IF NOT EXISTS ai_pattern_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name TEXT NOT NULL,
    pattern_type TEXT NOT NULL,  -- 'symbol_verdict', 'session', 'confidence', 'regime'
    sample_size INTEGER,
    win_rate REAL,
    avg_pnl REAL,
    best_conditions TEXT,  -- JSON
    recommendation TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);

-- v3.0 FIX: Comment corrected. This index covers the equality predicates
-- (symbol, verdict_label) and the confidence_score range used by the Trade
-- DNA lookup. NOTE: the IST-hour filter uses datetime(opened_at, …), which is
-- a non-sargable expression — no index can serve it directly. If hour-filtered
-- lookups become hot, add a generated/stored ist_hour column and index THAT.
CREATE INDEX IF NOT EXISTS idx_trades_similarity 
ON paper_trades(symbol, verdict_label, confidence_score);
```

### 1.3 Integration Points

**File:** `src/engine/pipeline.py` (add after intelligence generation)

```python
# After generating intelligence, analyze patterns
# v2.2 FIX: Use singleton instead of re-instantiating per scan
from src.intelligence.history_analyzer import get_analyzer, IST_OFFSET
from datetime import datetime, timezone

analyzer = get_analyzer()  # Module-level singleton, created once
current_context = {
    "symbol": symbol,
    "verdict_label": intel.verdict_label,
    "confidence": intel.confidence,
    # v2.0 FIX: Pass IST hour explicitly
    "ist_hour": (datetime.now(timezone.utc) + IST_OFFSET).hour,
}
dna_match = analyzer.get_trade_dna_match(current_context)

# Add to intelligence result
intel.trade_dna = dna_match
```

### 1.4 API Endpoints

**File:** `dashboard_server.py` (add new routes)

```python
@app.route("/api/ai/patterns")
def get_ai_patterns():
    """
    Get discovered trading patterns.
    
    v2.2 FIX: Uses module-level singleton + 5-min in-memory cache.
    Previously re-instantiated TradeHistoryAnalyzer on every API call,
    running 5 full SQL aggregation queries per request (every 30s poll).
    Cache invalidates on trade close or after 5 minutes.
    """
    from src.intelligence.history_analyzer import get_analyzer
    analyzer = get_analyzer()
    patterns = analyzer.get_cached_patterns()  # v2.2: Cached with 5-min TTL
    return jsonify([{
        "name": p.pattern_name,
        "win_rate": p.win_rate,
        "avg_pnl": p.avg_pnl,
        "sample_size": p.sample_size,
        "recommendation": p.recommendation
    } for p in patterns[:10]])

@app.route("/api/ai/trade-dna/<symbol>")
def get_trade_dna(symbol):
    """Get historical match for potential trade."""
    from src.intelligence.history_analyzer import get_analyzer, IST_OFFSET
    from datetime import datetime, timezone
    
    analyzer = get_analyzer()  # v2.2: Use singleton
    context = {
        "symbol": symbol,
        "verdict_label": request.args.get("verdict"),
        "confidence": int(request.args.get("confidence", 0)),
        "ist_hour": (datetime.now(timezone.utc) + IST_OFFSET).hour,
    }
    return jsonify(analyzer.get_trade_dna_match(context))
```

### 1.5 Phase 1 Deliverables

- ✅ `TradeHistoryAnalyzer` class with 5 pattern dimensions
- ✅ IST-corrected session bucketing
- ✅ Minimum 10 trades per pattern threshold
- ✅ Widened confidence bands (±20) for early-stage bots
- ✅ Database indexes for fast similarity lookup
- ✅ API endpoints for patterns and trade DNA
- ✅ Integration with existing pipeline

**Estimated Effort:** 8-12 hours  
**Impact:** Immediate value - shows what's working without ML


---

## 🤖 Phase 2: ML Success Predictor (Weeks 3-5)

**Goal:** Train XGBoost model to predict P(trade profitable)

### 2.1 New Module: `src/intelligence/ml_predictor.py`

```python
"""
Machine Learning model for predicting trade success probability.
Uses XGBoost with features extracted from trade context.

v2.0 FIXES:
- Feature leakage: uses opened_at, not datetime.now()
- Class imbalance: scale_pos_weight = n_neg/n_pos
- Feature ordering: explicit FEATURE_ORDER constant (not sorted())
- Model versioning: only deploy if AUC improves ≥2%
- Training source: UNION paper_trades + live_trades
- Event-driven retraining: edge health < 60 OR 20+ new trades
"""
import json
import logging
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ML dependencies (optional - graceful degradation)
try:
    import xgboost as xgb
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, roc_auc_score
    import shap
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    log.warning("XGBoost/sklearn not installed. ML predictions disabled.")

MODEL_DIR = Path("data/models")
MODEL_PATH = MODEL_DIR / "ml_model.json"
FEATURES_PATH = MODEL_DIR / "ml_features.json"
MIN_TRADES_FOR_TRAINING = 30
MIN_TRADES_FOR_PREDICTION = 10
AUC_IMPROVEMENT_THRESHOLD = 0.02  # v2.0: Only deploy if AUC improves ≥2%

# v2.0 FIX: Explicit feature order — never use sorted()
# This MUST match between training and prediction
FEATURE_ORDER = [
    # Core signal features
    "confidence",
    "price_change_pct",
    "pcr",
    # OI features
    "ce_oi_change",
    "pe_oi_change",
    "net_oi_change",
    # Distance features
    "distance_to_support_pct",
    "distance_to_resistance_pct",
    "distance_to_max_pain_pct",
    # Time features (from trade record, NOT datetime.now())
    "hour_of_day",
    "day_of_week",
    "days_to_expiry",
    # Chart features
    "chart_conflict",
    "rsi_1h",
    "rsi_3h",
    # Verdict encoding (one-hot — ALL 8 verdicts from VERDICT_ACTION_MAP)
    # v2.2 FIX: Added 4 missing verdicts. Previously only 4/8 were encoded,
    # meaning Call Writing, Put Writing, OI Bias Bullish, and OI Bias Bearish
    # trades were indistinguishable from each other (all read as zeros).
    "verdict_long_buildup",
    "verdict_short_buildup",
    "verdict_short_covering",
    "verdict_long_unwinding",
    "verdict_call_writing",
    "verdict_put_writing",
    "verdict_oi_bias_bullish",
    "verdict_oi_bias_bearish",
    # Regime features
    "regime_trending",
    "regime_rangebound",
]

IST_OFFSET = timedelta(hours=5, minutes=30)

@dataclass
class MLPrediction:
    """ML model prediction for a trade."""
    success_probability: float      # 0.0-1.0
    confidence_level: str           # "LOW", "MEDIUM", "HIGH"
    top_factors: list[tuple]        # [(feature_name, impact_score)]
    model_version: str
    training_samples: int


# ── Singleton (v2.2 FIX) ──────────────────────────────────────────────────
# Previously, pipeline.py instantiated TradeSuccessPredictor() on every scan
# cycle (5 symbols × every 3 minutes). Each instantiation calls _load_model()
# which loads XGBoost from disk (~50-100ms) and SHAP TreeExplainer init
# (~50-200ms). That's 1-2.5 seconds of disk I/O per cycle for zero benefit.
#
# Fix: Module-level singleton with lazy init. Loaded once, reused forever.
# After retraining, call invalidate_predictor() to force reload from disk.
import threading

_predictor: "TradeSuccessPredictor | None" = None
_predictor_lock = threading.Lock()

def get_predictor() -> "TradeSuccessPredictor":
    """Return the module-level TradeSuccessPredictor singleton."""
    global _predictor
    if _predictor is None:
        with _predictor_lock:
            if _predictor is None:  # double-checked locking
                _predictor = TradeSuccessPredictor()
    return _predictor

def invalidate_predictor():
    """v2.2: Call after retraining to force reload from disk."""
    global _predictor
    with _predictor_lock:
        _predictor = None


class TradeSuccessPredictor:
    """Predicts probability of trade success using XGBoost."""
    
    def __init__(self):
        self.model = None
        self.feature_names = list(FEATURE_ORDER)  # v2.0: Use explicit order
        self.model_version = "0.0"
        self.training_samples = 0
        self.current_auc = 0.0
        self._shap_explainer = None  # v2.1 FIX: Cache SHAP explainer
        self._needs_retrain = False  # v3.0: set when a stale model is discarded
        self._load_model()
    
    def _load_model(self):
        """Load pre-trained model from disk."""
        if not ML_AVAILABLE or not MODEL_PATH.exists():
            return
        
        try:
            self.model = xgb.XGBClassifier()
            self.model.load_model(str(MODEL_PATH))
            
            with open(FEATURES_PATH) as f:
                meta = json.load(f)
                saved_features = meta["feature_names"]
                self.model_version = meta["version"]
                self.training_samples = meta["training_samples"]
                self.current_auc = meta.get("auc", 0.0)
            
            # v3.0 FIX: Guard against FEATURE_ORDER drift. v2.2 added 4 verdict
            # one-hots (24 → 28 features). A model trained under the old order,
            # loaded and fed a NEW 28-wide vector, would either throw on shape
            # or — worse — silently miscolumn every feature. Refuse to use a
            # model whose feature set doesn't match the current constant.
            if list(saved_features) != list(FEATURE_ORDER):
                log.error(
                    f"Model feature set mismatch: saved={len(saved_features)} "
                    f"features, current FEATURE_ORDER={len(FEATURE_ORDER)}. "
                    f"Discarding stale model — retrain required."
                )
                self.model = None
                self._shap_explainer = None
                self._needs_retrain = True
                return
            self.feature_names = saved_features
            
            # v2.1 FIX: Cache SHAP explainer — init costs ~50-200ms per call.
            # Only recreate after retrain via _invalidate_shap_cache().
            self._shap_explainer = shap.TreeExplainer(self.model)
            
            log.info(f"Loaded ML model v{self.model_version} "
                    f"({self.training_samples} samples, AUC={self.current_auc:.3f})")
        except Exception as e:
            log.error(f"Failed to load ML model: {e}")
            self.model = None
            self._shap_explainer = None
    
    def _invalidate_shap_cache(self):
        """v2.1: Call after retraining to force explainer rebuild."""
        self._shap_explainer = None
    
    def _get_shap_explainer(self) -> shap.TreeExplainer:
        """v2.1: Lazy-init cached SHAP explainer."""
        if self._shap_explainer is None and self.model is not None:
            self._shap_explainer = shap.TreeExplainer(self.model)
        return self._shap_explainer
    
    def predict(self, trade_context: dict) -> MLPrediction | None:
        """Predict success probability for a trade."""
        if self.model is None:
            return None
        
        features = self._extract_features(trade_context)
        if features is None:
            return None
        
        # v2.0 FIX: Use explicit FEATURE_ORDER, not sorted()
        feature_vector = [features.get(name, 0) for name in FEATURE_ORDER]
        
        # Get probability
        proba = self.model.predict_proba([feature_vector])[0]
        success_prob = proba[1]  # P(profitable)
        
        # v2.1 FIX: Use cached SHAP explainer instead of creating new one
        # per prediction (saves ~50-200ms per call).
        # v3.0 FIX: Normalize SHAP return shape. TreeExplainer.shap_values()
        # returns an ndarray (n, features) on newer SHAP for binary models, but
        # a list [class0_arr, class1_arr] on older versions. The old code did
        # [0], which on the list form grabbed class-0's full feature array
        # instead of the row — mislabeling every top factor. Handle both.
        explainer = self._get_shap_explainer()
        raw = explainer.shap_values([feature_vector])
        if isinstance(raw, list):
            # Per-class list → take positive class, then the single row
            shap_values = np.asarray(raw[-1])[0]
        else:
            shap_values = np.asarray(raw)[0]
        
        # Top 3 factors driving prediction
        top_indices = np.argsort(np.abs(shap_values))[-3:][::-1]
        top_factors = [
            (FEATURE_ORDER[i], float(shap_values[i]))
            for i in top_indices
        ]
        
        # v3.0 FIX: Confidence level now blends TWO signals, not just sample
        # count. A model with 200 samples can still be unsure on a borderline
        # trade (p≈0.5). margin = |p - 0.5| measures how decisive THIS
        # prediction is. Old code labelled everything "HIGH" once n>100 even
        # for coin-flip predictions — misleading to the trader.
        margin = abs(success_prob - 0.5)  # 0 (coin-flip) … 0.5 (certain)
        if self.training_samples < 50 or margin < 0.10:
            confidence = "LOW"
        elif self.training_samples < 100 or margin < 0.20:
            confidence = "MEDIUM"
        else:
            confidence = "HIGH"
        
        return MLPrediction(
            success_probability=float(success_prob),
            confidence_level=confidence,
            top_factors=top_factors,
            model_version=self.model_version,
            training_samples=self.training_samples
        )


    def _extract_features(self, ctx: dict) -> dict | None:
        """
        Extract numeric features from trade context.
        
        v2.0 FIX: Time features use opened_at from trade record,
        NOT datetime.now(). Using current time during training would
        cause feature leakage — all historical trades would get
        today's hour/day instead of their actual entry time.
        """
        try:
            # v2.0 FIX: Extract time from trade record, not current time
            opened_at = ctx.get("opened_at")
            if opened_at:
                # Convert UTC → IST for meaningful hour/day features
                trade_time = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
                ist_time = trade_time + IST_OFFSET
                hour_of_day = ist_time.hour
                day_of_week = ist_time.weekday()
            else:
                # Fallback for live predictions (no opened_at yet)
                now_ist = datetime.now(timezone.utc) + IST_OFFSET
                hour_of_day = now_ist.hour
                day_of_week = now_ist.weekday()
            
            features = {
                # Core signal features
                "confidence": float(ctx.get("confidence", 0)),
                "price_change_pct": float(ctx.get("price_change_pct", 0)),
                "pcr": float(ctx.get("pcr", 1.0)),
                
                # OI features
                "ce_oi_change": float(ctx.get("ce_oi_change", 0)),
                "pe_oi_change": float(ctx.get("pe_oi_change", 0)),
                # v3.0 FIX: "net" implies direction. Old code used ce + pe,
                # which is TOTAL OI change (collinear with its two inputs and
                # directionless). Redefine as pe - ce: positive = put-writing /
                # bullish OI bias, negative = call-writing / bearish OI bias.
                "net_oi_change": float(ctx.get("pe_oi_change", 0)) - float(ctx.get("ce_oi_change", 0)),
                
                # Distance features
                "distance_to_support_pct": self._calc_distance_pct(
                    ctx.get("underlying"), ctx.get("support")
                ),
                "distance_to_resistance_pct": self._calc_distance_pct(
                    ctx.get("underlying"), ctx.get("resistance")
                ),
                "distance_to_max_pain_pct": self._calc_distance_pct(
                    ctx.get("underlying"), ctx.get("max_pain")
                ),
                
                # Time features (v2.0: from trade record)
                "hour_of_day": hour_of_day,
                "day_of_week": day_of_week,
                "days_to_expiry": int(ctx.get("days_to_expiry", 7)),
                
                # Chart features
                "chart_conflict": 1 if ctx.get("chart_conflict") else 0,
                "rsi_1h": float(ctx.get("rsi_1h", 50)),
                "rsi_3h": float(ctx.get("rsi_3h", 50)),
                
                # Verdict encoding (one-hot — ALL 8 verdicts)
                # v2.2 FIX: Encode all 8 verdicts from VERDICT_ACTION_MAP.
                # Previously only 4 were encoded — Call Writing, Put Writing,
                # OI Bias Bullish, OI Bias Bearish were silently dropped.
                "verdict_long_buildup": 1 if ctx.get("verdict_label") == "Long Buildup" else 0,
                "verdict_short_buildup": 1 if ctx.get("verdict_label") == "Short Buildup" else 0,
                "verdict_short_covering": 1 if ctx.get("verdict_label") == "Short Covering" else 0,
                "verdict_long_unwinding": 1 if ctx.get("verdict_label") == "Long Unwinding" else 0,
                "verdict_call_writing": 1 if ctx.get("verdict_label") == "Call Writing" else 0,
                "verdict_put_writing": 1 if ctx.get("verdict_label") == "Put Writing" else 0,
                "verdict_oi_bias_bullish": 1 if ctx.get("verdict_label") == "OI Bias Bullish" else 0,
                "verdict_oi_bias_bearish": 1 if ctx.get("verdict_label") == "OI Bias Bearish" else 0,
                
                # Regime features
                "regime_trending": 1 if "trending" in str(ctx.get("regime", "")).lower() else 0,
                "regime_rangebound": 1 if "range" in str(ctx.get("regime", "")).lower() else 0,
            }
            return features
        except Exception as e:
            log.error(f"Feature extraction failed: {e}")
            return None
    
    def _calc_distance_pct(self, underlying, level) -> float:
        """Calculate percentage distance to a level."""
        if not underlying or not level:
            return 0.0
        return abs(float(underlying) - float(level)) / float(underlying) * 100
    
    def train(self) -> bool:
        """
        Train model on historical trades.
        
        v2.0 FIXES:
        - Uses UNION of paper_trades + live_trades
        - Handles class imbalance with scale_pos_weight
        - Uses FEATURE_ORDER (not sorted()) for consistent ordering
        - Version comparison: only deploy if AUC improves ≥2%
        """
        if not ML_AVAILABLE:
            log.warning("ML libraries not available. Training skipped.")
            return False
        
        # v3.0 FIX: Phase 0 gate. Refuse to train if too many closed trades
        # lack persisted feature columns — otherwise the model trains on zeros
        # and looks fine while learning nothing. See Section 0.3.
        from src.intelligence.feature_coverage import assert_feature_coverage
        if not assert_feature_coverage(min_pct=0.90):
            log.warning("Feature coverage below threshold. Training skipped.")
            return False
        
        from src.models.schema import get_conn
        
        # v2.0 FIX: Fetch from BOTH paper_trades and live_trades
        with get_conn() as conn:
            trades = conn.execute("""
                SELECT *, 'paper' as source FROM paper_trades
                WHERE status != 'OPEN'
                  AND closed_at IS NOT NULL
                  AND pnl_rupees IS NOT NULL
                UNION ALL
                SELECT *, 'live' as source FROM live_trades
                WHERE status != 'OPEN'
                  AND closed_at IS NOT NULL
                  AND pnl_rupees IS NOT NULL
            """).fetchall()
        
        if len(trades) < MIN_TRADES_FOR_TRAINING:
            log.info(f"Insufficient trades for training ({len(trades)}/{MIN_TRADES_FOR_TRAINING})")
            return False
        
        log.info(f"Training ML model on {len(trades)} trades...")
        
        # Extract features and labels
        X = []
        y = []
        
        for trade in trades:
            trade_dict = dict(trade)
            features = self._extract_features(trade_dict)
            if features is None:
                continue
            
            label = 1 if float(trade["pnl_rupees"]) > 0 else 0
            
            # v2.0 FIX: Use FEATURE_ORDER, not sorted(features.keys())
            X.append([features.get(name, 0) for name in FEATURE_ORDER])
            y.append(label)
        
        if len(X) < MIN_TRADES_FOR_TRAINING:
            log.warning(f"Insufficient valid samples ({len(X)}/{MIN_TRADES_FOR_TRAINING})")
            return False
        
        # v2.0 FIX: Handle class imbalance
        n_pos = sum(y)
        n_neg = len(y) - n_pos
        scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0
        log.info(f"Class balance: {n_pos} positive, {n_neg} negative "
                f"(scale_pos_weight={scale_pos_weight:.2f})")
        
        # v3.0 FIX: Stratified split. With ~30 imbalanced trades an unstratified
        # 20% holdout can land all-one-class → roc_auc_score is undefined and
        # falls back to 0.5, so the deploy gate fails forever. stratify=y keeps
        # both classes in train AND test.
        from sklearn.model_selection import StratifiedKFold
        from sklearn.base import clone
        X_arr, y_arr = np.asarray(X), np.asarray(y)

        X_train, X_test, y_train, y_test = train_test_split(
            X_arr, y_arr, test_size=0.2, random_state=42, stratify=y_arr
        )
        
        # Train XGBoost with class imbalance handling
        new_model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            scale_pos_weight=scale_pos_weight,  # v2.0 FIX
            random_state=42
        )
        new_model.fit(X_train, y_train)
        
        # Evaluate on holdout (reported) …
        y_pred_proba = new_model.predict_proba(X_test)[:, 1]
        holdout_auc = roc_auc_score(y_test, y_pred_proba) if len(set(y_test)) > 1 else 0.5
        accuracy = accuracy_score(y_test, new_model.predict(X_test))

        # v3.0 FIX: …but GATE on cross-validated AUC, not the holdout. A 6-sample
        # holdout at n≈30 is pure noise — a ±2% gate on it is meaningless.
        # Stratified K-fold gives a far more stable estimate to deploy against.
        n_splits = min(5, n_pos, n_neg)  # can't have more folds than minority count
        if n_splits >= 2:
            skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
            cv_aucs = []
            for tr_idx, va_idx in skf.split(X_arr, y_arr):
                m = clone(new_model)
                m.fit(X_arr[tr_idx], y_arr[tr_idx])
                if len(set(y_arr[va_idx])) > 1:
                    cv_aucs.append(roc_auc_score(
                        y_arr[va_idx], m.predict_proba(X_arr[va_idx])[:, 1]))
            new_auc = float(np.mean(cv_aucs)) if cv_aucs else holdout_auc
        else:
            new_auc = holdout_auc
        log.info(f"New model: holdout_acc={accuracy:.2%}, "
                 f"holdout_AUC={holdout_auc:.3f}, CV_AUC={new_auc:.3f}")
        
        # v2.1 FIX: Use a meaningful AUC floor to prevent startup blind spot.
        # On first-ever training (current_auc == 0.0), the old gate was skipped
        # entirely, allowing any model to deploy. On subsequent runs with a weak
        # baseline (e.g., AUC=0.51 from imbalanced early data), better models
        # could be locked out. Floor of 0.55 ensures the gate always enforces
        # a minimum quality bar while still allowing first deployment.
        AUC_FLOOR = 0.55
        effective_baseline = max(self.current_auc, AUC_FLOOR)
        
        if new_auc < effective_baseline + AUC_IMPROVEMENT_THRESHOLD:
            log.warning(f"New model AUC ({new_auc:.3f}) not ≥{AUC_IMPROVEMENT_THRESHOLD} "
                       f"better than baseline ({effective_baseline:.3f}, "
                       f"current={self.current_auc:.3f}). Keeping old model.")
            return False
        
        # Save model with versioning
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self.model = new_model
        self.feature_names = list(FEATURE_ORDER)
        self.model_version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")  # v3.0: UTC, not naive local
        self.training_samples = len(X)
        self.current_auc = new_auc
        
        # v2.1 FIX: Invalidate cached SHAP explainer after retrain
        self._invalidate_shap_cache()
        
        self.model.save_model(str(MODEL_PATH))
        
        with open(FEATURES_PATH, "w") as f:
            json.dump({
                "feature_names": self.feature_names,
                "version": self.model_version,
                "training_samples": self.training_samples,
                "accuracy": accuracy,
                "auc": new_auc,
                "scale_pos_weight": scale_pos_weight,
            }, f)
        
        log.info(f"✅ Model deployed: v{self.model_version} "
                f"({self.training_samples} samples, AUC={new_auc:.3f})")
        return True
```


### 2.2 Training Scheduler (Event-Driven)

**File:** `src/scheduler/ml_training_job.py`

```python
"""
ML model retraining job.

v2.0 CHANGE: Event-driven instead of weekly-only.
Triggers when:
1. Edge health score drops below 60
2. 20+ new trades accumulated since last training
3. Weekly fallback (Sunday 2 AM IST) as safety net
"""
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# v2.1 FIX: Thread-safe trade counter. APScheduler's BackgroundScheduler runs
# the weekly job in a background thread while on_trade_closed() is called from
# the pipeline thread. A bare global int += 1 is NOT atomic under threading.
import threading

_retrain_lock = threading.Lock()
_trades_since_last_train = 0
TRADES_THRESHOLD_FOR_RETRAIN = 20
EDGE_HEALTH_THRESHOLD = 60

def on_trade_closed():
    """Called after each trade closes. Increments counter (thread-safe)."""
    global _trades_since_last_train
    with _retrain_lock:
        _trades_since_last_train += 1
        count = _trades_since_last_train
    
    if count >= TRADES_THRESHOLD_FOR_RETRAIN:
        log.info(f"Retrain triggered: {count} new trades")
        run_training()

def on_edge_health_alert(health_score: float):
    """Called when edge decay monitor detects declining performance."""
    if health_score < EDGE_HEALTH_THRESHOLD:
        log.info(f"Retrain triggered: edge health {health_score} < {EDGE_HEALTH_THRESHOLD}")
        run_training()

def run_weekly_training():
    """Weekly fallback retraining (Sunday 2 AM IST)."""
    log.info("Starting weekly ML training job...")
    run_training()

def run_training():
    """
    Execute model training with rollback protection.
    
    v2.2 FIX: Uses singleton predictor and invalidates it after successful
    training so the next get_predictor() call loads the new model from disk.
    """
    global _trades_since_last_train
    
    from src.intelligence.ml_predictor import get_predictor, invalidate_predictor
    
    predictor = get_predictor()  # v2.2: Use singleton
    success = predictor.train()
    
    if success:
        with _retrain_lock:
            _trades_since_last_train = 0
        invalidate_predictor()  # v2.2: Force reload of new model on next use
        log.info("✅ ML model training completed successfully")
    else:
        log.warning("⚠️ ML model training failed or skipped (AUC not improved)")
    
    return success
```

**Integration:** Add to `src/scheduler/__init__.py`

```python
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()

# Weekly fallback (Sunday 2 AM IST)
scheduler.add_job(
    run_weekly_training,
    'cron',
    day_of_week='sun',
    hour=2,
    minute=0,
    id='ml_training_weekly'
)

# Event-driven triggers are called directly from pipeline/edge_monitor
```

### 2.3 Integration with Pipeline

**File:** `src/engine/pipeline.py` (modify after intelligence generation)

```python
# Add ML prediction to intelligence
# v2.2 FIX: Use singleton instead of re-instantiating per scan cycle.
# TradeSuccessPredictor.__init__() calls _load_model() which loads XGBoost
# from disk (~50-100ms) and _shap_explainer init (~50-200ms). At 5 symbols
# every 3 minutes, that's 1-2.5 seconds of disk I/O per cycle.
from src.intelligence.ml_predictor import get_predictor

ml_predictor = get_predictor()  # Module-level singleton, loaded once
ml_prediction = ml_predictor.predict({
    "symbol": symbol,
    "confidence": intel.confidence,
    "verdict_label": intel.verdict_label,
    "price_change_pct": scan_context.get("price_change_pct"),
    "pcr": scan_context.get("pcr"),
    "ce_oi_change": scan_context.get("ce_oi_change"),
    "pe_oi_change": scan_context.get("pe_oi_change"),
    "underlying": scan_context.get("underlying"),
    "support": scan_context.get("support"),
    "resistance": scan_context.get("resistance"),
    "max_pain": scan_context.get("max_pain"),
    "chart_conflict": intel.chart_conflict,
    "days_to_expiry": intel.days_to_expiry,
    # v2.0 FIX: Pass opened_at for correct time features
    "opened_at": scan_context.get("timestamp"),
})

if ml_prediction:
    intel.ml_prediction = ml_prediction
    log.info(f"[ML] {symbol}: P(success) = {ml_prediction.success_probability:.1%} "
             f"(confidence: {ml_prediction.confidence_level})")
```

### 2.4 Phase 2 Deliverables

- ✅ `TradeSuccessPredictor` with XGBoost model
- ✅ Feature extraction using `opened_at` (no leakage)
- ✅ Class imbalance handling (`scale_pos_weight`)
- ✅ Explicit `FEATURE_ORDER` constant (no `sorted()`)
- ✅ SHAP explainability (top 3 factors)
- ✅ Model versioning with AUC-based rollback
- ✅ Event-driven retraining (edge health + trade count)
- ✅ UNION of paper_trades + live_trades for training
- ✅ API endpoint for predictions

**Estimated Effort:** 15-20 hours  
**Dependencies:** `pip install xgboost scikit-learn shap`  
**Impact:** Quantitative success probability with explainability


---

## 📉 Phase 3: Edge Decay Monitor (Weeks 6-7)

**Goal:** Track strategy performance over time, detect when edge is weakening

> ⚠️ **v2.0 Note:** Behavioral Coach (Phase 3 in v1.0) has been **removed**.
> The bot is automated — FOMO/overtrading are human problems already handled
> by existing gates in `trade_decision.py` (`MIN_CONFIDENCE_CORE=70`,
> `TREND_MIN_SCANS=3`, regime checks). Adding a redundant behavioral layer
> creates conflicting control points with `risk_engine.py` limits
> (`MAX_OPEN_TRADES_PER_SYMBOL=2`, `MAX_OPEN_TRADES_TOTAL=5`).

### 3.1 New Module: `src/intelligence/edge_monitor.py`

```python
"""
Monitors strategy performance over time to detect edge decay.
Alerts when win rate or profitability is declining.

v2.0 FIXES:
- Guard for insufficient historical data (< 5 trades → INSUFFICIENT_HISTORY)
- Health score includes absolute performance, not just deltas
- Single GROUP BY query instead of N+1 per strategy
- IST-corrected time windows
"""
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

log = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)

@dataclass
class EdgeHealth:
    """Health status of a trading strategy."""
    strategy_name: str
    current_win_rate: float
    historical_win_rate: float
    win_rate_trend: str          # "IMPROVING", "STABLE", "DECLINING", "INSUFFICIENT_HISTORY"
    pnl_trend: str               # "IMPROVING", "STABLE", "DECLINING", "INSUFFICIENT_HISTORY"
    health_score: float          # 0-100
    recommendation: str

class EdgeDecayMonitor:
    """Detects when trading edge is weakening."""
    
    MIN_HISTORICAL_TRADES = 5  # v2.0: Guard against biased comparisons
    
    def __init__(self):
        self.rolling_window_days = 30
        self.historical_window_days = 90
        self.decay_threshold = 0.15  # 15% decline triggers alert
    
    def check_edge_health(self, strategy_filter: dict | None = None) -> list[EdgeHealth]:
        """Check health of all strategies or filtered subset."""
        from src.models.schema import get_conn
        
        where_clause = "status != 'OPEN' AND closed_at IS NOT NULL"
        params = []
        
        if strategy_filter:
            if "symbol" in strategy_filter:
                where_clause += " AND symbol = ?"
                params.append(strategy_filter["symbol"])
            if "verdict_label" in strategy_filter:
                where_clause += " AND verdict_label = ?"
                params.append(strategy_filter["verdict_label"])
        
        with get_conn() as conn:
            recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=self.rolling_window_days)).isoformat()
            recent = conn.execute(f"""
                SELECT 
                    COUNT(*) as count,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(pnl_rupees) as total_pnl,
                    AVG(pnl_rupees) as avg_pnl
                FROM paper_trades
                WHERE {where_clause} AND closed_at >= ?
            """, params + [recent_cutoff]).fetchone()
            
            hist_start = (datetime.now(timezone.utc) - timedelta(days=self.historical_window_days)).isoformat()
            hist_end = recent_cutoff
            historical = conn.execute(f"""
                SELECT 
                    COUNT(*) as count,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(pnl_rupees) as total_pnl,
                    AVG(pnl_rupees) as avg_pnl
                FROM paper_trades
                WHERE {where_clause} AND closed_at >= ? AND closed_at < ?
            """, params + [hist_start, hist_end]).fetchone()
        
        if not recent or recent["count"] < self.MIN_HISTORICAL_TRADES:
            return [EdgeHealth(
                strategy_name="All Strategies",
                current_win_rate=0,
                historical_win_rate=0,
                win_rate_trend="INSUFFICIENT_HISTORY",  # v3.0: unified sentinel
                pnl_trend="INSUFFICIENT_HISTORY",
                health_score=50,
                recommendation="Not enough recent trades to assess"
            )]
        
        current_win_rate = recent["wins"] / recent["count"] if recent["count"] > 0 else 0
        
        # v2.0 FIX: Guard against biased comparison when historical window is empty
        hist_count = historical["count"] if historical else 0
        if hist_count < self.MIN_HISTORICAL_TRADES:
            return [EdgeHealth(
                strategy_name=self._get_strategy_name(strategy_filter),
                current_win_rate=current_win_rate,
                historical_win_rate=0,
                win_rate_trend="INSUFFICIENT_HISTORY",
                pnl_trend="INSUFFICIENT_HISTORY",
                health_score=self._calculate_health_score_absolute(current_win_rate, recent["avg_pnl"]),
                recommendation=f"⏳ Building history ({hist_count}/{self.MIN_HISTORICAL_TRADES} trades). "
                              f"Current win rate: {current_win_rate:.0%}"
            )]
        
        hist_win_rate = historical["wins"] / historical["count"]
        
        win_rate_change = current_win_rate - hist_win_rate
        pnl_change = (recent["avg_pnl"] - historical["avg_pnl"]) if historical else 0
        
        win_rate_trend = self._classify_trend(win_rate_change, hist_win_rate)
        # v3.0 FIX: Floor the PnL baseline. When historical avg PnL ≈ 0 the
        # ratio change/baseline explodes and every period reads IMPROVING or
        # DECLINING. Floor at ₹100 so tiny baselines can't dominate.
        pnl_baseline = max(abs(historical["avg_pnl"]) if historical else 0.0, 100.0)
        pnl_trend = self._classify_trend(pnl_change, pnl_baseline)
        
        # v2.0 FIX: Health score includes absolute performance
        health_score = self._calculate_health_score(
            current_win_rate, hist_win_rate, recent["avg_pnl"], 
            historical["avg_pnl"] if historical else 0
        )
        
        recommendation = self._generate_edge_recommendation(
            current_win_rate, hist_win_rate, recent["avg_pnl"], win_rate_trend
        )
        
        return [EdgeHealth(
            strategy_name=self._get_strategy_name(strategy_filter),
            current_win_rate=current_win_rate,
            historical_win_rate=hist_win_rate,
            win_rate_trend=win_rate_trend,
            pnl_trend=pnl_trend,
            health_score=health_score,
            recommendation=recommendation
        )]


    def _get_strategy_name(self, strategy_filter: dict | None) -> str:
        if not strategy_filter:
            return "All Strategies"
        parts = []
        if "symbol" in strategy_filter:
            parts.append(strategy_filter["symbol"])
        if "verdict_label" in strategy_filter:
            parts.append(strategy_filter["verdict_label"])
        return " ".join(parts) if parts else "All Strategies"
    
    def _classify_trend(self, change: float, baseline: float) -> str:
        """Classify trend as IMPROVING, STABLE, or DECLINING."""
        if baseline == 0:
            return "STABLE"
        change_pct = change / baseline
        if change_pct > 0.10:
            return "IMPROVING"
        elif change_pct < -self.decay_threshold:
            return "DECLINING"
        else:
            return "STABLE"
    
    def _calculate_health_score_absolute(self, current_wr: float, avg_pnl: float) -> float:
        """
        v2.0 FIX: Health score based on absolute performance only.
        Used when historical data is insufficient for comparison.
        """
        score = 50.0  # Start at neutral
        
        # Win rate component (0-30 points)
        if current_wr >= 0.70:
            score += 30
        elif current_wr >= 0.60:
            score += 20
        elif current_wr >= 0.50:
            score += 10
        elif current_wr >= 0.40:
            score -= 10
        else:
            score -= 20
        
        # PnL component (0-20 points)
        if avg_pnl > 1000:
            score += 20
        elif avg_pnl > 0:
            score += 10
        elif avg_pnl > -500:
            score -= 5
        else:
            score -= 20
        
        return max(0, min(100, score))
    
    def _calculate_health_score(self, current_wr: float, hist_wr: float, 
                                current_pnl: float, hist_pnl: float) -> float:
        """
        Calculate overall health score (0-100).
        
        v2.0 FIX: Includes BOTH absolute performance AND trend.
        Previous version only subtracted from 100, causing new strategies
        with 60% win rate to score higher than declining strategies at 70%.
        """
        score = 0.0
        
        # Absolute win rate (40 points max)
        if current_wr >= 0.70:
            score += 40
        elif current_wr >= 0.60:
            score += 30
        elif current_wr >= 0.50:
            score += 20
        elif current_wr >= 0.40:
            score += 10
        # Below 40% gets 0 points
        
        # Win rate trend (30 points max)
        wr_change = current_wr - hist_wr if hist_wr > 0 else 0
        if wr_change > 0.10:
            score += 30
        elif wr_change > 0.05:
            score += 20
        elif wr_change > -0.05:
            score += 15  # Stable is OK
        elif wr_change > -0.10:
            score += 5
        elif wr_change > -0.15:
            score += 0
        # Severe decline gets 0 points
        
        # Absolute PnL (15 points max)
        if current_pnl > 1000:
            score += 15
        elif current_pnl > 0:
            score += 10
        elif current_pnl > -500:
            score += 5
        # Negative PnL gets 0
        
        # PnL trend (15 points max)
        if hist_pnl != 0:
            pnl_change_pct = (current_pnl - hist_pnl) / abs(hist_pnl)
            if pnl_change_pct > 0.20:
                score += 15
            elif pnl_change_pct > 0:
                score += 10
            elif pnl_change_pct > -0.20:
                score += 5
            # Severe PnL decline gets 0
        
        return max(0, min(100, score))
    
    def _generate_edge_recommendation(self, current_wr: float, hist_wr: float,
                                     avg_pnl: float, trend: str) -> str:
        """Generate actionable recommendation."""
        if trend == "DECLINING":
            return ("🔴 EDGE DECAY DETECTED - Your strategy is underperforming. "
                   "Consider: (1) Reducing position size, (2) Raising confidence threshold, "
                   "(3) Pausing this strategy for 1 week to recalibrate.")
        
        if current_wr < 0.50:
            return ("🟠 BELOW BREAKEVEN - Win rate below 50%. "
                   "Review recent trades for common mistakes. "
                   "Consider pausing until you identify the issue.")
        
        if current_wr < 0.60:
            return ("🟡 MARGINAL EDGE - Win rate is acceptable but not strong. "
                   "Look for higher confidence setups or better confluence.")
        
        if current_wr >= 0.70 and avg_pnl > 1000:
            return ("🟢 STRONG EDGE - Your strategy is performing well. "
                   "Continue with current parameters. Consider slight size increase.")
        
        return ("⚪ STABLE - Strategy is performing as expected. "
               "Monitor for changes over next 2 weeks.")
    
    def get_all_strategies_health(self) -> list[EdgeHealth]:
        """
        Check health of all strategy combinations.
        
        v2.0 FIX: Single GROUP BY query instead of N+1 per strategy.
        v2.1 FIX: Score per-strategy health directly from pre-fetched row
        data instead of calling check_edge_health() which re-queries the DB.
        
        v2.2 FIX: Previous version called undefined module-level _classify_trend()
        and EdgeHealthReport (neither exist). Now uses self._classify_trend(change, baseline)
        and the EdgeHealth dataclass with correct field names. Inline scoring delegates
        to _calculate_health_score_absolute() to avoid formula divergence.
        """
        from src.models.schema import get_conn
        
        # Single GROUP BY query — fetches all strategy metrics at once
        with get_conn() as conn:
            recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=self.rolling_window_days)).isoformat()
            
            rows = conn.execute("""
                SELECT 
                    symbol,
                    verdict_label,
                    COUNT(*) as count,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    AVG(pnl_rupees) as avg_pnl
                FROM paper_trades
                WHERE status != 'OPEN' 
                  AND closed_at IS NOT NULL
                  AND closed_at >= ?
                GROUP BY symbol, verdict_label
                HAVING COUNT(*) >= 10
            """, (recent_cutoff,)).fetchall()
        
        health_reports = []
        
        # v3.0 FIX: Keep the two scoring methods in separate, clearly-labelled
        # groups instead of interleaving them in one sorted list. The overall
        # report uses trend-based scoring (real historical comparison); the
        # per-strategy rows have NO historical window here, so they get
        # absolute-only scoring and an honest INSUFFICIENT_HISTORY trend rather
        # than a fabricated IMPROVING/DECLINING label derived from a fixed 0.55.

        # Group A: overall health (trend-based, real comparison)
        overall = self.check_edge_health()

        # Group B: per-strategy snapshots (absolute scoring, no trend claim)
        per_strategy = []
        for row in rows:
            total = row["count"]
            wins = row["wins"]
            avg_pnl = row["avg_pnl"]
            win_rate = wins / total if total > 0 else 0.0
            score = self._calculate_health_score_absolute(win_rate, avg_pnl)
            per_strategy.append(EdgeHealth(
                strategy_name=f"{row['symbol']} {row['verdict_label']}",
                current_win_rate=win_rate,
                historical_win_rate=0.0,
                # v3.0: honest sentinel — this is a snapshot, not a trend
                win_rate_trend="INSUFFICIENT_HISTORY",
                pnl_trend="INSUFFICIENT_HISTORY",
                health_score=score,
                recommendation=self._generate_edge_recommendation(
                    win_rate, win_rate, avg_pnl, "INSUFFICIENT_HISTORY"),
            ))

        # Sort each group by health_score; overall first, then worst strategies.
        health_reports = overall + sorted(per_strategy, key=lambda h: h.health_score)
        return health_reports
```

### 3.2 Integration with ML Retraining

**File:** `src/engine/pipeline.py` (add after trade close)

```python
# After closing a trade, check edge health and trigger retrain if needed
from src.intelligence.edge_monitor import EdgeDecayMonitor
from src.scheduler.ml_training_job import on_trade_closed, on_edge_health_alert

on_trade_closed()  # Increment trade counter

monitor = EdgeDecayMonitor()
overall_health = monitor.check_edge_health()
if overall_health:
    h = overall_health[0]
    # v3.0 FIX: Do NOT trigger retrain on the insufficient-data sentinel.
    # The early-data branch returns health_score=50, which is < the retrain
    # threshold (60) — so the old code fired run_training() on EVERY early
    # trade close (wasted calls; train() bails under 30 trades anyway, and
    # thrashes once just past it). Only react to a REAL declining edge.
    if h.win_rate_trend not in ("INSUFFICIENT_HISTORY",):
        on_edge_health_alert(h.health_score)
```

### 3.3 Phase 3 Deliverables

- ✅ `EdgeDecayMonitor` with corrected health scoring
- ✅ Guard against insufficient historical data
- ✅ Absolute + relative performance in health score
- ✅ Single GROUP BY query — no hidden N+1 (v2.1: inline scoring from pre-fetched rows)
- ✅ Win rate and PnL trend detection
- ✅ Edge decay alerts triggering ML retraining
- ✅ Strategy-specific health reports
- ✅ Dashboard integration

**Estimated Effort:** 8-10 hours  
**Impact:** Prevents continued use of failing strategies


---

## 🎨 Phase 4: AI Dashboard UI (Weeks 8-10)

**Goal:** Visual interface for all AI insights

### 4.0 Design Critique of the v2.2 UI (why it was rebuilt)

The v2.2 dashboard worked but read as a developer placeholder, not a trading tool.

**Overall impression:** Four equal-weight cards in an undefined grid, every value the same size, semantics ("danger"/"success") referenced in JS but never defined in CSS, and only a single `Loading…`/`error` state. Nothing tells the eye what matters.

| Finding | Severity | Fix in v3.0 |
|---|---|---|
| No visual hierarchy — DNA, Patterns, ML, Edge all weighted equally | 🔴 | ML probability is the hero (one big gauge); supporting cards are secondary |
| Color classes (`success`/`warning`/`danger`) used in JS, never defined | 🔴 | Semantic tokens defined once in `:root`, applied consistently |
| Only `loading`/`error` states; empty/partial never designed | 🟡 | Four explicit states per panel: loading (skeleton), empty, error (with retry), ready |
| `renderEdgeHealth` read `h.strategy` but API returns `strategy_name` | 🔴 | Field names reconciled (`strategy_name`, `current_win_rate`) |
| Win-rate trend `.toLowerCase()` → `insufficient_history` class never styled | 🟡 | Trend pills mapped explicitly, neutral styling for INSUFFICIENT_HISTORY |
| No contrast spec; emoji-only severity fails screen readers | 🟡 | WCAG AA tokens; `aria-label` + text on every status, not color/emoji alone |
| Raw feature names shown (`net_oi_change: +0.42`) | 🟢 | Human labels + signed bar; tooltip carries the raw value |

**What worked and was kept:** per-panel `try/catch`, 30-s polling (no WebSocket), the singleton/cache backend.

---

### 4.1 Design Tokens

**File:** `src/dashboard/ai_insights.css` — defined once, referenced everywhere.
Color is never the *only* signal (accessibility): every status also carries text/`aria-label`.

```css
:root {
  /* Surfaces (dark trading UI) */
  --ai-bg:        #0e1117;
  --ai-surface:   #161b22;
  --ai-surface-2: #1c2330;
  --ai-border:    #2b3340;

  /* Text — all ≥ 4.5:1 on --ai-surface (WCAG AA) */
  --ai-text:      #e6edf3;   /* 13.4:1 */
  --ai-text-dim:  #9aa7b4;   /* 5.1:1  */

  /* Semantic status — paired bg/fg meet AA; used for BOTH fill and text */
  --ai-good:      #2ea043;  --ai-good-bg:  #12261a;
  --ai-warn:      #d29922;  --ai-warn-bg:  #2a2212;
  --ai-bad:       #f85149;  --ai-bad-bg:   #2a1517;
  --ai-neutral:   #6e7b8a;  --ai-neutral-bg:#1b222c;
  --ai-accent:    #4c8dff;

  /* Type scale */
  --fs-hero: 2.75rem; --fs-h3: 1rem; --fs-body: .875rem; --fs-micro: .75rem;
  /* Spacing (8px base) */
  --sp-1:.25rem; --sp-2:.5rem; --sp-3:.75rem; --sp-4:1rem; --sp-6:1.5rem;
  --radius: 12px;
  --shadow: 0 1px 2px rgba(0,0,0,.4), 0 4px 16px rgba(0,0,0,.25);
}
```

### 4.2 Layout & Components

**File:** `src/dashboard/ai_insights.html`

Hierarchy: the ML success gauge is the hero (full width, top). Trade DNA + Edge
Health sit on a responsive two-up row. Patterns span full width below. Grid
collapses to a single column under 720px. Each panel owns a live region so
screen readers announce updates.

```html
<section class="ai-insights" aria-labelledby="ai-title">
  <header class="ai-head">
    <h2 id="ai-title">AI Intelligence</h2>
    <div class="ai-head__meta">
      <span id="ai-symbol-pill" class="pill">—</span>
      <button id="ai-refresh" class="btn-ghost" aria-label="Refresh insights">↻</button>
      <span id="ai-updated" class="ai-micro" aria-live="polite"></span>
    </div>
  </header>

  <!-- HERO: ML success probability -->
  <article class="card card--hero" aria-labelledby="ml-title">
    <h3 id="ml-title" class="card__title">🤖 Success Probability</h3>
    <div id="ml-prediction-content" class="card__body" aria-live="polite"
         role="region" data-state="loading"></div>
  </article>

  <div class="grid-2">
    <article class="card" aria-labelledby="dna-title">
      <h3 id="dna-title" class="card__title">🧬 Trade DNA</h3>
      <div id="dna-match-content" class="card__body" aria-live="polite"
           role="region" data-state="loading"></div>
    </article>

    <article class="card" aria-labelledby="edge-title">
      <h3 id="edge-title" class="card__title">📉 Edge Health</h3>
      <div id="edge-health-content" class="card__body" aria-live="polite"
           role="region" data-state="loading"></div>
    </article>
  </div>

  <article class="card" aria-labelledby="pat-title">
    <h3 id="pat-title" class="card__title">📊 Top Patterns</h3>
    <div id="patterns-list" class="card__body" aria-live="polite"
         role="region" data-state="loading"></div>
  </article>
</section>
```

```css
.ai-insights { display:flex; flex-direction:column; gap:var(--sp-4);
  background:var(--ai-bg); padding:var(--sp-6); color:var(--ai-text);
  font:var(--fs-body)/1.5 system-ui, sans-serif; }
.ai-head { display:flex; justify-content:space-between; align-items:center; }
.ai-head__meta { display:flex; gap:var(--sp-3); align-items:center; }
.ai-micro { font-size:var(--fs-micro); color:var(--ai-text-dim); }

.card { background:var(--ai-surface); border:1px solid var(--ai-border);
  border-radius:var(--radius); padding:var(--sp-4); box-shadow:var(--shadow); }
.card__title { font-size:var(--fs-h3); margin:0 0 var(--sp-3); color:var(--ai-text);
  display:flex; align-items:center; gap:var(--sp-2); }
.card--hero { padding:var(--sp-6); }
.grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:var(--sp-4); }
@media (max-width:720px){ .grid-2{ grid-template-columns:1fr; } }

/* Hero gauge */
.gauge { display:flex; flex-direction:column; align-items:center; gap:var(--sp-2); }
.gauge__value { font-size:var(--fs-hero); font-weight:700; line-height:1; }
.gauge__track { width:100%; height:10px; border-radius:999px;
  background:var(--ai-surface-2); overflow:hidden; }
.gauge__fill { height:100%; transition:width .4s ease; }

/* Status helpers — fill + text both use the token, never color-only */
.is-good   { color:var(--ai-good); }   .bg-good   { background:var(--ai-good); }
.is-warn   { color:var(--ai-warn); }   .bg-warn   { background:var(--ai-warn); }
.is-bad    { color:var(--ai-bad); }    .bg-bad    { background:var(--ai-bad); }
.is-neutral{ color:var(--ai-neutral);} .bg-neutral{ background:var(--ai-neutral); }

.pill { display:inline-flex; align-items:center; gap:var(--sp-1);
  padding:2px var(--sp-2); border-radius:999px; font-size:var(--fs-micro);
  font-weight:600; background:var(--ai-surface-2); }

/* List rows */
.row { display:flex; flex-direction:column; gap:var(--sp-1);
  padding:var(--sp-3) 0; border-top:1px solid var(--ai-border); }
.row:first-child { border-top:0; }
.row__head { display:flex; justify-content:space-between; align-items:center; }
.row__name { font-weight:600; }
.row__meta { display:flex; gap:var(--sp-3); color:var(--ai-text-dim);
  font-size:var(--fs-micro); }
.rec { font-size:var(--fs-micro); color:var(--ai-text-dim); }

/* Factor bars (SHAP) */
.factor { display:grid; grid-template-columns:9rem 1fr 3rem; gap:var(--sp-2);
  align-items:center; }
.factor__bar { height:6px; border-radius:999px; background:var(--ai-surface-2); position:relative; }
.factor__bar i { position:absolute; top:0; bottom:0; left:50%; border-radius:999px; }

/* States: skeleton / empty / error */
.skeleton { height:1rem; border-radius:6px;
  background:linear-gradient(90deg,var(--ai-surface-2) 25%,#222b38 37%,var(--ai-surface-2) 63%);
  background-size:400% 100%; animation:shimmer 1.4s infinite; }
@keyframes shimmer { 0%{background-position:100% 0;} 100%{background-position:0 0;} }
@media (prefers-reduced-motion:reduce){ .skeleton{animation:none;} .gauge__fill{transition:none;} }
.state { display:flex; flex-direction:column; align-items:center; gap:var(--sp-2);
  padding:var(--sp-6) var(--sp-4); text-align:center; color:var(--ai-text-dim); }
.state__icon { font-size:1.5rem; opacity:.7; }
.btn-ghost { background:transparent; border:1px solid var(--ai-border);
  color:var(--ai-text); border-radius:8px; padding:var(--sp-1) var(--sp-3);
  cursor:pointer; min-height:32px; min-width:32px; }
.btn-ghost:hover { border-color:var(--ai-accent); }
.btn-ghost:focus-visible { outline:2px solid var(--ai-accent); outline-offset:2px; }
```

### 4.3 JavaScript Integration

**File:** `src/dashboard/ai_insights.js` (ES module)

v3.0: shared fetch helper with explicit **loading → ready / empty / error**
states (error state offers retry); status mapped to tokens + text (never
color-only); field names reconciled with the API (`strategy_name`,
`current_win_rate`); raw feature names humanized.

```javascript
const AIInsights = {
  symbol: null, verdict: null, confidence: 0,

  // ── shared helpers ──────────────────────────────────────────────
  _esc(s){ return String(s ?? '').replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); },

  _setState(el, state){ el.dataset.state = state; },

  _skeleton(el, rows=3){
    this._setState(el,'loading');
    el.innerHTML = Array.from({length:rows},
      ()=>`<div class="skeleton" style="margin:.5rem 0;width:${60+Math.random()*40}%"></div>`).join('');
  },

  _empty(el, msg){
    this._setState(el,'empty');
    el.innerHTML = `<div class="state"><span class="state__icon">○</span><p>${this._esc(msg)}</p></div>`;
  },

  _error(el, retryFn){
    this._setState(el,'error');
    el.innerHTML = `<div class="state"><span class="state__icon is-bad">⚠</span>
      <p>Couldn't load this panel.</p>
      <button class="btn-ghost" type="button">Retry</button></div>`;
    el.querySelector('button').addEventListener('click', retryFn);
  },

  // status token + accessible label from win rate / score
  _status(v, good, warn){
    if (v >= good) return {cls:'good', label:'Strong'};
    if (v >= warn) return {cls:'warn', label:'Moderate'};
    return {cls:'bad', label:'Weak'};
  },

  _humanize(name){
    const map = {
      confidence:'Confidence', pcr:'Put/Call ratio',
      net_oi_change:'OI bias (PE−CE)', ce_oi_change:'Call OI Δ',
      pe_oi_change:'Put OI Δ', rsi_1h:'RSI 1h', rsi_3h:'RSI 3h',
      hour_of_day:'Time of day', days_to_expiry:'Days to expiry',
      distance_to_max_pain_pct:'Dist. to max pain', chart_conflict:'Chart conflict',
    };
    return map[name] || name.replace(/_/g,' ').replace(/\bpct\b/,'%');
  },

  async _get(url){
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },

  // ── ML prediction (HERO) ────────────────────────────────────────
  async loadML(){
    const el = document.getElementById('ml-prediction-content');
    if (!this.symbol || !this.verdict){ return this._empty(el,'Select a live signal to score'); }
    this._skeleton(el, 4);
    try {
      const d = await this._get(`/api/ai/ml-prediction/${this.symbol}`
        + `?verdict=${encodeURIComponent(this.verdict)}&confidence=${this.confidence}`);
      if (!d.available) return this._empty(el, d.message || 'Model not trained yet (needs 30+ trades)');
      this._renderML(el, d);
    } catch(e){ console.error('ML', e); this._error(el, ()=>this.loadML()); }
  },

  _renderML(el, d){
    this._setState(el,'ready');
    const pct = (d.success_probability*100);
    const s = this._status(d.success_probability, .6, .5);
    const partial = d.context_complete === false
      ? `<span class="pill is-warn" title="Scan features incomplete — using defaults">partial features</span>` : '';
    const factors = (d.top_factors||[]).map(([name,imp])=>{
      const w = Math.min(50, Math.abs(imp)*50); const pos = imp>=0;
      return `<div class="factor">
        <span title="${this._esc(name)}">${this._esc(this._humanize(name))}</span>
        <span class="factor__bar"><i class="${pos?'bg-good':'bg-bad'}"
          style="${pos?'left:50%':'right:50%;left:auto'};width:${w}%"></i></span>
        <span class="ai-micro ${pos?'is-good':'is-bad'}">${pos?'+':''}${imp.toFixed(2)}</span>
      </div>`;
    }).join('');
    el.innerHTML = `
      <div class="gauge">
        <div class="gauge__value is-${s.cls}">${pct.toFixed(0)}%</div>
        <div class="pill is-${s.cls}" role="status"
             aria-label="Success probability ${pct.toFixed(0)} percent, ${s.label}">
          ${s.label} · ${this._esc(d.confidence_level)} confidence
        </div>
        <div class="gauge__track"><div class="gauge__fill bg-${s.cls}" style="width:${pct}%"></div></div>
        ${partial}
      </div>
      <div style="margin-top:var(--sp-4)">
        <div class="ai-micro" style="margin-bottom:var(--sp-2)">Top drivers (SHAP)</div>
        ${factors || '<p class="ai-micro">No factor data</p>'}
      </div>
      <p class="ai-micro" style="margin-top:var(--sp-3)">
        Model ${this._esc(d.model_version)} · ${d.training_samples} samples</p>`;
  },

  // ── Trade DNA ───────────────────────────────────────────────────
  async loadDNA(){
    const el = document.getElementById('dna-match-content');
    if (!this.symbol || !this.verdict){ return this._empty(el,'No active signal'); }
    this._skeleton(el, 3);
    try {
      const d = await this._get(`/api/ai/trade-dna/${this.symbol}`
        + `?verdict=${encodeURIComponent(this.verdict)}&confidence=${this.confidence}`);
      if (!d.match_found) return this._empty(el, d.message || 'No similar historical trades');
      this._renderDNA(el, d);
    } catch(e){ console.error('DNA', e); this._error(el, ()=>this.loadDNA()); }
  },

  _renderDNA(el, d){
    this._setState(el,'ready');
    const wr = d.historical_win_rate; const s = this._status(wr, .6, .5);
    el.innerHTML = `
      <div class="gauge" style="align-items:flex-start">
        <div><span class="gauge__value is-${s.cls}" style="font-size:2rem">${(wr*100).toFixed(0)}%</span>
          <span class="ai-micro">win rate</span></div>
      </div>
      <div class="row__meta" style="margin-top:var(--sp-3)">
        <span><strong>${d.similar_trades}</strong> similar</span>
        <span>Avg ₹${Math.round(d.avg_pnl)}</span>
        <span class="is-good">Win ₹${Math.round(d.avg_win)}</span>
        <span class="is-bad">Loss ₹${Math.round(d.avg_loss)}</span>
      </div>
      <p class="rec" style="margin-top:var(--sp-2)">${this._esc(d.confidence_note)}</p>`;
  },

  // ── Patterns ────────────────────────────────────────────────────
  async loadPatterns(){
    const el = document.getElementById('patterns-list');
    this._skeleton(el, 4);
    try {
      const list = await this._get('/api/ai/patterns');
      if (!list || !list.length) return this._empty(el,'No patterns yet (need 10+ trades per pattern)');
      this._setState(el,'ready');
      el.innerHTML = list.slice(0,6).map(p=>{
        const s = this._status(p.win_rate, .6, .5);
        return `<div class="row">
          <div class="row__head">
            <span class="row__name">${this._esc(p.name)}</span>
            <span class="pill is-${s.cls}" aria-label="${(p.win_rate*100).toFixed(0)} percent win rate">
              ${(p.win_rate*100).toFixed(0)}%</span>
          </div>
          <div class="row__meta"><span>${p.sample_size} trades</span><span>Avg ₹${Math.round(p.avg_pnl)}</span></div>
          <div class="rec">${this._esc(p.recommendation)}</div>
        </div>`;
      }).join('');
    } catch(e){ console.error('patterns', e); this._error(el, ()=>this.loadPatterns()); }
  },

  // ── Edge health ─────────────────────────────────────────────────
  async loadEdge(){
    const el = document.getElementById('edge-health-content');
    this._skeleton(el, 3);
    try {
      const list = await this._get('/api/ai/edge-health');
      if (!list || !list.length) return this._empty(el,'No strategy data yet');
      this._setState(el,'ready');
      const trendPill = t => {
        const m = {IMPROVING:['good','Improving'], DECLINING:['bad','Declining'],
                   STABLE:['neutral','Stable'], INSUFFICIENT_HISTORY:['neutral','New']};
        const [cls,label] = m[t] || ['neutral', t];
        return `<span class="pill is-${cls}">${label}</span>`;
      };
      el.innerHTML = list.slice(0,6).map(h=>{
        const sc = h.health_score; const cls = sc>=70?'good':sc>=50?'warn':'bad';
        return `<div class="row">
          <div class="row__head">
            <span class="row__name">${this._esc(h.strategy_name)}</span>
            <span class="pill is-${cls}" aria-label="Health score ${sc} of 100">${Math.round(sc)}/100</span>
          </div>
          <div class="row__meta">
            <span>Win ${(h.current_win_rate*100).toFixed(0)}%</span>
            ${trendPill(h.win_rate_trend)}
          </div>
          <div class="rec">${this._esc(h.recommendation)}</div>
        </div>`;
      }).join('');
    } catch(e){ console.error('edge', e); this._error(el, ()=>this.loadEdge()); }
  },

  setSignal({symbol, verdict, confidence}){
    this.symbol = symbol; this.verdict = verdict; this.confidence = confidence ?? 0;
    document.getElementById('ai-symbol-pill').textContent = symbol || '—';
    this.loadML(); this.loadDNA();
  },

  refreshAll(){
    document.getElementById('ai-updated').textContent =
      'Updated ' + new Date().toLocaleTimeString();
    this.loadML(); this.loadDNA(); this.loadPatterns(); this.loadEdge();
  },
};

// Wire up: lazy-load on tab open, manual refresh, 30s poll while visible
document.addEventListener('DOMContentLoaded', () => {
  const tab = document.querySelector('[data-tab="ai-insights"]');
  const panel = document.querySelector('.ai-insights');
  let timer = null;
  const startPoll = () => { AIInsights.refreshAll();
    timer = setInterval(()=>AIInsights.refreshAll(), 30000); };
  const stopPoll  = () => { if (timer) clearInterval(timer); timer = null; };

  tab?.addEventListener('click', startPoll);
  document.getElementById('ai-refresh')?.addEventListener('click', ()=>AIInsights.refreshAll());
  // Pause polling when tab/panel not visible (saves the 2,880 calls/day problem)
  document.addEventListener('visibilitychange',
    () => document.hidden ? stopPoll() : (panel?.offsetParent && startPoll()));
});
```

```css
/* Only the active-state container shows its children; keeps DOM simple */
[data-state="loading"] .state, [data-state="ready"] .state,
[data-state="empty"] .skeleton, [data-state="error"] .skeleton { display:none; }
```

### 4.3 API Endpoint for ML Prediction

**File:** `dashboard_server.py` (add route)

```python
@app.route("/api/ai/ml-prediction/<symbol>")
def get_ml_prediction(symbol):
    """
    Get ML prediction for a potential trade.
    
    v2.0 FIX: Requires verdict and confidence query params.
    v2.2 FIX: Uses singleton instead of re-instantiating per request.
    v3.0 FIX: Hydrates the FULL feature context from the latest scan snapshot.
    Previously this passed only symbol/verdict/confidence, so the model scored
    a near-empty vector (every OI/distance/RSI/regime feature = 0) and returned
    a DIFFERENT probability than the pipeline, which passes full context. Same
    trade, two answers. Now both call sites feed identical features.
    """
    from src.intelligence.ml_predictor import get_predictor
    from src.engine.scan_cache import get_latest_scan_snapshot  # latest per-symbol context
    
    verdict = request.args.get("verdict")
    confidence = request.args.get("confidence", "0")
    
    if not verdict:
        return jsonify({"available": False, "error": "Missing verdict parameter"}), 400
    
    predictor = get_predictor()  # v2.2: Use singleton
    if predictor.model is None:
        msg = ("Model discarded — retrain required (feature-set drift)"
               if getattr(predictor, "_needs_retrain", False)
               else "Model not trained yet")
        return jsonify({"available": False, "message": msg})
    
    # v3.0: Pull the most recent scan context for this symbol and merge the
    # user-supplied verdict/confidence on top. Missing snapshot → explicit
    # low-confidence response rather than a silent zero-vector prediction.
    snap = get_latest_scan_snapshot(symbol) or {}
    ctx = {
        **snap,
        "symbol": symbol,
        "verdict_label": verdict,
        "confidence": int(confidence),
    }
    prediction = predictor.predict(ctx)
    
    if prediction is None:
        return jsonify({"available": False, "message": "Prediction failed"})
    
    return jsonify({
        "available": True,
        "context_complete": bool(snap),  # v3.0: tells UI if features were full
        "success_probability": prediction.success_probability,
        "confidence_level": prediction.confidence_level,
        "top_factors": prediction.top_factors,
        "model_version": prediction.model_version,
        "training_samples": prediction.training_samples,
    })
```

### 4.4 Phase 4 Deliverables

- ✅ Redesigned AI Insights tab with clear hierarchy (ML gauge as hero)
- ✅ Design tokens in `:root` — semantic color used consistently (no undefined classes)
- ✅ Four explicit panel states: loading skeleton, ready, empty, error-with-retry
- ✅ WCAG AA contrast; status conveyed by text + `aria-label`, never color/emoji alone
- ✅ `aria-live` regions so screen readers announce updates
- ✅ Responsive grid (two-up → single column < 720px), reduced-motion support
- ✅ Field names reconciled with API (`strategy_name`, `current_win_rate`)
- ✅ Humanized SHAP factor labels with signed impact bars
- ✅ Polling pauses when tab hidden (kills the 2,880 calls/day waste)
- ✅ ML endpoint hydrates full feature context (matches pipeline predictions)
- ✅ XSS-safe rendering (`_esc()` on all interpolated values)

**Estimated Effort:** 18-24 hours  
**Impact:** A dashboard a trader actually reads at a glance — not a placeholder


---

## 📊 Implementation Summary

| Phase | Deliverables | Effort | Impact | Dependencies |
|-------|-------------|--------|--------|--------------|
| **Phase 0** | Feature persistence migration (BLOCKER) | 3-5h | ⭐⭐⭐⭐⭐ | None |
| **Phase 1** | History Analyzer (IST-fixed) | 8-12h | ⭐⭐⭐⭐ | Phase 0 (DNA hour fix) |
| **Phase 2** | ML Predictor (leakage + stratify fixed) | 15-20h | ⭐⭐⭐⭐⭐ | Phase 0, xgboost, sklearn, shap |
| **Phase 3** | Edge Monitor (score + sentinel fixed) | 8-10h | ⭐⭐⭐ | None |
| **Phase 4** | Dashboard UI (redesigned, AA, stateful) | 18-24h | ⭐⭐⭐⭐⭐ | All previous phases |

**Total Estimated Effort:** 52-71 hours (~9-12 weeks part-time)

> ⚠️ **v2.0 Change:** Reduced from 61-79 hours by removing Behavioral Coach phase.
> The bot's existing gates in `trade_decision.py` and `risk_engine.py` already handle
> overtrading/FOMO prevention for an automated system.

---

## 🚀 Quick Start Guide

### Immediate Actions (This Week)

0. **Run Phase 0 first (BLOCKER):**
   ```bash
   sqlite3 data/bot.db < migrations/004_feature_columns.sql
   ```
   Then wire `open_trade()` to snapshot scan context. Until feature coverage
   ≥ 90%, ML training is a no-op by design.

1. **Install ML dependencies:**
   ```bash
   pip install xgboost scikit-learn shap
   ```

2. **Create intelligence directory:**
   ```bash
   mkdir -p src/intelligence data/models
   ```

3. **Start with Phase 1:**
   - Implement `TradeHistoryAnalyzer` with IST conversion
   - Add API endpoints
   - Test with existing trade data

4. **Train initial ML model (after 30 trades):**
   ```python
   from src.intelligence.ml_predictor import get_predictor
   predictor = get_predictor()  # v2.2: Use singleton
   predictor.train()
   ```

### Success Metrics

- **Phase 1:** Discover 5+ actionable patterns with correct IST session bucketing
- **Phase 2:** ML model AUC > 0.60 on holdout set; no feature leakage
- **Phase 3:** Detect edge decay 2+ weeks before major losses
- **Phase 4:** 80% dashboard adoption; zero JS errors in production

---

## 🎯 Key Differentiators from Original Plan

1. **Builds on existing code** - No rewrites, only extensions
2. **Leverages LLM integration** - Uses `llm_enrichment.py` for narrative advice
3. **Incremental delivery** - Each phase provides immediate value
4. **Practical ML** - XGBoost (proven, fast) vs theoretical approaches
5. **Edge monitoring** - Detects strategy decay before catastrophic losses
6. **Existing database** - Uses current schema, minimal migrations
7. **IST-aware** - All time-based analysis correctly converts UTC → IST
8. **No redundant behavioral layer** - Trusts existing `trade_decision.py` gates
9. **Production-hardened** - Error handling, model versioning, rollback protection

---

## 📝 v2.0 Changelog (from Technical Review)

### Bugs Fixed
| # | Issue | Severity | Fix |
|---|-------|----------|-----|
| 1 | UTC timezone bug in session bucketing | 🔴 Critical | IST conversion via `datetime(opened_at, '+5 hours', '+30 minutes')` |
| 2 | Feature leakage (`datetime.now()` in training) | 🔴 Critical | Use `opened_at` from trade record |
| 3 | Feature ordering (`sorted()` fragile) | 🔴 Critical | Explicit `FEATURE_ORDER` constant |
| 4 | Class imbalance (loss-heavy early data) | 🟡 High | `scale_pos_weight = n_neg/n_pos` |
| 5 | Flawed health score formula | 🟡 High | Include absolute performance + trend |
| 6 | N+1 queries in strategy health | 🟡 High | Single GROUP BY query |
| 7 | Biased edge decay windows | 🟡 High | Guard: INSUFFICIENT_HISTORY if hist < 5 |
| 8 | Confidence bands too tight (±10) | 🟠 Medium | Widened to ±20 |
| 9 | HAVING COUNT >= 3 too loose | 🟠 Medium | Raised to >= 10 |
| 10 | Missing API params in JS | 🟠 Medium | Pass verdict + confidence |
| 11 | No error handling in fetch() | 🟠 Medium | `.catch()` on all API calls |
| 12 | Weekly-only retraining too slow | 🟠 Medium | Event-driven triggers |
| 13 | No model rollback protection | 🟠 Medium | AUC comparison before deploy |
| 14 | Paper trades only for training | 🟢 Low | UNION with live_trades |
| 15 | WebSocket scope creep | 🟢 Low | Removed — use polling |

### Design Changes
| Change | Rationale |
|--------|-----------|
| Removed Behavioral Coach (Phase 3) | Bot is automated; existing gates handle this |
| Event-driven retraining | Weekly is too slow when edge decays mid-week |
| Model versioning with AUC gate | Prevents deploying worse models |
| Absolute + relative health score | New strategies shouldn't score higher than declining ones |

---

## 📝 Next Steps

1. **Review this roadmap** and prioritize phases
2. **Start Phase 1** (History Analyzer) - no dependencies, immediate value
3. **Accumulate 30+ closed trades** for ML training
4. **Install ML dependencies** when ready for Phase 2
5. **Iterate based on results** - adjust thresholds, add features

---

**Document Version:** 3.0  
**Last Updated:** June 21, 2026  
**Review Status:** ✅ Revised after fourth-pass review (18 v3.0 issues + UI overhaul)  
**Status:** Ready for Implementation — start with **Phase 0 (blocker)**
