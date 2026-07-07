# ADR-007: AI Role Redesign — Decide Fast, Explain Slow

**Version:** 2.0 (corrected against repo — see §0 Changelog)
**Status:** Proposed
**Decision owner:** Manvendra
**Scope:** `decision_pipeline.py`, `src/alerts/telegram_dispatcher.py`, `src/alerts/digest.py`, `src/intelligence/ml_predictor.py`, `pipeline.py`, `trade_decision.py`, `pattern_history.py` (NEW), schema, `config/settings.py`
**Applies to:** all symbols, paper and live

---

## 0. Changelog v1 → v2 (code-verified)

| # | Issue | Fix |
|---|---|---|
| 1 | `pattern_history.py` / `get_pattern_stats()` claimed "already exists" — **it does not exist anywhere in repo** | A1 reclassified as net-new build, not rewire. Re-estimated. |
| 2 | Wrong file paths: `ml_predictor.py`, `digest.py`, `telegram_dispatcher.py` | Corrected to `src/intelligence/` and `src/alerts/` |
| 3 | Untouched 2nd `ai_conf` gate at `decision_pipeline.py:244-249` (CORE_OI full-mode veto) — would fail §9's grep test | Added to A1 scope explicitly |
| 4 | `ml_predictor.py` already has AUC deploy-gating (`floor=max(auc,0.55)`, 2% improvement threshold) | A2 guard scoped as separate live-consumption check; noted for code-review overlap |

---

## 1. Context & Problem

Current architecture grants AI authority inversely proportional to its reliability:

| Evidence | Location |
|---|---|
| LLM boost overrides rule BLOCK on uncalibrated confidence (`ai_conf >= 80` resurrects trades) | `decision_pipeline.py:447-450` (Priority 5) |
| Second, separate gate: CORE_OI full-mode veto also keyed on raw `ai_conf` | `decision_pipeline.py:244-249` |
| Incident 2026-07-07 16:04: NATURALGAS rules blocked (alignment 10/70, momentum 20, no sustained direction) → AI promoted GO_SHORT @ "75%" in PARITY regime against parity pull; alert shipped with inverted premium targets | scheduler log + Telegram digest |
| ML predictor live in decision path at AUC 0.449 / 85 samples (below coin-flip) | `src/intelligence/ml_predictor.py`, startup log |
| LLM entry verdict sits in hot path (~74s budget of scan cycle) re-deriving direction the OI engine computed in ms; `_enforce_engine_alignment()` exists because the LLM kept flipping it | `llm_enrichment.py:1635` (fn def), `:1919` (call site) |

**Core principle violated:** an LLM's confidence scalar is not a calibrated probability. It must never be the deciding variable at a P&L-weighted gate — **in any of its current call sites**, not just Priority 5.

## 2. Decision

Split AI responsibilities into three planes with hard boundaries:

```
DECISION PLANE   (ms, deterministic)  → rules + measured statistics only
LANGUAGE PLANE   (async, post-decision) → LLM: prose, unstructured extraction
LEARNING PLANE   (nightly, offline)   → shadow ML, autopsies, calibration scoreboards
```

Nothing in the Language or Learning plane may alter a trade decision until it earns promotion via §7 gates.

---

## 3. Phase A — Defang the hot path (highest P&L impact, **~3.5–4 days, revised**)

### A0. Build `pattern_history.py` — NEW MODULE (~1–1.5 days, not in v1 estimate)

Does not exist today. Required before A1 can ship.

- `get_pattern_stats(symbol, verdict, pcr_regime) -> PatternStats {n_trades, win_rate, avg_pnl}`
- Source: aggregate from closed `paper_trades` + `live_trades`, keyed by `(symbol, setup_type/verdict_label, pcr_regime)`.
- Needs a backing table or materialized view — decide now: live-computed query vs. precomputed rollup refreshed nightly. **Recommend nightly rollup** (matches autopsy cadence in B1, avoids hot-path query cost).
- Minimum viable: rolling 90-day window, refreshed once daily.

### A1. Replace LLM-confidence boost with empirical boost — **both gates**

**File:** `decision_pipeline.py`

