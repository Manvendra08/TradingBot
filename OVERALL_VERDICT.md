# OVERALL VERDICT: Technical Audit & Live-Broker Readiness Plan

## Executive Summary

**STATUS: HIGH RISK - DO NOT ENABLE LIVE BROKERAGE**

The trading bot is currently not safe for live brokerage integration. While protective settings (shadow mode, trading paused) are active in the configuration, the underlying architecture contains critical vulnerabilities that could lead to unauthorized or unmanaged real-money trades.

The primary failure mode is a **distributed control architecture**: safety rules, LLM policies, and broker gates are implemented in fragments across multiple modules. This allows specific execution paths (especially in the MULTILEG strategy) to bypass the centralized safety checks that the rest of the system relies on.

---

## Severity-Ranked Findings

### CRITICAL

#### 1. Multi-leg execution bypasses centralized broker safety gates
*   **Mechanism:** While single-leg paths respect `live_shadow_mode` and `trading_paused`, the `multileg_live_trading.py` paths (both entry and exit) do not uniformly enforce these central policies.
*   **Risk:** Real orders could be placed while the operator believes the system is in shadow mode, or during an emergency trading pause.

#### 2. Asynchronous LLM enrichment creates "Ghost Authorization"
*   **Mechanism:** `llm_enrichment_async` allows strategy execution to proceed before the LLM verdict is available. The LLM result is later used to "edit" the digest.
*   **Risk:** The user sees an AI verdict in Telegram that appears to have authorized a trade, but the trade was actually executed based on deterministic logic *before* the AI ever spoke. This destroys the audit trail.

#### 3. MULTILEG strategy bypasses engine-alignment guards
*   **Mechanism:** The single-leg path uses `_enforce_engine_alignment` to prevent the LLM from flipping direction. The MULTILEG path does not.
*   **Risk:** The most complex and capital-intensive strategy (MULTILEG) is the one with the least protection against model hallucination or directional error.

#### 4. Tolerant JSON parsing allows "Repaired" execution decisions
*   **Mechanism:** The parser uses aggressive repair loops (fixing quotes, repairing truncated JSON, unwrapping arrays).
*   **Risk:** A broken or partial LLM response can be "fixed" into a syntactically valid but semantically nonsensical trade plan, which is then executed.

#### 5. Failed multi-leg exits cause database/broker divergence
*   **Mechanism:** If a broker order for one leg of a multi-leg trade fails, the system continues to close the remaining legs and marks the entire book as `CLOSED` in the database.
*   **Risk:** The system loses track of "orphan" positions, leaving real money at risk in the market without any active monitoring.

### HIGH

*   **Configuration Inconsistency:** Defaults in code differ from persisted `runtime_config.json`, and configuration validation is weak.
*   **Non-Deterministic Fallbacks:** The LLM provider fallback policy accepts the "first parseable response," meaning different models can lead to wildly different trading behaviors in the same market condition.
*   **Narrow Cache Identity:** The verdict cache is keyed too narrowly, risking the reuse of stale trade plans when context (expiry, liquidity, news) has changed.
*   **Context Divergence:** Discrepancies exist between the option chains used for signal detection, execution, and persistence (especially on MCX expiry days).

---

## Architecture Diagnosis

The system suffers from **Layered Authority Conflict**. 

The "Engine" layer (deterministic) and the "LLM" layer (probabilistic) are meant to be decoupled, but the implementation allows the LLM to leak into the execution path in uncontrolled ways. The lack of an **Immutable Scan Snapshot** means that as a scan progresses from "Fetch" to "Detect" to "Execute," the underlying data can be mutated or replaced, leading to decisions made on data that no longer matches the record.

---

## Strategic Improvement Plan (Ordered)

### Phase 1: Unified Authorization (Immediate)
1.  **Centralize the Broker Gate:** Implement a single `ExecutionAuthorization` object. Every single path that calls a broker (entry, exit, adjustment, GTT, reconciliation) MUST request and receive this object.
2.  **Fail-Closed Config:** Rewrite the configuration loader to treat any parse error or missing safety key as `broker_disabled=True` and `trading_paused=True`.

### Phase 2: Data Integrity & Immutability (Next Sprint)
1.  **Introduce ScanSnapshots:** Create an immutable `ScanSnapshot` object. All components (LLM, Strategy, Persistence, UI) must reference a single Snapshot ID.
2.  **Strict Execution Parser:** Create a dedicated "Execution Parser" that rejects any repaired, truncated, or ambiguous JSON. If it isn't perfect, it's a `DATA_INTEGRITY_FAILURE`.

### Phase 3: Strategy Alignment (Next Sprint)
1.  **Enforce Multi-leg Alignment:** Apply the single-leg direction-alignment guard to the MULTILEG engine.
2.  **Formalize AI Modes:** Clearly distinguish between `ADVISORY` (asynchronous, non-gating) and `AUTONOMOUS` (synchronous, gating) modes.

### Phase 4: Robustness & Audit (Long-term)
1.  **Broker State Machine:** Implement a proper state machine for legs (`OPEN` -> `EXIT_PENDING` -> `EXIT_FILLED`) to prevent database/broker divergence.
2.  **Deterministic Provider Policy:** Replace "first-success" fallback with a defined, auditable provider ladder.
3.  **Adversarial Testing:** Build a test suite that specifically injects malformed JSON, stale quotes, and provider timeouts.

## Readiness Gate for Live Trading

**Do not enable live trading until:**
*   [ ] Every order path passes the centralized authorization check.
*   [ ] Zero "repaired" JSON objects are used for execution.
*   [ ] Multi-leg exits are confirmed via broker status before DB closure.
*   [ ] A complete "Audit Trace" can be generated for any trade, linking the specific Snapshot ID to the specific LLM response and the specific Broker Order ID.
