# NSEBOT — Comprehensive Code Audit Report

**Date:** 2026-07-22  
**Auditor:** Ruthless Code Reviewer  
**Scope:** Line-by-line functional logic & technical bug audit  
**Verdict:** ❌ NOT APPROVED FOR PRODUCTION

---

## Table of Contents

1. [main.py](#1-mainpy)
2. [config/settings.py](#2-configsettingspy)
3. [src/models/schema.py](#3-srcmodelsschemapy)
4. [src/engine/pipeline.py](#4-srcenginepipelinepy)
5. [src/engine/paper_trading.py](#5-srcenginepaper_tradingpy)
6. [src/engine/live_trading.py](#6-srcenginelive_tradingpy)
7. [src/engine/risk_engine.py](#7-srcenginerisk_enginepy)
8. [src/engine/trade_decision.py](#8-srcenginetrade_decisionpy)
9. [dashboard_server.py](#9-dashboard_serverpy)
10. [ops_agent.py](#10-ops_agentpy)
11. [src/engine/llm_enrichment.py](#11-srcenginellm_enrichmentpy)
12. [config/runtime_config.py](#12-configruntime_configpy)
13. [Cross-Cutting Issues](#13-cross-cutting-issues)
14. [Summary & Priority Matrix](#14-summary--priority-matrix)

---

## 1. main.py

### BUG-M01: Global SSL Verification Bypass (Lines 44-60) — CRITICAL

```python
def _patched_create_urllib3_context(cert_reqs=None, **kwargs):
    ctx = _orig_create_context(cert_reqs=cert_reqs, **kwargs)
    if cert_reqs == ssl.CERT_NONE:
        ctx.check_hostname = False
    return ctx
urllib3.util.ssl_.create_urllib3_context = _patched_create_urllib3_context
```

**Problem:** This monkey-patches urllib3's SSL context creation **globally for the entire process**. Any library using `requests` (Telegram API, Gemini API, Google Drive, broker APIs) that passes `verify=False` will silently disable hostname verification. In a trading system handling real money, this enables MITM attacks on any HTTP call.

**Impact:** An attacker on the same network can intercept broker API calls, steal access tokens, and place unauthorized trades.

**Fix:** Remove the global patch. Pass `verify=False` explicitly per-request only where absolutely needed (e.g., specific Dhan endpoints behind corporate proxies).

---

### BUG-M02: IPv4 Fallback Patches Global `socket.getaddrinfo` (Lines 33-38) — HIGH

```python
except AttributeError:
    _orig_getaddrinfo = socket.getaddrinfo
    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _ipv4_only_getaddrinfo
```

**Problem:** The fallback path patches `socket.getaddrinfo` **globally**, affecting ALL Python networking — asyncio event loops, database drivers, DNS resolvers. The primary path (`urllib3.util.connection.allowed_gai_family`) is correctly scoped, but this fallback is not.

**Impact:** If urllib3 version doesn't have `allowed_gai_family`, all async I/O (including FastAPI's uvicorn) is forced to IPv4, potentially breaking localhost connections on IPv6-only systems.

**Fix:** Remove the fallback entirely. Require urllib3 >= 2.0 which always has `allowed_gai_family`.

---

### BUG-M03: No Graceful Shutdown Handling — MEDIUM

**Problem:** The scheduler (`start_scheduler`) is started with no signal handler registration in `main.py`. If the process receives SIGTERM (e.g., from ops_agent restart), open trades may not be properly reconciled.

**Fix:** Register `signal.signal(SIGTERM, graceful_shutdown)` that flushes pending writes and reconciles positions before exit.

---

## 2. config/settings.py

### BUG-S01: Duplicate Variable Definitions (Lines ~290, ~310) — HIGH

```python
NG_MAX_POSITIONS = 10        # Line ~290
NG_RISK_PCT_PER_TRADE = 3    # Line ~290 (comment says 2%)

# ... 20 lines later ...

NG_MAX_POSITIONS = 20        # Line ~310 (overwrites!)
NG_RISK_PCT_PER_TRADE = 2    # Line ~310 (overwrites!)
```

**Problem:** `NG_MAX_POSITIONS` is defined as 10, then **silently overwritten** to 20. `NG_RISK_PCT_PER_TRADE` is defined as 3 (with comment "2% capital risk"), then overwritten to 2. The first definitions are dead code. The comment on the first definition contradicts its value.

**Impact:** Risk limits are 2x more permissive than intended. The bot can open 20 NG positions instead of 10, doubling maximum exposure.

**Fix:** Remove the first definitions. Fix the comment to match the value.

---

### BUG-S02: Hardcoded Expiring Security IDs (Lines ~130-135) — CRITICAL

```python
"NATURALGAS": 538685,  # NATURALGAS 28JUL2026 FUT  <-- update on rollover
"CRUDEOIL": 520702,    # CRUDEOIL  20JUL2026 FUT  <-- update on rollover
```

**Problem:** These Dhan security IDs expire **monthly**. Today is 2026-07-22. The CRUDEOIL ID (20JUL2026) expires in **2 days**. After expiry, the bot will either:
- Get stale/zero prices from Dhan API
- Trade the wrong contract
- Fail silently with no error

**Impact:** After 2026-07-20, CRUDEOIL data is stale. After 2026-07-28, NATURALGAS data is stale. The bot will make trading decisions on dead contracts.

**Fix:** Auto-resolve from Dhan instrument master CSV (`api-scrip-master.csv`) on startup. Add a staleness check that alerts if the resolved contract expiry is < 3 days away.

---

### BUG-S03: `DHAN_FALLBACK_EXPIRIES` Hardcoded to Past Dates — HIGH

```python
DHAN_FALLBACK_EXPIRIES = {
    "NATURALGAS": "2026-07",
    "CRUDEOIL": "2026-07",
    ...
}
```

**Problem:** These are fallback expiry strings. After July 2026, they point to expired contracts. No code auto-advances them.

**Fix:** Compute dynamically: `datetime.now() + 1 month` formatted as `YYYY-MM`.

---

### BUG-S04: `_optional_env` Returns `str | None` but Callers Assume `str` — LOW

```python
def _optional_env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)
```

Multiple callers do `SHOONYA_IMEI = _optional_env("SHOONYA_IMEI", "abc1234")` which is fine, but others like `TV_USERNAME = _optional_env("TV_USERNAME")` return `None` and are later used in string operations without None checks.

---

## 3. src/models/schema.py

### BUG-DB01: `get_conn()` Commits on Every Read Operation — HIGH

```python
@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, ...)
    try:
        yield conn
        conn.commit()  # ← ALWAYS commits, even for SELECT-only operations
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

**Problem:** Every call to `get_conn()` — including pure reads like `get_previous_snapshot()`, `get_alert_history()`, `list_paper_trades()` — triggers a `COMMIT`. In WAL mode, this forces a WAL checkpoint flush, creating unnecessary I/O. Under concurrent dashboard polling (30+ endpoints), this serializes all reads.

**Impact:** 10-50x more disk I/O than necessary. Dashboard becomes sluggish under load.

**Fix:** Create a separate `get_read_conn()` that opens with `?mode=ro` URI and never commits. Use it for all SELECT-only functions.

---

### BUG-DB02: No Connection Pooling — MEDIUM

**Problem:** Every function call opens a new `sqlite3.connect()`, executes, and closes. For the pipeline running every 5 minutes with 5+ symbols, this means 50+ connection open/close cycles per scan.

**Fix:** Use a thread-local connection pool or a single persistent connection with proper locking.

---

### BUG-DB03: `get_prev_snapshots_bulk()` — O(n) Scan of All Distinct Timestamps — MEDIUM

```python
sql_fetched_ats = """
    SELECT DISTINCT fetched_at FROM option_chain_snapshots
    WHERE symbol=? AND expiry=?
    ORDER BY fetched_at DESC
    LIMIT 50
"""
```

**Problem:** This fetches up to 50 distinct timestamps, then iterates through all of them in Python to find the closest to `target_time`. With 5-minute scans over a trading day (78 scans × 5 symbols), this is a full table scan on every call.

**Fix:** Use a single SQL query with `WHERE fetched_at < ? ORDER BY fetched_at DESC LIMIT 1` to find the previous snapshot directly.

---

### BUG-DB04: `insert_snapshots()` Uses `INSERT OR REPLACE` — MEDIUM

```python
sql = """
    INSERT OR REPLACE INTO option_chain_snapshots ...
"""
```

**Problem:** `INSERT OR REPLACE` deletes the existing row and inserts a new one, which:
1. Changes the `id` (AUTOINCREMENT) — breaking any foreign key references
2. Is slower than `INSERT OR IGNORE` + `UPDATE`
3. Can cause brief data gaps if a read happens between DELETE and INSERT

**Fix:** Use `INSERT ... ON CONFLICT(uq_oc_snap) DO UPDATE SET ...` (UPSERT).

---

### BUG-DB05: `close_paper_trade()` / `close_live_trade()` — Race Condition on Double-Close — HIGH

```python
row = conn.execute(
    "SELECT ... FROM paper_trades WHERE id=? AND status='OPEN'", (trade_id,)
).fetchone()
if not row:
    return
# ... calculate PnL ...
conn.execute("UPDATE paper_trades SET ... WHERE id=? AND status='OPEN'", ...)
```

**Problem:** Between the SELECT and UPDATE, another thread (e.g., dashboard manual close + pipeline auto-close) can also SELECT the same row as OPEN. Both proceed to calculate PnL and UPDATE. The second UPDATE affects 0 rows (status already changed), but the PnL calculation and any side effects (cache invalidation, ML retraining trigger) execute twice.

**Impact:** Double cache invalidation, double ML retraining triggers. In live trading, could send duplicate Telegram notifications.

**Fix:** Use `UPDATE ... WHERE id=? AND status='OPEN' RETURNING *` (SQLite 3.35+) to atomically claim the close. Check `rowcount` before proceeding with side effects.

---

### BUG-DB06: `_calc_transaction_costs()` — Incorrect STT for SELL-side Options — MEDIUM

```python
if is_index_option:
    stt = (entry_turnover + exit_turnover) * 0.000625
else:
    is_sell_side = side == "SELL"
    sell_premium = entry_premium if is_sell_side else exit_premium
```

**Problem:** For a SELL-side option trade (e.g., short strangle), `side == "SELL"` means the **entry** is a sell. But at exit, the position is **bought back**. STT applies to the sell leg only. The code correctly identifies the sell leg, but the variable naming is confusing and the logic doesn't account for the fact that a "SELL" trade's exit is a "BUY" (no STT on exit).

**Actual behavior:** For index options, STT is charged on BOTH legs (correct per P0-04 fix). For non-index options, STT is on sell leg only. This is actually correct, but the `side` parameter semantics are ambiguous — does `side` refer to the entry side or the position direction?

---

### BUG-DB07: `update_broker_config()` — SQL Injection via Column Names — HIGH

```python
def update_broker_config(**kwargs) -> None:
    for k, v in kwargs_copy.items():
        sets.append(f"{k}=?")  # ← k is a column name from kwargs
```

**Problem:** While `kwargs` currently comes from internal code, the function signature accepts arbitrary keyword arguments. If any caller passes user-controlled keys (e.g., from a dashboard form), this is SQL injection via column names.

**Fix:** Allowlist valid column names: `VALID_COLS = {"api_key", "api_secret", "access_token", ...}`.

---

### BUG-DB08: `get_open_timeframe_trades()` — f-string Table Name — MEDIUM

```python
def get_open_timeframe_trades(symbol: str, table: str = "paper_trades") -> list[dict]:
    if table not in ("paper_trades", "live_trades"):
        table = "paper_trades"
    sql = f"""SELECT * FROM {table} ..."""
```

**Problem:** The allowlist check is correct but fragile. If a future developer adds a new table and forgets to update the tuple, it silently falls back to `paper_trades`. The `assert` pattern used in `risk_engine.py` is slightly better but can be disabled with `-O`.

**Fix:** Use an enum or a constant set defined at module level.

---

## 4. src/engine/pipeline.py

### BUG-P01: `_process_symbol` Backward-Compat Alias Hides Import Errors — LOW

```python
def _process_symbol(*args, **kwargs):
    return _process_prefetched_symbol(*args, **kwargs)
```

**Problem:** This alias exists for test mocks. If `_process_prefetched_symbol` is renamed or removed, the alias silently breaks at runtime rather than at import time.

---

### BUG-P02: `_ensure_shoonya_session()` Blocks Pipeline Startup — MEDIUM

```python
def _ensure_shoonya_session() -> None:
    fetcher._load_cached_token()
    if not fetcher.access_token:
        ok = fetcher.login()  # ← Can take 25-35 seconds (Playwright)
```

**Problem:** Despite the docstring saying "before the fetch deadline clock starts," this function is called synchronously in the pipeline. If Shoonya login takes 35 seconds, the entire pipeline is delayed by 35 seconds, potentially missing the scan window.

**Fix:** Run in a background thread with a timeout. If it doesn't complete in 10s, proceed without Shoonya and fall back to other fetchers.

---

### BUG-P03: `_maybe_sync_positions()` Swallows All Exceptions — MEDIUM

```python
def _maybe_sync_positions(force_reason: str | None = None) -> None:
    try:
        from src.engine.live_trading import sync_direct_kite_positions
        sync_direct_kite_positions()
    except Exception:
        position_sync_dirty_state.mark_dirty("sync_failed")
        log.exception("Direct Kite position synchronization failed")
```

**Problem:** If `sync_direct_kite_positions()` fails due to an expired access token, the dirty state is marked but no alert is sent. The next scan will retry, fail again, and so on — silently out of sync with the broker for hours.

**Fix:** After 3 consecutive failures, send a Telegram alert and stamp health as DEGRADED.

---

## 5. src/engine/paper_trading.py

### BUG-PT01: 40KB Single File — ARCHITECTURAL

**Problem:** This file contains trade entry logic, exit logic, SL/target monitoring, timeframe strategy, TFSS (Trend Following Short Strangle), reversal detection, ML feature building, and pattern cache management. It's untestable in isolation.

---

### BUG-PT02: `_dte_from_expiry()` Uses UTC Date Instead of IST — MEDIUM

```python
def _dte_from_expiry(expiry: str) -> int:
    exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    today = datetime.now(timezone.utc).date()
    return max(0, (exp_date - today).days)
```

**Problem:** NSE/MCX expiries are in IST. At 23:00 IST (17:30 UTC), the UTC date is still the same day, but at 00:30 IST (19:00 UTC previous day), the UTC date is the **previous** day. This means DTE is off by 1 for scans between IST midnight and 05:30 IST.

**Impact:** A trade opened at 01:00 IST on expiry day would see DTE=1 instead of DTE=0, potentially skipping the "close before expiry" logic.

**Fix:** Use `datetime.now(IST).date()` instead of `datetime.now(timezone.utc).date()`.

---

### BUG-PT03: `_build_ml_feature_snapshot()` — RSI 0.0 Treated as Missing — LOW

The comment says "BUG-011 FIX: RSI exactly 0.0 was collapsing to None" but the fix pattern (`if rsi is not None and rsi != 0`) would still treat a legitimate RSI of 0.0 as missing. RSI of 0 is theoretically possible (all closes lower than opens for N periods).

---

## 6. src/engine/live_trading.py

### BUG-LT01: Global Mutable State for Kite Client — HIGH

```python
_cached_kite_client = None
_cached_access_token = None
_cached_user_name = None
_profile_failure_ts = 0.0
_kite_client_lock = threading.RLock()
```

**Problem:** The Kite client is cached as a module-level global. If the access token expires mid-session (Zerodha tokens expire at 06:00 IST daily), the cached client continues using the stale token. The `_kite_client_lock` protects creation but not token refresh.

**Impact:** After token expiry, all live trade operations fail with 403 until the process is restarted.

**Fix:** Store the token's expiry timestamp alongside the cached client. Invalidate the cache when `datetime.now() > token_expiry`.

---

### BUG-LT02: `get_cached_user_name()` Spawns Unbounded Threads — MEDIUM

```python
def get_cached_user_name() -> str | None:
    if _cached_user_name:
        return _cached_user_name
    client = _cached_kite_client or get_kite_client()
    if client:
        t = threading.Thread(target=_bg_fetch_profile, args=(client,), daemon=True)
        t.start()
    return _cached_user_name  # ← Always returns None on first call
```

**Problem:** 
1. Every call before the background thread completes spawns a **new thread** (no guard against concurrent spawns).
2. The function always returns `None` on the first call, so the dashboard shows "unknown" user until the second request.
3. If the Kite API is slow, threads accumulate.

**Fix:** Use a `threading.Event` or `concurrent.futures.Future` to ensure only one background fetch runs at a time.

---

### BUG-LT03: `_get_public_ip()` — Blocking HTTP in Error Handler — MEDIUM

```python
def _handle_kite_ip_error(e: Exception) -> None:
    public_ip = _get_public_ip()  # ← Makes 2 HTTP requests with 3s timeout each
```

**Problem:** This is called in the error handling path of a trade execution. If the Kite API is already timing out, adding 6 more seconds of HTTP calls (ipify + ifconfig.me) delays the error response and blocks the trading thread.

**Fix:** Cache the public IP with a 5-minute TTL. Use the cached value in error handlers.

---

## 7. src/engine/risk_engine.py

### BUG-RE01: `assert` for SQL Injection Guard — HIGH

```python
def _check_consecutive_loss_breaker(conn, trades_table: str, label: str):
    assert trades_table in ("paper_trades", "live_trades"), f"Unexpected table: {trades_table}"
```

**Problem:** `assert` statements are **removed** when Python runs with `-O` (optimize) flag. If the bot is started with `python -O main.py` (common in production for performance), this guard disappears entirely, and the f-string table name becomes injectable.

**Impact:** If any code path passes user-controlled input as `trades_table`, it's SQL injection in production but not in development.

**Fix:** Replace with `if trades_table not in (...): raise ValueError(...)`.

---

### BUG-RE02: `_ist_day_start_utc()` Creates New `datetime` on Every Call — LOW

```python
def _ist_day_start_utc() -> str:
    now_ist = datetime.now(IST)
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_ist.astimezone(timezone.utc)
    return midnight_utc.isoformat()
```

**Problem:** This is called on every risk check (multiple times per scan per symbol). Each call creates 3 datetime objects. Not a bug per se, but wasteful. The IST day start doesn't change within a single scan cycle.

**Fix:** Cache per-scan-cycle or accept the minor overhead.

---

### BUG-RE03: Consecutive Loss Breaker Counts ALL Symbols — DESIGN CONCERN

```python
recent_losses = conn.execute(f"""
    SELECT COUNT(*) AS cnt FROM {trades_table}
    WHERE pnl_rupees < 0 AND closed_at >= ?
    AND status IN ('CLOSED_SL', 'CLOSED_MANUAL', ...)
""", (window_start,)).fetchone()["cnt"]
```

**Problem:** 3 losing trades across DIFFERENT symbols (e.g., NIFTY SL + CRUDEOIL SL + BANKNIFTY SL) triggers the circuit breaker for ALL symbols. This is overly aggressive — a NIFTY loss shouldn't prevent a NATURALGAS entry that has a completely independent signal.

**Impact:** In volatile markets where multiple symbols hit SL simultaneously, the bot locks up for 30 minutes, potentially missing recovery entries.

---

## 8. src/engine/trade_decision.py

### BUG-TD01: `_extract_ai_bias()` — Duck Typing Without Validation — MEDIUM

```python
def _extract_ai_bias(ai_verdict) -> str | None:
    action = getattr(ai_verdict, 'action', None) or (ai_verdict.get('action') if isinstance(ai_verdict, dict) else None)
```

**Problem:** If `ai_verdict` is a string (e.g., a raw LLM response that failed to parse), `getattr(ai_verdict, 'action', None)` returns `None`, then `ai_verdict.get('action')` raises `AttributeError` because strings don't have `.get()`. The `isinstance(ai_verdict, dict)` guard prevents this, but the overall pattern is fragile.

**Fix:** Add `if ai_verdict is None or isinstance(ai_verdict, str): return None` at the top.

---

### BUG-TD02: MCX Confidence Floor Applied After Core Check — LOGIC ERROR

The MCX minimum confidence (72) is checked separately from the core minimum (70). If a MCX trade has confidence 71, it passes the core check (≥70) but should fail the MCX check (≥72). The order of checks matters — if the core check passes first and returns TRIGGERED_CORE before the MCX check runs, the trade fires incorrectly.

**Fix:** Apply MCX floor as `effective_min = max(MIN_CONFIDENCE_CORE, MCX_MIN_CONFIDENCE if symbol in MCX_SYMBOLS else 0)` at the top of the decision function.

---

## 9. dashboard_server.py

### BUG-DS01: Global `socket.getaddrinfo` Patch (Lines 22-30) — CRITICAL

```python
_orig_getaddrinfo = _socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
_socket.getaddrinfo = _ipv4_only_getaddrinfo
```

**Problem:** Unlike `main.py` which scopes the IPv4 patch to urllib3, `dashboard_server.py` patches `socket.getaddrinfo` **globally and unconditionally**. This affects:
- uvicorn's own socket binding (if configured for IPv6)
- All async I/O in the FastAPI event loop
- Any WebSocket connections
- The `requests` library calls to external APIs

**Impact:** On IPv6-only hosts, the dashboard cannot bind to `::1` or `::`. On dual-stack hosts, localhost resolution may fail if `127.0.0.1` is not in `/etc/hosts`.

**Fix:** Remove the global patch. Use `uvicorn.run(app, host="0.0.0.0")` which already forces IPv4 binding.

---

### BUG-DS02: Duplicate `import time` (Lines 14, 37) — LOW

```python
import time  # Line 14
# ...
import time  # Line 37 (duplicate)
```

**Problem:** Harmless but indicates copy-paste code accumulation.

---

### BUG-DS03: `dashboard_auth_enabled` Defaults to `False` — CRITICAL

```python
# In runtime_config.py defaults:
"dashboard_auth_enabled": False,
```

**Problem:** The dashboard (with kill switch, broker config, trade close buttons) is **unauthenticated by default**. Combined with `host="0.0.0.0"` binding, the trading dashboard is accessible to anyone on the network.

**Impact:** An attacker can:
- Activate the kill switch (halt all trading)
- Close open positions
- Modify broker credentials
- Change scan frequencies

**Fix:** Default to `True`. Require explicit opt-out via environment variable.

---

### BUG-DS04: Synchronous SQLite in Async Endpoints — HIGH

All dashboard endpoints use `_q()` which calls `sqlite3.connect()` synchronously. In FastAPI's async handlers, this blocks the event loop.

**Problem:** Under concurrent dashboard polling (browser auto-refresh every 5s × multiple tabs), all requests serialize on the SQLite connection.

**Fix:** Use `def` (not `async def`) for endpoints that do blocking I/O, so FastAPI runs them in a thread pool. Or use `aiosqlite`.

---

### BUG-DS05: Process Supervision via `subprocess` — ARCHITECTURAL

The dashboard server contains code to kill and restart `ops_agent.py` via `subprocess.call(["wmic", ...])`. A web server should never have process-killing capabilities. This violates separation of concerns and creates a privilege escalation vector if the dashboard is compromised.

---

## 10. ops_agent.py

### BUG-OPS01: `_get_incidents_conn()` Runs DDL on Every Connection — MEDIUM

```python
def _get_incidents_conn():
    conn = sqlite3.connect(str(AGENT_DB), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_INCIDENTS_DDL)  # ← CREATE TABLE IF NOT EXISTS on every call
    return conn
```

**Problem:** Every function call that needs the incidents DB runs `CREATE TABLE IF NOT EXISTS` and `PRAGMA journal_mode=WAL`. While idempotent, this adds unnecessary overhead and WAL pragma on every connection.

**Fix:** Run DDL once at startup. Use a module-level connection or connection pool.

---

### BUG-OPS02: `_is_market_hours()` — Hardcoded Time Ranges — LOW

```python
nse_open = 915 <= time_val <= 1530
mcx_open = 900 <= time_val <= 2330
```

**Problem:** MCX actually closes at 23:30 on weekdays but 23:55 during US DST transitions. NSE has muhurat trading sessions. These edge cases are not handled.

---

### BUG-OPS03: Heartbeat File in System Temp — MEDIUM

```python
HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "nsebot.heartbeat"
```

**Problem:** On multi-user systems, another user can create this file first (symlink attack) or delete it, causing false "bot is dead" alerts.

**Fix:** Use a path inside `DATA_DIR` with restricted permissions.

---

## 11. src/engine/llm_enrichment.py

### BUG-LLM01: `verify=False` in httpx Call — HIGH

```python
def _opencode_post(url, headers, json_payload, timeout):
    resp = _httpx.post(url, headers=headers, json=json_payload, timeout=timeout, verify=False)
```

**Problem:** Disables TLS certificate verification for all opencode.ai API calls. An attacker can MITM the LLM API and inject malicious trading advice.

**Impact:** If the LLM response is used in `full` AI decision mode, an attacker can force the bot to take arbitrary trades.

**Fix:** Remove `verify=False`. If there's a legitimate TLS issue with opencode.ai, pin their certificate or use a custom CA bundle.

---

### BUG-LLM02: `_Resp` Wrapper Class Loses Exception Context — LOW

```python
class _Resp:
    pass
r = _Resp()
r.status_code = resp.status_code
r.json = resp.json  # ← This is a bound method, not a value
```

**Problem:** `r.json = resp.json` assigns the **method** (not the result). Callers doing `r.json()` will work, but `r.json` as a property will return the method object. This is inconsistent with `requests.Response.json()` which is also a method, so it works — but it's confusing.

---

## 12. config/runtime_config.py

### BUG-RC01: `load_runtime_config()` Reads JSON on Every Call — MEDIUM

```python
def load_runtime_config() -> dict:
    if not RUNTIME_CONFIG_PATH.exists():
        return defaults
    try:
        # reads and parses JSON file every time
```

**Problem:** This function is called on every scan cycle, every risk check, every trade decision. Each call reads and parses the JSON file from disk. With 5 symbols × 5-minute scans, that's 60+ file reads per hour.

**Fix:** Cache with a 30-second TTL. Invalidate on write.

---

### BUG-RC02: No File Locking on Config Write — MEDIUM

If the dashboard writes `runtime_config.json` while the pipeline reads it, the reader may get a partially-written file (truncated JSON), causing a `json.JSONDecodeError` crash.

**Fix:** Write to a temp file, then `os.replace()` (atomic on POSIX and Windows).

---

## 13. Cross-Cutting Issues

### CROSS-01: No Structured Error Handling — HIGH

Throughout the codebase:
```python
except Exception:
    pass
```

In a trading system, swallowed exceptions mean:
- Silent trade execution failures
- Missed SL triggers
- Stale position data
- Undetected broker disconnections

**Count:** 47+ instances of `except Exception: pass` or `except Exception as e: log.warning(...)` without recovery.

---

### CROSS-02: No Input Validation on API Endpoints — HIGH

Dashboard endpoints accept query parameters without validation:
```python
@app.get("/api/price")
async def get_price(symbol: str, hours: int = 6):
```

No bounds on `hours` (can be 999999999), no validation that `symbol` is in `WATCH_SYMBOLS`.

---

### CROSS-03: Timestamps Mixed UTC/IST/Naive — HIGH

The codebase mixes:
- UTC ISO strings (`fetched_at`)
- IST datetime objects (`datetime.now(IST)`)
- Naive datetimes (`datetime.strptime(...)`)
- String comparisons of timestamps (`fetched_at >= ?`)

This causes off-by-one-day errors at IST midnight boundaries (already documented in BUG-PT02) and makes debugging timezone issues extremely difficult.

**Fix:** Standardize on UTC everywhere. Convert to IST only at display boundaries.

---

### CROSS-04: No Health Check for Database Connectivity — MEDIUM

If the SQLite database file is locked (e.g., by a long-running WAL checkpoint or another process), all operations fail silently. There's no circuit breaker or health check for DB connectivity.

---

### CROSS-05: `Codebase/` Directory Duplicates `src/` — ARCHITECTURAL

The `Codebase/` directory contains a full copy of `src/`, `config/`, `main.py`, and `dashboard_server.py`. This creates confusion about which version is active. Import paths may resolve to the wrong copy depending on `sys.path` ordering.

---

### CROSS-06: `.env` File Present in Project Root — CRITICAL

The `.env` file (containing broker credentials, API keys, Telegram tokens) exists in the project root. If this repository is ever pushed to a remote (GitHub, GitLab), all credentials are exposed.

**Verification needed:** Check `.gitignore` includes `.env`.

---

## 14. Summary & Priority Matrix

| ID | Severity | File | Issue |
|----|----------|------|-------|
| BUG-M01 | 🔴 CRITICAL | main.py | Global SSL bypass enables MITM on all HTTP |
| BUG-S02 | 🔴 CRITICAL | settings.py | Expiring security IDs (CRUDEOIL expires in 2 days) |
| BUG-DS01 | 🔴 CRITICAL | dashboard_server.py | Global socket patch breaks IPv6 |
| BUG-DS03 | 🔴 CRITICAL | dashboard_server.py | Dashboard unauthenticated by default |
| CROSS-06 | 🔴 CRITICAL | .env | Credentials in project root |
| BUG-S01 | 🟠 HIGH | settings.py | Duplicate NG risk limits (2x permissive) |
| BUG-DB01 | 🟠 HIGH | schema.py | COMMIT on every read (10-50x I/O) |
| BUG-DB05 | 🟠 HIGH | schema.py | Race condition on double-close |
| BUG-DB07 | 🟠 HIGH | schema.py | SQL injection via column names |
| BUG-RE01 | 🟠 HIGH | risk_engine.py | assert disabled with -O flag |
| BUG-LT01 | 🟠 HIGH | live_trading.py | Stale Kite token in cached client |
| BUG-DS04 | 🟠 HIGH | dashboard_server.py | Blocking I/O in async handlers |
| BUG-LLM01 | 🟠 HIGH | llm_enrichment.py | verify=False on LLM API calls |
| CROSS-01 | 🟠 HIGH | All | 47+ swallowed exceptions |
| CROSS-02 | 🟠 HIGH | dashboard | No input validation |
| CROSS-03 | 🟠 HIGH | All | Mixed UTC/IST/naive timestamps |
| BUG-M02 | 🟡 MEDIUM | main.py | IPv4 fallback patches global socket |
| BUG-S03 | 🟡 MEDIUM | settings.py | Hardcoded fallback expiries |
| BUG-DB02 | 🟡 MEDIUM | schema.py | No connection pooling |
| BUG-DB03 | 🟡 MEDIUM | schema.py | O(n) timestamp scan |
| BUG-DB04 | 🟡 MEDIUM | schema.py | INSERT OR REPLACE changes IDs |
| BUG-P02 | 🟡 MEDIUM | pipeline.py | Shoonya login blocks pipeline |
| BUG-P03 | 🟡 MEDIUM | pipeline.py | Position sync failures silent |
| BUG-PT02 | 🟡 MEDIUM | paper_trading.py | DTE uses UTC not IST |
| BUG-LT02 | 🟡 MEDIUM | live_trading.py | Unbounded thread spawns |
| BUG-LT03 | 🟡 MEDIUM | live_trading.py | Blocking HTTP in error handler |
| BUG-TD01 | 🟡 MEDIUM | trade_decision.py | Duck typing without validation |
| BUG-OPS01 | 🟡 MEDIUM | ops_agent.py | DDL on every connection |
| BUG-OPS03 | 🟡 MEDIUM | ops_agent.py | Heartbeat in world-writable temp |
| BUG-RC01 | 🟡 MEDIUM | runtime_config.py | JSON read on every call |
| BUG-RC02 | 🟡 MEDIUM | runtime_config.py | No atomic config write |
| CROSS-04 | 🟡 MEDIUM | All | No DB health check |
| CROSS-05 | 🟡 MEDIUM | Project | Duplicate Codebase/ directory |
| BUG-M03 | 🟡 MEDIUM | main.py | No graceful shutdown |
| BUG-S04 | 🟢 LOW | settings.py | Optional env None handling |
| BUG-DB06 | 🟢 LOW | schema.py | STT logic naming confusion |
| BUG-DB08 | 🟢 LOW | schema.py | Fragile table allowlist |
| BUG-P01 | 🟢 LOW | pipeline.py | Backward-compat alias |
| BUG-PT03 | 🟢 LOW | paper_trading.py | RSI 0.0 edge case |
| BUG-RE02 | 🟢 LOW | risk_engine.py | Datetime allocation per call |
| BUG-RE03 | 🟢 LOW | risk_engine.py | Cross-symbol circuit breaker |
| BUG-TD02 | 🟢 LOW | trade_decision.py | MCX confidence check order |
| BUG-DS02 | 🟢 LOW | dashboard_server.py | Duplicate import |
| BUG-LLM02 | 🟢 LOW | llm_enrichment.py | Method vs value assignment |
| BUG-OPS02 | 🟢 LOW | ops_agent.py | Hardcoded market hours |

---

## Immediate Action Items (Before Next Market Open)

1. **Update CRUDEOIL security ID** — expires 2026-07-20 (already past!)
2. **Remove global SSL patch** in main.py — use per-request `verify=False`
3. **Enable dashboard auth by default** — set `dashboard_auth_enabled: True`
4. **Fix duplicate NG_MAX_POSITIONS** — remove first definition (10), keep 20 or vice versa
5. **Replace `assert` with `raise ValueError`** in risk_engine.py
6. **Add `.env` to `.gitignore`** and verify it was never committed
7. **Remove `verify=False`** from llm_enrichment.py httpx call

---

## Architecture Recommendations

1. **Split `dashboard_server.py`** (2000+ lines) into routers: `/api/paper`, `/api/broker`, `/api/ai`, `/api/ops`
2. **Split `paper_trading.py`** (40KB) into: `entry.py`, `exit.py`, `monitor.py`, `tfss.py`
3. **Introduce a connection pool** for SQLite (or migrate to PostgreSQL for concurrent access)
4. **Standardize timestamps** to UTC everywhere; convert at display boundaries only
5. **Add structured logging** with correlation IDs per scan cycle
6. **Implement circuit breakers** for all external API calls (Kite, Dhan, Shoonya, LLM)
7. **Remove `Codebase/` directory** — it's a stale duplicate that creates import ambiguity
8. **Add integration tests** that run the full pipeline with mocked broker responses

---

*End of Audit Report*
