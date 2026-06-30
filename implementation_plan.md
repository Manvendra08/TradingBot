# Decision Audit Pipeline — Full Bot Architecture Plan

## Scope

This plan covers **both** decision-making paths in the bot:

| Engine | Entry Point | Trigger |
|--------|-------------|---------|
| **Core OI Engine** | `make_trade_decision()` in [trade_decision.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/trade_decision.py) | OI anomaly signals from `detect_anomalies()` |
| **Timeframe Strategy** | `run_timeframe_strategy()` in [paper_trading.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/paper_trading.py) | 3H candle breakout above/below prev bar high/low |

Both are orchestrated by `_process_symbol()` in [pipeline.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/pipeline.py) every scan cycle.

---

## Current State: What Each Engine Does (and Why It's Hard to Audit)

### Core OI Engine — `make_trade_decision()` (L93–L418, trade_decision.py)

The function already computes scores (`entry_quality`, `trend_alignment`, `regime_score`, `ai_confidence`), but they are:

| Problem | Code Location | Impact |
|---------|--------------|--------|
| Scores computed but **not persisted** | L184–L202: `scores = {...}` dict returned in response but never written to DB | Cannot review why a trade fired 3 days ago |
| **Mode-conditional logic** (conservative/balanced/aggressive/hybrid) evaluates different sub-checks — no uniform step list | L210–L359 | A SKIP in hybrid mode vs conservative mode looks identical in logs |
| AI boost (`AI_PROMOTED`) is a **Priority 5 override** that fires _after_ all rules failed — not a first-class pipeline step | L314–L326 | Impossible to know if the AI boost was the deciding factor vs. trend persistence |
| `_blocked()` helper (L409–L417) returns `status=BLOCKED` with a **concatenated string reason** — no structured step trail | L312, L328, L358 | Parsing `"; ".join(block_parts)` for analytics is fragile |
| `soft_conflicts` list appended ad-hoc (L148, L177, L219, L264, L321) | Various | No defined vocabulary of soft conflict types |

### Timeframe Strategy — `run_timeframe_strategy()` (L690–L1345, paper_trading.py)

| Problem | Code Location | Impact |
|---------|--------------|--------|
| Signal detection and OI filter **fused into one boolean** | L1071: `is_long_trigger = c_3h_close > p_3h_high + buffer AND long_oi_support` | Cannot tell "price broke out but OI didn't confirm" vs "no breakout at all" |
| AI alignment check **early-returns** with generic `BLOCKED_PLAN` | L1097–L1119 | All AI rejections look identical |
| Risk check (L1090) is `(bool, str)` — **no sub-check detail** | `check_risk_limits()` in risk_engine.py | Daily cap? Cooldown? Max open? All merge into one string |
| No **entry quality or regime scoring** — only 3H price action + OI binary | Entire function | Weaker entries and stronger entries are indistinguishable |
| **Exit reasons are free-text** strings | L830, L912, L953, L984, L1002, L1013 | Post-trade attribution is fragile |

---

## Proposed Pipeline: Unified Design

Both engines adopt the **same `StepResult` / `PipelineContext` contract**, with engine-specific step implementations.

### Core Data Structures

```python
# src/engine/decision_pipeline.py  [NEW]

@dataclass
class StepResult:
    name: str         # Vocabulary: signal|rule|ai|entry_quality|trend|regime|risk
    passed: bool      # True = allowed to proceed
    score: float      # 0-100 (numeric steps) or -1 (binary steps)
    reason: str       # Human-readable, one sentence
    data: dict        # Raw inputs used (for backtesting replay)

@dataclass
class PipelineContext:
    engine: str           # "CORE_OI" or "TIMEFRAME"
    symbol: str
    direction: str | None # "LONG" / "SHORT" / None (set by signal step)
    underlying: float
    scan_context: dict
    ai_verdict: dict | None
    steps: list[StepResult]   # Filled as pipeline runs

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps)

    @property
    def final_action(self) -> str:
        return "TRADE" if self.passed else "SKIP"

    @property
    def block_step(self) -> str | None:
        failed = [s for s in self.steps if not s.passed]
        return failed[0].name if failed else None

    @property
    def block_reason(self) -> str:
        failed = [s for s in self.steps if not s.passed]
        return failed[0].reason if failed else ""
```