**Gate 1 — Priority-5 boost, lines 447-450:**

Current:
```python
if not passed and ctx.ai_verdict and ai_decision_mode in ("boost_only", "full"):
    if ai_agrees and ai_conf >= ai_min_confidence_boost:
        setup_type = "AI_PROMOTED"
```

Target:
```python
if not passed and ai_decision_mode in ("boost_only", "full"):
    precedent = get_pattern_stats(
        symbol=ctx.symbol, verdict=ctx.verdict_label, pcr_regime=ctx.pcr_regime,
    )
    if (precedent.n_trades >= EMP_BOOST_MIN_TRADES        # default 20
        and precedent.win_rate >= EMP_BOOST_MIN_WINRATE    # default 0.60
        and precedent.avg_pnl > 0
        and not (ctx.ai_verdict and ctx.ai_verdict.veto_flag)):
        setup_type = "EMPIRICAL_PROMOTED"
        reason = (f"Empirical boost: {precedent.win_rate:.0%} over "
                  f"{precedent.n_trades} trades | LLM veto: none | "
                  f"Rule blocked: {'; '.join(block_parts)}")
```

**Gate 2 — CORE_OI full-mode veto, lines 244-249 (NEW in v2, missed in v1):**

Current: `if not ai_agrees and ai_conf >= ai_min_confidence_veto: return StepResult(passed=False, ...)`

Target: swap the confidence-threshold check for `ctx.ai_verdict.veto_flag` (binary), consistent with A1's veto model. This is the **same class of bug** as Priority 5 — do it in the same PR or the grep-test in §9 fails.

Rules:
- LLM confidence number is **removed from all gate comparisons**, both sites above. Grep-verify: no `ai_conf >=` remains outside logging.
- LLM contribution shrinks to `veto_flag: bool` + `veto_reason: str` (qualitative disqualifiers only). A veto flag **blocks a boost**; it never blocks a rule-passed trade until Tier-2 promotion (§7).
- Every would-have-boosted decision under the OLD rule is still logged to shadow (§C) so foregone P&L is measurable.

### A2. ML predictor → shadow mode

**Files:** `pipeline.py` / `trade_decision.py` (consumers), `src/intelligence/ml_predictor.py`.

- `ML_PREDICTOR_MODE = "shadow"` in `config/settings.py` (values: `off | shadow | live`; default `shadow`).
- In shadow: `predict()` runs, result written to `shadow_predictions` (§C schema), **consumed by nothing**.
- Add startup guard in `ml_predictor._load_model()`: if `auc < 0.55 or n_samples < 300` → log WARNING, force shadow regardless of setting.
- **Note:** this module already has separate deploy-time AUC gating (`floor=max(current_auc,0.55)`, 2%-improvement threshold for model *promotion*). The new guard governs *live consumption*, not deployment — different concern, same file. Flag both explicitly in code review so they aren't merged/confused.

### A3. Move `get_llm_verdict` out of the scan hot path

**Files:** `pipeline.py`, `llm_enrichment.py`, `src/alerts/digest.py`, `src/alerts/telegram_dispatcher.py`.

- Pipeline order: rules decide → decision persisted → digest v1 sent (rule-derived levels, "thesis pending" line) → async task calls `get_llm_verdict` → digest v2 edit (Telegram `editMessageText`) with thesis/invalidation/veto-flag, or follow-up message if edit fails.
- Enrichment timeout: 120s soft, decoupled from scan cadence. Circuit breaker unchanged, now only affects prose.
- **Exception (stays synchronous):** `get_exit_advice()` on open positions.
- Acceptance: scan wall-time without open position drops ~4 min → < 30s; total LLM outage still produces complete, tradeable alerts with "context unavailable" line.

---

## 4. Phase B — LLM to unstructured data (its actual comparative advantage, ~2 days)

