# TradingBot — Consolidated Code Audit Report (Pass 1 + Pass 2)
**Repository:** https://github.com/Manvendra08/TradingBot  
**Audit Date:** 2026-07-07  
**Auditor:** Perplexity AI  
**Total Issues Found:** 55 (24 Pass 1 · 31 Pass 2)  
**Severity Scale:** 🔴 Critical · 🟠 High · 🟡 Medium · 🟢 Low

---

## Quick Stats

| Pass | Files Reviewed | Critical | High | Medium | Low | Total |
|------|---------------|----------|------|--------|-----|-------|
| Pass 1 | `live_trading.py`, `risk_engine.py`, `capital_allocator.py`, `time_guards.py`, `ng_risk_manager.py`, `trade_plan.py` | 4 | 5 | 10 | 5 | 24 |
| Pass 2 | Above files (line-by-line) + `decision_pipeline.py` (new) | 6 | 8 | 10 | 7 | 31 |
| **Total** | **7 files** | **10** | **13** | **20** | **12** | **55** |

---

## Priority Fix Order (All Issues, Ranked)

| Rank | ID | File | Severity | Description |
|------|----|------|----------|-------------|
| 1 | XBUG-001 | Cross-file | 🔴 | `risk_engine.check_live_risk_limits` never called from `run_live_trading` — entire risk engine bypassed live |
| 2 | BUG-001 | live_trading.py | 🔴 | SELL slippage buffer identical to BUY — SELL limit order placed above LTP, never fills |
| 3 | BUG-002 | live_trading.py | 🔴 | GTT OCO trigger array order wrong for SELL legs — wrong leg triggers on SL/target hits |
| 4 | BUG-003 | live_trading.py | 🔴 | GTT placed after PENDING state with no SL for up to 15 min (next scan cycle) |
| 5 | BUG-019 | ng_risk_manager.py | 🔴 | `check_ng_daily_loss_cap()` returns `True` when cap IS hit — inverted boolean |
| 6 | P2-BUG-022 | decision_pipeline.py | 🔴 | `next()` without default → `RuntimeError` crash when pipeline short-circuits |
| 7 | P2-BUG-021 | decision_pipeline.py | 🔴 | Live duplicate signal check queries `paper_trades` — same signal placed twice in live |
| 8 | P2-BUG-023 | decision_pipeline.py | 🔴 | Confidence boost mutates `scan_context["intel"]` in-place — corrupts scan history |
| 9 | P2-BUG-013 | capital_allocator.py | 🔴 | Deprecated `kite.margins("orders", ...)` — broker margin optimization permanently dead |
| 10 | P2-XBUG-001 | Cross-file | 🔴 | `is_live` flag never validated in pipeline — wrong DB table selected silently |
| 11 | P2-BUG-005 | time_guards.py | 🔴 | Opening auction block uses `<= (9, 30)` inclusive — blocks first minute of normal trading |
| 12 | P2-BUG-009 | ng_risk_manager.py | 🔴 | Same as BUG-019 — confirmed in Pass 2 source review |
| 13 | BUG-004 | live_trading.py | 🟠 | Timeframe strategy hardcodes `broker_status=COMPLETE` without fill verification |
| 14 | BUG-006 | live_trading.py | 🟠 | Kill switch checked after PENDING reconciliation — active kill switch can still place orders |
| 15 | BUG-011 | risk_engine.py | 🟠 | Daily loss cap only counts closed trades — open unrealized losses ignored |
| 16 | BUG-014 | capital_allocator.py | 🟠 | Deprecated `kite.margins()` — confirmed open in Pass 2 |
| 17 | XBUG-002 | Cross-file | 🟠 | `ng_risk_manager` never integrated — all NG risk controls are dead code |
| 18 | P2-BUG-006 | time_guards.py | 🟠 | CRUDEOIL EIA block fires Thursday (NatGas day) instead of Wednesday (Crude day) |
| 19 | P2-BUG-017 | trade_plan.py | 🟠 | `ltp=0.0` treated same as missing key — valid deep-OTM options blocked |
| 20 | P2-BUG-018 | trade_plan.py | 🟠 | Default delta=0.5 wrong for OTM options — SL/Target premiums mispriced |
| 21 | P2-BUG-024 | decision_pipeline.py | 🟠 | `step_ai_alignment` dual attr/dict access fragile for falsy confidence values |
| 22 | P2-BUG-025 | decision_pipeline.py | 🟠 | Pyramiding check calls `paper_trading._get_option_premium` for live trades |
| 23 | P2-XBUG-002 | Cross-file | 🟠 | Circular import risk: `decision_pipeline ↔ paper_trading` |
| 24 | P2-XBUG-003 | Cross-file | 🟠 | No live-mode integration tests for `decision_pipeline` |

