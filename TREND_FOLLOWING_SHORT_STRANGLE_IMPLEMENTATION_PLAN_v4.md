# TREND_FOLLOWING_SHORT_STRANGLE — Implementation Plan (v4)

*Architectural Blueprint | Based on FRS v1.1 — corrected against live codebase review and updated execution-topology decisions*

> Scope: Implementation blueprint only. No production code. Paper-trading scope only.
>
> **v4 changelog (supersedes v3):** TFSS is no longer modeled as a standalone peer strategy. It is now the **mandatory execution strategy for Core engine bullish/bearish option expressions**. Core remains the source of verdict and intelligence; TFSS becomes the execution layer. This replaces both (a) naked directional Core option-buying mappings such as `GO_LONG → BUY CE` and `GO_SHORT → BUY PE`, and (b) Core OI-bias writing mappings such as `Put Writing / OI Bias Bullish → SELL PE` and `Call Writing / OI Bias Bearish → SELL CE`. Timeframe strategy remains a separate parallel system and its execution flow stays unchanged. The native AC-011 persistence correction from v3 remains in force: TFSS must use a new native `compute_persisted_trend()` with the literal `>=3 of last 5 scans, most recent agreeing` rule; `check_trend_persistence()` remains optional secondary corroboration only.

---

## 1. Updated Topology

### 1.1 Execution ownership model

**New canonical model:**
- **Core engine** = verdict + intelligence producer.
- **TFSS** = mandatory execution adapter for qualifying Core bullish/bearish option expressions.
- **Timeframe engine** = separate parallel strategy; unchanged.

### 1.2 Core signals that must route into TFSS

The following Core verdict families must no longer directly emit naked option plans. They must be normalized into TFSS execution intents:

- `Long Buildup` / `Short Covering` / `GO_LONG`
- `Short Buildup` / `Long Unwinding` / `GO_SHORT`
- `Put Writing` / `OI Bias Bullish`
- `Call Writing` / `OI Bias Bearish`

### 1.3 Normalized execution intent mapping

Core verdicts should be translated into a small normalized intent layer before execution:

- `Long Buildup`, `Short Covering`, `GO_LONG`, `Put Writing`, `OI Bias Bullish` → `TFSS_BULLISH`
- `Short Buildup`, `Long Unwinding`, `GO_SHORT`, `Call Writing`, `OI Bias Bearish` → `TFSS_BEARISH`

TFSS then determines actual execution details (`SELL PE` vs `SELL CE`, add/reduce/block/exit) using persisted-trend confirmation, reversal sequencing, delta bands, DTE rules, ATR tightening, and risk controls.

### 1.4 Explicit replacement of old behavior

The following Core execution mappings are deprecated and must be removed or bypassed for Core-engine flows:

- `GO_LONG → BUY CE`
- `GO_SHORT → BUY PE`
- any direct naked Core-buying path tied to `Long Buildup` / `Short Covering`
- any direct naked Core-buying path tied to `Short Buildup` / `Long Unwinding`
- any direct Core-writing shortcut that bypasses TFSS governance for `Put Writing / OI Bias Bullish → SELL PE`
- any direct Core-writing shortcut that bypasses TFSS governance for `Call Writing / OI Bias Bearish → SELL CE`

Timeframe execution behavior is explicitly out of scope for modification.

---

## 2. Explicit Dependency Graph

1. `config/trend_following_short_strangle.py`
2. `src/engine/trend_following_short_strangle.py`
3. `src/engine/decision_pipeline.py`
4. `src/engine/trade_decision.py`
5. `src/engine/trade_plan.py`
6. `src/engine/risk_engine.py`
7. `src/engine/paper_plan.py`
8. `src/engine/paper_trading.py`
9. `src/engine/decision_audit.py`
10. `src/engine/strategy_registry.py` (minimal/no-op if Core path owns TFSS dispatch directly)
11. `src/engine/telegram_formatter.py`
12. `src/engine/scan_summary.py`
13. `src/models/schema.py` (deferred)
14. `tests/unit/engine/test_trend_following_short_strangle.py`
15. `tests/unit/engine/test_tfss_core_mapping.py`
16. `tests/unit/engine/test_tfss_reversal_and_exit_priority.py`

---

## 3. Codebase-Verified Corrections and v4 Implementation Rules

### 3.1 Registry note — TFSS exists, but Core-owned routing now takes priority

`strategy_registry.py` already contains a disabled `"TFSS"` stub and a skip path. In v3 this was treated as the primary integration route. In v4, this is no longer the main execution topology.

