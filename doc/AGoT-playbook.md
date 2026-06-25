# AGoT Playbook — AI Intelligence Roadmap v3.0

> **Last Updated:** 2026-06-25
> **Status:** Phase 1–4 Complete ✅ | Phase 5+ Pending

This playbook documents the implementation, architecture, and acceptance criteria for each phase of the AI Intelligence Roadmap v3.0. It serves as the single source of truth for what was built, how it integrates, and how to verify correctness.

---

## Table of Contents

1. [Phase 1: Pattern Intelligence & Cache Invalidation](#phase-1-pattern-intelligence--cache-invalidation)
2. [Phase 2: ML Success Predictor Integration](#phase-2-ml-success-predictor-integration)
3. [Phase 3: Edge Decay Monitor](#phase-3-edge-decay-monitor)
4. [Phase 4: AI Dashboard UI](#phase-4-ai-dashboard-ui)
5. [Cross-Phase Integration Map](#cross-phase-integration-map)
6. [Testing Strategy](#testing-strategy)
7. [Known Limitations & Future Work](#known-limitations--future-work)

---

## Phase 1: Pattern Intelligence & Cache Invalidation

### Objective
Establish baseline pattern discovery from closed trades and ensure cache consistency so stale patterns are never served after new trade closures.

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `_invalidate_pattern_cache()` | `src/engine/paper_trading.py` | Clears cached pattern data on every trade close |
| Pattern Discovery Engine | `src/intelligence/history_analyzer.py` | Identifies recurring symbol × verdict combinations |
| Trade Close Hook | `src/engine/paper_trading.py::close_paper_trade()` | Wires invalidation + ML retrain counter + edge health check |

### Architecture

```
close_paper_trade()
  ├── _invalidate_pattern_cache()     ← Phase 1
  ├── _trigger_ml_retraining()        ← Phase 2
  └── _check_edge_health_and_trigger_retrain()  ← Phase 3
```

### Acceptance Criteria

- [x] Pattern cache invalidates synchronously within `close_paper_trade()`
- [x] Discovered patterns include win rate, avg PnL, and trade count
- [x] No stale patterns served after new trade closure
- [x] Graceful degradation when < 5 trades exist for a pattern
- [x] Function exists and is callable without error even with empty cache

### Test Coverage

- `TestPhase1PatternCacheInvalidation` in `tests/test_ai_intelligence_phases.py`
  - `test_invalidate_pattern_cache_function_exists`
  - `test_invalidate_pattern_cache_callable_without_error`
  - `test_close_paper_trade_calls_invalidation` (source inspection)

---

## Phase 2: ML Success Predictor Integration

### Objective
Integrate XGBoost-based success probability predictions into the pipeline and dashboard, with event-driven and scheduled retraining.

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `ScanCache` | `src/engine/scan_cache.py` | Thread-safe in-memory cache with 10-min TTL for ML feature hydration |
| Pipeline ML Step | `src/engine/pipeline.py` | Attaches `ml_prediction` to intelligence dict after anomaly detection |
| Event-Driven Retraining | `src/engine/paper_trading.py::_trigger_ml_retraining()` | Increments counter; triggers `run_training()` at ≥20 trades |
| Weekly Fallback Job | `src/scheduler/job_runner.py` | Sunday 2 AM IST safety net retraining |
| Dashboard Endpoint | `dashboard_server.py::/api/ai/ml-prediction/{symbol}` | Full context hydration from scan cache |
| `TradeSuccessPredictor` | `src/intelligence/ml_predictor.py` | XGBoost model with SHAP explainability, versioning, AUC rollback |

### Architecture

```
Pipeline Flow:
  scan → anomalies → scan_cache.update() → intelligence → [ML PREDICTION] → paper_trading
                                                              ↓
                                                    ml_predictor.predict()
                                                              ↓
                                                    intel["ml_prediction"] attached

Trade Close Flow:
  close_paper_trade()
    → _trigger_ml_retraining()
         ↓
      if counter >= 20 → run_training()

Dashboard API:
  GET /api/ai/ml-prediction/NIFTY?verdict=Long+Buildup&confidence=80
      → scan_cache.get_latest_scan_snapshot("NIFTY")
      → predictor.predict(full_context)
      → {success_probability, confidence_level, top_factors, ...}
```

### Scan Cache Design Decisions

- **Thread Safety**: Uses `threading.Lock` for concurrent read/write safety
- **TTL**: 10-minute expiry prevents stale feature contexts
- **Case Insensitivity**: Symbol keys normalized to uppercase
- **Copy Semantics**: `get_latest_scan_snapshot()` returns a deep copy to prevent mutation
- **Graceful Degradation**: Returns `None` for missing/expired entries; pipeline continues

### Acceptance Criteria

- [x] Scan cache updates atomically after each scan cycle
- [x] ML prediction returns `None` gracefully when model unavailable
- [x] Event-driven retraining fires at exactly 20-trade threshold
- [x] Dashboard endpoint returns identical predictions to pipeline (same feature context)
- [x] Model versioning prevents AUC regression on retrain
- [x] Cache TTL expiry works correctly
- [x] Case-insensitive symbol lookup
- [x] Thread safety under concurrent access

### Test Coverage

- `TestPhase2ScanCache` in `tests/test_ai_intelligence_phases.py`
  - `test_update_and_retrieve`
  - `test_case_insensitive_symbol`
  - `test_returns_copy_not_reference`
  - `test_missing_symbol_returns_none`
  - `test_empty_symbol_returns_none`
  - `test_ttl_expiry`
  - `test_clear_cache`
  - `test_get_all_cached_symbols`
  - `test_overwrite_existing`
- `TestPhase2MLPredictorIntegration` in `tests/test_ai_intelligence_phases.py`
  - `test_pipeline_has_ml_prediction_step`
  - `test_pipeline_updates_scan_cache`
  - `test_paper_trading_has_retrain_trigger`
  - `test_scheduler_has_weekly_training_job`

---

## Phase 3: Edge Decay Monitor

### Objective
Detect strategy edge degradation through dual-window performance comparison and trigger corrective ML retraining when health drops below threshold.

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `EdgeDecayMonitor` | `src/intelligence/edge_monitor.py` | Singleton with health scoring, trend classification, strategy breakdowns |
| `EdgeHealth` | `src/intelligence/edge_monitor.py` | Dataclass for structured health results |
| Pipeline Integration | `src/engine/pipeline.py::_check_edge_health_and_trigger_retrain()` | Wired after AI CLOSE_EARLY and paper trade CLOSED events |
| Dashboard Endpoints | `dashboard_server.py` | `/api/ai/edge-health` (all) and `/api/ai/edge-health/{symbol}` (filtered) |

### Health Scoring Formula (100 points total)

| Component | Points | Criteria |
|-----------|--------|----------|
| Absolute Win Rate | 0–40 | ≥70%=40, ≥60%=30, ≥50%=20, ≥40%=10, <40%=0 |
| Win Rate Trend | 0–30 | >+10%=30, >+5%=20, ±5%=15, >-10%=5, >-15%=0 |
| Absolute PnL | 0–15 | >₹1000=15, >₹0=10, >-₹500=5, ≤-₹500=0 |
| PnL Trend | 0–15 | >+20%=15, >0%=10, >-20%=5, ≤-20%=0 |

### Trend Classification Thresholds

| Trend | Condition |
|-------|-----------|
| IMPROVING | Change > +10% of baseline |
| STABLE | Change within ±10% of baseline |
| DECLINING | Change < -15% of baseline |
| INSUFFICIENT_HISTORY | < 5 trades in either window (sentinel) |

### Safety Guards

| Scenario | Behavior |
|----------|----------|
| 0–4 closed trades total | Returns `INSUFFICIENT_HISTORY` with score=50, no retrain |
| 5+ recent, <5 historical | Returns `INSUFFICIENT_HISTORY` with absolute-only score |
| Both windows ≥5 trades | Full trend comparison, real scoring |
| Historical avg PnL ≈ 0 | Floor at ₹100 prevents ratio explosion |
| Early trade closes (<30 trades) | Sentinel prevents spurious `run_training()` calls |
| Retrain trigger | Only fires when `win_rate_trend != "INSUFFICIENT_HISTORY"` AND `health_score < 60` |

### Acceptance Criteria

- [x] Returns `INSUFFICIENT_HISTORY` when < 5 trades in either window
- [x] PnL baseline floor of ₹100 prevents division by zero / ratio explosion
- [x] Single GROUP BY query — no N+1 pattern in `get_all_strategies_health()`
- [x] Retrain only fires when trend != `INSUFFICIENT_HISTORY`
- [x] Strategy health sorted worst-first in API response
- [x] Singleton pattern ensures consistent state
- [x] Handles empty DB gracefully
- [x] Recommendations map correctly to trend/wr/pnl combinations

### Test Coverage

- `TestPhase3EdgeDecayMonitorUnit` in `tests/test_ai_intelligence_phases.py`
  - `test_singleton_pattern`
  - `test_edge_health_dataclass`
  - `test_classify_trend_improving` / `_stable` / `_declining` / `_zero_baseline`
  - `test_health_score_absolute_high_wr` / `_low_wr` / `_neutral` / `_bounds`
  - `test_health_score_trend_based` / `_trend_declining`
  - `test_strategy_name_builder`
  - `test_recommendation_insufficient_history` / `_declining` / `_strong_edge` / `_breakeven`
- `TestPhase3EdgeDecayMonitorDB` in `tests/test_ai_intelligence_phases.py`
  - `test_insufficient_recent_trades`
  - `test_sufficient_recent_insufficient_historical`
  - `test_full_comparison`
  - `test_get_all_strategies_health_structure`
  - `test_pnl_baseline_floor`
- `TestPhase3PipelineIntegration` in `tests/test_ai_intelligence_phases.py`
  - `test_pipeline_has_edge_health_check`

---

## Phase 4: AI Dashboard UI

### Objective
Visualize AI insights with accessible, responsive, and XSS-safe UI components integrated into the existing dashboard.

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `ai_insights.css` | `src/dashboard/ai_insights.css` | Design tokens, semantic color system, responsive grid, WCAG AA |
| `ai_insights.js` | `src/dashboard/ai_insights.js` | ES module with 4 panels × 4 states, polling, XSS-safe rendering |
| Tab Integration | `src/dashboard/index.html` | "AI Insights" tab with show/hide logic |
| Static Routes | `dashboard_server.py` | `/static/ai_insights.css` and `/static/ai_insights.js` |

### Panel Layout

```
┌─────────────────────────────────────────────────────────────┐
│  HERO: ML Success Probability Gauge (full width)            │
│  - Circular gauge with percentage                           │
│  - Confidence pill + SHAP top 3 factors                     │
│  - Model version + training sample count                    │
├──────────────────────────┬──────────────────────────────────┤
│  Trade DNA               │  Edge Health                     │
│  - Historical WR         │  - Overall health score          │
│  - Similar trades count  │  - Per-strategy breakdown        │
│  - Avg P&L / Win / Loss  │  - Trend indicators              │
├──────────────────────────┴──────────────────────────────────┤
│  Top Patterns (full width)                                  │
│  - Pattern name + win rate bar                              │
│  - Trade count + avg P&L                                    │
│  - Recommendation badge                                     │
└─────────────────────────────────────────────────────────────┘
```

### State Machine (per panel)

| State | Trigger | Visual |
|-------|---------|--------|
| Loading | Initial fetch / refresh | Skeleton placeholder |
| Ready | Successful fetch with data | Full content rendered |
| Empty | Successful fetch, no data | Actionable guidance message |
| Error | Failed fetch | Error message + retry button |

### Accessibility Features

| Feature | Implementation |
|---------|----------------|
| Screen reader announcements | `aria-live="polite"` on all panel bodies |
| Status not color-only | Every status has both color class AND text label |
| Keyboard navigation | All buttons have `:focus-visible` outline |
| Semantic HTML | `<article>`, `<section>`, `<header>`, proper heading hierarchy |
| Reduced motion | Animations disabled via `@media (prefers-reduced-motion)` |
| Contrast | WCAG AA compliant color ratios |

### Security

- **XSS Prevention**: All interpolated values escaped via `_esc()` helper
- **No `innerHTML` with user data**: Only `textContent` or pre-sanitized templates
- **CSP Compatible**: No inline scripts or eval

### Acceptance Criteria

- [x] Tab switching between Dashboard and AI Insights works without page reload
- [x] Symbol selector change refreshes ML and DNA panels with new context
- [x] Empty states show actionable guidance (not blank panels)
- [x] Error states include retry button
- [x] Screen readers announce panel updates via `aria-live`
- [x] No raw HTML injection possible in any rendered field
- [x] Grid collapses to single column < 720px
- [x] Polling pauses when tab/panel not visible
- [x] Static CSS/JS files servable via dashboard server
- [x] Light/dark theme support via CSS custom properties

### Test Coverage

- `TestPhase4DashboardAPI` in `tests/test_ai_intelligence_phases.py`
  - `test_ml_prediction_endpoint_exists`
  - `test_edge_health_endpoint_exists`
  - `test_edge_health_filtered_endpoint_exists`
  - `test_static_css_route`
  - `test_static_js_route`
- `TestPhase4UIFilesExist` in `tests/test_ai_intelligence_phases.py`
  - `test_ai_insights_css_exists`
  - `test_ai_insights_js_exists`
  - `test_index_html_has_ai_tab`

---

## Cross-Phase Integration Map

### Data Flow Diagram

```
                    ┌─────────────────┐
                    │   NSE Scanner   │
                    └────────┬────────┘
                             │ scan results
                             ▼
                    ┌─────────────────┐
                    │    Pipeline     │
                    │                 │
                    │ 1. Anomaly Det. │
                    │ 2. Scan Cache ↑ │──── Phase 2
                    │ 3. Intelligence │
                    │ 4. ML Predict   │──── Phase 2
                    │ 5. Paper Trade  │
                    └────────┬────────┘
                             │ trade opened/closed
                             ▼
                    ┌─────────────────┐
                    │ Paper Trading   │
                    │                 │
                    │ On Close:       │
                    │ • Invalidate ↗  │──── Phase 1
                    │ • ML Counter ↗  │──── Phase 2
                    │ • Edge Check ↗  │──── Phase 3
                    └────────┬────────┘
                             │ health alert / retrain
                             ▼
                    ┌─────────────────┐
                    │ Edge Monitor    │──── Phase 3
                    │                 │
                    │ • Health Score  │
                    │ • Trend Class.  │
                    │ • Retrain Trig. │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  ML Predictor   │──── Phase 2
                    │                 │
                    │ • Train/Eval    │
                    │ • Version Ctrl  │
                    │ • SHAP Explain  │
                    └────────┬────────┘
                             │ predictions
                             ▼
                    ┌─────────────────┐
                    │ Dashboard API   │──── Phase 4
                    │                 │
                    │ /ml-prediction  │
                    │ /edge-health    │
                    │ /patterns       │
                    │ /trade-dna      │
                    └────────┬────────┘
                             │ JSON
                             ▼
                    ┌─────────────────┐
                    │ AI Insights UI  │──── Phase 4
                    │                 │
                    │ ML Gauge        │
                    │ Trade DNA       │
                    │ Edge Health     │
                    │ Patterns        │
                    └─────────────────┘
```

### Dependency Matrix

| Phase | Depends On | Depended On By |
|-------|-----------|----------------|
| 1 | — | 2, 3, 4 |
| 2 | 1 (cache invalidation) | 3 (retrain trigger), 4 (API) |
| 3 | 1, 2 (trade data, ML retrain) | 4 (API) |
| 4 | 2, 3 (API endpoints) | — |

---

## Testing Strategy

### Test File

All Phase 1–4 tests reside in `tests/test_ai_intelligence_phases.py`.

### Test Categories

| Category | Count | Description |
|----------|-------|-------------|
| Phase 1 Unit | 3 | Cache invalidation function existence and wiring |
| Phase 2 Unit | 9 | Scan cache CRUD, TTL, thread safety, copy semantics |
| Phase 2 Integration | 4 | Source inspection for pipeline/scheduler wiring |
| Phase 3 Unit | 16 | Trend classification, health scoring, recommendations |
| Phase 3 DB Integration | 5 | Real DB queries with isolated test data |
| Phase 3 Pipeline | 1 | Source inspection for edge health wiring |
| Phase 4 API | 5 | Endpoint existence and response validation |
| Phase 4 UI Files | 3 | File existence and HTML content checks |
| Cross-Phase Safety | 3 | Clean imports, thread safety, empty DB handling |
| **Total** | **49** | |

### Running Tests

```bash
# All Phase 1-4 tests
pytest tests/test_ai_intelligence_phases.py -v

# Specific phase
pytest tests/test_ai_intelligence_phases.py -v -k "Phase3"

# With coverage
pytest tests/test_ai_intelligence_phases.py -v --cov=src/intelligence --cov=src/engine --cov-report=term-missing
```

### Test Fixtures

Tests use `conftest.py` fixtures including:
- Isolated SQLite test database (via `init_db()`)
- `httpx.Client` patched for dashboard server testing
- Temporary directories for file-based tests

---

## Known Limitations & Future Work

### Current Limitations

1. **ML Model Cold Start**: First 30 trades produce no predictions; system gracefully degrades
2. **Edge Monitor Latency**: Health checks run synchronously on trade close; may add latency with large DBs
3. **Dashboard Polling**: Fixed 30s interval; no WebSocket push yet
4. **SHAP Performance**: SHAP value computation can be slow for large feature sets; cached per model version

### Future Phases (Not Yet Implemented)

- **Phase 5**: Adaptive Position Sizing based on ML confidence + edge health
- **Phase 6**: Regime-Aware Strategy Selection
- **Phase 7**: Multi-Timeframe Confluence Engine
- **Phase 8**: Live Trading Graduation Gate

### Maintenance Notes

- Scan cache TTL (10 min) may need tuning based on scan frequency
- Edge monitor windows (30d recent / 30-90d historical) should be reviewed quarterly
- ML retrain threshold (20 trades) should scale with portfolio size
- Dashboard polling interval should migrate to WebSocket in Phase 5+

---

*This playbook is a living document. Update it whenever phases are modified, extended, or retired.*
