# TREND_FOLLOWING_SHORT_STRANGLE — Implementation Plan (v3)

*Architectural Blueprint | Based on FRS v1.1 — Amended Baseline, corrected against live codebase review*

> Scope: Implementation blueprint only. No production code. Paper-trading scope only, per FRS §4.1/§4.2.
>
> **v3 changelog (supersedes v2):** `check_trend_persistence()` in `trend_analysis.py` was confirmed real but semantically wrong for this use case — it evaluates a 50-scan broad-trend bucket (`get_broader_trend_from_alerts(limit=50)`), not the FRS §5 "≥3 of last 5 scans, most recent agreeing" majority rule. The literal ≥3-of-5 logic that DOES exist lives inside `calculate_momentum_score()`'s "Component 2: recent scan agreement" block, but only as a 0–30 scoring weight, not a boolean gate, and it is never called by `check_trend_persistence()`. v3 introduces a dedicated native `compute_persisted_trend()` that implements the FRS-literal ≥3-of-5 rule directly, and demotes `check_trend_persistence()` to an optional secondary corroboration signal only.

---

## 1. Explicit Dependency Graph

1. `config/trend_following_short_strangle.py`
2. `src/engine/trend_following_short_strangle.py` (TFSS_STEPS + step functions, includes native `compute_persisted_trend()`)
3. `src/engine/decision_pipeline.py` (extend `run_entry_pipeline()` dispatch + add `TFSS_STEPS`)
4. `src/engine/trade_decision.py`
5. `src/engine/trade_plan.py`
6. `src/engine/risk_engine.py` (additive hook inside `_check_risk_limits_for_table()`)
7. `src/engine/paper_plan.py`
8. `src/engine/paper_trading.py`
9. `src/engine/decision_audit.py`
10. `src/engine/strategy_registry.py` (remove existing TFSS skip; wire `get_runner()`)
11. `src/engine/telegram_formatter.py` and `src/engine/scan_summary.py`
12. `src/models/schema.py` (deferred — prefer `signal_key` string-encoding pattern)
13. `tests/unit/engine/test_trend_following_short_strangle.py`
14. `tests/unit/engine/test_tfss_reversal_and_exit_priority.py`

---

## 2. Codebase-Verified Corrections

### 2.1 Registry already has a TFSS stub
`strategy_registry.py` has a disabled `"TFSS"` entry in `DEFAULT_STRATEGIES`, and `active_strategies_for()` explicitly skips it (`# Exception: TFSS has no runner yet`).
**Action:** Add `elif sid == "TFSS": return run_tfss_strategy` inside `get_runner()`; delete the `continue` skip. Do not build a new dispatch abstraction.

### 2.2 Persistence must be a NATIVE ≥3-of-5 check, not check_trend_persistence()
`check_trend_persistence()` calls `get_broader_trend_from_alerts(symbol, limit=50)` and returns a boolean based on a 50-scan broad-trend bucket (Strong/Moderate Bullish/Bearish, with a Mixed-trend override gated by `confidence < MIN_CONFIDENCE_CORE`). It contains **no ≥3-of-5 majority logic**. That logic exists only inside `calculate_momentum_score()`'s "Component 2: recent scan agreement (0-30)" block — as a *scoring weight*, not a boolean gate, and `check_trend_persistence()` never calls it.

**Why this matters:** Wiring TFSS's AC-011 gate directly to `check_trend_persistence()` would make persistence pass on 50-scan directional skew even when only 2 of the last 5 scans actually agree — a false positive that defeats the exact purpose of AC-011.

**Action (v3 — corrected):**
- Implement a **new, native** `compute_persisted_trend()` inside the TFSS module.
- It must query the last 5 non-fallback `scan_summaries` rows for the symbol (`ORDER BY fetched_at DESC LIMIT 5`), reusing the same query/helper pattern `calculate_momentum_score()`'s Component 2 block already uses.
- Count directional agreement using the same `is_bullish`/`is_bearish` classification helpers already used elsewhere in that file.
- Require `agreeing_count >= 3` **AND** the most recent row's direction matches the current verdict direction. This is the literal, sole AC-011 test — no other function should gate AC-011.
- `check_trend_persistence()` may be called **afterward**, only as an optional secondary corroboration signal (e.g., to suppress entries during a 50-scan "Mixed/Unclear" broad regime) — it must never replace or loosen the ≥3-of-5 gate.