**Action:**
- Keep the registry wiring minimal.
- If the codebase still requires a resolvable TFSS runner for consistency or testing, wire it cleanly; otherwise Core-path dispatch can own TFSS execution directly.
- Do **not** force the implementation through strategy peer-registration if the actual Core pipeline owns execution selection.

### 3.2 Persistence must remain the native AC-011 gate

This v4 topology change does **not** alter the v3 persistence correction.

`check_trend_persistence()` is real but semantically wrong for AC-011 because it evaluates a 50-scan broad-trend bucket, not the required `>=3 of last 5` rule.

**Action:**
- Implement native `compute_persisted_trend()` inside TFSS.
- Query the last 5 non-fallback `scan_summaries` rows.
- Require `agreeing_count >= 3` and the most recent row to define the persisted direction.
- `check_trend_persistence()` may be used only as optional secondary corroboration.

### 3.3 Core pipeline now owns the TFSS handoff

`decision_pipeline.py` is step-based and already performs Core decision staging before paper plan construction.

**Action:**
- Introduce a Core-to-TFSS handoff step in the Core path, likely at or before the current paper-plan-construction stage.
- Replace direct Core instrument-style plan construction with normalized TFSS intent construction for qualifying bullish/bearish verdict families.
- Preserve existing Timeframe path selection and behavior unchanged.

### 3.4 TFSS is execution logic, not signal origination

TFSS must not independently originate signal direction in v4. Its job is to receive a Core-derived normalized bullish/bearish execution intent and decide:
- whether to open/add/block/reduce/exit,
- which short-premium side to express (`SELL PE` for bullish, `SELL CE` for bearish),
- which strike/delta/DTE/tranche to use,
- whether a reversal requires the reduction/recheck/open sequence,
- which exit trigger wins when multiple triggers are simultaneously true.

### 3.5 Risk integration remains additive inside the existing engine

`_check_risk_limits_for_table()` remains the correct integration seam.

**Action:**
- Add TFSS-specific enforcement as an additive branch.
- Do not fork a separate risk module.
- Do not disturb NATURALGAS behavior, generic checks, or table allowlist semantics.

### 3.6 Prefer signal_key encoding over schema migration

No change from v3.

**Action:**
- Encode `book_side`, `tranche_index`, `combined_group_id`, and possibly Core execution-intent family into `signal_key` first.
- Only migrate schema if local implementation proves string encoding insufficient.

---

## 4. Step-by-Step Workspace Modifications

### 4.1 `config/trend_following_short_strangle.py`
- **Layer:** Config
- **State impact:** `STRATEGY_MODE="TREND_FOLLOWING_SHORT_STRANGLE"`, `PERSISTENCE_WINDOW=5`, `PERSISTENCE_MIN_MATCH=3`, DTE delta-band table, ATR tightening settings, tranche sequence `[0.50,0.30,0.20]`, exit-priority map.
- **TDD task:** Ensure config values are consumed by TFSS execution called from the Core path, not only from a standalone TFSS runner.

### 4.2 `src/engine/trend_following_short_strangle.py`
- **Layer:** Engine / Execution adapter
- **State impact:** `core_execution_intent`, `normalized_tfss_bias`, `persisted_trend`, `persisted_trend_agreeing_count`, `persisted_trend_source="native_5scan"`, `broad_trend_corroboration`, `trend_supported_side`, `confirmed_reversal`, `side_book_state`, `combined_book_state`, `candidate_strike`, `candidate_delta`, `delta_band`, `atr_regime_tightened`, `tranche_index`, `eligible_triggers`, `selected_trigger`, `also_eligible_triggers`, `decision_reason_code`, `reversal_sequence_step`.
- **TDD task:** A Core bullish verdict routed into TFSS can only express `SELL PE`; a Core bearish verdict routed into TFSS can only express `SELL CE`.

**Pseudocode — native persisted trend (unchanged from v3):**

```text
function compute_persisted_trend(symbol, config):
    recent_scans = query_scan_summaries(symbol=symbol, exclude_fallback=True,
                                        order_by="fetched_at DESC",
                                        limit=config.PERSISTENCE_WINDOW)
    if len(recent_scans) < config.PERSISTENCE_WINDOW:
        return PersistenceResult(is_valid=False, reason="INSUFFICIENT_SCAN_HISTORY")

    directions = [classify_direction(scan.verdict_label) for scan in recent_scans]
    most_recent_direction = directions[0]
    agreeing_count = count(d == most_recent_direction for d in directions)

    if agreeing_count < config.PERSISTENCE_MIN_MATCH:
        return PersistenceResult(is_valid=False, reason="BELOW_MIN_MATCH",
                                 agreeing_count=agreeing_count)

    result = PersistenceResult(is_valid=True, label=most_recent_direction,
                               agreeing_count=agreeing_count,
                               source="native_5scan")

    broad = check_trend_persistence(symbol)  # optional secondary corroboration only
    result.broad_trend_corroboration = broad
    if config.REQUIRE_BROAD_CORROBORATION and not broad:
        result.is_valid = False
        result.reason = "BROAD_TREND_CONTRADICTS_RECENT"

    return result
```

