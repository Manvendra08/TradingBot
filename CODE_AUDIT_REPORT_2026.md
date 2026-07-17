# NSEBOT Code Audit Report — Functional Logic & Technical Bugs

**Audit Date:** 2026-07-14  
**Auditor:** Automated Line-by-Line Code Review  
**Scope:** All Python source files in root, `config/`, `src/engine/`, `src/models/`, `src/fetchers/`, `src/alerts/`, `src/utils/`, `src/scheduler/`, `src/services/`, `src/intelligence/`, `src/dashboard/`, `tools/`  
**Methodology:** Strict line-by-line review of functional logic, control flow, error handling, concurrency, security, and maintainability

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Critical Severity Findings](#critical-severity-findings)
3. [High Severity Findings](#high-severity-findings)
4. [Medium Severity Findings](#medium-severity-findings)
5. [Low Severity / Code Quality Findings](#low-severity--code-quality-findings)
6. [File-by-File Detailed Analysis](#file-by-file-detailed-analysis)
7. [Recommendations](#recommendations)

---

## Executive Summary

NSEBOT is a sophisticated multi-exchange (NSE, BSE, MCX) options/futures trading bot with paper trading, live trading via Zerodha Kite, AI/LLM enrichment, ML prediction, an ops agent monitor, and a FastAPI dashboard. The codebase is well-structured with good separation of concerns, but contains several functional logic bugs and technical issues that require attention.

### Issues Summary

| Severity | Count | Description |
|----------|-------|-------------|
| **Critical** | 3 | Causes hang, data corruption, or prevents automated operation |
| **High** | 12 | Causes incorrect calculations, order failures, or data inconsistency |
| **Medium** | 15 | Edge case failures, security concerns, or performance degradation |
| **Low** | 12 | Code quality, maintainability, or minor functional issues |

---

## Critical Severity Findings

### C1: `emergency_flat.py` — Interactive confirmation blocks automated execution

**File:** `emergency_flat.py` — `main()` function  
**Impact:** Bot hangs indefinitely when `emergency_flat.py` is called by `ops_agent.py` via `subprocess.run()` because there is no `stdin` available for the `input()` prompt. The 60s timeout in ops_agent will expire and the emergency flat will fail silently.

**Root Cause:**
```python
if not DRY_RUN:
    confirmation = input("Type CONFIRM to proceed with emergency flat: ").strip()
    if confirmation != "CONFIRM":
        log.info("Emergency flat cancelled — confirmation not received")
        print("Cancelled. Type exactly 'CONFIRM' to execute.")
        sys.exit(0)
```

**Fix:** Add a `--auto` or `--force` CLI flag to skip confirmation when called programmatically:
```python
AUTO_MODE = "--auto" in sys.argv or "--force" in sys.argv
if not DRY_RUN and not AUTO_MODE:
    confirmation = input("Type CONFIRM to proceed: ").strip()
    ...
```

---

### C2: `ops_agent.py` — Hardcoded `/tmp` path breaks on Windows

**File:** `ops_agent.py` — module-level constants  
**Impact:** The heartbeat file path `/tmp/nsebot.heartbeat` does not exist on Windows. The ops agent will never detect bot heartbeat, causing false "bot dead" alerts and unnecessary restart attempts.

**Root Cause:**
```python
HEARTBEAT_PATH = Path("/tmp/nsebot.heartbeat")
```

**Fix:** Use platform-appropriate temp directory:
```python
import tempfile
HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "nsebot.heartbeat"
```

---

### C3: `ops_agent.py` — `_prune_temp()` uses `/tmp` which doesn't exist on Windows

**File:** `ops_agent.py` — `_prune_temp()` function  
**Impact:** Function silently fails on Windows, never cleaning up temp files. If `/tmp` glob raises an exception, it's caught and ignored.

**Fix:** Same as C2 — use `tempfile.gettempdir()`.

---

## High Severity Findings

### H1: `live_trading.py` — Product type mismatch between entry orders and GTTs

**File:** `live_trading.py` — `place_kite_order()` and `place_kite_gtt()`  
**Impact:** Entry orders use `PRODUCT_MIS` (intraday/margin) but GTT exit orders use `PRODUCT_NRML` (normal/overnight). This causes GTT placement failures because MIS positions cannot have NRML GTTs attached. Zerodha rejects the GTT with "Invalid product" error.

**Root Cause:**
```python
# place_kite_order uses MIS
product=kite.PRODUCT_MIS,

# place_kite_gtt uses NRML
"product": kite.PRODUCT_NRML,
```

**Fix:** Standardize product type. Use `PRODUCT_NRML` for all orders that may hold overnight, or `PRODUCT_MIS` consistently for intraday-only strategies.

---

### H2: `schema.py` — `close_live_trade()` doesn't read stored `lot_size` from DB

**File:** `schema.py` — `close_live_trade()` function  
**Impact:** PnL calculation uses hardcoded `LOT_SIZES.get(base_symbol, 1)` instead of the stored `lot_size` from the trade row. If exchange revises lot sizes between entry and exit (which NSE does periodically), PnL will be calculated with wrong lot size, producing incorrect profit/loss figures.

**Root Cause:** The SELECT query in `close_live_trade()` doesn't include `lot_size`:
```python
row = conn.execute(
    "SELECT symbol, expiry, option_type, verdict_label, entry_underlying, entry_premium, lots, strike, side FROM live_trades WHERE id=? AND status='OPEN'",
    (trade_id,),
).fetchone()
```

**Fix:** Add `lot_size` to the SELECT and use stored value:
```python
lot_size = int(row["lot_size"]) if row["lot_size"] else LOT_SIZES.get(base_symbol, 1)
```

---

### H3: `schema.py` — `close_paper_trade()` / `close_live_trade()` exit premium fallback

**File:** `schema.py` — both close functions  
**Impact:** When `exit_premium` is unavailable, the code falls back to intrinsic value `max(0, underlying - strike)` for CE or `max(0, strike - underlying)` for PE. For OTM options with remaining time value, this returns 0.0, overstating losses on closed trades.

**Fix:** Use option chain snapshot LTP as first fallback before intrinsic value:
```python
snap_row = conn.execute(
    "SELECT ltp FROM option_chain_snapshots WHERE symbol=? AND strike=? AND option_type=? ORDER BY fetched_at DESC LIMIT 1",
    (symbol, strike, option_type),
).fetchone()
```

---

### H4: `ops_agent.py` — P09 force-flat only checks `live_trades` table

**File:** `ops_agent.py` — P09 playbook section  
**Impact:** Open paper positions for NATURALGAS won't be force-flatted on EIA cutoff (Thursday 19:40 IST). Only live trades are checked.

**Fix:** Add `paper_trades` table check:
```python
ng_open = conn.execute(
    "SELECT COUNT(*) as c FROM live_trades WHERE symbol='NATURALGAS' AND status='OPEN'"
).fetchone()["c"]
ng_open += conn.execute(
    "SELECT COUNT(*) as c FROM paper_trades WHERE symbol='NATURALGAS' AND status='OPEN'"
).fetchone()["c"]
```

---

### H5: `dashboard_server.py` — `_db()` uses writable connections for read queries

**File:** `dashboard_server.py` — `_db()` function  
**Impact:** Opens writable SQLite connections for read-only queries, causing WAL lock contention with the main bot process during high-frequency dashboard polling.

**Fix:** Use read-only SQLite connections:
```python
def _db():
    db_uri = Path(DB_PATH).as_uri() + "?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
```

---

### H6: `pipeline.py` — `_prefetch_symbol_data()` news_result `__dict__` access

**File:** `pipeline.py` — `_prefetch_symbol_data()` function  
**Impact:** `news_future.result().__dict__` will raise `AttributeError` if the result is a simple type (string, int) or namedtuple without `__dict__`.

**Fix:** Use safe dict conversion:
```python
result = news_future.result()
if hasattr(result, '__dict__'):
    packet["news_result"] = result.__dict__
elif isinstance(result, dict):
    packet["news_result"] = result
else:
    packet["news_result"] = {"ok": True, "data": result}
```

---

### H7: `pipeline.py` — `run_pipeline()` sorted() key error

**File:** `pipeline.py` — `run_pipeline()` function  
**Impact:** `symbols.index(x["symbol"])` raises `ValueError` if a prefetched symbol isn't in the original symbols list (e.g., if symbol was normalized differently).

**Fix:**
```python
symbols_list = list(symbols)
for packet in sorted(prefetched, key=lambda x: symbols_list.index(x["symbol"]) if x["symbol"] in symbols_list else 999):
```

---

### H8: `dashboard_server.py` — PatchedCursor SQL rewrite fragility

**File:** `dashboard_server.py` — `PatchedCursor` and `PatchedConnection`  
**Impact:** The regex-based SQL rewrite injects a UNION ALL subquery with hardcoded column lists. If `paper_trades` or `live_trades` schema changes (columns added/removed), the UNION ALL will fail with column count mismatch, breaking all dashboard queries.

**Fix:** Use a SQLite VIEW instead of runtime SQL rewriting:
```sql
CREATE VIEW IF NOT EXISTS paper_trades_unified AS
SELECT ... FROM paper_trades
UNION ALL
SELECT ... FROM live_trades WHERE ...
```

---

### H9: `dashboard_server.py` — Thread safety for global caches

**File:** `dashboard_server.py` — module-level globals  
**Impact:** `_positions_cache`, `_margins_cache`, `_positions_failure_ts`, `_margins_failure_ts` are modified without locks from concurrent uvicorn requests, creating race conditions.

**Fix:** Add `threading.Lock()` around all cache reads and writes.

---

### H10: `decision_pipeline.py` — `step_signal_core_oi()` scan_context mutation

**File:** `decision_pipeline.py` — `step_signal_core_oi()` function  
**Impact:** When boosting "Low Conviction" confidence, the code replaces `ctx.scan_context` with a new dict: `ctx.scan_context = {**ctx.scan_context, "intel": {...}}`. This breaks references held by other pipeline steps that still point to the old dict.

**Fix:** Mutate in-place:
```python
ctx.scan_context["intel"] = {**ctx.scan_context["intel"], "confidence": confidence}
```

---

### H11: `schema.py` — `get_prev_snapshots_bulk()` connection reuse

**File:** `schema.py` — `get_prev_snapshots_bulk()` function  
**Impact:** The `conn` object is used after a complex loop that may have left it in an inconsistent state if any exception occurred during iteration.

**Fix:** Wrap entire function body in single `with get_conn()` context manager.

---

### H12: `dashboard_server.py` — `_positions_cache` race condition

**File:** `dashboard_server.py` — `_fetch_real_kite_positions()`  
**Impact:** Cache updated without synchronization. Multiple concurrent requests could read stale data or cause double-writes to Kite API.

**Fix:** Same as H9 — add threading locks.

---

## Medium Severity Findings

### M1: `settings.py` — `_is_testing` detection fragile

**File:** `config/settings.py` — module-level code  
**Impact:** Non-test scripts with "test_" prefix will incorrectly use the test database. The `sys.argv` checking is unreliable.

**Fix:** Use `pytest` in `sys.modules` and environment variables:
```python
_is_testing = "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") is not None
```

---

### M2: `live_trading.py` — `confirm_order_fill()` insufficient polling time

**File:** `live_trading.py` — `confirm_order_fill()` function  
**Impact:** Only 2.5 seconds total polling time (5 retries × 0.5s delay). For limit orders in illiquid options, this may not be enough to confirm fill status.

**Fix:** Increase `max_retries` to 10 and `delay_sec` to 1.0 for options.

---

### M3: `dashboard_server.py` — `_fetch_real_kite_positions()` fractional lots for NSE

**File:** `dashboard_server.py` — `_fetch_real_kite_positions()`  
**Impact:** `round(abs(qty) / lot_size, 2)` for NSE produces fractional lots (e.g., 1.5 lots) which should be integers.

**Fix:** Use `round(abs(qty) / lot_size)` without decimal places for NSE.

---

### M4: `risk_engine.py` — Daily loss cap SQL timezone mixing

**File:** `src/engine/risk_engine.py` — `_check_risk_limits_for_table()`  
**Impact:** `closed_at >= ? AND closed_at <= CURRENT_TIMESTAMP` mixes parameterized and function-based comparisons. If `closed_at` stored with different timezone offsets, some trades may be missed.

**Fix:** Use only parameterized timestamps:
```python
now_utc = datetime.now(timezone.utc).isoformat()
# ... closed_at >= ? AND closed_at <= ?
```

---

### M5: `dashboard_server.py` — `_enrich_open_trades_with_live_pnl()` symbol lookup

**File:** `dashboard_server.py`  
**Impact:** `LOT_SIZES.get(symbol, 1)` where symbol may include expiry suffixes like "NIFTY 25JUL CE".

**Fix:** Extract base symbol before LOT_SIZES lookup:
```python
base_sym = symbol.upper().split()[0]
lot_size = LOT_SIZES.get(base_sym, 1)
```

---

### M6: `ops_agent.py` — `_resolve_playbooks_if_healthy()` immediate auto-resolve

**File:** `ops_agent.py`  
**Impact:** Auto-resolving incidents immediately when component reports OK could mask transient issues. A single OK reading shouldn't clear an incident.

**Fix:** Add debounce counter: require 3 consecutive OK readings before resolving.

---

### M7: `dashboard_server.py` — `_fetch_scanx_heatmap()` SSL verification disable

**File:** `dashboard_server.py` — `_fetch_scanx_heatmap()`  
**Impact:** Disabling SSL verify on 3rd attempt is a security concern and may leak data to MITM.

**Fix:** Remove SSL verify disable or use proper certificate pinning.

---

### M8: `live_trading.py` — `sync_direct_kite_positions()` midnight boundary issue

**File:** `live_trading.py` — `sync_direct_kite_positions()`  
**Impact:** `opened_at LIKE ?` with date prefix could miss trades opened very close to midnight UTC.

**Fix:** Use `opened_at >= ? AND opened_at < ?` with precise timestamps.

---

### M9: `ops_agent.py` — `_set_trading_paused()` one-way switch comment mismatch

**File:** `ops_agent.py`  
**Impact:** Comment says "one-way (human-only unpause)" but implementation allows programmatic unpause.

**Fix:** Enforce one-way behavior by checking current state before allowing False.

---

### M10: `decision_pipeline.py` — `step_entry_quality_core()` plan_ctx type filtering

**File:** `src/engine/decision_pipeline.py`  
**Impact:** `{k: v for k, v in ctx.scan_context.items() if isinstance(k, str)}` filters out non-string keys which may include important data.

**Fix:** Use `dict(ctx.scan_context)` without filtering.

---

### M11: `pipeline.py` — `_async_llm_enrich_and_edit()` executor keyword args

**File:** `src/engine/pipeline.py`  
**Impact:** Function submitted to executor with keyword arguments but signature uses positional parameters. Fragile if function signature changes.

**Fix:** Use `functools.partial` or explicit lambda wrapper.

---

### M12: `dashboard_server.py` — `_get_kite_closed_trades()` side determination

**File:** `dashboard_server.py`  
**Impact:** Side determined from `entry_order.get('transaction_type')` may not be actual entry if orders are complex (partial fills, amendments).

**Fix:** Verify side from position quantity sign instead of order history.

---

### M13: `schema.py` — `close_paper_trade()` exit premium fallback for options

**File:** `src/models/schema.py`  
**Impact:** Fallback to intrinsic value for OTM options returns 0.0, not reflecting actual time value.

**Fix:** Use option chain snapshot LTP as first fallback, intrinsic as second.

---

### M14: `dashboard_server.py` — Multiple bare `except:` clauses

**File:** `dashboard_server.py` — multiple locations  
**Impact:** Catches all exceptions silently, making debugging difficult. Found in `_enrich_trade_details()`, `_calculate_holding_analysis()`, etc.

**Fix:** Replace with specific exception types or at minimum log the exception.

---

### M15: `live_trading.py` — `_is_reversal_against_open_trade()` optional ctx parameter

**File:** `src/engine/live_trading.py`  
**Impact:** `ctx: dict | None = None` but function uses ctx without None check in guard 2.

**Fix:** Add `if ctx is None: return False` at start of function.

---

## Low Severity / Code Quality Findings

### L1: Hardcoded 2026 dates only

**Files:** `config/holidays.py`, `config/cme_holidays.py`, `config/symbol_classes.py`  
**Impact:** After 2026, all holiday dates and MCX expiry dates will be stale. `is_market_holiday()` will always return False.

**Fix:** implement dynamic holiday fetching from exchange APIs.

---

### L2: `main.py` — Double `import urllib3`

**File:** `main.py`  
**Impact:** Cosmetic issue, no functional impact but indicates code cleanup needed.

**Fix:** Remove duplicate import.

---

### L3: `ops_agent.py` — Global mutable state without thread safety

**File:** `ops_agent.py`  
**Impact:** `_critical_last_sent` and `_last_digest_date` modified without locks.

**Fix:** Use `threading.Lock()` for globals or refactor to class-based state.

---

### L4: `settings.py` — LOT_SIZES inconsistency

**File:** `config/settings.py` vs `dashboard_server.py`  
**Impact:** `settings.py` has NIFTY: 65 but `dashboard_server.py` fallback has NIFTY: 25. This causes inconsistent lot calculations.

**Fix:** Reconcile lot sizes to use single source of truth as settings.py.

---

### L5: `schema.py` — `_MIGRATIONS` list grows unbounded

**File:** `src/models/schema.py`  
**Impact:** Every schema change adds to list, slowing `init_db()` over time.

**Fix:** Track applied migrations in a separate table and only run new ones.

---

### L6: `pipeline.py` — `_build_structured_payload()` dte access

**File:** `src/engine/pipeline.py`  
**Impact:** `getattr(scan_context, "dte", 0)` always returns 0 because scan_context is a dict, not an object.

**Fix:** Use `scan_context.get('dte', 0)` instead.

---

### L7: `ops_agent.py` — P05 parity_down threshold comment mismatch

**File:** `ops_agent.py`  
**Impact:** Comment says "~2 min" but actual timing depends on 60s loop interval.

**Fix:** Update comment to reflect actual timing.

---

### L8: `dashboard_server.py` — `_parse_intel_fields()` redundant import

**File:** `dashboard_server.py`  
**Impact:** `from src.engine.intelligence import IntelligenceResult` inside function causes repeated import overhead.

**Fix:** Move import to module level.

---

### L9: Multiple files — Inconsistent error logging

**Impact:** Some functions log warnings on error, others silently swallow exceptions.

**Fix:** Standardize error handling policy across codebase.

---

### L10: `emergency_flat.py` — Missing close_live_trade import

**File:** `emergency_flat.py`  
**Impact:** Uses `from src.models.schema import get_conn` but close function not imported at top.

**Fix:** Add `from src.models.schema import close_live_trade` at module level.

---

### L11: `dashboard_server.py` — `_positions_cache` not cleared on logout

**File:** `dashboard_server.py` — `broker_logout()`  
**Impact:** Cache globals are cleared on logout but if logout fails, stale cache persists.

**Fix:** Always clear caches in finally block.

---

### L12: `schema.py` — Unused local imports

**Files:** Multiple  
**Impact:** `datetime` imported at module level but also imported locally in several functions.

**Fix:** Remove redundant local imports.

---

## File-by-File Detailed Analysis

### Root Files

| File | Lines | Critical | High | Medium | Low |
|------|-------|----------|------|--------|-----|
| `main.py` | ~130 | 0 | 0 | 0 | 1 |
| `ops_agent.py` | ~650 | 2 | 1 | 3 | 2 |
| `dashboard_server.py` | ~2500 | 0 | 3 | 5 | 4 |
| `emergency_flat.py` | ~180 | 1 | 0 | 0 | 1 |
| `verify_metrics.py` | ~20 | 0 | 0 | 0 | 0 |

### Config Files

| File | Lines | Critical | High | Medium | Low |
|------|-------|----------|------|--------|-----|
| `settings.py` | ~350 | 0 | 0 | 1 | 1 |
| `runtime_config.py` | ~100 | 0 | 0 | 0 | 0 |
| `logging_config.py` | ~80 | 0 | 0 | 0 | 0 |
| `holidays.py` | ~60 | 0 | 0 | 0 | 1 |
| `symbol_classes.py` | ~200 | 0 | 0 | 0 | 1 |
| `cme_holidays.py` | ~30 | 0 | 0 | 0 | 0 |

### Engine Files

| File | Lines | Critical | High | Medium | Low |
|------|-------|----------|------|--------|-----|
| `pipeline.py` | ~400 | 0 | 2 | 1 | 1 |
| `live_trading.py` | ~1200 | 0 | 1 | 1 | 2 |
| `risk_engine.py` | ~300 | 0 | 0 | 1 | 0 |
| `paper_trading.py` | ~1000 | 0 | 0 | 0 | 1 |
| `decision_pipeline.py` | ~700 | 0 | 1 | 1 | 0 |
| `schema.py` | ~1200 | 0 | 2 | 2 | 1 |
| `trade_decision.py` | ~200 | 0 | 0 | 0 | 0 |
| `trend_analysis.py` | ~350 | 0 | 0 | 0 | 0 |
| `time_guards.py` | ~150 | 0 | 0 | 0 | 0 |

---

## Recommendations

### Immediate Actions (P0)

1. **Fix `emergency_flat.py` confirmation for automated use** — Add `--auto` flag to skip confirmation when called by ops_agent
2. **Fix Windows path issues in `ops_agent.py`** — Use `tempfile.gettempdir()` instead of hardcoded `/tmp`
3. **Fix product type mismatch in `live_trading.py`** — Standardize on PRODUCT_NRML for all overnight-capable orders
4. **Fix `close_live_trade()` lot_size reading** — Read stored lot_size from DB row

### Short-term Improvements (P1)

5. **Add threading locks to dashboard caches** — Prevent race conditions in multi-threaded uvicorn
6. **Use read-only SQLite connections for dashboard reads** — Prevent WAL lock contention
7. **Add debounce to ops_agent auto-resolve** — Prevent transient OK from clearing real incidents
8. **Fix SQL rewrite fragility in dashboard** — Use SQLite views instead of regex-based SQL rewriting

### Long-term Improvements (P2)

9. **Implement dynamic holiday fetching** — Fetch exchange holidays from NSE/MCX APIs instead of hardcoding
10. **Add migration tracking table** — Track applied schema migrations to avoid re-running
11. **Standardize error handling policy** — Define and enforce consistent error logging across all modules
12. **Add integration test suite** — Current tests focus on unit level; add end-to-end pipeline tests

---

## Severity Definitions

| Level | Definition |
|-------|-----------|
| **Critical** | Causes hang, data corruption, or prevents automated operation |
| **High** | Causes incorrect calculations, order failures, or data inconsistency |
| **Medium** | Edge case failures, security concerns, or performance degradation |
| **Low** | Code quality, maintainability, or minor functional issues |

---

## Appendix: Bug Reference Table

| Bug ID | File | Line (approx) | Severity | Category |
|--------|------|---------------|----------|----------|
| C1 | emergency_flat.py | ~170 | Critical | Automation |
| C2 | ops_agent.py | ~30 | Critical | Platform |
| C3 | ops_agent.py | ~420 | Critical | Platform |
| H1 | live_trading.py | ~400, ~550 | High | Trading Logic |
| H2 | schema.py | ~750 | High | Data Integrity |
| H3 | schema.py | ~700, ~800 | High | PnL Accuracy |
| H4 | ops_agent.py | ~500 | High | Safety |
| H5 | dashboard_server.py | ~100 | High | Performance |
| H6 | pipeline.py | ~120 | High | Robustness |
| H7 | pipeline.py | ~200 | High | Robustness |
| H8 | dashboard_server.py | ~50-100 | High | Maintainability |
| H9 | dashboard_server.py | ~1500 | High | Concurrency |
| H10 | decision_pipeline.py | ~150 | High | Data Flow |
| H11 | schema.py | ~400 | High | Robustness |
| H12 | dashboard_server.py | ~1500 | High | Concurrency |
| M1 | settings.py | ~20 | Medium | Testing |
| M2 | live_trading.py | ~350 | Medium | Reliability |
| M3 | dashboard_server.py | ~1500 | Medium | Data Display |
| M4 | risk_engine.py | ~100 | Medium | Data Integrity |
| M5 | dashboard_server.py | ~1200 | Medium | Data Accuracy |
| M6 | ops_agent.py | ~300 | Medium | Reliability |
| M7 | dashboard_server.py | ~800 | Medium | Security |
| M8 | live_trading.py | ~900 | Medium | Data Accuracy |
| M9 | ops_agent.py | ~450 | Medium | Documentation |
| M10 | decision_pipeline.py | ~300 | Medium | Data Flow |
| M11 | pipeline.py | ~150 | Medium | Robustness |
| M12 | dashboard_server.py | ~1800 | Medium | Data Accuracy |
| M13 | schema.py | ~700 | Medium | PnL Accuracy |
| M14 | dashboard_server.py | Multiple | Medium | Code Quality |
| M15 | live_trading.py | ~250 | Medium | Robustness |

---

*Report generated by automated code audit tool. All findings should be verified and prioritized by the development team before implementation.*
