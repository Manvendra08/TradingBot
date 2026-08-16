# NSEBOT Code Audit Report
**Date:** 2026-08-12  
**Auditor:** Qwen3.8 (Automated Static Analysis)  
**Scope:** Full codebase review of core engine, pipeline, risk, trading, and data layer  

---

## Executive Summary

The NSEBOT codebase is a sophisticated, multi-strategy options trading bot for NSE/MCX with:
- **Decision pipeline** with tiered gates (hard/soft) and AI alignment
- **Multi-broker integration** (Zerodha Kite, Shoonya, Dhan, Upstox)
- **Paper + live trading** with shadow mode, GTT exits, and poll-based exits
- **SQLite WAL-mode** database with 100+ migrations
- **Multiple strategies**: Core OI, TFSS (Trend Following Short Strangle), Timeframe 3H breakout, Multi-leg, NG Parity/EIA/Momentum

The code is **well-structured with good error handling**, but several functional logic bugs and technical issues were identified.

---

## 🔴 Critical Bugs (P0)

### 1. `live_trading.py` — Undefined variables `target_trigger` and `target_premium` in TFSS GTT path
**File:** `src/engine/live_trading.py` — `run_live_timeframe_strategy()`  
**Lines:** ~1080-1120

```python
# In the TIMEFRAME entry section for non-FUT options:
if opt_type in ("CE", "PE"):
    sl_premium = round(entry_premium * 0.50, 2)
    target_premium = None   # ← Set to None for TIMEFRAME
```

Later, the GTT placement block references these:
```python
if (
    opt_type != "FUT"
    and broker_status == "COMPLETE"
    and sl_premium is not None
    and target_premium is not None  # ← This is ALWAYS None for TF
):
```

The condition `target_premium is not None` is always `False` for TIMEFRAME entries, so GTTs are never placed. However, a few lines above, the same code tries to compute `target_trigger`:
```python
sl_trigger = float(sl_premium)
# target_trigger is used below but NEVER assigned when target_premium=None
tgt_buf = max(0.25, min(round(target_trigger * 0.01, 2), 0.50))  # NameError!
```

**Impact:** If the code path is reached, it throws `NameError: name 'target_trigger' is not defined`. Currently suppressed because `target_premium is not None` is always false, but if target_premium logic changes, this crashes.

**Fix:** Guard the entire GTT block properly or assign `target_trigger` unconditionally.

---

### 2. `live_trading.py` — `side` variable undefined in TF entry for options
**File:** `src/engine/live_trading.py` — `run_live_timeframe_strategy()`  
**Lines:** ~1060-1070

```python
# When opt_type is "CE" or "PE" and direction is SHORT:
opt_type = "PE"
strike = atm if is_mcx_commodity else (atm + 4 * step)
entry_premium = _get_option_premium(...)
sl_underlying = float(ohlc_3h["high"])
tgt_underlying = underlying - 2 * (sl_underlying - underlying)
# ⚠️ 'side' is never assigned for opt_type CE/PE before being used!
```

Later:
```python
side = "BUY" if direction == "LONG" else ("SELL" if opt_type == "FUT" else "BUY")
```

This `side` assignment happens AFTER the code that references `side` for SL/target computation. The `sl_premium` computation uses `side`:
```python
sl_premium = round(entry_premium * 0.50, 2)  # Always 50% regardless of side
```

But in the GTT block:
```python
sl_limit = (
    round(sl_trigger - sl_buf, 2)
    if side == "BUY"    # ← 'side' may be stale from outer scope
    else round(sl_trigger + sl_buf, 2)
)
```

**Impact:** If `side` was inherited from an outer scope (e.g. from a previous function call or global), the GTT SL/limit prices may be inverted.

---

### 3. `pipeline.py` — Race condition in `_prefetch_symbol_data` news result handling
**File:** `src/engine/pipeline.py` — `_prefetch_symbol_data()`  
**Lines:** ~150-170