### 2.3 Engine uses a step-based framework — TFSS must follow it
`decision_pipeline.py` uses `PipelineContext` + `StepResult` + ordered step lists (`CORE_OI_STEPS`, `TIMEFRAME_STEPS`) via `run_entry_pipeline()`.
**Action:** Define `TFSS_STEPS = [step_signal_tfss, step_rule_tfss, step_delta_selection, step_reversal_sequence, step_exit_priority, step_risk_tfss]`; extend `run_entry_pipeline()` with `elif ctx.engine == "TFSS": steps = TFSS_STEPS`. No monolithic orchestrator module.

### 2.4 Risk checks hook into the existing symbol-branch pattern
`_check_risk_limits_for_table()` has a `if symbol == "NATURALGAS":` hook branch before generic checks, and hard-allowlists `trades_table` to `("paper_trades", "live_trades")` via assert.
**Action:** Add TFSS-specific checks (tested-side breach, combined-book cap, delta hard-stop) as an additional isolated branch there. Store TFSS trades in existing `paper_trades`/`live_trades` tables. Do not duplicate daily-loss-cap/circuit-breaker/cooldown logic.

### 2.5 Prefer signal_key encoding over schema migration
`step_rule_timeframe()` already builds a `signal_key` string (e.g., `f"{symbol}:TIMEFRAME:3H:{direction}:{bar_end_3h}"`) for dedup.
**Action:** Encode `book_side`, `tranche_index`, `combined_group_id` into a similar `signal_key` string first; only add schema columns if this proves insufficient in local testing.

---

## 3. Step-by-Step Workspace Modifications

### 3.1 `config/trend_following_short_strangle.py`
- **Layer:** Config
- **State impact:** `STRATEGY_MODE`, `PERSISTENCE_WINDOW=5`, `PERSISTENCE_MIN_MATCH=3`, `ATR_FAST_WINDOW=5`, `ATR_SLOW_WINDOW=10`, DTE delta-band table, tranche sequence `[0.50,0.30,0.20]`, exit-priority map.
- **TDD task:** Assert `PERSISTENCE_WINDOW=5` and `PERSISTENCE_MIN_MATCH=3` are read by `compute_persisted_trend()` and not hardcoded inline.

### 3.2 `src/engine/trend_following_short_strangle.py`
- **Layer:** Engine (step functions consumed by `TFSS_STEPS`)
- **State impact:** `strategy_mode`, `persisted_trend`, `persisted_trend_agreement_count`, `persisted_trend_source="native_5scan"`, `broad_trend_corroboration` (from `check_trend_persistence()`, optional/secondary), `trend_supported_side`, `confirmed_reversal`, `side_book_state`, `combined_book_state`, `candidate_strike`, `candidate_delta`, `delta_band`, `atr_regime_tightened`, `tranche_index`, `eligible_triggers`, `selected_trigger`, `also_eligible_triggers`, `decision_reason_code`, `reversal_sequence_step`.
- **TDD task (AC-011, corrected):** Fixture with exactly 2 of last 5 scans agreeing (5th/most recent also non-agreeing) must return `persisted_trend.is_valid = False` via `compute_persisted_trend()` — **independent of** what `check_trend_persistence()`'s 50-scan bucket would return. Add a second fixture where the 50-scan broad bucket is "Strong Bullish" but only 2/5 recent scans agree, asserting persistence STILL fails — this is the regression test that directly targets the bug this v3 revision fixes.

**Pseudocode — `compute_persisted_trend()` (NEW — native ≥3-of-5 rule):**