---

### Pipeline Steps: Core OI Engine vs Timeframe Strategy

| Step | Core OI Engine | Timeframe Strategy | Shared? |
|------|---------------|-------------------|---------|
| **1. Signal** | OI anomaly verdict from `intel` (BULLISH/BEARISH) + confidence threshold | 3H breakout (close > prev_high + ATR buffer) | ❌ Engine-specific |
| **2. Rule** | Time guard + min confidence + scan count gate + duplicate check | Duplicate signal_key + market hours + pyramid direction check | ❌ Engine-specific |
| **3. AI Verdict** | `ai_bias` vs verdict + risk_rating + veto/boost mode | `ai_bias` vs direction + risk_rating | ✅ Shared `evaluate_ai_alignment()` |
| **4. Entry Quality** | `calculate_entry_quality()` score (already exists in [entry_quality.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/entry_quality.py)) | Body%, breakout conviction, wick rejection (new calc) | ⚠️ Reuse existing for Core; build for Timeframe |
| **5. Trend Alignment** | `get_trend_alignment_score()` (already exists in [trend_analysis.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/trend_analysis.py)) | OI bias strength + 1H confirmation | ⚠️ Reuse existing for Core; OI-specific for Timeframe |
| **6. Regime** | `detect_market_regime()` + `regime_score_for_trade()` (already exists in [regime_detector.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/regime_detector.py)) | Same regime detection | ✅ Shared — reuse existing |
| **7. Risk** | `check_risk_limits()` with **sub-check detail** (currently returns opaque string) | Same `check_risk_limits()` | ✅ Shared — enhance to return structured reason |

> [!NOTE]
> Steps 3, 6, and 7 are **already implemented** in the bot. The pipeline refactor wraps them in `StepResult` containers — no new logic, just structured output. Steps 4 and 5 for Timeframe need new scoring logic. Step 1 and 2 are engine-specific extractions of existing inline code.

---

### Pipeline Executor

```python
# src/engine/decision_pipeline.py  [NEW]

CORE_OI_STEPS = [
    step_signal_core_oi,
    step_rule_core_oi,
    step_ai_alignment,         # shared
    step_entry_quality_core,   # wraps existing calculate_entry_quality()
    step_trend_alignment_core, # wraps existing get_trend_alignment_score()
    step_regime,               # shared — wraps detect_market_regime()
    step_risk,                 # shared — wraps check_risk_limits()
]

TIMEFRAME_STEPS = [
    step_signal_timeframe,
    step_rule_timeframe,
    step_ai_alignment,         # shared
    step_entry_quality_tf,     # new scoring for TF candle quality
    step_trend_alignment_tf,   # OI bias + 1H confirm
    step_regime,               # shared
    step_risk,                 # shared
]

def run_entry_pipeline(ctx: PipelineContext) -> PipelineContext:
    steps = CORE_OI_STEPS if ctx.engine == "CORE_OI" else TIMEFRAME_STEPS
    for step_fn in steps:
        result = step_fn(ctx)
        ctx.steps.append(result)
        if not result.passed and PIPELINE_SHORT_CIRCUIT:
            break   # configurable: run all steps for full audit
    return ctx
```

---

## Files to Create / Modify

### NEW Files

#### [NEW] [decision_pipeline.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/decision_pipeline.py)
- `StepResult`, `PipelineContext` dataclasses
- `run_entry_pipeline()` executor (shared)
- All 7 step functions (engine-specific and shared)
- ~350 lines

#### [NEW] [decision_audit.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/decision_audit.py)
- `log_decision(ctx, action, trade_id=None)` — writes one row per evaluation
- `get_recent_decisions(symbol, limit)` — for dashboard API
- `cleanup_old_decisions(days=90)` — retention management
- ~120 lines

### MODIFY Files

#### [MODIFY] [trade_decision.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/trade_decision.py)
**Core OI Engine entry point refactor:**

Current flow in `make_trade_decision()` (L93–L359):
- 5 hard blocks (time, underlying, verdict direction, confidence, scan count)
- Build plan, calculate 4 scores
- Mode-conditional branching (conservative/balanced/aggressive/hybrid)
- AI boost/veto at end