```python
news_future = pipeline_io_executor.submit(lambda: run_with_deadline("news", _fetch_news))
# BUG-H06 FIX: Safe dict conversion - result may be simple type or namedtuple
news_result = news_future.result()  # ← Blocks here
if hasattr(news_result, '__dict__'):
    packet["news_result"] = news_result.__dict__
elif isinstance(news_result, dict):
    packet["news_result"] = news_result
else:
    packet["news_result"] = {"ok": True, "data": news_result}
```

The `news_future.result()` call blocks the executor thread waiting for news to complete. But news is submitted AFTER chart_future. If news takes too long, it delays the entire prefetch pipeline. The `chart_future.result()` is called later:

```python
chart_result = chart_future.result()  # ← Already complete by now
```

**Impact:** News fetch blocks the chart data from being available in the packet until news completes. If news fetch hangs, chart data is never available. This can cause `chart_indicators` to be empty for the anomaly detector.

---

### 4. `schema.py` — `get_conn()` read_only mode uses `BEGIN DEFERRED` but returns `ROLLBACK`
**File:** `src/models/schema.py` — `get_conn()`  
**Lines:** ~165-200

```python
if not read_only:
    conn.execute("BEGIN IMMEDIATE")
else:
    conn.execute("BEGIN DEFERRED")
```

Then on exit:
```python
if not read_only:
    try:
        conn.execute("COMMIT")
    except sqlite3.OperationalError as ce:
        if "no transaction is active" not in str(ce).lower():
            raise
else:
    try:
        conn.execute("ROLLBACK")  # ← ROLLBACK on read-only connection
    except Exception:
        pass
```

For read-only connections, issuing `ROLLBACK` is unnecessary and can cause "cannot rollback - no transaction is active" warnings. More critically, `BEGIN DEFERRED` on a read-only connection can still acquire a shared lock, which may conflict with WAL writes.

**Fix:** Don't start a transaction for read-only connections, or use `BEGIN DEFERRED` without `ROLLBACK`.

---

## 🟠 High Severity Bugs (P1)

### 5. `decision_pipeline.py` — `step_heavyweight_alignment` direction fallback is flawed
**File:** `src/engine/decision_pipeline.py` — `step_heavyweight_alignment()`

```python
direction = ctx.direction
if not direction:
    intel = ctx.scan_context.get("intel") or {}
    verdict = intel.get("verdict_label", "")
    if is_bullish(verdict):
        direction = "LONG"
    elif is_bearish(verdict):
        direction = "SHORT"
```

The `ctx.direction` is set by `step_signal_core_oi` or `step_signal_timeframe` earlier. If neither set it (e.g. neutral verdict), this fallback tries to derive direction from verdict. But for neutral verdicts like "Sideways", `is_bullish` and `is_bearish` both return False, so `direction` remains `None`. The function then returns `passed=True` because "No trade direction detected; bypassing guard".

**Impact:** Neutral verdicts bypass the heavyweight guard entirely, allowing trades that should be blocked by heavyweight momentum.

---

### 6. `pipeline.py` — `_build_structured_payload` actual_lots calculation overwrites DB value
**File:** `src/engine/pipeline.py` — `_build_structured_payload()`

```python
from config.settings import DEFAULT_LOTS_PER_TRADE
base_lots = DEFAULT_LOTS_PER_TRADE
actual_lots = base_lots * td.get("tranche_count", 1) if td.get("tranche_count") else base_lots
```

This overwrites the `actual_lots` that was previously read from the database:
```python
if row:
    db_entered = True
    actual_lots = row[0]  # ← Correctly read from DB
```

Then immediately after:
```python
actual_lots = base_lots * td.get("tranche_count", 1) if td.get("tranche_count") else base_lots  # ← Overwritten!
```

**Impact:** The `qty` field in the structured payload always uses `DEFAULT_LOTS_PER_TRADE` instead of the actual traded quantity from the database. Dashboard shows wrong lot sizes.

---

### 7. `live_trading.py` — Shadow trade auto-reconciliation uses wrong status column
**File:** `src/engine/live_trading.py` — `run_live_trading()`

