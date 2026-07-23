# NSEBOT — Audit Re-Validation Report (Non-Prod)

**Re-validation date:** 2026-07-22
**Source audit:** `CODE_AUDIT_REPORT_2026-07-22.md`
**Scope:** Re-inspected current source under `src/` and `config/` to verify which of the 17 reported bugs are actually fixed in code, and what remains.
**Verdict:** **15 of 17 FIXED · 2 still open** (1 HIGH, 1 MEDIUM). Neither open item blocks non-prod/paper operation.

---

## ✅ FIXED — Verified in current code (15)

### CRITICAL
| ID | File | Evidence of fix |
|----|------|-----------------|
| **BUG-001** | `src/engine/paper_trading.py` | `_is_reversal_against_open_trade()` now enforces **3 guards** before closing/flipping: (1) `confidence >= REVERSAL_MIN_CONFIDENCE` (75), (2) `entry_quality >= MIN_ENTRY_QUALITY_CORE` (60), (3) `trend_alignment <= 40`. Live path aligned too (`live_trading.py` "Fix C1"). |
| **BUG-002** | `src/engine/pipeline.py` | `run_pipeline()` wraps `_process_prefetched_symbol()` in `try/except` → `log.exception("Unhandled pipeline error for %s")` + stamps health `DOWN`. One symbol no longer kills the cycle. |
| **BUG-003** | `src/models/schema.py` | `close_paper_trade()` & `close_live_trade()` now `SELECT ... lot_size` and use the **stored** `lot_size` with `LOT_SIZES[base_symbol]` fallback (P0-05 base-symbol extraction). |

### HIGH
| ID | File | Evidence of fix |
|----|------|-----------------|
| **BUG-004** | `src/models/schema.py` | `insert_paper_trade()` now includes `lot_size` in the INSERT column list and `row_data`, defaulting to `LOT_SIZES.get(symbol, 1)`. |
| **BUG-005** | `src/engine/live_trading.py` | `place_kite_order()` is wrapped in `try/except` (IP-error helper + log + re-raise); callers catch it → `update_live_trade_entry(... REJECTED)` → return `BLOCKED_ORDER_FAILED`. Pipeline no longer crashes. |
| **BUG-006** | `src/models/schema.py` | Futures P&L direction now uses **only `side`** (explicit `# BUG-006 FIX` comment); removed the `verdict_label`/`is_bearish` OR-chain that could invert P&L. |
| **BUG-007** | `src/models/schema.py` | `get_prev_snapshots_bulk()` now accepts `fetched_at`, adds a **staleness guard** (NSE 4h / MCX 6h cap) and an **IST cross-session day check** → returns empty baseline instead of stale OI. |
| **BUG-009** | `src/engine/trade_decision.py` | `_extract_ai_bias()` uses `getattr(...) or (dict.get if isinstance(dict))` for both `action` and `bias` — handles dataclass **and** dict safely. |
| **BUG-010** | `src/engine/decision_pipeline.py` | Confidence read as `int(intel.get("confidence") or 0)` throughout the pipeline; `None` no longer raises `TypeError`. |
| **BUG-011** | `src/engine/llm_enrichment.py` | Tolerant `_extract_json()` (strips markdown fences, extracts `{...}`, removes control chars); `_call_llm_api`/`get_llm_verdict` catch `ValueError`+`Exception` → return `None`. No unhandled JSON crash. |
| **BUG-012** | `src/engine/decision_pipeline.py` | Inverted `_matches_direction` removed. Now `verdict_bias = "BULLISH" if direction=="LONG" else "BEARISH"; ai_agrees = (ai_bias == verdict_bias)` — correct alignment. |

### MEDIUM
| ID | File | Evidence of fix |
|----|------|-----------------|
| **BUG-013** | `src/engine/paper_trading.py` | RSI capture uses explicit `if raw_rsi_1h is not None` instead of truthy `or None`; RSI=0.0 no longer collapses to `None`. |
| **BUG-015** | `src/engine/telegram_formatter.py` | `_format_forces()` uses `intel.get("bull_forces") or []` then iterates tuples — no `.get()` on a list. (File is also marked **DEPRECATED**; prod uses `digest.build_llm_consolidated_digest`.) |
| **BUG-016** | `src/fetchers/router.py` | `fetch_option_chain()` now does dual-source parallel fetch + merge + sequential fallback, and on total failure logs `log.error("❌ ALL fetchers failed")` + stamps health `DOWN`. No longer silent. |
| **BUG-017** | `src/engine/live_trading.py` | After order placement, `update_live_trade_entry(inserted_id, broker_order_id=order_id, gtt_order_id=..., broker_status=..., exit_mode=...)` persists the order ID for reconciliation. |

---

## ❌ STILL OPEN (2)

### BUG-008 — HIGH — Runtime position limit not enforced
**File:** `src/engine/risk_engine.py`
**Status:** The Cockpit-settable runtime value `live_max_concurrent_positions` (default 2, in `config/runtime_config.py`) and `live_max_daily_loss_rupees` (default 200000) are **never read** by the risk engine. `_check_risk_limits_for_table()` only checks `MAX_OPEN_TRADES_TOTAL` and `MAX_DAILY_LOSS_RUPEES` imported from `config.settings`. There is no `load_runtime_config()` call in the risk path.
**Impact (non-prod):** User changes to the position/loss cap in the dashboard are silently ignored; only the hardcoded `settings.py` caps apply. Contained in paper/shadow mode, but the control is misleading.
**Recommended fix:** In `_check_risk_limits_for_table()`, read `rconf = load_runtime_config()` and enforce `min(MAX_OPEN_TRADES_TOTAL, rconf["live_max_concurrent_positions"])` for live tables (and the paper equivalent), plus `rconf["live_max_daily_loss_rupees"]` for the daily-loss cap.

### BUG-014 — MEDIUM — Live ML prediction still fed `None` features (partial fix)
**File:** `src/engine/pipeline.py` (`_process_prefetched_symbol`)
**Status:** The Phase-0 `_build_ml_feature_snapshot()` (paper_trading.py) **does** correctly compute and persist `days_to_expiry`, `rsi_1h`, `rsi_3h` into the trade row at open time — so **training data is now correct**. However, the **live** `get_predictor().predict({...})` call in `pipeline.py` still hardcodes `"days_to_expiry": None, "rsi_1h": None, "rsi_3h": None`.
**Impact (non-prod):** Real-time ML `success_probability` is computed with 3 missing features → degraded/conflicting signal vs. the stored features. Low impact while `ml_predictor_mode = "shadow"` (default).
**Recommended fix:** Build the feature dict once (reuse `_build_ml_feature_snapshot(scan_context, intel)`) and pass the same populated dict to `get_predictor().predict()` instead of hardcoded `None`.

---

## Bottom line for non-prod sign-off
- **All 3 CRITICAL and 9 of 10 HIGH bugs are fixed and verified.**
- The single remaining HIGH (**BUG-008**) is a config-enforcement gap, not a crash or data-corruption risk — safe to defer in non-prod but should be fixed before relying on dashboard risk caps.
- The remaining MEDIUM (**BUG-014**) only affects shadow-mode ML scoring — negligible for non-prod.
- **Recommendation:** Non-prod is in a runnable state. Schedule BUG-008 (and BUG-014) in the next fix pass before any live-capital enablement.
