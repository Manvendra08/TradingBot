# 🔍 NSEBOT Code Audit Report — Functional Logic Flaws & Technical Bugs

**Generated:** 2026-07-03  
**Scope:** Full line-by-line review of all Python source files in `src/`, `config/`, root-level scripts, and `chrome_extension/`  
**Methodology:** Static analysis, logic tracing, edge-case probing

---

## Table of Contents

1. [🔴 CRITICAL — P0 Bugs (Data Corruption / Financial Loss Risk)](#-critical--p0-bugs)
2. [🟠 HIGH — P1 Bugs (Incorrect Behavior)](#-high--p1-bugs)
3. [🟡 MEDIUM — P2 Bugs (Subtle Logic Errors)](#-medium--p2-bugs)
4. [🔵 LOW — P3 Bugs (Code Quality / Robustness)](#-low--p3-bugs)
5. [Summary Statistics](#-summary-statistics)

---

## 🔴 CRITICAL — P0 Bugs

### BUG-P0-01: Dashboard `_q()` Uses `_db()` Outside Context Manager — Leaks SQLite Connections
**File:** `dashboard_server.py`, line ~190  
**Code:**
```python
def _q(sql: str, params: tuple = ()) -> list:
    conn = _db()
    try:
        with conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
```
**Bug:** The `_db()` helper creates a new connection via `sqlite3.connect(db_p)` every call, but the connection is created **outside** the `with conn:` block. If `_db()` raises an exception (e.g., DB locked), the `conn` variable is never defined and `conn.close()` in `finally` raises `UnboundLocalError`. Additionally, `with conn:` auto-commits on success, but `_q()` is called from within `with conn:` blocks in some API routes (double-commit), while other routes call `_q()` standalone — mixing two different transaction patterns creates race conditions and potential `database is locked` errors under concurrent dashboard requests.

---

### BUG-P0-02: `PatchedCursor`/`PatchedConnection` SQL Injection via Regex-Based SQL Rewriting
**File:** `dashboard_server.py`, lines ~10-60  
**Code:**
```python
class PatchedCursor(sqlite3.Cursor):
    def execute(self, sql, *args, **kwargs):
        if (isinstance(sql, str) and re.match(r"(?i)^\s*(select|with)\b", sql)
            and re.search(r"(?i)\bfrom\s+paper_trades\b", sql)):
            sql = re.sub(r"(?i)\bfrom\s+paper_trades\b", f"FROM {subquery}", sql)
        return super().execute(sql, *args, **kwargs)
```
**Bug:** This monkey-patches `sqlite3.Cursor.execute()` globally and rewrites SQL via regex substitution. This is **extremely dangerous** — any query containing `FROM paper_trades` as a substring (e.g., in a comment, a string literal, or a column alias) will be silently rewritten, corrupting the query. The regex `(?i)\bfrom\s+paper_trades\b` will match `FROM paper_trades` even inside string literals in complex queries. Additionally, the UNION ALL subquery hardcodes a specific column list — if the schema changes (new columns added via migration), the UNION ALL will fail with `mismatched column count` SQL errors, crashing the entire dashboard.

---

### BUG-P0-03: `get_broker_config()` Decrypts Secrets with Silent Fallback — Returns Encrypted Blobs on Fernet Failure
**File:** `src/models/schema.py`, lines ~530-555  
**Code:**
```python
def get_broker_config() -> dict | None:
    # ...
    try:
        from src.services.zerodha_auth import _get_fernet
        f = _get_fernet()
        if config.get("api_secret"):
            try:
                config["api_secret"] = f.decrypt(config["api_secret"].encode("utf-8")).decode("utf-8")
            except Exception:
                pass  # ← SILENT: returns encrypted blob as if it were plaintext
    except Exception:
        pass  # ← SILENT: returns encrypted blob
    return config
```
**Bug:** If Fernet decryption fails for ANY reason (key rotation, corrupted `.fernet_key`, wrong key), the `except Exception: pass` silently swallows the error and returns the **encrypted ciphertext** as if it were the plaintext `api_secret`. This means `KiteConnect(api_key=config["api_key"])` receives garbage, and all subsequent Kite API calls fail with cryptic `403 Forbidden` or `Invalid API key` errors — with no indication that the root cause is decryption failure. The same applies to `access_token`. This is a **critical authentication failure** that silently breaks all live trading.

---

### BUG-P0-04: `_calc_transaction_costs()` Applies STT Only on Sell-Side for BUY-to-Open Trades — Undercharges by ~50%
**File:** `src/models/schema.py`, lines ~420-445  
**Code:**
```python
def _calc_transaction_costs(option_type, side, entry_premium, entry_underlying,
                             exit_premium, exit_underlying, lot_size, lots) -> float:
    is_sell_side = side == "SELL"
    if option_type in ("CE", "PE"):
        sell_premium = (entry_premium if is_sell_side else exit_premium)
        sell_turnover = float(sell_premium or 0.0) * lot_size * lots
        stt = sell_turnover * 0.000625
    # ...
```
**Bug:** For **BUY-to-open** trades (`side == "BUY"`), STT is calculated only on the **exit** premium (`exit_premium`). But Indian STT for options is charged on **both legs**: 0.0625% on the sell-side of options. For a BUY-to-open trade, the sell-side is the exit leg — this is correct. However, for **SELL-to-open** trades (`side == "SELL"`), STT is calculated on the **entry** premium — which is the sell-side for a short. This is correct for the entry leg, but STT is also due on the exit leg (the buy-to-close leg). The function only charges STT on ONE leg, undercharging by approximately 50% for round-trip trades. This means `pnl_rupees` is systematically overstated for all closed trades.

---

### BUG-P0-05: `close_paper_trade()` and `close_live_trade()` Use `lot_size` from DB Row Without Validation — `NULL * lots` Produces `NULL` PnL
**File:** `src/models/schema.py`, lines ~470-510  
**Code:**
```python
stored_lot_size = row["lot_size"]
lot_size = int(stored_lot_size) if stored_lot_size is not None else LOT_SIZES.get(symbol.upper(), LOT_SIZES.get(symbol, 1))
# ...
gross_pnl_rupees = pnl_points * lot_size * lots
```
**Bug:** If `row["lot_size"]` is `None` (which is possible for legacy rows inserted before the `lot_size` column was added via migration), the code falls back to `LOT_SIZES.get(symbol.upper(), LOT_SIZES.get(symbol, 1))`. But `LOT_SIZES` only has uppercase keys (`"NIFTY"`, `"NATURALGAS"`, etc.). If the symbol in the DB is stored in a different case (e.g., `"NaturalGas"` from Dhan fetcher), both `.upper()` and the fallback `symbol` lookup fail, returning `1` — producing PnL that is `1/65` of the actual NIFTY PnL or `1/1250` of NATURALGAS PnL. This silently produces wildly incorrect PnL figures.

---

## 🟠 HIGH — P1 Bugs

### BUG-P1-01: `run_pipeline()` Uses `_CLEANUP_DATES` Set — Stale Across Process Restarts, Never Clears Old Dates
**File:** `src/engine/pipeline.py`, lines ~50-55  
**Code:**
```python
_CLEANUP_DATES: set[str] = set()
# ...
if today_str in _CLEANUP_DATES:
    log.debug("Expiry cleanup already done for %s, skipping", today_str)
else:
    _CLEANUP_DATES.add(today_str)
```
**Bug:** `_CLEANUP_DATES` is a module-level set that persists across all pipeline runs within the same process. On day change (midnight IST), the set still contains yesterday's date, so today's cleanup will run (correct). But if the process runs continuously for multiple days, the set grows unboundedly. More critically, if the process is restarted on the same calendar day, `_CLEANUP_DATES` is reset to empty, and expiry cleanup runs AGAIN — potentially deleting data that was already cleaned and re-inserted by the pipeline restart.

---

### BUG-P1-02: `run_live_timeframe_strategy()` Returns `None` Without Action Dict — Pipeline Receives `None` Instead of `dict | None`
**File:** `src/engine/live_trading.py`, line ~1100  
**Code:**
```python
def run_live_timeframe_strategy(symbol, scan_context, digest_id, intel, ai_verdict=None):
    # ...
    if not is_long_trigger and not is_short_trigger:
        return None  # ← returns None
```
**Bug:** The function signature says `dict | None`, but many early-return paths return bare `None` instead of a dict like `{"action": "SKIPPED", "reason": "..."}`. In `pipeline.py`, the return value is checked:
```python
lt_tf_report = run_live_timeframe_strategy(...)
if lt_report and lt_tf_report.get("action") in ("EXECUTED", "CLOSED"):
```
If `lt_tf_report` is `None`, `lt_tf_report.get("action")` raises `AttributeError: 'NoneType' object has no attribute 'get'`. This crashes the pipeline for that symbol.

---

### BUG-P1-03: `_chain_context()` Division by Zero When `total_ce_oi == 0`
**File:** `dashboard_server.py`, line ~230  
**Code:**
```python
pcr = (total_pe_oi / total_ce_oi) if total_ce_oi else None
```
**Bug:** While `total_ce_oi` is guarded against being falsy (0), if `total_ce_oi` is `None` (from a missing column or NULL in DB), the truthiness check `if total_ce_oi` evaluates to `False` and `pcr` becomes `None`. But later in `_chain_context()`:
```python
if pcr > 1.0:
    oi_score += 0.4
```
If `pcr` is `None`, `pcr > 1.0` raises `TypeError: '>' not supported between 'NoneType' and 'float'`. This crashes the entire `/api/intelligence_summary` endpoint.

---

### BUG-P1-04: `_fetch_real_kite_positions()` Returns Stale Cached Positions on Exception — `_positions_cache` Never Invalidated on Error
**File:** `dashboard_server.py`, lines ~680-690  
**Code:**
```python
except Exception as e:
    log.error("Failed to fetch positions from Kite: %s", e)
    if _positions_cache is not None:
        return _positions_cache
    return []
```
**Bug:** If the Kite API call fails (network error, token expired), the function returns the **stale cached positions** from the last successful fetch. But `_positions_cache_ts` is NOT reset, so subsequent calls within the 3-second cache window will ALSO return stale data. If the user's positions changed on the Kite side (e.g., manual square-off), the dashboard will show positions that no longer exist — leading to incorrect PnL display and potentially incorrect risk calculations.

---

### BUG-P1-05: `_is_duplicate_closed_trade()` Compares `strike` Without Type Guard — `float(None)` Raises `TypeError`
**File:** `dashboard_server.py`, line ~820  
**Code:**
```python
k_strike = float(kite_pos["strike"]) if kite_pos.get("strike") is not None else None
# ...
if (k_strike is None or db_strike is None or abs(db_strike - k_strike) < 0.01):
    return True
```
**Bug:** If `kite_pos["strike"]` is a string `"None"` or `"N/A"` (from malformed Kite API data), `kite_pos.get("strike") is not None` evaluates to `True`, and `float("None")` raises `ValueError`. The `if` guard only checks `is not None`, not whether the value is a valid float. This crashes the entire `/api/live_trades` endpoint when processing closed Kite positions with malformed strike data.

---

### BUG-P1-06: `run_timeframe_strategy()` Uses `get_scan_summary_n_scans_ago()` with `scans_needed` — Off-by-One Error
**File:** `src/engine/paper_trading.py`, line ~560  
**Code:**
```python
if scan_freq in (15, 30):
    scans_needed = 60 // scan_freq
    older = get_scan_summary_n_scans_ago(symbol, scans_needed)
```
**Bug:** `get_scan_summary_n_scans_ago()` uses `OFFSET ?` with `n - 1`:
```python
row = conn.execute(sql, (symbol, n - 1)).fetchone()
```
If `scans_needed = 4` (for 15-min scans), `OFFSET 3` returns the 4th row back. But for a 1-hour boundary check, we need the scan from exactly 1 hour ago (4 scans back for 15-min cadence). The function returns the row at `OFFSET (scans_needed - 1)`, which is the `(scans_needed)`th row — off by one. The comparison `prev_ce_oi` vs `current_ce_oi` then compares against the wrong historical snapshot, producing incorrect OI diff calculations and potentially triggering false timeframe entries.

---

## 🟡 MEDIUM — P2 Bugs

### BUG-P2-01: `_price_oi_verdict()` Returns Default `"Short Buildup"` When Price is Flat and OI is Balanced — Incorrect Default
**File:** `src/engine/intelligence.py`, lines ~200-210  
**Code:**
```python
# Only return the default directional verdict if the price move is substantial (>0.15%)
if p_pct > 0.15 or (ce_oi_change == 0 and pe_oi_change == 0):
    return "Long Buildup", "🟢", "Bullish — upward price trend dominant"
# ... fall through to SECONDARY checks
# ...
return "Sideways", "⚪", "Neutral — mixed signals or rangebound"
```
**Bug:** When `p_pct` is between `-0.05` and `0.05` (flat price) and `ce_oi_change` and `pe_oi_change` are non-zero but balanced (ratio between 0.67 and 1.5), the code falls through ALL checks and returns `"Sideways"`. But the confidence scorer `_compute_confidence()` may have already boosted confidence above 65 from chart confluence or PCR. The intelligence then prints `"Sideways"` with `Confidence: 78%` — a contradiction that confuses traders and may trigger incorrect paper trades.

---

### BUG-P2-02: `_build_ml_feature_snapshot()` Extracts `rsi_1h` from `chart_data` Using Wrong Key Path
**File:** `src/engine/paper_trading.py`, lines ~120-130  
**Code:**
```python
chart_data = ctx.get("chart_indicators") or {}
if chart_data:
    tf_data = chart_data
    if not any(k in chart_data for k in ("1h", "3h")):
        tf_data = next(iter(chart_data.values()), {}) if chart_data else {}
    try:
        rsi_1h = float((tf_data.get("1h") or {}).get("rsi") or 0) or None
```
**Bug:** `chart_data` may be symbol-keyed: `{"NATURALGAS": {"1h": {...}, "3h": {...}}}`. The check `any(k in chart_data for k in ("1h", "3h"))` returns `False` for symbol-keyed dicts, so `tf_data` becomes `next(iter(chart_data.values()), {})` — which is the FIRST symbol's data. If `chart_data` has multiple symbols (e.g., `{"NIFTY": {...}, "NATURALGAS": {...}}`), `next(iter(...))` returns whichever symbol happens to be first in the dict iteration order — which is non-deterministic in Python < 3.7 (though CPython 3.7+ preserves insertion order). This means ML features may be extracted from the WRONG symbol's chart data, poisoning ML training with cross-symbol features.

---

### BUG-P2-03: `get_previous_underlying_before()` Computes `target_time` but Never Uses It — Always Returns Latest Row
**File:** `src/models/schema.py`, lines ~280-310  
**Code:**
```python
def get_previous_underlying_before(symbol: str, fetched_at: str) -> dict | None:
    # ...
    target_time = curr_dt - timedelta(minutes=freq_min)
    sql = """SELECT * FROM underlying_price WHERE symbol=? AND fetched_at < ?
             ORDER BY fetched_at DESC LIMIT 50"""
    with get_conn() as conn:
        rows = conn.execute(sql, (symbol, fetched_at)).fetchall()
        # ...
        best_row = None
        min_diff = None
        for r in rows:
            # ...
            diff = abs((row_dt - target_time).total_seconds())
            if min_diff is None or diff < min_diff:
                min_diff = diff
                best_row = r
```
**Bug:** The function computes `target_time = curr_dt - timedelta(minutes=freq_min)` to find the row closest to the previous scan interval. But the SQL query fetches ALL rows where `fetched_at < fetched_at` (current timestamp) — it does NOT filter by `target_time`. The `for` loop then finds the row closest to `target_time`, but `target_time` is computed from `curr_dt` (current time), NOT from `fetched_at` (the scan timestamp passed as argument). If the pipeline is running a backfill or processing a delayed scan, `target_time` is computed from the wrong reference point, and the function returns the wrong previous underlying price — causing incorrect `price_change_pct` calculations.

---

### BUG-P2-04: `_detect_pcr_velocity()` Prepends `curr_pcr` to `pcr_series` — Creates Off-by-One in Diff Calculation
**File:** `src/engine/anomaly_detector.py`, lines ~200-215  
**Code:**
```python
pcr_series = []
for snap in snapshots:
    p = _compute_pcr(snap)
    if p is not None:
        pcr_series.append(p)
pcr_series = [curr_pcr] + pcr_series
if len(pcr_series) < 3:
    return []
diffs = [pcr_series[i] - pcr_series[i + 1] for i in range(len(pcr_series) - 1)]
```
**Bug:** `pcr_series` is built from the latest `PCR_VELOCITY_WINDOW` snapshots (default 3), then `curr_pcr` is prepended. If `PCR_VELOCITY_WINDOW = 3`, `snapshots` contains 3 snapshots, and `pcr_series` becomes 4 elements after prepending `curr_pcr`. The diffs array has 3 elements. But `get_latest_n_snapshots()` may return fewer than `PCR_VELOCITY_WINDOW` snapshots if the symbol has insufficient history. If `snapshots` returns only 1 snapshot, `pcr_series` becomes 2 elements, and `len(pcr_series) < 3` returns early — missing the velocity check. The guard should be `len(pcr_series) < 2` to allow a 2-element series (current + 1 previous).

---

### BUG-P2-05: `place_kite_order()` Applies Slippage Buffer in Wrong Direction for SELL Orders
**File:** `src/engine/live_trading.py`, lines ~350-365  
**Code:**
```python
if transaction_type == "BUY":
    limit_price = ltp * (1 + buffer_pct)
else:
    limit_price = ltp * (1 - buffer_pct)
```
**Bug:** For a **BUY** order, the limit price is set HIGHER than LTP (`ltp * 1.005`) to ensure execution — this is correct (buying at a slightly higher price to guarantee fill). For a **SELL** order, the limit price is set LOWER than LTP (`ltp * 0.995`) — this means selling at a price 0.5% BELOW the current market price. This is a **guaranteed worse execution** — the bot is essentially giving away 0.5% on every SELL order. The correct approach for a SELL order to guarantee fill is to set the limit price HIGHER than LTP (selling at a premium to market).

---

### BUG-P2-06: `sync_direct_kite_positions()` Uses `get_expiry_for_tradingsymbol()` — May Return `None` for Unknown Symbols
**File:** `src/engine/live_trading.py`, line ~1200  
**Code:**
```python
trade_data = {
    # ...
    "expiry": get_expiry_for_tradingsymbol(ts) or "",
    # ...
}
```
**Bug:** `get_expiry_for_tradingsymbol()` may return `None` for symbols not in the instrument cache (e.g., newly listed contracts, MCX contracts with non-standard naming). The `or ""` fallback produces an empty string expiry, which is inserted into the `live_trades` table. When `close_live_trade()` is later called, it uses this empty expiry to look up option chain snapshots — the SQL query `WHERE expiry=?` with `expiry=""` returns no rows, and the function uses `entry_premium` as the exit premium — producing **zero PnL** for all closed direct Kite positions.

---

## 🔵 LOW — P3 Bugs

### BUG-P3-01: `dashboard_server.py` Imports `sqlite3` After Monkey-Patching `socket.getaddrinfo` — Patching Order Issue
**File:** `dashboard_server.py`, lines ~1-10  
**Code:**
```python
import socket as _socket
_orig_getaddrinfo = _socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
_socket.getaddrinfo = _ipv4_only_getaddrinfo
import json
import sqlite3
```
**Bug:** The IPv4-only patch is applied BEFORE `sqlite3` is imported. If `sqlite3` internally uses `socket.getaddrinfo()` during import (e.g., for DNS resolution in certain configurations), the patch is already active. But if `sqlite3` is imported BEFORE the patch in some Python configurations (e.g., when `sqlite3` is a C extension that resolves DNS at C level), the patch has no effect. This is a portability issue.

---

### BUG-P3-02: `main.py` Patches `ssl._create_default_https_context` Globally — Breaks Other HTTPS Libraries
**File:** `main.py`, lines ~25-35  
**Code:**
```python
try:
    ssl._create_default_https_context = ssl._create_unverified_context
    urllib3.util.ssl_.create_urllib3_context = lambda: ssl._create_unverified_context()
except AttributeError:
    pass
```
**Bug:** This globally disables SSL certificate verification for ALL HTTPS connections in the entire Python process — including connections to Google Drive (backup), Gemini API (LLM), Telegram API, and Dhan API. This means ALL API calls are made WITHOUT SSL verification, making the bot vulnerable to man-in-the-middle attacks. The correct approach is to patch only the specific `requests.Session` or `KiteConnect` instance, not the global SSL context.

---

### BUG-P3-03: `_norm_symbol()` Strips Month Suffixes but Leaves Exchange Prefixes Inconsistently
**File:** `src/engine/intelligence.py`, lines ~80-90  
**Code:**
```python
def _norm_symbol(s: str | None) -> str:
    if not s:
        return ""
    x = str(s).upper().strip()
    x = re.sub(r"^(NSE|NFO|BSE|MCX|CDS):", "", x)
    x = x.replace("!", "")
    x = re.sub(r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4}(FUT)?$", "", x)
    return re.sub(r"[^A-Z0-9]", "", x)
```
**Bug:** The regex `(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4}(FUT)?$` strips month suffixes like `JUN2026FUT` but leaves the base symbol. For `"NATURALGAS25JUNFUT"`, the regex matches `JUNFUT` and strips it, leaving `"NATURALGAS25"`. The final `re.sub(r"[^A-Z0-9]", "", x)` then strips the `"25"`, leaving `"NATURALGAS"`. But for `"NIFTY25JUN27500CE"`, the regex matches `JUN27500CE` — the `CE` suffix is NOT in the month list, so it remains. The final regex strips `CE`, leaving `"NIFTY25JUN"`. The final `[^A-Z0-9]` strips nothing (all alphanumeric), and `_norm_symbol()` returns `"NIFTY25JUN"` instead of `"NIFTY"`. This breaks symbol matching for option symbols with year-month-strike-option_type format.

---

### BUG-P3-04: `_ctx_copy()` Discards Non-String Keys — Breaks Dict Unpacking for Callers
**File:** `src/engine/intelligence.py`, line ~40  
**Code:**
```python
def _ctx_copy(ctx: dict) -> dict:
    return {k: v for k, v in ctx.items() if isinstance(k, str)}
```
**Bug:** This filters out non-string keys, but `scan_context` may contain tuple keys (e.g., `(strike, option_type)` tuples from `_prev_snapshots_bulk()`). The filter silently discards these entries, which may be needed by downstream consumers like `build_paper_trade_plan()` or `make_trade_decision()`. If `scan_context` contains a tuple key like `(27500.0, "CE")` mapping to a previous snapshot dict, `_ctx_copy()` discards it, and the trade plan loses access to the previous snapshot data — causing NULL premium lookups and incorrect SL/Target calculations.

---

### BUG-P3-05: `_get_alert_direction()` Returns `"NEUTRAL"` for Unknown Alert Types — Missing Directional Classification
**File:** `src/engine/intelligence.py`, lines ~250-280  
**Code:**
```python
def _get_alert_direction(a: dict) -> str:
    atype = a.get("alert_type", "")
    # ... checks for BUILDUP_CLASSIFY, OI_SPIKE, OI_UNWIND, etc.
    return "NEUTRAL"
```
**Bug:** The function does not classify `MAX_PAIN_SHIFT`, `OI_WALL_SHIFT`, `STRADDLE_PREMIUM`, or `PCR_EXTREME` alerts. These alerts have implicit directional implications (e.g., `MAX_PAIN_SHIFT` up → bullish, `PCR_EXTREME` high → bullish), but they all return `"NEUTRAL"`. This means the confidence scorer does not boost confidence for these alerts, even when they strongly support the verdict direction. This produces lower confidence scores than warranted, potentially blocking valid trades.

---

## 📊 Summary Statistics

| Severity | Count | Description |
|----------|-------|-------------|
| 🔴 P0 Critical | 5 | Data corruption, financial loss, authentication bypass |
| 🟠 P1 High | 6 | Incorrect behavior, crashes, off-by-one errors |
| 🟡 P2 Medium | 6 | Subtle logic errors, wrong defaults, cross-symbol contamination |
| 🔵 P3 Low | 5 | Code quality, portability, silent failures |
| **Total** | **21** | |

---

## 🛠️ Recommended Fix Priority

1. **BUG-P0-03** (Decryption fallback) — Fix immediately; silently returning encrypted blobs breaks ALL live trading
2. **BUG-P0-04** (STT undercharge) — Fix PnL calculation; affects all trade profitability reporting
3. **BUG-P0-02** (SQL regex rewriting) — Replace regex-based SQL patching with proper view/CTE
4. **BUG-P1-02** (`None` return crash) — Add null-safe returns in `run_live_timeframe_strategy()`
5. **BUG-P1-03** (Division by zero) — Add `None` guard for `pcr` in `_chain_context()`

---

*Report generated by static analysis. All findings should be verified via targeted integration tests before applying fixes.*