```text
function compute_persisted_trend(symbol, config):
    recent_scans = query_scan_summaries(
        symbol=symbol,
        exclude_fallback=True,
        order_by="fetched_at DESC",
        limit=config.PERSISTENCE_WINDOW  # 5
    )

    if len(recent_scans) < config.PERSISTENCE_WINDOW:
        return PersistenceResult(is_valid=False, reason="INSUFFICIENT_SCAN_HISTORY")

    directions = [classify_direction(scan.verdict_label) for scan in recent_scans]
    # classify_direction reuses existing is_bullish/is_bearish helpers used by
    # calculate_momentum_score()'s Component 2 block

    most_recent_direction = directions[0]
    agreeing_count = count(d == most_recent_direction for d in directions)

    if agreeing_count < config.PERSISTENCE_MIN_MATCH:  # 3
        return PersistenceResult(is_valid=False, reason="BELOW_MIN_MATCH",
                                  agreeing_count=agreeing_count)

    # most_recent_direction already satisfies "most recent scan agrees" by construction

    result = PersistenceResult(
        is_valid=True,
        label=most_recent_direction,
        agreeing_count=agreeing_count,
        source="native_5scan"
    )

    # OPTIONAL secondary corroboration only -- must never override or loosen the gate above
    broad = check_trend_persistence(symbol)  # 50-scan broad bucket, existing function
    result.broad_trend_corroboration = broad
    if config.REQUIRE_BROAD_CORROBORATION and not broad:
        result.is_valid = False
        result.reason = "BROAD_TREND_CONTRADICTS_RECENT"

    return result
```

**Pseudocode — §11 Reversal Sequence (updated call site):**

```text
function evaluate_reversal(symbol_state, market_state, config):
    persisted = compute_persisted_trend(symbol_state.symbol, config)   # native ≥3-of-5, NOT check_trend_persistence()
    if not persisted.is_valid:
        return BLOCK(reason="PERSISTENCE_NOT_CONFIRMED", detail=persisted.reason)

    original_side = get_open_side_book(symbol_state)
    reversal_side = side_opposite(original_side)

    if not is_confirmed_reversal(persisted.label, original_side):
        return NO_REVERSAL_ACTION

    reversal_audit = []
    tested_status = risk_engine.check_tested_side(original_side, market_state, config)
    reversal_audit.append({"step": 1, "tested_status": tested_status})
    if tested_status.beyond_threshold:
        reduce_result = paper_plan.reduce_or_close(original_side, tested_status)
        apply_virtual_book_change(symbol_state, reduce_result)
        reversal_audit.append({"step": 1, "action": reduce_result.action})

    combined_state = risk_engine.compute_combined_book(symbol_state, market_state)
    reversal_audit.append({"step": 2, "combined_state": combined_state})
    if not combined_state.within_caps:
        return BLOCK(reason="REVERSAL_BLOCKED_COMBINED_CAP", reversal_sequence_log=reversal_audit)

    candidate = trade_plan.select_candidate(side=reversal_side, persisted_label=persisted.label,
                                             dte=market_state.dte, atr_state=market_state.atr_state,
                                             option_chain=market_state.option_chain)
    if not candidate:
        return BLOCK(reason="REVERSAL_NO_VALID_CANDIDATE", reversal_sequence_log=reversal_audit)

    if risk_engine.binary_event_or_high_risk_regime_block(market_state, config):
        return BLOCK(reason="REVERSAL_BLOCKED_HIGH_RISK_REGIME", reversal_sequence_log=reversal_audit)

    return OPEN_OR_ADD(side=reversal_side, strike=candidate.strike, delta=candidate.delta,
                        premium=candidate.premium, reversal_sequence_log=reversal_audit)
```

### 3.3 `src/engine/decision_pipeline.py`
- **Layer:** Engine
- **State impact:** Add `TFSS_STEPS`; extend `run_entry_pipeline()` dispatch. No changes to `CORE_OI_STEPS`/`TIMEFRAME_STEPS`.
- **TDD task:** `ctx.engine=="TFSS"` runs only `TFSS_STEPS`; other engines unchanged (regression snapshot).