| Task | Module | Cadence |
|---|---|---|
| EIA consensus article → `{consensus_bcf, source, confidence}` | `eia_consensus_fetcher.py` (NG plan §3.1) | Wed |
| Weather context line interpretation for digest | NG plan §11.6 | per run |
| News headline → structured `{event_type, direction_hint, veto_flag}` for NG news-only route | `intelligence.py` NG branch | per scan (async) |
| Anomaly narrative in digest (explain top-3, never decide) | `src/alerts/digest.py` | per alert |
| **Nightly trade autopsy** (new, see B1) | `autopsy_writer.py` NEW | 23:45 IST |

### B1. `src/engine/autopsy_writer.py` — NEW

Nightly job (23:45 IST via `job_runner`):
1. Pull all trades closed today + all `shadow_decisions` rows.
2. One LLM call per closed trade (batchable): inputs = decision record, block/boost reasons, entry/exit prices, MFE/MAE; output = `{reasons_held: bool, primary_failure: str, note: str}` — 3 sentences max.
3. Append to `trade_autopsies` table + write daily `docs/autopsy_YYYYMMDD.md`.
4. Weekly rollup: win-rate by setup_type, empirical-boost vs shadow LLM-boost performance, veto-flag precision.

This is the artifact that justifies (or permanently buries) Tier-2 promotion — evidence, not vibes.

---

## 5. Phase C — Schema (~0.5 day)

```sql
CREATE TABLE IF NOT EXISTS shadow_decisions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT, engine TEXT,                 -- CORE_OI / TIMEFRAME
    rule_action TEXT, rule_block_reason TEXT,
    old_ai_would_boost INTEGER,               -- OLD rule: ai_agrees & conf>=80
    ai_bias TEXT, ai_conf INTEGER, ai_veto_flag INTEGER, ai_veto_reason TEXT,
    empirical_n INTEGER, empirical_winrate REAL, empirical_avg_pnl REAL,
    final_action TEXT, setup_type TEXT,
    outcome_pnl REAL, outcome_filled_at TEXT  -- backfilled on close/expiry
);

CREATE TABLE IF NOT EXISTS shadow_predictions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL, symbol TEXT,
    model_version TEXT, p_success REAL,
    features_json TEXT,                        -- Phase-0 roadmap dependency
    decision_id INTEGER,                       -- FK → shadow_decisions
    outcome INTEGER                            -- backfilled: 1 win / 0 loss
);

CREATE TABLE IF NOT EXISTS trade_autopsies (
    id INTEGER PRIMARY KEY,
    trade_id INTEGER, ts TEXT,
    reasons_held INTEGER, primary_failure TEXT, note TEXT,
    llm_model TEXT
);

-- NEW in v2: backing store for pattern_history.py (A0)
CREATE TABLE IF NOT EXISTS pattern_stats_rollup (
    symbol TEXT, verdict_label TEXT, pcr_regime TEXT,
    n_trades INTEGER, win_rate REAL, avg_pnl REAL,
    computed_at TEXT,
    PRIMARY KEY (symbol, verdict_label, pcr_regime)
);
```

Backfill job: on paper/live trade close, update matching `shadow_decisions.outcome_pnl` and `shadow_predictions.outcome`, and trigger `pattern_stats_rollup` nightly refresh.

---

## 6. Settings

```python
# ── ADR-007: AI role redesign ──
AI_DECISION_MODE = "empirical"        # advisory | empirical | full (full = post-Tier-2 only)
EMP_BOOST_MIN_TRADES = 20
EMP_BOOST_MIN_WINRATE = 0.60
ML_PREDICTOR_MODE = "shadow"          # off | shadow | live (live gated by §7)
LLM_ENRICHMENT_ASYNC = True
LLM_ENRICH_TIMEOUT_S = 120
AUTOPSY_ENABLED = True
AUTOPSY_TIME_IST = "23:45"
```

Note: current default in `config/settings.py` is `AI_DECISION_MODE = "boost_only"` — this ADR changes the default. `boost_only` mode is **removed** (breaking change, intentional). Runtime-config keys `live_ai_min_confidence_boost/veto` become dead → delete from `runtime_config` handling to avoid zombie gates.

---

## 7. Promotion gates (written now, enforced later)

