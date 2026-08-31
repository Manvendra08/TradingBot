2026-07-29 13:55:42 | ERROR    | src.engine.pipeline       | Direct Kite position synchronization failed
Traceback (most recent call last):
  File "C:\Users\manve\Downloads\NSEBOT\src\engine\pipeline.py", line 84, in _maybe_sync_positions
    sync_direct_kite_positions()
  File "C:\Users\manve\Downloads\NSEBOT\src\engine\live_trading.py", line 2238, in sync_direct_kite_positions
    prev_und = get_previous_underlying(base_sym, read_only=True)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: get_previous_underlying() got an unexpected keyword argument 'read_only'# NSEBOT Architecture — Scan Sentinel Knowledge Base

## 1. Core Pipeline Flow
1. **Option Chain Fetching (`src.fetchers.router`)**: Cascading dual-source parallel fetch (`Sensibull` / `Shoonya` / `Dhan` / `Paytm` / `NSE`). Returns immediately on primary success (12s deadline) or falls back to completed sources.
2. **Anomaly Detection (`src.engine.anomaly_detector`)**: Computes OI, PCR, Max Pain, support/resistance walls, volume anomalies, and checks fake LTP / Zero OI triggers.
3. **Engine-Aligned LLM Enrichment (`src.engine.llm_enrichment`)**: High-priority LLM chain provides execution context without overriding engine direction. Fast failover (12s) with 10-minute provider failure cooldown.
4. **Strategy Execution (`src.engine.strategy_registry`)**: Executes strategy runners (`CORE`, `TIMEFRAME`, `NG_PARITY`, `NG_MOMENTUM`, `NG_EVENT`).
5. **Risk Engine Guardrails (`src.engine.risk_engine`)**: Evaluates capital limits, max open trades, and symbol-level TFSS combined net delta cap (0.60).
6. **Persistence & Position Tracking (`src.models.schema`)**: WAL-mode SQLite database with `BEGIN IMMEDIATE` transaction locks and lock retries.
7. **Structured Alerting (`src.alerts.digest`)**: Formats Telegram alerts verified strictly against committed database trades (`paper_trades`/`live_trades`).

---

## 2. Active Operational Failure Modes & Self-Healing Guardrails

### F1: Fake Option Premium (P0-CRITICAL)
- **Symptom:** Target/SL premium within 5% of underlying spot price.
- **Root Cause:** Illiquid or untraded options return spot price as LTP.
- **Self-Heal:** Flag verdict as `INVALID`, block trade execution in `trade_decision.py`.

### F2: Option Type Inversion / CE-PE Mismatch (P0-CRITICAL)
- **Symptom:** `GO_SHORT` with CE or `GO_LONG` with PE instrument.
- **Root Cause:** LLM output inversion or prompt parsing confusion.
- **Self-Heal:** `_enforce_engine_alignment()` auto-corrects LLM side to match OI direction. If unresolvable, blocks trade.

### F3: Pipeline Re-entrancy & Concurrency Locks (P0-CRITICAL)
- **Symptom:** Concurrent pipeline runs attempt duplicate trade entries.
- **Root Cause:** Overlapping scan intervals when previous tick stalls.
- **Self-Heal:** Non-blocking `_PIPELINE_LOCK = threading.Lock()` at pipeline start skips overlapping ticks cleanly.

### F4: SQLite Database Lock Contention (P1-HIGH)
- **Symptom:** `sqlite3.OperationalError: database is locked` during concurrent scan and position sync writes.
- **Root Cause:** Python `sqlite3` implicit DEFERRED transactions deadlocking during lock escalation.
- **Self-Heal:** `get_conn()` uses `isolation_level=None` with explicit `BEGIN IMMEDIATE` for write operations, 60s busy timeout, WAL mode, and a 5-attempt backoff retry loop.

### F5: Fetcher Degradation, MCX Priority & Fast Dual-Fetch Failover (P1-HIGH)
- **Symptom:** Primary fetcher hangs or fails (e.g. Shoonya timeout on MCX or Dhan timeout).
- **Root Cause:** 
  1. Shoonya API is an equity/index broker (NFO/BFO) and lacks full MCX commodity option chain structures, causing 12s timeouts when listed first for commodities.
  2. Zerodha Kite API accounts lack MCX market data permissions, raising `Insufficient permission` warnings when querying `MCX:...` symbols.