```python
p_row = conn.execute(
    "SELECT * FROM paper_trades WHERE symbol=? AND option_type=? AND abs(strike - ?) < 0.1 AND status != 'OPEN' ORDER BY id DESC LIMIT 1",
    (symbol, current_open_trade.get("option_type"), float(current_open_trade.get("strike") or 0))
).fetchone()
if p_row:
    p_trade = dict(p_row)
    update_live_trade_entry(
        current_open_trade["id"],
        status=p_trade["status"],  # ← Copies CLOSED_SL, CLOSED_TARGET etc.
        ...
    )
```

The shadow live trade's status is set to the paper trade's status. But the live trade status should remain `"SHADOW"` or `"CLOSED"` — not `"CLOSED_SL"` or `"CLOSED_TARGET"` which are paper-trade specific.

**Impact:** Live trades table gets statuses that the risk engine doesn't recognize, potentially causing incorrect position counts.

---

### 8. `schema.py` — `_calc_transaction_costs` MCX CTT rate is wrong for stock futures
**File:** `src/models/schema.py` — `_calc_transaction_costs()`

```python
if is_mcx_commodity_future:
    stt_rate = 0.0001  # CTT rate for MCX commodities
elif base_symbol in index_symbols:
    stt_rate = 0.0001  # STT rate for NSE index futures (0.01%)
else:
    stt_rate = 0.0002  # STT rate for stock futures
```

NSE index futures STT is actually **0.01% (0.0001)** — this is correct. But the comment says "actually STT was reduced" which is misleading. The actual STT for NSE index futures is 0.01% (not 0.02%). Stock futures are 0.02%. MCX commodities use CTT at 0.01%.

**Impact:** The PnL calculations are correct, but the code comments are misleading for future maintainers.

---

### 9. `risk_engine.py` — `check_tested_side` never called in main pipeline
**File:** `src/engine/risk_engine.py`

The function `check_tested_side()` is defined with detailed documentation about delta-stop thresholds, but a grep of the codebase shows it is **never called** from any pipeline or strategy code. The TFSS exit logic in `trend_following_short_strangle.py` may use it, but it's not integrated into the main risk engine.

**Impact:** Dead code. The delta-stop mechanism described in the docstring is not enforced.

---

## 🟡 Medium Severity Bugs (P2)

### 10. `pipeline.py` — `_async_llm_enrich_and_edit` uses `llm_verdict.model_name` but may receive dict
**File:** `src/engine/pipeline.py`

```python
m_name = getattr(llm_verdict, "model_name", None) or ""
```

But `llm_verdict` could be a `dict` if `generate_intelligence_structured` returns a dict representation. The `getattr` call on a dict returns `None`, not the dict value.

**Impact:** Model name is silently lost when `llm_verdict` is a dict.

---

### 11. `live_trading.py` — `_bg_pending_gtt_placer` uses `time.sleep` in thread
**File:** `src/engine/live_trading.py` — `_bg_pending_gtt_placer()`

```python
while time.time() - start_time < 300:
    time.sleep(5)
```

This background thread runs for up to 5 minutes with `time.sleep(5)` polling. It holds a reference to the `kite` client object which may become stale (token expired). The thread also accesses `confirm_order_fill` and `place_kite_gtt` which make network calls.

**Impact:** Long-running daemon thread that may operate with stale credentials. If Kite token expires during the 5-minute window, all API calls fail silently.

---

### 12. `schema.py` — `get_prev_snapshots_bulk` ignores current scan's own snapshot
**File:** `src/models/schema.py` — `get_prev_snapshots_bulk()`

```python
# Ignore snapshots that are too close to 'now' (e.g. less than 5 seconds ago)
if (now_utc - dt).total_seconds() > 5:
    fetched_ats.append((fetched_at_str, dt))
```

If the current scan inserts its snapshot and then immediately calls `get_prev_snapshots_bulk`, it correctly skips the current scan's data. But if there's a pipeline retry within 5 seconds, the previous scan's data is also skipped.