**Pseudocode — Core intent normalization:**

```text
function normalize_core_verdict_to_tfss_intent(core_verdict, intelligence_context):
    if core_verdict in ["Long Buildup", "Short Covering", "GO_LONG", "Put Writing", "OI Bias Bullish"]:
        return TFSSIntent(bias="BULLISH", execution_family="TFSS_BULLISH")
    if core_verdict in ["Short Buildup", "Long Unwinding", "GO_SHORT", "Call Writing", "OI Bias Bearish"]:
        return TFSSIntent(bias="BEARISH", execution_family="TFSS_BEARISH")
    return None
```

**Pseudocode — execution side resolution:**

```text
function resolve_tfss_execution_side(tfss_intent, persisted_trend):
    if not persisted_trend.is_valid:
        return BLOCK(reason="PERSISTENCE_NOT_CONFIRMED")
    if tfss_intent.bias == "BULLISH":
        return "SELL_PE"
    if tfss_intent.bias == "BEARISH":
        return "SELL_CE"
    return BLOCK(reason="UNSUPPORTED_TFSS_INTENT")
```

**Pseudocode — reversal sequence (still mandatory):**

```text
function evaluate_reversal(symbol_state, market_state, config):
    persisted = compute_persisted_trend(symbol_state.symbol, config)
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

    return OPEN_OR_ADD(side=reversal_side, strike=candidate.strike, delta=candidate.delta,
                        premium=candidate.premium, reversal_sequence_log=reversal_audit)
```

### 4.3 `src/engine/decision_pipeline.py`
- **Layer:** Engine
- **State impact:** Add a Core-path handoff step, e.g. `step_tfss_handoff_core` or equivalent, before any direct paper trade plan construction for qualifying Core verdicts.
- **Required behavior:**
  - Core qualifying bullish/bearish verdicts must normalize into TFSS intent and then use TFSS execution.
  - Non-qualifying Core verdicts may continue existing handling if intentionally preserved.
  - Timeframe strategy branch remains unchanged.
- **TDD task:** Snapshot test proving Timeframe path is unchanged while Core `GO_LONG`, `GO_SHORT`, `Put Writing`, `Call Writing`, `OI Bias Bullish`, and `OI Bias Bearish` all route into TFSS instead of legacy direct plan construction.

### 4.4 `src/engine/trade_decision.py`
- **Layer:** Engine
- **State impact:** Extend decision DTO with `execution_source="CORE_TFSS" | "TIMEFRAME" | existing`, `core_verdict_family`, `normalized_tfss_bias`, `action`, `reason`, `symbol`, `option_side`, `strike`, `delta`, `premium`, `risk_metrics`, `eligible_triggers`, `also_eligible_triggers`, `tested_side_status`, `combined_book_status`, `tranche_index`.
- **TDD task:** Core-routed TFSS decision exposes enough metadata to distinguish original verdict from final execution side.

### 4.5 `src/engine/trade_plan.py`
- **Layer:** Engine
- **State impact:** `entry_delta_band`, `add_delta_band`, `hard_stop_delta`, `atr_tightened_bucket`, `otm_distance_ok`, `premium_filter_ok`, `liquidity_filter_ok`, `same_strike_blocked`, `worsening_delta_blocked`.
- **Required behavior:** Operates on normalized TFSS bias, not on naked Core CE/PE buy instructions.
- **TDD task:** Bullish Core verdict cannot generate `BUY CE`; bearish Core verdict cannot generate `BUY PE`; side resolution must be `SELL PE` or `SELL CE` through TFSS.

### 4.6 `src/engine/risk_engine.py`
- **Layer:** Engine
- **State impact:** TFSS-specific checks inside `_check_risk_limits_for_table()` plus `exit_trigger_priority_list` helper.
- **TDD task:** When both delta-stop and profit-decay are true, delta-stop wins. Separate regression confirms old generic checks and NATURALGAS branch remain untouched.

### 4.7 `src/engine/paper_plan.py`
- **Layer:** Engine
- **State impact:** `pe_side_book`, `ce_side_book`, `combined_book`, `parent_group_id`, `tranche_count`, `last_placement_at`, `last_placement_spot`, `entry_basis`, `reversal_origin_side`, `core_origin_verdict`.
- **TDD task:** Existing Core-origin metadata survives into the paper plan while execution side is TFSS-governed.