### 3.4 `src/engine/trade_decision.py`
- **Layer:** Engine
- **State impact:** Extend DTO with `action`, `reason`, `symbol`, `option_side`, `strike`, `delta`, `premium`, `risk_metrics`, `eligible_triggers`, `also_eligible_triggers`, `tested_side_status`, `combined_book_status`, `tranche_index`.
- **TDD task:** AC-010 field completeness for `OPEN` and `BLOCK`.

### 3.5 `src/engine/trade_plan.py`
- **Layer:** Engine
- **State impact:** `entry_delta_band`, `add_delta_band`, `hard_stop_delta`, `atr_tightened_bucket`, `otm_distance_ok`, `premium_filter_ok`, `liquidity_filter_ok`, `same_strike_blocked`, `worsening_delta_blocked`.
- **Logic order:** persisted trend (native) → DTE bucket → one-bucket ATR tightening → delta qualification → premium/liquidity/OTM filters → midpoint preference.
- **TDD task:** AC-009 one-bucket-only tightening, never looser.

### 3.6 `src/engine/risk_engine.py`
- **Layer:** Engine
- **State impact:** New TFSS hook branch inside `_check_risk_limits_for_table()`; `exit_trigger_priority_list` helper.
- **TDD task:** Multi-trigger priority ordering (DELTA_STOP wins over PROFIT_DECAY); NATURALGAS branch and table-allowlist assert unmodified.

**Pseudocode — §12 Exit Priority Loop:** (unchanged from v2)

```text
function choose_exit_action(position_state, market_state, config):
    eligible = []
    if written_leg_delta_crossed_hard_stop(position_state, market_state, config):
        eligible.append(trigger("DELTA_STOP", priority=1, action="EXIT_OR_REDUCE"))
    if margin_usage_exceeds_budget(position_state, market_state, config):
        eligible.append(trigger("MARGIN_CAP", priority=2, action="BLOCK_NEW_OR_REDUCE"))
    if adverse_spot_move_exceeds_atr_multiple(position_state, market_state, config):
        eligible.append(trigger("ATR_ADVERSE_MOVE", priority=3, action="EXIT_OR_REDUCE"))
    if combined_book_drawdown_exceeds_budget(position_state, market_state, config):
        eligible.append(trigger("COMBINED_BOOK_DRAWDOWN", priority=4, action="REDUCE"))
    if premium_stop_loss_hit(position_state, market_state, config):
        eligible.append(trigger("PREMIUM_SL", priority=5, action="EXIT"))
    if dte_cutoff_active(position_state, market_state, config):
        eligible.append(trigger("DTE_CUTOFF", priority=6, action="RISK_OFF"))
    if premium_decay_target_hit(position_state, market_state, config):
        eligible.append(trigger("PROFIT_DECAY_TARGET", priority=7, action="BOOK_OR_PARTIAL"))
    if eligible is empty:
        return NO_EXIT_ACTION
    eligible.sort(by=priority ascending)
    primary = eligible[0]
    also_eligible = eligible[1:]
    return Decision(action=primary.action, reason=primary.code,
                     eligible_triggers=[e.code for e in eligible],
                     also_eligible_triggers=[e.code for e in also_eligible])
```

### 3.7 `src/engine/paper_plan.py`
- **State impact:** `pe_side_book`, `ce_side_book`, `combined_book`, `parent_group_id`, `tranche_count`, `last_placement_at`, `last_placement_spot`, `entry_basis`, `reversal_origin_side`.
- **TDD task:** CE reversal leg doesn't overwrite PE-side state; both feed combined-book view.

### 3.8 `src/engine/paper_trading.py`
- **State impact:** Persist `strategy_mode`, `book_side`, `tranche_index`, `combined_group_id` (via `signal_key`), `decision_reason_code`, `risk_snapshot`. Storage in existing `paper_trades`/`live_trades`.
- **TDD task:** AC-001 — bullish persisted trend → short PE only, never buy leg.