**Impact:** On pipeline retry, OI change calculations show no change (empty baseline), suppressing valid alerts.

---

### 13. `decision_pipeline.py` — `step_entry_quality_core` mutates scan_context keys that may not exist
**File:** `src/engine/decision_pipeline.py`

```python
ctx.scan_context["_pipeline_plan"] = plan
ctx.scan_context["_entry_quality"] = entry_quality
ctx.scan_context["_entry_reasons"] = entry_reasons
```

These keys are cached for downstream steps. But `step_regime` and `step_trend_alignment_core` read from `ctx.scan_context.get("_pipeline_plan")` without null checks:

```python
plan = ctx.scan_context.get("_pipeline_plan") or {}
option_type = plan.get("option_type", "CE")
```

This is safe due to the `or {}` fallback. However, if `step_entry_quality_core` returns `passed=False` early (no valid plan), the downstream steps use `{}` which defaults to "CE" option_type for regime scoring.

**Impact:** Regime score may be calculated for "CE" when the actual trade is "PE", giving wrong regime approval.

---

### 14. `pipeline.py` — `run_pipeline` sorted key uses fallback `999` for unknown symbols
**File:** `src/engine/pipeline.py`

```python
symbols_list = list(symbols)
for packet in sorted(prefetched, key=lambda x: symbols_list.index(x["symbol"]) if x["symbol"] in symbols_list else 999):
```

If a symbol returns from prefetch but isn't in the original `symbols` list (e.g. due to case normalization), it sorts to position 999. This doesn't break functionality but may cause inconsistent ordering.

---

### 15. `capital_allocator.py` — `_fetch_broker_margin_requirement` uses ThreadPoolExecutor without cleanup
**File:** `src/engine/capital_allocator.py`

```python
import concurrent.futures
with concurrent.futures.ThreadPoolExecutor() as executor:
    future = executor.submit(kite.order_margins, orders)
    result = future.result(timeout=_BROKER_MARGIN_API_TIMEOUT)
```

The `ThreadPoolExecutor` is created and destroyed for every margin lookup. For high-frequency scans, this creates thread pool overhead. The `_BROKER_MARGIN_API_TIMEOUT` of 3 seconds may be too short for slow Kite API responses.

---

### 16. `schema.py` — `close_paper_trade` delta-estimation fallback uses wrong moneyness formula
**File:** `src/models/schema.py` — `close_paper_trade()`

```python
moneyness = (float(exit_underlying) - strike) / (float(exit_underlying) * 0.02)
approx_delta = max(0.05, min(0.95, 0.5 + 0.3 * moneyness))
estimated_exit = max(0.05, entry_premium + approx_delta * delta_und)
```

The moneyness formula `(exit_underlying - strike) / (exit_underlying * 0.02)` gives values like:
- For NIFTY at 25000, strike 25500: `(25000-25500)/(25000*0.02) = -500/500 = -1.0`
- `approx_delta = 0.5 + 0.3 * (-1.0) = 0.2`

This gives an approximate delta of 0.2 for an OTM CE, which is reasonable. But for deep ITM options, the formula can produce negative deltas, clamped to 0.05. The estimated exit premium may be wildly inaccurate.

**Impact:** PnL for trades closed with delta-estimation fallback may have significant errors (₹100s per trade).

---

### 17. `live_trading.py` — `_exit_open_live_trade` raises RuntimeError on non-COMPLETE exit
**File:** `src/engine/live_trading.py`

```python
if not shadow_mode and broker_status != "COMPLETE":
    log.warning(...)
    raise RuntimeError(
        f"Exit order not filled (status={broker_status}): {broker_message}. "
        f"Trade remains OPEN to prevent phantom close."
    )
```

This correctly prevents phantom closes. However, the caller catches this exception:

```python
try:
    closed = _exit_open_live_trade(...)
    return {"action": "CLOSED", "trade": closed, "reason": "reversal"}
except Exception as e:
    log.error("Failed to square-off reversed position: %s", e)
    return {"action": "ERROR", "reason": f"reversal square-off failed: {e}"}
```