### 4.8 `src/engine/paper_trading.py`
- **Layer:** Engine
- **State impact:** Persist `strategy_mode`, `execution_source`, `book_side`, `tranche_index`, `combined_group_id` (via `signal_key`), `decision_reason_code`, `risk_snapshot`, `core_origin_verdict`.
- **TDD task:** Core bullish families store short-PE execution only; Core bearish families store short-CE execution only.

### 4.9 `src/engine/decision_audit.py`
- **Layer:** Engine
- **State impact:** `core_origin_verdict`, `core_execution_intent`, `primary_trigger`, `also_eligible_triggers`, `input_metrics`, `delta_band_source`, `atr_regime_shift_applied`, `reversal_sequence_log`, `combined_cap_recheck`, `persistence_source="native_5scan"`, `persistence_agreeing_count`.
- **TDD task:** Audit clearly shows original Core verdict family and final TFSS execution path.

### 4.10 `src/engine/strategy_registry.py`
- **Layer:** Engine
- **State impact:** Minimal. If kept, ensure TFSS remains resolvable without forcing Core routing through registry as a peer strategy.
- **TDD task:** Registry behavior remains backward-compatible; Core-owned TFSS routing does not depend on strategy peer activation.

### 4.11 `src/engine/telegram_formatter.py` / `src/engine/scan_summary.py`
- **Layer:** Presentation / Reporting
- **State impact:** Formatter should show original Core verdict, normalized TFSS bias, final execution side, risk reason, selected trigger, also eligible triggers. Scan summary should show TFSS-routed Core counts separately from unchanged Timeframe counts.
- **TDD task:** Messaging clearly distinguishes `Core verdict: GO_LONG` from `Execution: TFSS bullish → SELL PE`.

### 4.12 `src/models/schema.py` (deferred)
- **Layer:** Models
- **State impact:** None by default. Use encoded keys and existing tables first.
- **TDD task:** Only add migration tests if schema changes are actually introduced.

### 4.13 `tests/unit/engine/test_trend_following_short_strangle.py`
- Native persistence tests: 3/5 pass, 2/5 fail, 2/5 fail even when 50-scan broad bucket is bullish.
- Delta-band selection, one-bucket ATR tightening, same-strike block, worsening-delta add block.

### 4.14 `tests/unit/engine/test_tfss_core_mapping.py`
- `GO_LONG`, `Long Buildup`, `Short Covering`, `Put Writing`, `OI Bias Bullish` route to TFSS bullish execution and can only emit `SELL PE` or block.
- `GO_SHORT`, `Short Buildup`, `Long Unwinding`, `Call Writing`, `OI Bias Bearish` route to TFSS bearish execution and can only emit `SELL CE` or block.
- Timeframe path remains unchanged and still uses its legacy execution behavior.

### 4.15 `tests/unit/engine/test_tfss_reversal_and_exit_priority.py`
- Reversal sequencing: tested-side reduction/close precedes combined-cap recheck precedes reversal-side open/add.
- Exit priority ordering with multiple simultaneous triggers.

---

## 5. Regression Guard Checklist

Do **NOT** modify or disrupt the following:

- Timeframe strategy execution path, mappings, step ordering, or semantics.
- Existing non-Core strategy behaviors unrelated to TFSS routing.
- NATURALGAS-specific risk behavior, generic `_check_risk_limits_for_table()` checks, or table allowlist assert.
- `check_trend_persistence()` semantics — TFSS may call it only as optional secondary corroboration.
- `calculate_momentum_score()` recent-agreement scoring block — mirror its classification/query style if needed, but do not repurpose it as the AC-011 gate.
- Fetchers, anomaly generation, LLM enrichment, or live broker execution.
- DTE delta-band permissiveness — ATR tightens only, by at most one bucket.
- Same-strike averaging default-block, worsening-delta add-block, reversal-before-reduction ordering.
- Profit-taking priority — never overrides a higher-priority risk stop in the same cycle.
- Schema — no migration unless string-key encoding is proven insufficient.

---

## 6. Canonical v4 Rule Summary

For implementation purposes, treat the following as the canonical execution policy:

- **Core does not directly buy CE/PE anymore** for bullish/bearish qualifying verdicts.
- **Core does not directly short PE/CE anymore** through ad hoc OI-bias mappings.
- **All qualifying Core bullish/bearish option expressions must pass through TFSS.**
- **TFSS bullish expression = short PE governance.**
- **TFSS bearish expression = short CE governance.**
- **Timeframe remains unchanged as a parallel system.**
- **AC-011 is enforced only by native `compute_persisted_trend()` using `>=3 of last 5`, not by `check_trend_persistence()`.**