### 3.9 `src/engine/decision_audit.py`
- **State impact:** `primary_trigger`, `also_eligible_triggers`, `input_metrics`, `delta_band_source`, `atr_regime_shift_applied`, `reversal_sequence_log`, `combined_cap_recheck`, `persistence_source="native_5scan"`, `persistence_agreeing_count`.
- **TDD task:** Audit log shows step order `CHECK_TESTED_SIDE -> REDUCE/CLOSE -> RECHECK_COMBINED -> OPEN/BLOCK_REVERSAL`, plus persistence agreement count for AC-011 evidence.

### 3.10 `src/engine/strategy_registry.py`
- **State impact:** Remove `continue` skip for `"TFSS"`; add `elif sid == "TFSS": return run_tfss_strategy` in `get_runner()`.
- **TDD task:** Registry resolves `"TFSS"`; other strategies unchanged.

### 3.11 `src/engine/telegram_formatter.py` / `src/engine/scan_summary.py`
- **State impact:** Formatter: `side_book`, `candidate_delta`, `premium`, `action`, `risk_reason`, `selected_trigger`, `also_eligible_triggers`. Scan summary: `strategy_mode`, `persisted_trend_label`, `persisted_trend_agreeing_count`, `confirmed_reversal`, `side_book_counts`, `combined_book_delta`, `exit_priority_selected`, `exit_priority_also_eligible`.
- **TDD task:** Snapshot test for blocked-reversal formatting; scan summary includes persistence agreement count and trigger-tier frequency.

### 3.12 `src/models/schema.py` (deferred)
- **State impact:** None by default — `signal_key` encoding first; new columns only if proven insufficient locally.
- **TDD task:** Migration test (if triggered) — old paper trades unaffected, new TFSS trades persist losslessly.

### 3.13 / 3.14 Test files
- `test_trend_following_short_strangle.py`: **native** persisted-trend test matrix (3/5 agree passes; 2/5 agree fails; 2/5 agree + 50-scan broad-bullish bucket STILL fails — the v3 regression case), delta-band selection, ATR tightening, same-strike block, worsening-delta block, signal-to-side mapping.
- `test_tfss_reversal_and_exit_priority.py`: §11 reversal sequencing (AC-012), §12 trigger ordering with multi-trigger cases.

---

## 4. Regression Guard Checklist

Do **NOT** modify or disrupt the following during local development:

- `CORE_OI_STEPS` / `TIMEFRAME_STEPS` lists, order, or short-circuit behavior — TFSS ships as an isolated `TFSS_STEPS` list.
- The `"NATURALGAS"` branch, `trades_table` allowlist assert, or the six generic checks inside `_check_risk_limits_for_table()`.
- Any `DEFAULT_STRATEGIES` entries other than `"TFSS"`, and the general `get_runner()`/`active_strategies_for()` flow for other strategies.
- `check_trend_persistence()` and `get_broader_trend_from_alerts()` internals — TFSS may call `check_trend_persistence()` only as an **optional secondary corroboration signal**, never as the primary AC-011 gate, and never modifies its 50-scan broad-bucket semantics.
- `calculate_momentum_score()`'s "Component 2" recent-scan-agreement block — TFSS's native `compute_persisted_trend()` may mirror its query/classification pattern but must not alter that scoring function itself.
- Non-TFSS signal mappings or generic option-buying behavior outside `TREND_FOLLOWING_SHORT_STRANGLE`.
- Live broker execution paths in `src/engine/live_trading.py`.
- Fetcher behavior, anomaly generation, or LLM enrichment logic.
- DTE delta-band permissiveness — ATR logic tightens only, by at most one bucket per adjustment.
- Same-strike averaging default-block, worsening-delta add-block, reversal-before-reduction ordering.
- Profit-taking priority — must never override an active higher-priority risk stop in the same cycle.
- Schema — no migration unless `signal_key` encoding is proven insufficient in local testing.