New flow:
```python
def make_trade_decision(...) -> dict:
    ctx = PipelineContext(engine="CORE_OI", ...)
    ctx = run_entry_pipeline(ctx)
    log_decision(ctx, ctx.final_action)
    if not ctx.passed:
        return _blocked(ctx.block_reason)
    # ... existing trade execution unchanged
```
The `scores` dict (L184) becomes `ctx.steps` — same data, structured differently.

#### [MODIFY] [paper_trading.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/paper_trading.py)
**Timeframe Strategy entry block refactor (L1066–L1170):**

Current inline blocks replaced with:
```python
ctx = PipelineContext(engine="TIMEFRAME", ...)
ctx = run_entry_pipeline(ctx)
log_decision(ctx, ctx.final_action)
if not ctx.passed:
    return {"action": "BLOCKED_PLAN", "reason": ctx.block_reason,
            "decision_trail": [asdict(s) for s in ctx.steps]}
# ... existing trade placement (L1171–L1345) unchanged
```

Exit logic (L792–L1064) stays untouched in Phase 1.

#### [MODIFY] [risk_engine.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/risk_engine.py)
Enhance `_check_risk_limits_for_table()` to return `(bool, str, dict)` where the `dict` contains:
```python
{
    "sub_check": "DAILY_LOSS_CAP",   # Vocabulary: DAILY_LOSS_CAP|COOLDOWN|MAX_OPEN|CIRCUIT_BREAKER|MAX_TRADES_PER_DAY|OK
    "detail": {"current_loss": -4500, "cap": -5000}
}
```
The `step_risk()` pipeline step uses this structured output so the audit log shows *exactly* which sub-check fired.

#### [MODIFY] [settings.py](file:///c:/Users/manve/Downloads/NSEBOT/config/settings.py)
New settings:
```python
PIPELINE_SHORT_CIRCUIT = True         # Stop at first failing step (False = log all steps)
ENTRY_QUALITY_MIN_SCORE_TF = 40       # Min entry quality for Timeframe (Core uses existing MIN_ENTRY_QUALITY_CORE)
TREND_ALIGNMENT_MIN_SCORE_TF = 35     # Min OI+1H trend score for Timeframe
DECISION_AUDIT_ENABLED = True         # Write to decision_audit table
DECISION_AUDIT_RETENTION_DAYS = 90
```

#### [MODIFY] [schema.py or db_setup equivalent](file:///c:/Users/manve/Downloads/NSEBOT/src/models/schema.py)
New table `decision_audit`:
```sql
CREATE TABLE IF NOT EXISTS decision_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    engine          TEXT NOT NULL,       -- CORE_OI | TIMEFRAME
    symbol          TEXT NOT NULL,
    direction       TEXT,                -- LONG | SHORT | NULL
    action          TEXT NOT NULL,       -- TRADE | SKIP
    -- Step scores/results (NULL if step not reached in short-circuit mode)
    signal_score    REAL,
    rule_passed     INTEGER,
    ai_score        REAL,
    ai_agrees       INTEGER,
    entry_quality   REAL,
    trend_score     REAL,
    regime_score    REAL,
    risk_passed     INTEGER,
    risk_sub_check  TEXT,               -- Which risk sub-check fired (if failed)
    -- Summary
    block_step      TEXT,               -- First failing step name (NULL if TRADE)
    block_reason    TEXT,
    trail_json      TEXT,               -- Full list[StepResult] as JSON
    trade_id        INTEGER,            -- FK to paper_trades.id (NULL if SKIP)
    bar_end_utc     TEXT,
    scan_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_da_symbol_ts ON decision_audit(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_da_action ON decision_audit(action, engine);
```

#### [MODIFY] [dashboard_server.py](file:///c:/Users/manve/Downloads/NSEBOT/dashboard_server.py)
New API endpoints:
- `GET /api/decisions?symbol=NIFTY&engine=CORE_OI&limit=50` — recent decisions with step trails
- `GET /api/decisions/stats?symbol=NIFTY` — block step frequency distribution (how often each step blocks)
- Paper trading `/paper` page: add "Decision Audit" panel showing last 20 evaluations per symbol