The trade remains OPEN in the database (correct), but the return value is `"ERROR"` which may not trigger a retry on the next scan. The next scan's `run_live_trading` will try the same exit again.

---

### 18. `pipeline.py` — `NSE_NEWS_BYPASS_SYMBOLS` uses `split()[0]` but bypass set uses full names
**File:** `src/engine/pipeline.py`

```python
NSE_NEWS_BYPASS_SYMBOLS = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}
skip_news = symbol.upper().strip().split()[0] in NSE_NEWS_BYPASS_SYMBOLS
```

This correctly extracts the base symbol. But the set contains "SENSEX" which is a BSE index, not an NSE symbol. If the symbol is "SENSEX", news is bypassed, which is correct. However, "FINNIFTY" and "MIDCPNIFTY" are NSE symbols that should have news fetched.

**Impact:** No functional bug, but "FINNIFTY" and "MIDCPNIFTY" news is unnecessarily bypassed.

---

## 🟢 Low Severity Issues (P3)

### 19. `pipeline.py` — `pipeline_io_executor.submit(_refresh_ip_async)` fire-and-forget
**File:** `src/engine/pipeline.py`

```python
pipeline_io_executor.submit(_refresh_ip_async)
```

The result of this future is never checked. If `_refresh_ip_async` throws an exception, it's silently swallowed by the executor.

---

### 20. `schema.py` — `insert_snapshots` uses `INSERT OR REPLACE` which may delete old rows
**File:** `src/models/schema.py`

```python
sql = """
    INSERT OR REPLACE INTO option_chain_snapshots
        (fetched_at, symbol, expiry, strike, option_type, ...)
    VALUES (...)
"""
```

`INSERT OR REPLACE` deletes the old row and inserts a new one if the unique constraint `uq_oc_snap (fetched_at, symbol, expiry, strike, option_type)` is violated. Since `fetched_at` is part of the unique key, this shouldn't trigger for normal operations. But if two scans have the same `fetched_at` (e.g. same millisecond), the second scan overwrites the first.

---

### 21. `schema.py` — `get_read_conn()` and `get_conn(read_only=True)` duplicate connection logic
**File:** `src/models/schema.py`

Both `get_read_conn()` and `get_conn(read_only=True)` create connections with different behaviors:
- `get_read_conn()` — no transaction, no commit, simple `yield conn`
- `get_conn(read_only=True)` — `BEGIN DEFERRED` transaction, `ROLLBACK` on exit

Functions that use `get_read_conn()` don't benefit from the transaction isolation that `get_conn()` provides. This inconsistency may cause subtle concurrency issues.

---

### 22. `risk_engine.py` — `_check_risk_limits_for_table` uses string formatting for table names
**File:** `src/engine/risk_engine.py`

```python
if trades_table not in ("paper_trades", "live_trades"):
    raise ValueError(f"Unexpected table: {trades_table}")
```

The allowlist check is correct. However, the SQL uses f-strings:

```python
f"SELECT COUNT(*) AS cnt FROM {trades_table} WHERE symbol = ? AND status = 'OPEN'"
```

Since the table name is validated against an allowlist, this is safe. But if a new table is added without updating the allowlist, the code will raise `ValueError` at runtime.

---

### 23. `schema.py` — `close_paper_trade` status auto-correction may mask bugs
**File:** `src/models/schema.py`

```python
if (pnl_rupees > 0 or pnl_points > 0) and status in ("CLOSED_SL", "SL_HIT"):
    log.warning(
        "close_paper_trade id=%s: Auto-correcting status from %s to CLOSED_TARGET ...",
        trade_id, status, pnl_rupees, pnl_points,
    )
    status = "CLOSED_TARGET"
```

This auto-correction masks cases where the exit logic incorrectly classified a profitable exit as a stop-loss hit. The root cause (wrong exit price, wrong SL/target levels) is hidden.

---