- **Self-Heal:** 
  1. `_priority_for` in [router.py](file:///c:/Users/manve/Downloads/NSEBOT/src/fetchers/router.py#L108) prioritizes dedicated commodity fetchers (`dhan_commodity` -> `moneycontrol`) ahead of `shoonya` for MCX commodities (`NATURALGAS`, `CRUDEOIL`, `GOLD`, `SILVER`).
  2. [live_trading.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/live_trading.py#L2212) bypasses `kite.ltp` for `MCX:` keys, resolving underlying prices via Dhan/MoneyControl and DB fallback cleanly without permission warnings.

### F6: Multi-Leg Expiry Filter & CMP Inflation (P0-CRITICAL)
- **Symptom:** Dashboard and paper trading show massive bloated CMPs (e.g. ₹500–790 on OTM options with entry premiums of ₹30–100), distorting open position PnL by lakhs.
- **Root Cause:** Live PnL enrichment queries (`_enrich_open_trades_with_live_pnl` in `dashboard_server.py` and `_calc_multileg_pnl` in `src/engine/multileg_paper_trading.py`) fell back to unfiltered option chain snapshot queries (`WHERE symbol=? AND strike=? AND option_type=?` WITHOUT filtering by `expiry`), selecting next-month expiry options (e.g., 2026-09-29 vs 2026-08-25) which have massive time value.
- **Self-Heal:** Enforce strict `expiry=?` filtering in option snapshot queries for both multi-leg and single-leg trades. Remove cross-expiry fallback queries completely.
- **Caveat — Shoonya Sequential Call Avalanche:** Shoonya `fetch_option_chain()` makes 3–4 sequential API calls (`_search_scrip`, `_get_quotes`, `_get_option_chain` + per-strike fallbacks), each with 6s timeout + 1 retry. Worst-case wall time (~22.5s+) exceeds the router's 12s deadline. The `0.1s` fallback timeout when the primary source succeeds can also produce false "fetch failed/timed out" warnings. Session-quota re-login (Playwright OAuth, 60–75s) guarantees a timeout if it triggers during a fetch cycle. No per-source timeout configuration exists — all Shoonya timeouts are hardcoded.

### F127: Direct Kite Manual Trade Auto-Reconciliation Fix (P0-CRITICAL)
- **Issue**: `sync_open_positions_with_kite()` in [live_trading.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/live_trading.py#L1990) auto-adopted active Zerodha positions into `live_trades` table as `setup_type='DIRECT_KITE'`. However, when those manual positions were closed on Zerodha, DB rows remained in `status='OPEN'`, causing stale positions (`SENSEX 75500 PE`, `NATURALGAS 285 PE/290 PE/290 CE/FUT`) to persist under `🤖 BOT LIVE TRADES` on the Broker Console page.
- **Fix**: Added automated position reconciliation to `sync_open_positions_with_kite()` in [live_trading.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/live_trading.py#L2045) — whenever an adopted `DIRECT_KITE` trade is no longer active in Zerodha's `net_positions`, it is automatically set to `status='CLOSED'`, `reason='Closed on Kite'` in SQLite DB. Reconciled and closed all stale DB rows.

### F128: Shoonya API Rate Limit (10 req/s User Cap) Fix (P0-CRITICAL)
- **Issue**: High-cadence option chain fetches (e.g., `SENSEX26AUG` option chain resolution) burst >10 requests per second to `api.shoonya.com/NorenWClientAPI/GetQuotes`, triggering HTTP 400 errors: `{"stat":"Not_Ok","emsg":"Invalid Input : Order Recieved 11 in a current second exceeds Limit 10 for user"}`.
- **Fix**: Implemented thread-safe sliding window rate limiter (`_throttle_rate_limit()` capping requests at max 8 req/sec) and added automatic 1.15s backoff retries on `exceeds Limit` responses in [shoonya_fetcher.py](file:///c:/Users/manve/Downloads/NSEBOT/src/fetchers/shoonya_fetcher.py#L585).

### F129: Dynamic Delta Rebalancing (0.50 Threshold) & 35% Time-Decay Profit Target (P1-HIGH)
- **Issue**: Single-trade pipeline guard prevented entering/rolling opposite strangle legs when an open trade drifted ITM, leaving tested legs exposed to unhedged directional losses. Time-decay targets were also taking profits too late or unevenly.
- **Fix**:
  1. Updated [config/trend_following_short_strangle.py](file:///c:/Users/manve/Downloads/NSEBOT/config/trend_following_short_strangle.py#L60) with `REBALANCE_DELTA_THRESHOLD = 0.50` and `TIME_DECAY_PROFIT_TARGET_PCT = 0.35`.
  2. Updated `evaluate_reversal` in [trend_following_short_strangle.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/trend_following_short_strangle.py#L220) to trigger `OPEN_OR_ADD` for dynamic delta rebalancing (rolling/adding opposite leg) when leg delta reaches `0.50`.
  3. Capped target premium for short options in [trade_plan.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/trade_plan.py#L585) to `entry_premium * 0.65` (35% time-decay profit target).

### F130: Ops Agent Detail Truncation & Market Hours Gate Fix (P1-HIGH)
- **Issue**:
  1. Ops Agent Activity Log on the dashboard UI (`/ops`) truncated detail text to 40 characters via JS string slicing (`result.slice(0, 40) + "…"`).
  2. Ops Agent evaluated heartbeat staleness (`P01`/`P02`) outside active market operating hours, triggering false "Bot dead with X open positions" escalations at midnight (12:06 AM IST).
- **Fix**:
  1. Removed `result.slice(0, 40)` truncation in [src/dashboard/ops.html](file:///c:/Users/manve/Downloads/NSEBOT/src/dashboard/ops.html#L596) and enabled clean multi-line word wrapping with hover tooltips (`title="${result}"`).
  2. Enforced `_is_market_hours()` gate before evaluating heartbeat staleness (`hb_stale`) in [ops_agent.py](file:///c:/Users/manve/Downloads/NSEBOT/ops_agent.py#L768) — suppresses off-market false alerts when scanners sleep.

### F131: Broker OFF Activity Isolation & `read_only` Keyword Argument Fix (P0-CRITICAL) (RESOLVED — do not re-diagnose)
- **Status:** RESOLVED & VERIFIED. `get_previous_underlying` accepts `*args, **kwargs` in `schema.py`. Do NOT cite `read_only` keyword argument TypeError unless it appears verbatim in log lines.
- **Issue**: When broker trading was OFF (e.g. `live_shadow_mode=True`), the pipeline and scheduler still executed Zerodha Kite client initialization, API health checks, and position syncs. When `sync_direct_kite_positions()` called `get_previous_underlying(base_sym, read_only=True)`, it threw `TypeError: get_previous_underlying() got an unexpected keyword argument 'read_only'`, crashing the pipeline.
- **Fix**:
  1. Updated `is_broker_trade_enabled()` in [config/runtime_config.py](file:///c:/Users/manve/VibeProjects/NSEBOT/config/runtime_config.py) to check `live_shadow_mode` — returns `False` whenever broker trading is OFF/Shadow mode.
  2. Added `is_broker_trade_enabled()` short-circuit guards to `sync_direct_kite_positions()`, `_maybe_sync_positions()`, `_ensure_kite_health()`, and scheduler background threads to completely suppress broker-specific API calls when Broker is OFF.
  3. Updated `get_read_conn()`, `get_conn()`, `get_previous_underlying()`, and `get_previous_underlying_before()` in [src/models/schema.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/models/schema.py) to accept `*args, **kwargs`, eliminating `read_only` parameter `TypeError` exceptions.

### F140: LLM Multi-Leg Exit Premature Closure (P0-CRITICAL) (RESOLVED — do not re-diagnose)
- **Status:** RESOLVED & VERIFIED. LLM exit/adjustment advice is advisory only; deterministic risk gates remain authoritative.
- **Issue:** In `multileg_paper_trading.py` and `multileg_live_trading.py`, `ai_mode="full"` allowed an LLM `CLOSE` response to immediately close an open book, even when deterministic profit/stop/expiry conditions had not fired. A BANKNIFTY short strangle was closed after roughly one hour at a loss because the model interpreted a delta move as “strangle broken.” The same path could close live broker books.
- **Fix:** AI `CLOSE` and `ADJUST` responses are now logged as advisory-only. They cannot close books, consume adjustment slots, or place orders. Only deterministic profit target, stop loss, expiry, and explicit validated execution paths can change position state.

### F139: 0DTE Entry Cutoff Legitimacy Reclassification & Sentinel Severity Cap (P1-HIGH) (RESOLVED — do not re-diagnose)
- **Status:** RESOLVED & VERIFIED.
- **Issue 1:** 0DTE entry cutoff (15:15 IST / 23:15 MCX) previously caused `validate_market_data()` to report `is_legitimate=False`, aborting the entire scan context and skipping exit monitoring / square-off.
- **Fix 1:** Reclassified 0DTE entry cutoff to a warning on `DataLegitimacyResult` (`is_legitimate=True`), setting `is_0dte_cutoff=True`. Wired into `risk_engine.py` and multi-leg entry modules to block NEW entries while allowing scan completion, exit monitoring, and square-off.
- **Issue 2:** Sentinel diagnostic LLM escalated WARNING-level flags (e.g. `R4_SLOW_SCAN`) to `CRITICAL` severity and recommended `PAUSE_SYMBOL` based on resolved KB entries (`F131` `read_only` TypeError hallucination).
- **Fix 2:** Added severity capping in `run_sentinel()` ensuring diagnostic severity cannot exceed max rule flag severity (caps `CRITICAL` to `WARNING` and `PAUSE_SYMBOL` to `ALERT_ONLY` when no `CRITICAL` rule flags are present). Marked `F131` explicitly RESOLVED in KB.

### F134: Heterogeneous Option Chain Normalization (`AttributeError: 'list' object has no attribute 'get'`) (RESOLVED — do not re-diagnose)
- **Status:** RESOLVED & VERIFIED. `_normalize_option_chain()` is in place in `multileg_strategy.py` and transparently handles both dict and list option-chain payloads. Do NOT cite this entry for new incidents unless a fresh `AttributeError: 'list' object has no attribute 'get'` traceback appears verbatim in the current RECENT LOG LINES.
- **Symptom:** `AttributeError: 'list' object has no attribute 'get'` in multi-leg strategy runner (`src/engine/multileg_strategy.py`) during paper or live multi-leg trade evaluation.
- **Root Cause:** `validate_legs()` and `build_execution_plan()` expected `option_chain` to be a `dict` keyed by `strike`, but callers in `multileg_paper_trading.py` and `multileg_live_trading.py` passed `option_rows` (a `list` of row dicts or list of contract dicts) from scan context.
- **Self-Heal:** Added `_normalize_option_chain()` helper in [multileg_strategy.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/multileg_strategy.py) to dynamically convert list representations (both row dicts with `CE`/`PE` keys and individual contract dicts) into standard `dict[float, dict]` before leg validation and execution plan construction. Unit test coverage in `tests/test_multileg_strategy.py`.

### F132: Multi-Leg Snapshot Expiry Resolution & Live PnL Distortion Fix (P0-CRITICAL)
- **Symptom:** Multi-leg trade cards on dashboard displayed abnormal CMP spikes (e.g., ₹480–791 for OTM options vs entry prices of ₹30–120) and distorted PnL (-₹1,46,475).
- **Root Cause:**
  1. `_enrich_open_trades_with_live_pnl()` in `dashboard_server.py` queried `option_chain_snapshots` without filtering by `expiry`, fetching arbitrary contract snapshots across different expiries.
  2. `_calc_multileg_pnl()` in `multileg_paper_trading.py` passed `leg.get("expiry", "")` which evaluated to `""` (since `expiry` is stored on the parent `book`), causing DB snapshot queries to return 0 rows and triggering inaccurate Delta-based price estimation.
  3. `_update_live_book_pnl()` was called in `multileg_live_trading.py` but was undefined, raising `NameError`.
  4. `get_latest_option_snapshot` was missing from `schema.py`.
- **Fix:**
  1. Added `expiry` filtering and `is_valid_option_premium()` sanity checks in `dashboard_server.py`.
  2. Resolved `leg_expiry = str(leg.get("expiry") or book.get("expiry") or "")` in `multileg_paper_trading.py`.
  3. Added `get_latest_option_snapshot` helper to `schema.py`.
  4. Implemented `_update_live_book_pnl()` in `multileg_live_trading.py`.

### F133: Inverse Target/SL Premium Order (`R8_INVERSE_TARGET_SL`) (P1-HIGH)
- **Symptom:** Rule `R8_INVERSE_TARGET_SL` flags trade plan with target premium <= entry premium or stop loss premium >= entry premium on an option BUY trade.
- **Root Cause:**
  1. Contradictory LLM raw output (e.g. LLM generated target premium < entry premium for BUY option).
  2. Level sanitization mismatch where `_sanitize_llm_verdict` misidentified option direction (e.g., treating `GO_LONG` + `PE` as bullish on underlying instead of bearish).
- **Fix / Self-Heal:** `_sanitize_llm_verdict` in `llm_enrichment.py` maps option direction correctly (`bullish = is_action_long if target_opt != 'PE' else not is_action_long`) so target premium always increases for option BUY trades. If R8 flags, Sentinel classifies as `ALERT_ONLY` if SL is underlying-level or `SKIP_TRADE` if target premium is truly inverse.

### F135: Zero OI & Illiquid Option Chain Anomaly (P1-HIGH)
- **Symptom:** >50% of strikes return `oi=0` and `volume=0`.
- **Root Cause:** After-hours scan, illiquid contract, or provider API drop.
- **Self-Heal:** Mark scan context as `LOW_CONFIDENCE` and downgrade signal confidence scores.

### F7: Symbol-Level TFSS Strangle Risk Limits (P0-CRITICAL)
- **Symptom:** Excess portfolio exposure from short strangle legs.
- **Guardrails:**
  1. **Max Legs:** Clamped to 6 open legs per symbol-day (3 PE, 3 CE).
  2. **Tranche Scaling:** Tranche lot sizing sequence `50% -> 30% -> 20%` derived from base Tranche 0 lots.
  3. **Symbol-Isolated Combined Net Delta Cap:** Calculated strictly per symbol (`WHERE symbol=?`). Capped at `|0.60|`. Entries exceeding 0.60 are blocked with `TFSS_COMBINED_DELTA_CAP`.
  4. **Delta-Stop Exit:** Selectively closes the tested side when leg delta reaches `0.60`.

### F8: Symbol-Aware LLM Provider Chain & Failure Cooldown (P1-HIGH)
- **Symptom:** Unresponsive LLM provider causes 35s scan stalls on every tick.
- **Chain Priorities (OpenCode Zen is always PRIMARY; OmniRouter Antigravity models come second):**
  - **NSE & BSE (live_verdict):** OpenCode Zen -> OmniRouter (Antigravity) -> Groq -> GitHub Models -> NVIDIA NIM -> Bedrock -> OpenRouter -> Gemini
  - **MCX Symbols (live_verdict):** OpenCode Zen -> OmniRouter (Antigravity) -> Groq -> GitHub Models -> AnyAPI Free -> Bedrock Mantle -> NVIDIA NIM -> Bedrock -> OpenRouter -> Gemini -> SambaNova
  - **eod_review:** OpenCode Zen EOD -> OmniRouter (Antigravity) -> Groq -> GitHub Models -> Bedrock Mantle -> NVIDIA NIM -> OpenRouter (Nemotron)
  - **formatting:** OpenCode Zen -> OmniRouter (Antigravity) -> Bedrock Mantle -> GitHub Models -> Groq -> NVIDIA NIM -> OpenRouter (Qwen coder)
- **OmniRouter group is Antigravity-only** (`antigravity/*` via port 20128, timeout 20s) — OpenCode free models were removed from it. No `claude/oc/*` models remain.
- **Self-Heal:** 12-second provider timeout (OmniRouter group 20s). HTTP 500, empty content, or network exceptions trigger an automatic 10-minute cooldown (`_PROVIDER_COOLDOWN_UNTIL[key] = now + 600.0`), skipping failing providers instantly on subsequent ticks.
- **OmniRouter read-timeout policy (2026-08-25 fix):** A single read timeout on one OmniRouter route (e.g. agent-backed `Claude/Antigravity` on large verdict prompts) cools down only that provider for 60s. The whole `omnirouter-primary`/`omnirouter-sentinel` group is cooled down 90s only after 2 consecutive route timeouts in one call, or immediately on host-level errors (connection refused/reset → 120s). This prevents one slow upstream route from benching all premium models behind the local proxy.

### F9: Alert Payload Execution Discrepancy Guard (P1-HIGH)
- **Symptom:** Alert header reported `🟢 Entered` while the Signal section reported `Trade: ✗ Not entered` for setups blocked by Risk Engine or missing valid contracts.
- **Root Cause:** 
  1. `_build_structured_payload()` in `pipeline.py` evaluated `trade_entered = True` via fallback flags even when `trade_decision` action was `BLOCK` or missing contract strike.
  2. `_format_alert_body()` in `digest.py` evaluated top header status from `header["trade_entered"]` independently of whether a contract existed and trade action was non-`BLOCK`.
- **Self-Heal:** 
  1. Updated `pipeline.py` to enforce `trade_entered = False` whenever `trade_decision` action is `BLOCK`/`NO_ACTION` or strike is missing (unless DB/paper trade is active).
  2. Updated `digest.py` (`_format_alert_body`, `format_compact_digest`, `format_experimental_digest`) so header status (`trade_status_str`) requires `is_entered` (`trade_entered == True`, valid contract, and non-`BLOCK` action), guaranteeing 100% truthfulness between header and signal body.

### F10: Friday Mandatory Exit Window (P0-CRITICAL)
- **Symptom:** Open position carried over the weekend exposing account to gap risk.
- **Self-Heal:** Range check `"15:25" <= current_time <= "15:30"` IST (MCX `"23:25" <= current_time <= "23:30"`) forces square-off of open positions on Fridays.

### F11: Natural Gas Session Regime Routing (P1-HIGH)
- **Symptom:** Static NG strategy logic misaligned with market session hours.
- **Self-Heal:** `ng_session_router.py` dynamically routes NATURALGAS to `NG_PARITY`, `NG_EVENT`, or `NG_MOMENTUM` based on time of day. Daily loss cap checks are disabled to allow valid setups.

### F13: Trade Status Discrepancy for Profitable Exits (P1-HIGH)
- **Symptom:** Trade History displays red `SL HIT` status badge for trades with positive net P&L (e.g. +₹26,863 and +₹27,288).
- **Root Cause:** 
  1. In [ng_parity_strategy.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/ng_parity_strategy.py#L200), exit evaluation used `abs(dev_pct) >= sl_pct` without checking position side direction (`BUY` vs `SELL`). When `SELL` (short) parity positions crossed 0 into negative deviation (profitable price drop), `abs()` triggered `hit_sl = True` and set status to `CLOSED_SL`.
  2. [schema.py](file:///c:/Users/manve/Downloads/NSEBOT/src/models/schema.py#L1460) stored `CLOSED_SL` status even if calculated net P&L was positive.
- **Fix:** 
  1. Fixed directional exit evaluation in `ng_parity_strategy.py` so profitable deviation contraction triggers `CLOSED_TARGET`.
  2. Added auto-correction safeguards in `close_paper_trade` and `close_live_trade` in `schema.py`: if `pnl_rupees > 0` or `pnl_points > 0`, status is auto-corrected to `CLOSED_TARGET`.
  3. Updated `sbadge()` in [paper.html](file:///c:/Users/manve/Downloads/NSEBOT/src/dashboard/paper.html#L3034) to render green `TARGET` badge for positive P&L trades.
  4. Migrated historical DB records #341 and #342 in `data/nsebot.db` to `CLOSED_TARGET`.

### F12: ContextManager Generator Protocol Violations & Read-Only Lock Contention (P0-CRITICAL)
- **Symptom:** Logs show `[scheduler] Kite position sync failed: generator didn't stop after throw()`, `get_previous_underlying: database is locked`, and `_update_live_cmps timed out after 120s`.
- **Root Cause:** 
  1. In [schema.py](file:///c:/Users/manve/Downloads/NSEBOT/src/models/schema.py), `get_conn()` placed the `attempt` loop *inside* `@contextlib.contextmanager` surrounding `yield conn`. When an exception was thrown to the generator via `.throw()`, executing `continue` caused the generator to `yield` a second time, violating Python's context manager protocol and raising `RuntimeError: generator didn't stop after throw()`.
  2. 25+ getter functions in `schema.py` (e.g. `get_previous_underlying`, `list_paper_trades`, `get_open_tfss_legs`) and `job_runner.py` called `get_conn()` without `read_only=True`. As a result, pure `SELECT` queries in concurrent background threads acquired exclusive `BEGIN IMMEDIATE` write locks, blocking active pipeline transactions with `sqlite3.OperationalError: database is locked`.
- **Fix:** (1) Refactored `get_conn()` in [schema.py](file:///c:/Users/manve/Downloads/NSEBOT/src/models/schema.py#L448-L490) so connection setup and `BEGIN IMMEDIATE` retry loops execute *prior* to yielding, guaranteeing exactly one yield statement per context lifecycle. (2) Audited and updated all 25+ read-only getter functions in [schema.py](file:///c:/Users/manve/Downloads/NSEBOT/src/models/schema.py) and [job_runner.py](file:///c:/Users/manve/Downloads/NSEBOT/src/scheduler/job_runner.py#L842-L864) to pass `read_only=True`, ensuring non-modifying queries execute fast non-blocking `BEGIN DEFERRED` read transactions.

### F14: Shoonya SearchScrip Concurrency Lock Timeout & Fallback Failure (P1-HIGH)
- **Symptom:** Warnings logged during parallel prefetch: `[shoonya] could not search scrip for SENSEX FUT` and `[shoonya] could not search scrip for BANKNIFTY`.
- **Root Cause:** 
  1. `_api_lock.acquire(blocking=False)` in [shoonya_fetcher.py](file:///c:/Users/manve/Downloads/NSEBOT/src/fetchers/shoonya_fetcher.py#L571) instantly dropped concurrent calls when multiple symbols (`NIFTY`, `BANKNIFTY`, `SENSEX`, `NATURALGAS`) were fetched simultaneously by the 16-worker `pipeline_io_executor`.
  2. `SearchScrip` primary text lacked fallback search queries when symbol format variants (`SENSEX FUT` vs `SENSEX`, `BANKNIFTY` vs `BANKNIFTY FUT`) produced no values.
  3. `fetch_option_chain` used hardcoded legacy futures token check `"25JUN26"`.
- **Fix:**
  1. Replaced non-blocking lock with a 3.0s timeout lock `_api_lock.acquire(blocking=True, timeout=3.0)` in `_api_call`, serializing parallel symbol requests smoothly.
  2. Added primary + fallback search queries in `fetch_option_chain` (`SENSEX FUT` -> `SENSEX`, `BANKNIFTY` -> `BANKNIFTY FUT`).
  3. Refactored futures contract resolution to sort by `exd` expiry date chronologically and pick the near-month contract.
  4. Removed orphaned dead code block after return line 1450.

### F136: Shoonya ISP IP Rotation — Automated Headless Portal IP Updater (P1-HIGH)
- **Symptom:** `[shoonya] GenAcsTok failed: {'stat': 'Not_Ok', 'emsg': 'Invalid Input : INVALID_IP', ...}` after a ~60s Playwright OAuth, recurring every time the ISP rotates the public IP (3–4 day DHCP leases).
- **Root Cause:** Shoonya validates the request source IP at login (`GenAcsTok`). A rotating ISP public IP is not bound to the account, so login is rejected even though the OAuth web login succeeds.
- **Self-Heal:** `src/fetchers/shoonya_ip_guard.py` runs a once-per-IST-day public-IP check (reusing `src/utils/ip_monitor.py` detection). On rotation, it immediately triggers `src/fetchers/shoonya_ip_updater.py` which launches headless Playwright, logs into Shoonya's portal (`https://api.shoonya.com/OAuthlogin/`), and automatically updates the `Primary IP Address` and `Backup IP Address` to the new public IP. If the update succeeds, the new baseline IP is saved, skip flag is cleared, and normal Shoonya fetching resumes without interruption. If the update fails, it falls back to setting `skip_date` and alerts via Telegram. Manual trigger available via `python main.py --update-shoonya-ip`. State: `data/shoonya_ip_state.json` (`baseline_ip`, `checked_date`, `skip_date`).

### F137: OmniRouter HTTP Timeout Cap Truncation (P1-HIGH)
- **Symptom:** LLM enrichment logs show `OmniRouter (antigravity/claude-sonnet-4-6) exception: JSON extract failed: Expecting ',' delimiter: line 19 column 6 (char 644)`.
- **Root Cause:** In `src/engine/llm_enrichment.py`, provider HTTP requests enforced `timeout=min(remaining, provider.get("timeout", 12.0))`. The fallback default of `12.0` seconds truncated response generation mid-JSON for large schemas (e.g. `LLMMultiLegVerdict`) on OmniRouter models configured with 20s–30s provider timeouts.
- **Fix:** Updated the timeout fallback in `_call_llm_api()` to default to `20.0` seconds (`provider.get("timeout", 20.0)`), allowing proxy-backed OmniRouter models (`antigravity/claude-sonnet-4-6`, `cx/gpt-5.5`) full timeout budget to complete complex structured JSON outputs without mid-stream truncation.

### F138: Pre-Flight Data Legitimacy Gates & Theoretical Option Bounds (P0-CRITICAL)
- **Symptom:** Generic spike/gap scrubbing on option premiums dropped breakout moves and 0DTE surges, blinding the risk engine during stop-loss events. Flat 8% proximity pruned Natural Gas chains, 0DTE caused division-by-zero risks in Greeks, and soft scores risked partial multi-leg execution.
- **Root Cause:** Inflexible single-tick heuristics and missing asset-class awareness in `data_validator.py` and `trade_plan.py`.
- **Fix:**
  1. Replaced arbitrary OTM premium caps with theoretical boundary limits ($P \le S$ for CE, $P \le \max(K, S)$ for PE). Percentage anomaly checks applied exclusively to underlying spot/futures.
  2. Implemented hierarchical liquidity validation (Primary market depth $\text{Spread}/\text{LTP} \le 0.40$ vs Secondary $\text{LTP} > 0 \land \text{OI} > 0$ with `FORCE_LIMIT` guardrails).
  3. Asset-class dynamic proximity: Indices $\pm 8\%$ / $\pm 25$ strikes; Commodities (Natural Gas, Crude, Metals) $\pm 20\%$ / $\pm 10$ strikes minimum.
  4. Fractional time-to-expiry ($T > 0$) calculation with hard 0DTE new-entry cutoffs at 15:15 IST (NSE) and 23:15 IST (MCX).
  5. Implemented strict 100% binary data integrity validation (`validate_trade_leg_data`) for all target legs before multi-leg and single-leg order dispatch.
  6. Internal candle continuity validation and spot vs forming candle envelope comparison $[0.95L, 1.05H]$.

### F139: R5 Option-Type Mismatch False-Positive & Sentinel Diagnostic Hallucination Guard (P1-HIGH)
- **Symptom:** Scan Sentinel logged spurious `CRITICAL` incidents such as `BANKNIFTY: Sentinel Diagnosis: Multi-leg strategy runner hitting AttributeError when option chain returns list instead of dict structure` and `NATURALGAS: Option chain data returned as list instead of expected dictionary structure causing AttributeError in multileg strategy`, with `EVIDENCE: None` and no matching traceback in the logs.
- **Root Cause:** Two compounding defects:
  1. Rule `R5_OPTION_TYPE_MISMATCH` flagged `GO_SHORT + CE` and `GO_LONG + PE` as `CRITICAL` "unresolved hedge mapping". These are VALID short-premium constructions (sell call / sell put) used by MULTILEG/TFSS strategies, and `_sanitize_llm_verdict` in `llm_enrichment.py` explicitly documents all four action/instrument combos as valid. The rule assumed CORE buy-premium mapping only.
  2. The diagnostic LLM, fed the full Knowledge Base, force-fit the resolved option-chain normalization entry (now F134) and hallucinated an `AttributeError` diagnosis with no supporting log evidence. Duplicate KB section IDs (two `F131`, two `F132`, two `F133`, two `F129`, two `F6`) made citation ambiguous and encouraged re-diagnosing already-fixed issues.
- **Fix:**
  1. Downgraded `R5_OPTION_TYPE_MISMATCH` from `CRITICAL` to `WARNING` in `scan_sentinel.py` and rewrote its detail to state that GO_SHORT+CE / GO_LONG+PE are valid short-premium constructions (review only if unexpected for a CORE buy-premium symbol).
  2. Added an R5-specific guideline and an EVIDENCE REQUIREMENT to the `_run_ai_diagnostic()` prompt: R5 must never be diagnosed as F134/AttributeError/option-chain-structure; every diagnosis must quote a real log line; no error line → at most WARNING, never CRITICAL; RESOLVED/FIXED KB entries must not be re-diagnosed without a fresh matching traceback.
  3. Deduplicated KB section IDs: option-chain normalization → `F134` (marked RESOLVED), Zero-OI anomaly → `F135`, Shoonya ISP IP rotation → `F136`, OmniRouter timeout cap → `F137`, Pre-flight data gates → `F138`. `F133` (Inverse Target/SL, referenced by the R8 guideline) is unchanged.
- **Verification:** No real multileg `AttributeError` exists in `logs/main.log`; `_normalize_option_chain()` is in place and `validate_legs()`/`compute_book_greeks()` are safe. The only genuine `AttributeError` (ShoonyaFetcher `_increment_and_save_call_count`) was already fixed.

---

## 3. Scan Sentinel Safety Suite (Rules R1–R12)
1. **R1 (Data Integrity):** Verifies LTP > 0, IV >= 0, and non-null strike increments.
2. **R2 (PCR Sanity):** Validates PCR within `[0.10, 5.00]`.
3. **R3 (AI Inversion Guard):** Confirms LLM thesis matches OI engine direction.
4. **R4 (Risk Cap Enforcement):** Verifies TFSS net delta <= 0.60 and book margin <= 600k.
5. **R5 (Position Synchronization):** Reconciles paper, shadow, and live broker trade states. `sync_direct_kite_positions()` in `live_trading.py` adopts manually placed Kite positions. Requires module-level `import time` in `schema.py` to avoid `NameError` in database lock retry handlers.
6. **R6 (Digest Truthfulness):** Verifies Telegram alert text matches DB entry status.
7. **R7 (Database Health):** Verifies WAL mode and connection lock availability.
8. **R8 (Fetcher Fallback Coverage):** Ensures secondary fetcher availability on primary failure.
9. **R9 (Expiry Alignment):** Confirms DTE calculation and contract rollover dates.
10. **R10 (Friday Square-off):** Ensures Friday exit triggers execute before weekend close.
11. **R11 (Cooldown Enforcement):** Verifies failing LLM and fetcher cooldown timers.
12. **R12 (Order Execution Integrity):** Confirms tick size, lot sizing, and slippage calculations.