---

## Exit Audit Coverage (Phase 5)

Exit decisions also get pipeline coverage — same pattern but for close events:

```
Price signal (SL hit / candle crossover / LLM reversal / dead trade)
↓
Exit rule (trade age, bar alignment check)
↓
AI exit verdict (LLM exit_advice confirmation)
↓
Final action: CLOSE / HOLD
↓
Reason + exit_audit log row
```

The `close_paper_trade()` call sites (L824–L986) get wrapped with a lightweight `ExitContext` that logs which exit trigger fired (not all 7 steps — exits are simpler).

---

## Implementation Phases

| Phase | Scope | Files Changed | Risk |
|-------|-------|--------------|------|
| **Phase 1** | Framework + Core OI pipeline (replaces `make_trade_decision` inline logic 1:1, no behavior change) + `decision_audit` table | `decision_pipeline.py` [NEW], `decision_audit.py` [NEW], `trade_decision.py`, `schema.py`, `settings.py` | Low — existing tests pass unchanged |
| **Phase 2** | Timeframe Strategy entry pipeline (replaces inline blocks L1066–L1170) | `paper_trading.py`, `decision_pipeline.py` | Low — exit logic untouched |
| **Phase 3** | Entry Quality + Trend Alignment scoring for Timeframe (new step logic) | `decision_pipeline.py`, `entry_quality.py` | Medium — new numeric thresholds need calibration |
| **Phase 4** | Dashboard Decision Audit panel + API endpoints | `dashboard_server.py`, paper trading UI | Low |
| **Phase 5** | Exit pipeline audit (CLOSE/HOLD trail) + `risk_engine.py` structured sub-checks | `paper_trading.py`, `risk_engine.py`, `decision_pipeline.py` | Low |

> [!TIP]
> **Phase 1 alone** gives you a structured audit trail for the Core OI Engine with zero behavioral change. Every trade decision (TRADE and SKIP) gets a row in `decision_audit` with all 4 existing scores (`confidence`, `entry_quality`, `trend_alignment`, `regime_score`) stored as structured columns instead of a concatenated string.

---

## Open Questions

> [!IMPORTANT]
> **Q1: Short-circuit vs full evaluation?**
> - `PIPELINE_SHORT_CIRCUIT = True` (default): Stops at first failing step. Faster, dashboard shows only the blocking step.
> - `PIPELINE_SHORT_CIRCUIT = False`: Runs all 7 steps regardless. Full audit — shows which steps passed even when another blocked.
> - **Recommendation**: Full evaluation always (False). Your scan cycle is ~30s with 6 symbols — the 7 step functions are all local computations, no network calls. Full audit data is far more valuable than the trivial time savings.

> [!IMPORTANT]
> **Q2: Should SKIP decisions be logged to `decision_audit`?**
> - `DECISION_AUDIT_ENABLED = True` means both TRADE and SKIP get a row. At 5m scan frequency with 6 symbols, that's ~1,728 rows/day maximum (most scans are fast SKIP due to no signal).
> - Practical volume: ~50–200 rows/day (most SKIPs happen at the Signal step before OI confirms, so the `trail_json` is short).
> - **Recommendation**: Log all. 90-day retention = max ~18,000 rows. Trivial for SQLite.

> [!IMPORTANT]
> **Q3: For the Core OI Engine, which `TREND_FILTER_MODE` should the pipeline represent?**
> - Currently the bot has 4 modes (conservative/balanced/aggressive/hybrid), each evaluating different sub-checks in different orders.
> - The pipeline design above **unifies** them under the same 7 steps — the mode controls thresholds, not which steps run.
> - This is a slight behavioral simplification. For example, in `hybrid` mode today, Priority 1 is reversal, Priority 2 is persistence, Priority 3 is momentum — each can independently trigger a TRADE. In the pipeline model, all three become sub-scores in Step 5 (Trend Alignment), and only one threshold gate controls the step result.
> - **Recommendation**: Preserve mode-specific logic inside `step_trend_alignment_core()` — each mode gets its own sub-evaluation path, but all return a `StepResult`. This preserves backward compatibility while adding auditability.