---

## Table of Contents
1. [live_trading.py — Pass 1](#live_tradingpy--pass-1)
2. [risk_engine.py — Pass 1 + Pass 2](#risk_enginepy--pass-1--pass-2)
3. [capital_allocator.py — Pass 1 + Pass 2](#capital_allocatorpy--pass-1--pass-2)
4. [time_guards.py — Pass 1 + Pass 2](#time_guardspy--pass-1--pass-2)
5. [ng_risk_manager.py — Pass 1 + Pass 2](#ng_risk_managerpy--pass-1--pass-2)
6. [trade_plan.py — Pass 1 + Pass 2](#trade_planpy--pass-1--pass-2)
7. [decision_pipeline.py — Pass 2 (New File)](#decision_pipelinepy--pass-2-new-file)
8. [Cross-File Issues — Pass 1 + Pass 2](#cross-file-issues--pass-1--pass-2)
9. [Pass 1 Bug Status After Pass 2 Review](#pass-1-bug-status-after-pass-2-review)

---

## live_trading.py — Pass 1

### 🔴 BUG-001 — Slippage buffer is identical for BUY and SELL (`place_kite_order`)
**Lines:** ~354–360  
**Code:**
```python
if transaction_type == "BUY":
    limit_price = ltp * (1 + buffer_pct)
else:
    limit_price = ltp * (1 + buffer_pct)   # ← SAME as BUY
```
**Problem:** A SELL limit at 5% *above* LTP will never fill unless the price moves up. The intent for a SELL limit order is `ltp * (1 - buffer_pct)` to offer slightly below current ask.  
**Fix:**
```python
if transaction_type == "SELL":
    limit_price = ltp * (1 - buffer_pct)
```

---

### 🔴 BUG-002 — GTT OCO order has mismatched SL/Target trigger order for SELL legs (`place_kite_gtt`)
**Lines:** ~400–430  
**Problem:** Kite's OCO requires `[lower_trigger, higher_trigger]`. For SELL legs `sl_premium > entry_premium`, so `sl_trigger > target_trigger`. Passing `[sl_trigger, target_trigger]` reverses the order Kite expects, causing the wrong leg to fire on SL/Target hits.  
**Fix:** Always sort triggers ascending: `sorted([sl_trigger, target_trigger])`.

---

### 🔴 BUG-003 — Race condition: PENDING trade has no SL protection for up to 15 minutes
**Lines:** ~570–600  
**Problem:** When `confirm_order_fill()` returns `PENDING` (max 2.5s polling), GTT is not placed and the trade sits unprotected until the next scan cycle (~15 min). Any adverse move in that window has no stop.  
**Fix:** Spawn a background thread immediately on PENDING status to poll fill and place GTT as soon as confirmed.

---

### 🟠 BUG-004 — `run_live_timeframe_strategy` hardcodes `broker_status="COMPLETE"` without fill verification
**Lines:** ~800–810  
**Code:**
```python
"broker_status": "COMPLETE" if not shadow_mode else "SHADOW",
```
**Problem:** No `confirm_order_fill()` call. REJECTED or PENDING orders are stored as COMPLETE, creating ghost open trades that are never reconciled.  
**Fix:** Call `confirm_order_fill()` and use the actual returned status.

---

### 🟠 BUG-005 — `_is_reversal_against_open_trade` uses new signal's `option_type` as fallback instead of open trade's
**Lines:** ~215–220  
**Code:**
```python
ot = str(open_trade.get("option_type") or option_type or "").upper()
```
**Problem:** If `open_trade["option_type"]` is None (FUT trade), the incoming signal's `option_type` is substituted, corrupting the directional check. A FUT trade could be incorrectly identified as a CE or PE, causing wrong reversal decisions.  
**Fix:** Use `open_trade.get("option_type", "")` without new-signal fallback.

---

### 🟠 BUG-006 — Kill switch checked *after* PENDING reconciliation and reversal logic
**Lines:** ~490–500  
**Problem:** Kill switch is only evaluated when no open trade exists. With an open trade, the code executes PENDING reconciliation and reversal exits before checking the kill switch.  
**Fix:** Move kill switch check to the absolute top of `run_live_trading()`, before all broker interactions.

---

### 🟡 BUG-007 — `_get_base_symbol` silently returns `"MCX"` for any `MCX`-prefixed symbol
**Lines:** ~440–455  
**Code:**
```python
if sym.startswith("MCX"):
    return "MCX"
```
**Problem:** Returns generic `"MCX"` string which fails `LOT_SIZES` lookup and `enabled_symbols` checks for symbols like `MCXGOLD`.  
**Fix:** Remove the `MCX` catch-all fallback or log a warning.

---

### 🟡 BUG-008 — `sync_direct_kite_positions` uses timestamp in `signal_key`, breaking deduplication
**Lines:** ~900  
**Code:**
```python
"signal_key": f"kite_direct_{ts}_{now_iso}",
```
**Problem:** Each call generates a unique key. Re-calling on retry within the same scan cycle adopts the same position twice, creating duplicate DB records.  
**Fix:** Use stable key: `f"kite_direct_{ts}"`.

---

### 🟡 BUG-009 — Local `check_live_risk_limits` in `live_trading.py` shadows and bypasses `risk_engine.py`
**Lines:** ~260–290  
**Problem:** The local function only checks `live_max_concurrent_positions`. All risk_engine checks (daily loss cap, cooldowns, circuit breakers) are bypassed for live trades. See also XBUG-001.  
**Fix:** Delete the local function; call `risk_engine.check_live_risk_limits(symbol)`.

---

### 🟢 BUG-010 — `threading` module imported twice
**Lines:** ~53 and ~167  
**Problem:** `import threading` appears at module level and again inside `get_kite_client()`.  
**Fix:** Remove the duplicate inner import.

---

## risk_engine.py — Pass 1 + Pass 2

### 🟠 BUG-011 — Daily loss cap counts only closed trades; unrealized open losses ignored
**Lines:** ~97–104  
**Code:**
```python
SELECT COALESCE(SUM(pnl_rupees), 0) AS total
FROM {trades_table}
WHERE closed_at >= ? AND pnl_rupees < 0
```
**Problem:** Three open trades each down 20% show zero daily loss until they close. The cap is defeated until realized.  
**Fix:** Explicitly document this is a "realized-only" cap, or include open trades with current MTM P&L.

---

### 🟡 BUG-012 — Circuit breaker status list is incomplete
**Lines:** ~67  
**Code:**
```python
AND status IN ('CLOSED_SL', 'CLOSED_MANUAL', 'CLOSED', 'SL_HIT')
```
**Problem:** `CLOSED_REVERSAL` and `CLOSED_TARGET` (with `pnl_rupees < 0`) are absent. Reversal-exit losses never trigger the circuit breaker.  
**Fix:** Include all terminal statuses: `'CLOSED_SL', 'CLOSED_MANUAL', 'CLOSED', 'SL_HIT', 'CLOSED_REVERSAL', 'CLOSED_TF_EXIT'`.

---

### 🟡 BUG-013 — `check_live_risk_limits` has different signature than local version in `live_trading.py`
**Lines:** risk_engine.py ~155 vs live_trading.py ~260  
**Problem:** Dual implementations with different scope — risk_engine version is never the one called. See XBUG-001.  
**Fix:** Consolidate.

---

### 🟠 P2-BUG-001 — Circuit breaker misses `CLOSED_REVERSAL` / `CLOSED_TF_EXIT` statuses *(Pass 2 confirmation of BUG-012)*
**Lines:** ~67  
**Problem:** Same issue confirmed in Pass 2 source. CLOSED_TF_EXIT specifically confirmed absent.  
**Fix:** As above — add all terminal statuses.

---

### 🟡 P2-BUG-002 — `_ist_day_start_utc()` returns naive datetime string — SQLite timestamp comparison may fail
**Lines:** ~46–55  
**Code:**
```python
midnight_utc = midnight_ist - IST_OFFSET
return midnight_utc.isoformat()
```
**Problem:** Subtracting a `timedelta` from an aware datetime yields an aware datetime, but the result is derived from `midnight_ist` which is `.replace()` constructed — the timezone info may be stripped depending on Python version. The resulting ISO string may have no `+00:00` suffix while SQLite stores `2026-07-07T00:00:00+05:30`, causing the comparison to silently skip all IST-day records.  
**Fix:**
```python
midnight_utc = midnight_ist.astimezone(timezone.utc)
return midnight_utc.isoformat()
```

---

### 🟡 P2-BUG-003 — Daily loss query has no upper bound on `closed_at` — clock drift vulnerability
**Lines:** ~130–140  
**Problem:** `WHERE closed_at >= ? AND pnl_rupees < 0` with no `closed_at <=` upper bound. A record with future `closed_at` (IST/UTC clock skew) appears in every daily loss aggregation forever.  
**Fix:** Add `AND closed_at <= CURRENT_TIMESTAMP`.

---

### 🟢 P2-BUG-004 — `_check_risk_limits_for_table` called as private from `decision_pipeline.py`
**Lines:** risk_engine.py signature, decision_pipeline.py import  
**Problem:** Tight coupling on a private function. Any signature change breaks the pipeline silently.  
**Fix:** Expose a public wrapper or have the pipeline call `check_risk_limits()` / `check_live_risk_limits()`.

---

## capital_allocator.py — Pass 1 + Pass 2

### 🟠 BUG-014 — Deprecated `kite.margins("orders", ...)` — broker margin API never active
**Lines:** ~70–80  
**Code:**
```python
future = executor.submit(kite.margins, "orders", orders)
```
**Problem:** KiteConnect v3 removed this call signature. Correct API is `kite.order_margins(orders)`. Every call raises `TypeError`, falls back to static multiplier silently via `except Exception`. Broker margin optimization is permanently dead.  
**Fix:**
```python
future = executor.submit(kite.order_margins, orders)
```

---

### 🟡 BUG-015 — Pyramiding scale applied on top of broker-mode paper path; `pyramid_level` key not guaranteed in plan
**Lines:** ~130–138  
**Problem:** If `build_paper_trade_plan()` doesn't set `pyramid_level` in the plan, it defaults to 1 and pyramiding is silently disabled for both paper and live.  
**Fix:** Assert or log when `pyramid_level` is absent from plan.

---

### 🟡 P2-BUG-014 — No warning log when `live_capital_per_trade_inr` defaults to ₹50,000
**Lines:** ~100  
**Code:**
```python
capital_per_trade = float(config.get("live_capital_per_trade_inr") or 50000.0)
```
**Problem:** Misconfigured runtime config silently allocates ₹50K per trade with no warning in logs.  
**Fix:**
```python
if not config.get("live_capital_per_trade_inr"):
    log.warning("%s: live_capital_per_trade_inr not set, defaulting to ₹50,000", base)
```

---

### 🟡 P2-BUG-015 — Pyramiding scale is a no-op for 1-lot base positions
**Lines:** ~148–155  
**Code:**
```python
if pyramid_level == 2:
    lots = max(1, int(lots * 0.5))  # int(1 * 0.5) = 0 → max(1,0) = 1
```
**Problem:** For a 1-lot base, level 2 and level 3 both clamp to 1. Pyramiding scaling is silently ineffective.  
**Fix:** Log when scaled lots equal base lots (scaling had no effect).

---

### 🟢 P2-BUG-016 — No debug log when broker margin API skipped for BUY legs
**Lines:** ~47  
**Problem:** Silent skip with no trace, making BUY lot sizing debugging opaque.  
**Fix:** Add `log.debug("%s: BUY leg — skipping broker margin API", symbol)`.

---

## time_guards.py — Pass 1 + Pass 2

### 🔴 P2-BUG-005 — Opening auction block `<= (9, 30)` inclusive — blocks first minute of normal trading
**Lines:** ~72  
**Code:**
```python
if (h, m) >= (9, 15) and (h, m) <= (9, 30):
```
**Problem:** Confirmed critical in Pass 2. Blocks 09:30:00–09:30:59 IST. NSE normal trading is fully live from 09:15. Costs one full minute of early-morning momentum window.  
**Fix:** Change to `< (9, 30)`.

> *Also filed as BUG-016 in Pass 1. Consolidated here with raised severity.*

---

### 🟠 P2-BUG-006 — CRUDEOIL EIA block fires Thursday instead of Wednesday
**Lines:** ~81–93  
**Code:**
```python
if sym in ("NATURALGAS", "NATGAS", "CRUDEOIL"):
    if now.weekday() == _EIA_WEEKDAY:   # Thursday = 3
```
**Problem:** EIA Natural Gas Storage = Thursday ✓. EIA Weekly Petroleum Status (Crude) = **Wednesday**. CRUDEOIL is blocked on the wrong day while the actual volatility window is missed.  
**Fix:**
```python
_EIA_CRUDE_WEEKDAY = 2  # Wednesday
if sym in ("NATURALGAS", "NATGAS"):
    if now.weekday() == _EIA_WEEKDAY:      # Thursday
        ...
if sym == "CRUDEOIL":
    if now.weekday() == _EIA_CRUDE_WEEKDAY:  # Wednesday
        ...
```

> *Also filed as BUG-018 in Pass 1 as Medium. Raised to High in Pass 2 after confirming it means the actual Wednesday volatility window is entirely unguarded.*

---

### 🟡 P2-BUG-007 — `is_mcx` boolean recomputed twice with identical logic
**Lines:** ~76 and ~90  
**Problem:** Identical expression in Window 2 and Window 5. Future MCX symbol additions must be made in two places.  
**Fix:** Compute once at function top before Window 0.

---

### 🟡 P2-BUG-008 — Exceptions in `is_trading_allowed_now` logged at DEBUG only — broken imports silently disable all guards
**Lines:** ~120–122  
**Problem:** If `MCX_SYMBOLS` import fails or `is_cme_closed` throws, all time guards are bypassed and the failure is invisible at default log levels.  
**Fix:** Change `log.debug` to `log.warning` for unexpected exceptions.

---

## ng_risk_manager.py — Pass 1 + Pass 2

### 🔴 BUG-019 — `check_ng_daily_loss_cap()` returns `True` when cap IS hit (inverted boolean)
**Lines:** ~50–60  
**Code:**
```python
if len(statuses) >= 2 and all(s == "CLOSED_SL" for s in statuses):
    log.warning("NG Daily Loss Cap hit!")
    return True   # ← True = cap hit, but callers read True = OK to trade
return False
```
**Problem:** Any caller treating the return as `is_allowed` will place NATURALGAS trades after 2 consecutive SL hits. Currently dormant (module not integrated), but a live financial risk the moment it's wired in.  
**Fix:** Invert: return `False` when cap hit, `True` when clear — or rename to `is_ng_daily_loss_cap_hit()`.

---

### 🟠 BUG-020 — `check_ng_daily_loss_cap()` queries by `opened_at` but orders by `closed_at` without NULL guard
**Lines:** ~44–55  
**Code:**
```python
WHERE symbol = 'NATURALGAS'
  AND status != 'OPEN'
  AND opened_at >= ?
ORDER BY closed_at DESC
LIMIT 2
```
**Problem:** REJECTED trades with NULL `closed_at` sort incorrectly. Trades opened yesterday but closed today are missed. `status != 'OPEN'` is too broad — includes REJECTED, CANCELLED.  
**Fix:**
```python
WHERE symbol = 'NATURALGAS'
  AND status = 'CLOSED_SL'
  AND closed_at >= ?
  AND closed_at IS NOT NULL
ORDER BY closed_at DESC
LIMIT 2
```

---

### 🟠 P2-BUG-010 — `check_ng_daily_loss_cap()` hardcodes `paper_trades` — wrong table when NATURALGAS goes live
**Lines:** ~41  
**Problem:** Queries `paper_trades` unconditionally. Live NATURALGAS trades won't be visible to the loss cap check.  
**Fix:** Accept `table: str = "paper_trades"` parameter (mirrors risk_engine.py pattern).

---

### 🟠 P2-BUG-011 — Status filter too broad in NG loss cap query — REJECTED trades included
**Lines:** ~44  
**Problem:** `status != 'OPEN'` includes REJECTED, PENDING, CANCELLED trades, poisoning the consecutive-SL count.  
**Fix:** Use `status = 'CLOSED_SL'` (explicit), not a negative filter.

---

### 🟡 BUG-021 — `calculate_ng_lot_size()` has no maximum lot cap
**Lines:** ~70–82  
**Problem:** At tight stop (0.10 on NATURALGAS), formula yields 80+ lots — catastrophic position. No upper bound unlike `capital_allocator.py`'s `_DEFAULT_MAX_AUTO_LOTS`.  
**Fix:** Add `MAX_NG_AUTO_LOTS = 5` and `lots = min(lots, MAX_NG_AUTO_LOTS)`.

> *Confirmed and severity raised in Pass 2 (P2-BUG-012). Consolidated here.*

---

## trade_plan.py — Pass 1 + Pass 2

### 🟠 BUG-022 — Default delta=0.5 for BUY in `convert_underlying_sl_to_premium` wrong for OTM options
**Lines:** ~260–265  
**Code:**
```python
if delta is None:
    delta = 0.5 if side == "BUY" else 0.3
```
**Problem:** Delta=0.5 assumes ATM. Deep OTM options (typical target: ATM ± 4 steps) have delta 0.15–0.25. Using 0.5 overstates premium movement — SL/Target premiums are 2× too wide or too tight.  
**Fix:** Default to `delta = 0.25 if side == "BUY" else 0.20`.

---

### 🟡 BUG-023 — `get_option_premium` returns `None` for `ltp=0.0` — valid deep-OTM options blocked
**Lines:** ~170–175  
**Code:**
```python
premium = float(row.get("ltp") or 0.0)
return premium if premium > 0 else None
```
**Problem:** `ltp=0.0` (real deep-OTM value) is treated identically to `ltp=None` (key absent). Both return `None`.  
**Fix:**
```python
ltp_raw = row.get("ltp")
premium = float(ltp_raw) if ltp_raw is not None else 0.0
return premium if premium > 0 else None
```

---

### 🟡 BUG-024 — `parse_verdict_and_confidence` greedy fallback regex captures multi-line text
**Lines:** ~220–235  
**Code:**
```python
m_v2 = re.search(r"Verdict:\s*([A-Z\s_]+)", intel_text, re.IGNORECASE)
```
**Problem:** `[A-Z\s_]+` with IGNORECASE matches newlines — captures `"LONG BUILDUP\nCONFIDENCE"` as verdict.  
**Fix:**
```python
m_v2 = re.search(r"Verdict:\s*([A-Z][A-Z _]{1,30})(?=\s*\n|\s*$|\s*\*)", intel_text, re.IGNORECASE)
```

---

### 🟢 P2-BUG-020 — `get_atr` prefers 3H ATR with no caller-specified timeframe preference
**Lines:** ~45–60  
**Problem:** Core OI engine (15-min scans) receives 3H ATR, producing wider SL distances than intended. No way for callers to specify preferred ATR timeframe.  
**Fix:** Add optional `preferred_tf: str = "3h"` parameter.

---

## decision_pipeline.py — Pass 2 (New File)

> This file was not reviewed in Pass 1. All bugs below are new Pass 2 findings.

### 🔴 P2-BUG-021 — Live duplicate signal check queries `paper_trades` — same signal can be placed twice in live
**Lines:** ~295–305  
**Code:**
```python
cnt = conn.execute(
    "SELECT COUNT(*) AS c FROM paper_trades WHERE signal_key=?", (signal_key,)
).fetchone()["c"]
```
**Problem:** When `is_live=True`, this still queries `paper_trades`. The same signal can be re-entered in live mode on every re-scan since `live_trades` is never checked for the duplicate.  
**Fix:**
```python
table = "live_trades" if ctx.scan_context.get("is_live") else "paper_trades"
cnt = conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE signal_key=?", ...)
```

---

### 🔴 P2-BUG-022 — `step_trend_alignment_core` uses `next()` without default — RuntimeError when pipeline short-circuits
**Lines:** ~355–358  
**Code:**
```python
entry_quality = next(s.score for s in ctx.steps if s.name == "entry_quality")
regime_sc = next(s.score for s in ctx.steps if s.name == "regime")
```
**Problem:** When `PIPELINE_SHORT_CIRCUIT=True` and a prior step failed, `entry_quality` or `regime` steps may not be in `ctx.steps`. `next()` on an empty generator raises `StopIteration`, which propagates as `RuntimeError: generator raised StopIteration` in Python 3.7+, crashing the entire pipeline.  
**Fix:**
```python
entry_quality = next((s.score for s in ctx.steps if s.name == "entry_quality"), 0)
regime_sc = next((s.score for s in ctx.steps if s.name == "regime"), 0)
```

---

### 🔴 P2-BUG-023 — `step_signal_core_oi` mutates `scan_context["intel"]["confidence"]` in-place — corrupts scan history
**Lines:** ~102–106  
**Code:**
```python
if "intel" in ctx.scan_context:
    ctx.scan_context["intel"]["confidence"] = confidence  # ← mutates shared dict
```
**Problem:** `ctx.scan_context` is passed by reference. Mutating `intel["confidence"]` permanently changes the confidence for ALL downstream uses of that signal in the same scan cycle, including logging, audit trail, and trend analysis. A boosted 55→70 confidence is persisted to scan history as 70.  
**Fix:** Do not mutate shared context. Use a local variable only, or shallow copy:
```python
ctx.scan_context = {**ctx.scan_context, "intel": {**ctx.scan_context["intel"], "confidence": confidence}}
```

---

### 🟠 P2-BUG-024 — `step_ai_alignment` dual attr/dict access pattern is fragile for falsy confidence values
**Lines:** ~155–160  
**Code:**
```python
ai_conf = getattr(ai_verdict, 'confidence', 0) or (ai_verdict.get('confidence', 0) if isinstance(ai_verdict, dict) else 0)
```
**Problem:** When `ai_verdict` is a dataclass with `confidence=0` (falsy), `getattr` returns 0, then `0 or dict_branch` evaluates the dict fallback unnecessarily. This logic is wrong for any zero confidence value and is hard to test deterministically.  
**Fix:** Enforce a single type contract — normalize `ai_verdict` to dict at the pipeline intake point.

---

### 🟠 P2-BUG-025 — Pyramiding profitability check uses `paper_trading._get_option_premium` for live trades
**Lines:** ~325–340  
**Code:**
```python
from src.engine.paper_trading import _get_option_premium
t_exit = _get_option_premium(symbol, ...)
```
**Problem:** Private function from `paper_trading.py`. For live trades, this fetches from paper snapshots, not live LTP. A live pyramid entry could be approved when the live position is actually at a loss.  
**Fix:** Replace with `from src.engine.trade_plan import get_option_premium` (the unified public function).

---

### 🟡 P2-BUG-026 — Heavyweight momentum threshold ±0.50% hardcoded — NIFTY and BANKNIFTY need different values
**Lines:** ~445–450  
**Code:**
```python
if direction == "LONG" and weighted_momentum <= -0.50:
    ...
elif direction == "SHORT" and weighted_momentum >= 0.50:
```
**Problem:** BANKNIFTY routinely moves 1–2% intraday; a -0.50% threshold blocks most BANKNIFTY LONG entries on normal pullbacks. NIFTY is more appropriate at ±0.30%.  
**Fix:** Per-symbol config: `HEAVYWEIGHT_THRESHOLD = {"NIFTY": 0.30, "BANKNIFTY": 0.60}`.

---

### 🟡 P2-BUG-027 — Pipeline short-circuit leaves `_pipeline_plan` unset — downstream callers get `None` silently
**Lines:** ~492–497  
**Problem:** When `PIPELINE_SHORT_CIRCUIT=True` and step fails before `step_entry_quality_core`, `ctx.scan_context["_pipeline_plan"]` is never set. Downstream code reading `ctx.scan_context.get("_pipeline_plan")` gets `None` with no error.  
**Fix:** Initialize all `_pipeline_*` keys to safe defaults at pipeline start.

---

### 🟢 P2-BUG-028 — Open trades pyramid check likely queries `paper_trades` even in live mode
**Lines:** ~282  
**Code:**
```python
open_trades = get_open_timeframe_trades(symbol)
```
**Problem:** If `get_open_timeframe_trades()` is hardcoded to `paper_trades`, the max pyramid level 3 check is bypassed for live trades — allowing unlimited live pyramids.  
**Fix:** Verify `get_open_timeframe_trades()` signature and pass `table` parameter.

---

## Cross-File Issues — Pass 1 + Pass 2

### 🔴 XBUG-001 — `risk_engine.check_live_risk_limits` never called from `run_live_trading` (Pass 1)
**Files:** `live_trading.py`, `risk_engine.py`  
**Problem:** `run_live_trading()` calls its own local `check_live_risk_limits()` which only checks `live_max_concurrent_positions`. The full risk engine (daily loss cap, cooldowns, circuit breakers) is **never invoked for live trades**. This is the most critical architectural gap.  
**Fix:** Delete the local `check_live_risk_limits` in `live_trading.py`; import and call `risk_engine.check_live_risk_limits(symbol)`.

---

### 🔴 P2-XBUG-001 — `is_live` flag in pipeline never validated — wrong DB table selected silently
**Files:** `decision_pipeline.py`, `risk_engine.py`  
**Problem:** `step_risk` reads `ctx.scan_context.get("is_live", False)` but nothing validates this flag at pipeline entry. A live call with `is_live` accidentally `False` queries `paper_trades` for risk limits — potentially allowing a live trade when the paper limit is already hit.  
**Fix:** Validate/assert `is_live` at `run_entry_pipeline()` entry point.

---

### 🟠 XBUG-002 — `ng_risk_manager` never integrated — all NG risk controls are dead code (Pass 1)
**Files:** `ng_risk_manager.py`, `live_trading.py`, `risk_engine.py`  
**Problem:** No imports of `ng_risk_manager` exist in any executing file. `check_ng_position_limit()`, `check_ng_daily_loss_cap()`, `calculate_ng_lot_size()` are all dead code.  
**Fix:** Integrate NG checks into `check_live_risk_limits` via a symbol-specific hook.

---

### 🟠 P2-XBUG-002 — Circular import risk: `decision_pipeline ↔ paper_trading`
**Files:** `decision_pipeline.py`, `paper_trading.py`  
**Problem:** `decision_pipeline.py` imports from `paper_trading.py`. If `paper_trading.py` imports from `decision_pipeline.py`, Python's partially-initialized module handling can cause `AttributeError` at runtime on startup.  
**Fix:** Move shared utilities (`_is_market_open`, `_get_option_premium`) to a `src/engine/utils.py` common module.

---

### 🟠 P2-XBUG-003 — No live-mode integration tests for `decision_pipeline`
**Files:** `tests/`, `decision_pipeline.py`  
**Problem:** All live-vs-paper table selection bugs (P2-BUG-021, P2-BUG-028, P2-XBUG-001) would be caught by a single parametrized integration test. None exists.  
**Fix:** Add `tests/test_decision_pipeline_live.py` with mock broker state and `is_live=True` scenarios.

---

### 🟡 XBUG-003 — `time_guards.is_trading_allowed_now` not called from `run_live_timeframe_strategy` (Pass 1)
**Files:** `live_trading.py`, `time_guards.py`  
**Status:** ✅ **FIXED in Pass 2** — `decision_pipeline.py` now calls `is_trading_allowed_now()` in both `step_rule_core_oi` and `step_rule_timeframe`.

---

## Pass 1 Bug Status After Pass 2 Review

| Bug ID | Description | Status |
|--------|-------------|--------|
| BUG-001 | SELL slippage buffer identical to BUY | ❌ Open — not reflected in reviewed files |
| BUG-002 | GTT OCO trigger order wrong for SELL | ❌ Open |
| BUG-003 | PENDING trade has no SL for 15 min | ❌ Open |
| BUG-004 | Timeframe hardcodes broker_status=COMPLETE | ❌ Open |
| BUG-005 | `_is_reversal` uses new signal's option_type | ❌ Open |
| BUG-006 | Kill switch checked after PENDING reconciliation | ❌ Open |
| BUG-007 | `_get_base_symbol` returns "MCX" for MCX-prefixed | ❌ Open |
| BUG-008 | `sync_direct_kite_positions` timestamp in signal_key | ❌ Open |
| BUG-009 | Local `check_live_risk_limits` bypasses risk_engine | ❌ Open (see XBUG-001) |
| BUG-010 | Duplicate `import threading` | ❌ Open — trivial, low priority |
| BUG-011 | Daily loss cap ignores open unrealized losses | ⚠️ Partial — risk_engine.py has comment "realized losses only" but unrealized still excluded |
| BUG-012 | Circuit breaker missing CLOSED_REVERSAL | ❌ Open — confirmed in Pass 2 |
| BUG-013 | Dual `check_live_risk_limits` signatures | ❌ Open |
| BUG-014 | Deprecated `kite.margins("orders", ...)` | ❌ Open — confirmed in Pass 2 |
| BUG-015 | `pyramid_level` key not guaranteed in plan | ❌ Open |
| BUG-016 | Opening auction `<=` 09:30 off-by-one | ❌ Open — raised to Critical in Pass 2 |
| BUG-017 | `is_mcx` computed twice | ❌ Open |
| BUG-018 | CRUDEOIL EIA block on wrong day (Thursday) | ❌ Open — raised to High in Pass 2 |
| BUG-019 | NG loss cap inverted boolean | ❌ Open — confirmed in Pass 2 |
| BUG-020 | NG loss cap NULL guard missing | ❌ Open |
| BUG-021 | `calculate_ng_lot_size` no upper cap | ❌ Open |
| BUG-022 | Default delta=0.5 wrong for OTM options | ❌ Open |
| BUG-023 | `ltp=0.0` returns None (blocks valid options) | ❌ Open |
| BUG-024 | Greedy verdict regex captures multi-line | ❌ Open |
| XBUG-001 | `risk_engine.check_live_risk_limits` never called live | ❌ Open |
| XBUG-002 | `ng_risk_manager` not integrated (dead code) | ❌ Open |
| XBUG-003 | `is_trading_allowed_now` not called in live timeframe | ✅ **FIXED** — `decision_pipeline.py` added |

---

*Consolidated report — Pass 1 + Pass 2 | 2026-07-07 | Source: https://github.com/Manvendra08/TradingBot*