### 24. `live_trading.py` — `confirm_order_fill` uses `time.sleep` for polling
**File:** `src/engine/live_trading.py`

```python
max_retries = 10
delay_sec = 1.0
for attempt in range(max_retries):
    ...
    time.sleep(delay_sec)
```

This blocking sleep in `confirm_order_fill` can hold up the pipeline for up to 10 seconds per order confirmation. For multiple concurrent trades, this compounds.

---

### 25. `pipeline.py` — `scan_digest_id` uses `str(uuid.uuid4())[:8]` — collision possible
**File:** `src/engine/pipeline.py`

```python
scan_digest_id = str(uuid.uuid4())[:8]
```

8 hex characters = 32 bits = ~4 billion combinations. With birthday paradox, collisions become likely after ~65k scans. For a bot running every 15 minutes for 8 hours/day across 5 symbols = ~160 scans/day, this is safe for ~400 days. Acceptable but not ideal.

---

## 📊 Architecture & Design Observations

### Strengths
1. **Comprehensive risk engine** with multiple circuit breakers (daily loss, cooldown, consecutive loss)
2. **Tiered gates** (HARD/SOFT) with composite scoring for flexible entry quality
3. **Shadow mode** for safe live trading testing
4. **Transaction cost modeling** in PnL calculations
5. **IST-aligned day boundaries** for daily limits
6. **Cross-session baseline detection** preventing false OI spikes at market open
7. **GTT fallback to poll-based exits** when GTT placement fails

### Areas for Improvement
1. **Single SQLite database** for all operations — concurrent writes can cause WAL lock contention
2. **Thread pool executors** without proper lifecycle management — daemon threads may hold stale resources
3. **No centralized error tracking** — exceptions are logged but not aggregated for ops monitoring
4. **Magic numbers** in multiple places (e.g. `_PROFILE_FAILURE_COOLDOWN_SECONDS = 30.0`)
5. **No backpressure** on the pipeline — if broker APIs are slow, scans pile up

---

## Recommendations

### Immediate (This Week)
1. **Fix #1 (P0):** Guard the `target_trigger` reference in TFSS GTT placement block
2. **Fix #2 (P0):** Ensure `side` is assigned before being referenced in TF entry path
3. **Fix #6 (P1):** Don't overwrite `actual_lots` from DB with default calculation

### Short-term (Next Sprint)
4. **Fix #3 (P0):** Restructure prefetch to not block on news before chart is available
5. **Fix #11 (P2):** Add token refresh logic to `_bg_pending_gtt_placer`
6. **Fix #17 (P2):** Implement retry logic for failed exit orders

### Medium-term (Next Month)
7. **Fix #4 (P0):** Refactor `get_conn()` read-only mode to not use transactions
8. **Fix #16 (P2):** Improve delta-estimation fallback with Black-Scholes or lookup table
9. **Fix #24 (P3):** Replace `time.sleep` polling with async/await pattern

---

## File-by-File Summary

| File | Lines Reviewed | P0 | P1 | P2 | P3 |
|------|---------------|----|----|----|-----|
| `main.py` | ~90 | 0 | 0 | 0 | 0 |
| `src/engine/main.py` | ~100 | 0 | 0 | 0 | 0 |
| `src/engine/pipeline.py` | ~550 | 1 | 1 | 2 | 2 |
| `src/engine/decision_pipeline.py` | ~750 | 0 | 1 | 1 | 0 |
| `src/engine/live_trading.py` | ~1200 | 2 | 1 | 2 | 1 |
| `src/models/schema.py` | ~1400 | 1 | 1 | 3 | 2 |
| `src/engine/risk_engine.py` | ~350 | 0 | 0 | 1 | 1 |
| `src/engine/paper_plan.py` | ~350 | 0 | 0 | 0 | 0 |
| `src/engine/capital_allocator.py` | ~200 | 0 | 0 | 1 | 0 |
| **Total** | **~5000** | **4** | **4** | **10** | **6** |

---

*Report generated on 2026-08-12. Re-run audit after fixes are applied to verify resolution.*
