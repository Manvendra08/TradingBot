# NSEBOT — Comprehensive Code Audit Report

**Date:** 2026-07-29  
**Scope:** Full line-by-line logic audit across all engine, fetcher, pipeline, and dashboard source files  
**Status:** ⚠️ NOT PRODUCTION READY — 3 CRITICAL, 5 HIGH, 4 MEDIUM, 2 LOW findings

---

## Table of Contents

1. [CRITICAL — P0 (Financial Loss / Data Corruption)](#1-critical--p0)
2. [HIGH — P1 (Incorrect Behavior / Silent Failures)](#2-high--p1)
3. [MEDIUM — P2 (Subtle Logic Errors)](#3-medium--p2)
4. [LOW — P3 (Code Quality / Robustness)](#4-low--p3)
5. [Cross-Cutting Issues](#5-cross-cutting-issues)
6. [Summary Matrix](#6-summary-matrix)

---

## 1. CRITICAL — P0

### P0-01: Global SSL Verification Bypass — MITM Attack Surface

**File:** `main.py:44-60`

```python
def _patched_create_urllib3_context(cert_reqs=None, **kwargs):
    ctx = _orig_create_context(cert_reqs=cert_reqs, **kwargs)
    if cert_reqs == ssl.CERT_NONE:
        ctx.check_hostname = False
    return ctx
urllib3.util.ssl_.create_urllib3_context = _patched_create_urllib3_context
```

**Bug:** Monkey-patches `urllib3` SSL context creation globally. Every library using `requests` (Telegram API, Gemini, broker APIs) that passes `verify=False` will have hostname verification silently disabled.

**Impact:** MITM attack on any HTTP call from the trading bot — attacker can intercept broker API calls, steal tokens, place unauthorized trades.

**Fix:** Remove global patch. Pass `verify=False` per-request only where absolutely necessary.

---

### P0-02: PatchedCursor SQL Injection via Regex Rewriting

**File:** `dashboard_server.py:40-48`

```python
class PatchedCursor(sqlite3.Cursor):
    def execute(self, sql, *args, **kwargs):
        if (isinstance(sql, str) and re.match(r"(?i)^\s*(select|with)\b", sql)
            and re.search(r"(?i)\bfrom\s+paper_trades\b", sql)):
            sql = re.sub(r"(?i)\bfrom\s+paper_trades\b", f"FROM {subquery}", sql)
        return super().execute(sql, *args, **kwargs)
```

**Bug:** Regex-based SQL rewriting is dangerous — matches `FROM paper_trades` even inside string literals, comments, or column aliases. The `UNION ALL` subquery hardcodes column list — any schema migration adding columns breaks the dashboard with `mismatched column count`.

**Fix:** Use a proper view or CTE instead of monkey-patching cursor execution globally.

---

### P0-03: `_fetch_local_ohlc_from_db` Missing `prev_ohlc` — Timeframe Strategy Blind on Restart

**File:** `src/fetchers/chart_fetcher.py:950-955`

```python
result = {
    "sentiment": _sentiment(o, c, h, l),
    "ohlc": {"open": o, "high": h, "low": l, "close": c},
    "bar_start_utc": start_utc.isoformat(),
    "bar_end_utc": end_utc.isoformat(),
}
# NO "prev_ohlc" key
```

**Root Cause:** Every other provider (`_fetch_tv`, `_fetch_yf`, `_fetch_shoonya_candles`, `_fetch_dhan_builtup_ohlc`) returns `prev_ohlc` in the payload. `_fetch_local_ohlc_from_db` does not. When the MCX fallback path hits this function (lines 1488-1500), the payload lacks `prev_ohlc`.

**Propagation:** `_merge_state` (line 1428) does:
```python
"prev_ohlc": payload.get("prev_ohlc")      # None
or prev_ohlc                                # prev_state["ohlc"] — also None on first scan
or prev.get("prev_ohlc"),                   # also None on first scan
```

**Impact:** On first scan after app restart (or after any interval where `_STATE` was evicted), `prev_3h` is `None` for MCX symbols. `decision_pipeline.py:686` resolves it:
```python
prev_3h = pay_3h.get("prev_ohlc") or pay_3h.get("last_closed_ohlc")
```
Both are `None` → `step_signal_timeframe` returns `StepResult(passed=False, reason="Missing or incomplete 3H candle data")` → entire timeframe strategy pipeline aborts. MCX symbols get zero timeframe trades on first scan cycle.

**Fix:** Add `prev_ohlc` computation to `_fetch_local_ohlc_from_db`:
```python
# Query the previous window to get prev_ohlc
prev_start = start_utc - (end_utc - start_utc)
prev_rows = conn.execute(...)
if prev_rows:
    result["prev_ohlc"] = {"open": ..., "high": ..., "low": ..., "close": ...}
```

---

## 2. HIGH — P1

### P1-01: FUT SL/TGT Computed from Stale `avg_price` Instead of CMP (`underlying_price`) — **FIXED**

**File:** `src/engine/live_trading.py:2261-2270`

**Before fix:**
```python
if option_type == "FUT":
    entry_val = avg_price        # stale — e.g. 268 when CMP is 261
    if init_mode == "dynamic" and atr and atr > 0:
        if side == "BUY":
            sl_underlying = round(entry_val - 1.5 * atr, 2)    # SL = 266.3 — ABOVE CMP 261
            tgt_underlying = round(entry_val + 2.0 * atr, 2)   # TGT = 270.26
```

**Bug:** `avg_price` is the historical entry price recorded in the manual trade table (could be hours or days old). For a BUY at 268 when CMP dropped to 261, SL = 266.3 is above CMP = 261 → immediate SL hit on first scan. TGT = 270.26 is unachievable.

**After fix (line 2264):**
```python
base_ref = underlying_price if underlying_price > 0 else avg_price
```
SL/TGT now computed from current market price when available.

**Impact:** Prevented guaranteed losing trades on FUT positions opened during intraday pullbacks.

---

### P1-02: MCX `underlying_price` Always 0 — FUT SL/TGT Falls Back to Stale Price

**File:** `src/engine/live_trading.py:2213-2215`

```python
if symbol_key.startswith("MCX:"):
    # Kite API accounts lack MCX market data permissions — bypass kite.ltp for MCX
    pass
```

**Bug:** MCX symbols explicitly skip Kite LTP lookup (the entire `if symbol_key.startswith("MCX:")` block is a no-op `pass`). The fallback `get_previous_underlying` (line 2238) may return `None` (no prior data in `underlying_price` table, or table empty for MCX symbols). Result: `underlying_price` stays `0.0`.

**Impact:** The FUT fix (P1-01) becomes ineffective for MCX — `base_ref = underlying_price if underlying_price > 0 else avg_price` evaluates to `avg_price` because `underlying_price` is 0. MCX FUT positions use stale entry prices for SL/TGT.

**Fix:** Add a local-ATR-based default or resolve MCX LTP via an alternative source (Shoonya, Dhan, or local DB latest price).

---

### P1-03: `_last_closed_window` Returns Incomplete Current Bar at Session Start

**File:** `src/fetchers/chart_fetcher.py:683-685`

```python
if delta_mins < tf_mins:
    start_ist = market_open
    end_ist = now_ist
```

**Bug:** When elapsed time from market open is less than one timeframe interval (e.g., 30 min into a 3H session), the "closed window" spans `[market_open, now_ist)` — which is the current incomplete bar, not a closed window. The function name promises a "last closed window" but delivers the in-progress window.

**Impact:** `_fetch_local_ohlc_from_db` builds OHLC from an incomplete bar. The resulting `ohlc` may have wildly inaccurate high/low (especially with low volume at session open), producing false breakout/breakdown signals.

**Fix:** Return `None` when `delta_mins < tf_mins` — there is no closed window yet. The caller should fall back to `_merge_state` which preserves the last known good state.

---

### P1-04: `_merge_state` Unbounded In-Memory `_STATE` Dict

**File:** `src/fetchers/chart_fetcher.py:1415-1444`

```python
with _STATE_LOCK:
    symbol_state = _STATE.setdefault(base_symbol, {})
    prev = symbol_state.get(tf, {})
```

**Bug:** `_STATE` is a module-level dict that accumulates state for every `(base_symbol, tf)` pair ever seen. Entries are never evicted. Over a multi-day run with 20+ symbols scanning every 5-15 minutes, this grows linearly and retains stale references.

**Impact:** Memory leak. Stale state from days ago could be used as `prev_ohlc` after a symbol ID change or contract rollover. The `seen_at` field is updated but `prev_ohlc` from 48 hours ago could incorrectly persist if a provider returns a payload without `prev_ohlc`.

**Fix:** Add a TTL-based eviction (e.g., prune entries where `updated_at` > 24h old). Key the state by `(base_symbol, tf)` with a fixed-size LRU cache.

---

### P1-05: `step_signal_timeframe` Silent Fallback When `chart_indicators` Is Non-Dict

**File:** `src/engine/decision_pipeline.py:681-683`

```python
chart_indicators = ctx.scan_context.get("chart_indicators") or {}
tf_data = chart_indicators
if not any(k in chart_indicators for k in ("1h", "3h")):
    tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}
```

**Bug:** When `chart_indicators` is a dict keyed by symbol name (e.g., `{"NATURALGAS": {"1h": ..., "3h": ...}}`), the check `any(k in chart_indicators for k in ("1h", "3h"))` is `False` because the keys are symbol names, not timeframe names. The fallback grabs `next(iter(chart_indicators.values()))` — which is the correct nested dict by accident only if there's exactly one symbol and it's the expected one.

**Impact:** Brittle unwrapping. If `chart_indicators` contains multiple symbols (possible in pipeline aggregation), `next(iter(...))` may grab the wrong symbol's data silently.

**Fix:** Normalize `chart_indicators` to a standard structure at the pipeline's entry point (`pipeline.py` or `decision_pipeline.py` entry), or access by `base_symbol` key explicitly.

---

## 3. MEDIUM — P2

### P2-01: 3H Breakout Detection Fails in Consolidating/Pullback Markets — No Signal for Hours

**File:** `src/engine/decision_pipeline.py:705-706`

```python
is_long_trigger = c_3h_close > p_3h_high + breakout_buffer
is_short_trigger = c_3h_close < p_3h_low - breakout_buffer
```

**Bug:** The breakout algorithm requires the **close** of the current 3H candle to exceed the **high** of the previous 3H candle (plus buffer). In a pullback or consolidation at 9 PM IST, the close (e.g., 262.2) is naturally below the previous candle's high (e.g., 265.3), so no signal. This is **correct by design** for trend-following, but can produce extended gaps (8+ hours) without signals.

**Impact:** Timeframe strategy can remain idle for entire trading sessions in ranging markets. Not a code bug per se, but a material limitation of the entry algorithm that operators should be aware of.

**Recommendation:** Document that this strategy generates signals primarily during strong trend sessions. Consider adding a mean-reversion fallback or OI-confirmed counter-trend variant for range-bound sessions.

---

### P2-02: `_bar_to_payload` Possibly Receives Naive `prev_ts` Without Timezone

**File:** `src/fetchers/chart_fetcher.py:1187-1191`

```python
prev_payload = _bar_to_payload(
    prev_bar, bar_start_ts=prev_ts, bar_end_ts=prev_ts, naive_tz=IST
)
```

**Bug:** `bar_start_ts` and `bar_end_ts` are set to the same `prev_ts` value. `prev_ts` comes from `df.index[-2]` which is a pandas Timestamp — may or may not be timezone-aware depending on the TV datafeed library version. If `prev_ts` is already UTC-aware and `_bar_to_payload` applies `naive_tz=IST` conversion, timestamps get double-shifted.

**Impact:** Bar timestamps in `prev_ohlc` may be off by 5.5 hours, causing time-window boundary mismatches in downstream calculations.

**Fix:** Normalize `prev_ts` to timezone-naive before passing to `_bar_to_payload`, or ensure `_bar_to_payload` handles both aware and naive timestamps.

---

### P2-03: `_get_atr` Unwraps `chart_indicators` Inconsistently Across Callers

**File:** `src/engine/trade_plan.py:35`, `src/engine/paper_trading.py` (internal `_get_atr`)

**Bug:** There are at least two different `_get_atr` implementations — one in `trade_plan.py` that takes a `scan_context` and unwraps `chart_indicators` by symbol, another in `paper_trading.py` used by the timeframe strategy. They may disagree on the structure of `chart_indicators`.

**Impact:** ATR values used for breakout buffer calculation in `step_signal_timeframe` (which calls `paper_trading._get_atr`) may differ from ATR values used in other parts of the pipeline, producing inconsistent buffer widths.

**Fix:** Unify to a single `get_atr(scan_context, symbol)` function with clear contract about `chart_indicators` structure.

---

### P2-04: `_is_payload_stale` Uses Fixed Age Threshold Without Symbol Awareness

**File:** `src/fetchers/chart_fetcher.py` (around `_is_payload_stale`)

**Bug:** The staleness threshold is a fixed duration. During market holidays or weekends, even a valid "latest" payload is marked stale, triggering unnecessary fallback fetches and log noise.

**Impact:** Unnecessary DB fallback reads and log spam on non-trading days.

**Fix:** Check `market_window()` first — if market is closed, extend the staleness threshold to cover the full closed period.

---

## 4. LOW — P3

### P3-01: `_parse_dt_utc` May Return `None` Silently

**File:** `src/fetchers/chart_fetcher.py` (multiple call sites)

**Bug:** `_parse_dt_utc` returns `None` on parse failure. Callers like `_fetch_local_ohlc_from_db` (line 935-940) silently skip rows where parsing fails, potentially building OHLC bars from an incomplete price sequence.

**Impact:** A single corrupt `fetched_at` entry in the DB drops a price point, potentially skewing the high/low of the OHLC bar.

**Fix:** Log a warning on parse failure with the offending value so operators can identify and clean up corrupt DB rows.

---

### P3-02: `_STATE` Lock Contention Under High-Frequency Scanning

**File:** `src/fetchers/chart_fetcher.py:1415`

```python
with _STATE_LOCK:
```

**Bug:** A single `threading.Lock` serializes all `_merge_state` calls across all symbols and timeframes. With 20 symbols scanning every 5 minutes on a multi-threaded schedule, this becomes a contention point.

**Impact:** Minor latency under normal load; may become material under burst scenarios (market open, news events).

**Fix:** Use `_STATE_LOCK` per-symbol or switch to `threading.RLock` with finer-grained sharding. Low priority — not observable at current scan volumes.

---

## 5. Cross-Cutting Issues

### CCI-01: MCX Symbol Lifecycle Gap

Three separate bugs (P0-03, P1-02, P1-03) compound for MCX symbols on first scan after restart:
1. `_fetch_local_ohlc_from_db` returns no `prev_ohlc` → `prev_3h = None`
2. `underlying_price` stays 0 → FUT SL/TGT falls back to stale `avg_price`
3. `_last_closed_window` returns incomplete bar at session start → invalid OHLC

**Result:** MCX timeframe strategy is non-functional on first scan cycle, and MCX FUT SL/TGT is computed from stale data even after the first cycle.

### CCI-02: No Centralized `chart_indicators` Structure Validation

`chart_indicators` flows through `pipeline.py` → `decision_pipeline.py` → `step_signal_timeframe` with no schema validation. The structure varies by provider (single-symbol dict vs. multi-symbol dict, nested timeframes, `prev_ohlc` presence). This causes silent fallback chains that mask missing data.

### CCI-03: `timeframe_res` Captured Only for `TIMEFRAME` Strategy ID

In `pipeline.py:824-825`, `timeframe_res` is set only when `sid == "TIMEFRAME"`. If the strategy registry does not include `TIMEFRAME` for a symbol (e.g., custom config), `timeframe_res` stays `None` even if the symbol has timeframe data. The downstream `_build_structured_payload` (line 847) passes `None` → dashboard reports no timeframe data.

---

## 6. Summary Matrix

| ID | Severity | Area | Status | Description |
|---|---|---|---|---|
| P0-01 | CRITICAL | `main.py` | UNFIXED | Global SSL bypass → MITM attack surface |
| P0-02 | CRITICAL | `dashboard_server.py` | UNFIXED | PatchedCursor SQL injection via regex SQL rewrite |
| P0-03 | CRITICAL | `chart_fetcher.py:950` | UNFIXED | `_fetch_local_ohlc_from_db` missing `prev_ohlc` → MCX timeframe strategy blind on restart |
| P1-01 | HIGH | `live_trading.py:2261` | **FIXED** | FUT SL/TGT from stale `avg_price` instead of CMP |
| P1-02 | HIGH | `live_trading.py:2213` | UNFIXED | MCX `underlying_price` always 0 → P1-01 fix ineffective for MCX |
| P1-03 | HIGH | `chart_fetcher.py:683` | UNFIXED | `_last_closed_window` returns incomplete current bar at session start |
| P1-04 | HIGH | `chart_fetcher.py:1415` | UNFIXED | `_STATE` dict unbounded memory leak |
| P1-05 | HIGH | `decision_pipeline.py:681` | UNFIXED | `step_signal_timeframe` silent fallback with non-standard `chart_indicators` structure |
| P2-01 | MEDIUM | `decision_pipeline.py:705` | BY DESIGN | Breakout algo fails in consolidation; extended no-signal periods |
| P2-02 | MEDIUM | `chart_fetcher.py:1187` | UNFIXED | `prev_ts` timezone ambiguity in `_fetch_tv` |
| P2-03 | MEDIUM | `trade_plan.py` / `paper_trading.py` | UNFIXED | Two divergent `_get_atr` implementations |
| P2-04 | MEDIUM | `chart_fetcher.py` | UNFIXED | `_is_payload_stale` doesn't account for market holidays |
| P3-01 | LOW | `chart_fetcher.py` | UNFIXED | `_parse_dt_utc` failures silently skipped |
| P3-02 | LOW | `chart_fetcher.py:1415` | UNFIXED | Single `_STATE_LOCK` contention under high-frequency scans |

---

**Fixes applied this session:** 1 of 14 (P1-01)  
**Remaining:** 13 findings — 2 CRITICAL, 4 HIGH, 3 MEDIUM, 2 LOW