| Candidate | Gate to gain authority | Measured from |
|---|---|---|
| LLM veto → Tier 2 (can block rule-passed trades) | Veto-flag precision ≥ 0.65 over ≥ 30 flagged shadow cases; flagged-trade avg P&L < unflagged by a margin | `shadow_decisions` + autopsy rollups |
| ML predictor → live | ≥ 300 closed trades with persisted features (Phase-0 roadmap done); out-of-fold AUC ≥ 0.62 across ≥ 2 regime periods; calibration curve published in docs | `shadow_predictions` |
| Empirical boost threshold tuning | ≥ 100 EMPIRICAL_PROMOTED outcomes; re-fit winrate/min-trades on realized P&L | `shadow_decisions` |
| Old LLM boost resurrection (if ever) | Shadow log shows old-rule boosts outperform empirical boosts over ≥ 100 paired cases | `shadow_decisions.old_ai_would_boost` |

Kill-clauses: any candidate failing its gate after the sample threshold is reached is **removed from the roadmap**, not retried with lowered thresholds.

---

## 8. What does NOT change

- OI engine owns direction (unchanged; this ADR extends the same principle to money-weighted gates).
- `_enforce_engine_alignment()` stays (defense in depth for the prose layer).
- Exit advisor stays synchronous.
- `step_ai_alignment` TIMEFRAME gates (bias mismatch / HIGH risk block) stay — but flip comparison to `veto_flag`, not `ai_conf`, same pass as A1.
- Digest template redesign (previous review) is orthogonal; the "provenance block" there should render `EMPIRICAL_PROMOTED` / veto flags from the decision record.

---

## 9. Test plan — `tests/test_adr007.py`

- Priority-5 rewrite: blocked trade + precedent (n=25, wr=0.65) → EMPIRICAL_PROMOTED; n=10 → stays BLOCKED; veto_flag=True → stays BLOCKED with veto_reason logged.
- **NEW:** CORE_OI full-mode veto (line 244) rewrite: same veto_flag substitution test, mirrored.
- Grep-level test: assert no `ai_conf >=` in gating code paths (AST or regex over `decision_pipeline.py`, `trade_decision.py`) — must cover **both** gate sites.
- `pattern_history.get_pattern_stats()`: unit test against known fixture rollup, edge case n=0.
- ML guard: fixture auc=0.449 → forced shadow, warning logged; auc=0.63/n=350 + `ML_PREDICTOR_MODE=live` → loads live.
- Async enrichment: LLM stub delayed 90s → digest v1 < 30s with pending line, v2 edit arrives; total LLM failure → v1 complete and tradeable.
- Shadow backfill: close a paper trade → matching shadow rows get outcome.
- Autopsy: closed-trade fixture → row in `trade_autopsies` + daily md file.

---

## 10. Rollout

| Step | Gate |
|---|---|
| 1. Phase C schema + shadow logging only (zero behavior change) | 3 days clean logs |
| 2. Phase A on paper (`AI_DECISION_MODE=empirical`, ML shadow, async enrichment, both gate sites rewritten) | 2 weeks paper; compare EMPIRICAL vs old-rule shadow boosts |
| 3. Phase B (extraction tasks + autopsies) | runs parallel from step 1 |
| 4. Live flip | Paper shows no regression in rule-passed trade handling; scan latency < 30s confirmed; digest v1/v2 flow stable |

**Effort:** ~6–6.5 dev-days (revised from ~5) + observation windows. Delta is entirely A0 (`pattern_history.py` net-new build).

---

## 11. Consequences

**Gains:** decisions become fully deterministic and auditable; scan latency ~4 min → seconds; LLM outages stop touching execution; every AI authority claim now backed (or killed) by its own shadow record; incident class of 2026-07-07 becomes structurally impossible — **at both gate sites, not just one**.

**Costs:** genuine LLM saves foregone until measured (bounded — shadow log prices this exactly); two-part alerts; `boost_only` removal is a breaking config change; more tables; A0 adds ~1.5 days not in the original estimate.

**Revisit when:** closed trades > 500 with persisted features — at that point the Learning plane (calibrated ML gate) likely outperforms both rule thresholds and any LLM opinion, and the end-state division of labor locks in: rules and statistics own every rupee, the LLM owns every sentence, ML earns entry through shadow performance.
