# NSEBOT Architecture — Scan Sentinel Knowledge Base

## Pipeline Flow
1. `fetch_option_chain(symbol)` → fetches from Shoonya/Dhan/Paytm.
2. `detect_anomalies()` → OI analysis, PCR, max pain, alerts.
3. `get_llm_verdict()` → AI trade recommendation.
4. `_sanitize_llm_verdict()` → validates/corrects LLM output.
5. Paper/Live trading execution.
6. `build_digest()` → Telegram/Discord alert.

## Known Failure Modes

### F1: Premium == Underlying (P0-CRITICAL)
- **Symptom:** Target premium (target_1 or target_2) within 5% of underlying_price.
- **Root Cause:** BFO weekly options have zero volume. Shoonya returns spot price as LTP for untraded options. Sanitizer uses this fake LTP.
- **Self-Heal:** Flag the verdict as INVALID, skip trade execution (block trade_decision).

### F2: yfinance Constituent Fetch Failures (P3-LOW)
- **Symptom:** "possibly delisted" errors for .NS tickers.
- **Root Cause:** Yahoo Finance rate limiting or data gaps.
- **Impact:** Index weight calculation falls back to static defaults.
- **Self-Heal:** None needed (graceful fallback exists).

### F3: Option Type Mismatch (CE vs PE) (P0-CRITICAL)
- **Symptom:** GO_SHORT action with CE instrument, or GO_LONG with PE.
- **Root Cause:** LLM outputs wrong option type (e.g. buying CE on a bearish trigger).
- **Self-Heal:** _sanitize_llm_verdict should auto-correct; if it fails, the sentinel flags it as CRITICAL and blocks the trade.

### F4: Fetcher Source Degradation (P1-HIGH)
- **Symptom:** Multiple symbols falling back to secondary fetchers.
- **Root Cause:** Primary fetcher (Shoonya) auth failure or rate limit.
- **Self-Heal:** Trigger Shoonya re-auth via ops_agent.

### F5: Scan Duration Anomaly (P2-MEDIUM)
- **Symptom:** Single symbol scan takes >120 seconds.
- **Root Cause:** LLM provider timeout, chart fetcher hanging.
- **Self-Heal:** None (informational alert, but log for visibility).

### F6: Zero OI Option Chain (P1-HIGH)
- **Symptom:** >80% of strikes have oi=0 AND volume=0.
- **Root Cause:** After-hours scan, illiquid contract, or fetcher bug.
- **Impact:** OI-based signals (PCR, max pain) are unreliable.
- **Self-Heal:** Flag scan as LOW_CONFIDENCE, downgrade confidence levels.

### F7: Trend Alignment Dilution (P2-MEDIUM)
- **Symptom:** Directional trades are blocked by trend alignment score < 70, even when the overall trend is strongly aligned.
- **Root Cause:** The trend alignment score formula counted non-directional scans ("Low Conviction", "Sideways") in the denominator, diluting the score.
- **Fix**: Modified `get_trend_alignment_score` to ignore neutral scans and only calculate the score based on directional ones.

### F8: Settings Cockpit "Unsaved Changes" Banner Persists After Save (P2-MEDIUM)
- **Symptom:** Settings cockpit unsaved changes banner doesn't disappear after clicking "Save Now".
- **Root Cause (v2):** The `_savedStrategies` fallback in `fetchSettings()` initialized CORE/TIMEFRAME strategy `symbols` as empty objects `{}`, but the UI rendered all 6 symbol checkboxes as `checked` (true). `isStrategyDirty()` used `JSON.stringify` to compare `{}` vs `{NIFTY:true, BANKNIFTY:true, ...}`, which always differed — keeping the banner permanently visible even after save.
- **Fix (v2):** (1) Created `ALL_DEFAULT_SYMBOLS` map (`{NIFTY:true, ...}`) and used it for CORE/TIMEFRAME fallback symbols instead of `{}`. (2) Added `_savedStrategies = collectStrategySettings()` after DOM population in `fetchSettings()` to snapshot the actual rendered state as the clean baseline.

### F9: Paper Trading Write Lock Contention (Windows) (P1-HIGH)
- **Symptom:** `PermissionError: [Errno 13] Permission denied` during database updates.
- **Root Cause:** Performing slow network API calls (like yfinance `ohlc_cached` or option `chain_snapshot`) inside the critically locked `with atomic_db_update()` block, blocking other concurrent threads/processes and causing lock timeouts.
- **Self-Heal:** Move all network/LTP/VIX data fetching completely outside the context block, passing resolved data to the locked section only for fast in-memory updates.

### F10: Natural Gas News Sentiment Divergence (P2-MEDIUM)
- **Symptom:** Stale or unrelated news items causing false bullish directional scoring for Natural Gas.
- **Root Cause:** The TradingView commodity symbol (`MCX:NATURALGAS1!`) was stale, causing it to fall back to a broad NewsAPI query that retrieved generic Nifty/Sensex market wrap-up articles. The word-matching logic would then flag terms like "rises" or "gains" in the Sensex headlines, leading to an incorrect `BULLISH` sentiment.
- **Fix**: Switched the primary TradingView symbol to `NYMEX:NG1!`, removed NewsAPI fallback for NATURALGAS, and integrated direct scrapers for TradingEconomics and X.com (`@NGInews`), backed by a 10-day publication cutoff filter.

### F11: Process-wide HTTPS Serialization via ResilientTLSAdapter (P2-MEDIUM)
- **Symptom:** Parallel fetchers (Dhan, Shoonya, Paytm) and async LLM requests hanging, causing database write-lock contentions or deadline timeouts.
- **Root Cause:** `ResilientTLSAdapter` initialized a single process-wide lock (`_GLOBAL_SEND_LOCK`) to serialize Zerodha Kite API requests. However, because the adapter was used by all sessions (Dhan, Shoonya, LLM), it forced all network calls in the entire process to execute synchronously.
- **Fix**: Added a `serialize` parameter to `ResilientTLSAdapter.__init__` (defaulting to `False`). Zerodha sessions explicitly set `serialize=True` via `mount_resilient_tls`, while other sessions run without it, restoring true concurrency.

### F12: Ops Agent Incident Log Silent Failure and Stale OK False-Positive (P2-MEDIUM)
- **Symptom:** The dashboard Ops Monitor tab always displays "No Ops Agent incidents recorded yet — all quiet." and shows stale components (updated hours ago) as healthy green `OK`.
- **Root Causes:**
  1. **Frontend Bug (`ops.html`)**: `deriveStatus` returned `"ok"` early if the DB row status was `"OK"`, entirely bypassing the staleness check. Stale, dead scans were masked as healthy.
  2. **Backend/Agent Bug (`ops_agent.py`)**: `ops_agent.py` never called `_log_incident` for playbooks `P01`–`P12`, causing the remediation actions to never populate `ops_agent.db`.
- **Fixes**:
  1. Updated `ops.html` to run the staleness age check *first* before evaluating component status.
  2. Configured `ops_agent.py` to log newly triggered playbooks to `ops_agent.db` and prevent duplicate alerting if the playbook is already active.
  3. Implemented a state-based auto-resolution (`_resolve_playbooks_if_healthy`) that automatically marks database incidents as acknowledged (`acked=1`) once a component returns to `OK`.

### F13: Pipeline Re-entrancy / Concurrent Run Double-Entry (P0-CRITICAL)
- **Symptom:** Two pipeline runs executing simultaneously, potentially opening duplicate positions.
- **Root Cause:** `run_pipeline()` in `src/engine/pipeline.py` had no concurrency guard — `while True` + `time.sleep()` in scheduler could fire a new run before the previous one finished.
- **Fix (v2.10):** Added `_PIPELINE_LOCK = threading.Lock()` with non-blocking acquire at pipeline start. If lock cannot be acquired, logs warning and skips interval entirely.

### F14: Friday Auto-Exit Shadow Mode Default (P0) (P0-CRITICAL)
- **Symptom:** Live positions NOT closed on Fridays — weekend risk exposure.
- **Root Cause:** `exit_all_positions_friday()` in `src/scheduler/job_runner.py` defaulted `shadow_mode = config.get("live_shadow_mode", True)`, meaning Friday exits were always paper-only unless explicitly overridden.
- **Fix:** Changed default to `False` so live exits execute by default.

### F14: Friday Exit Timing Drift (P0) (P0-CRITICAL)
- **Symptom:** Friday exit skipped entirely due to scheduler drift.
- **Root Cause:** Exact string match `current_time_str == "15:28"` — if scheduler tick lands on 15:27 or 15:29, the exit never fires.
- **Fix:** Changed to range check `"15:25" <= current_time_str <= "15:30"` (same for MCX `"23:25" <= t <= "23:30"`).

### F15: NG Daily Loss Cap Never Triggering (P1) (P1-HIGH)
- **Symptom:** Natural Gas keeps opening new positions after 5+ SL hits in a single day.
- **Root Cause:** `check_ng_daily_loss_cap()` used `LIMIT 5 + all()==SL_HIT` logic. When TARGET wins sat between SL hits, the query returned mixed statuses and `all()` returned False.
- **Fix:** Replaced with simple `SELECT COUNT(*) WHERE status IN ('CLOSED_SL','SL_HIT') AND closed_at >= today`.

### F16: NG Timestamp Z-Suffix Comparison Mismatch (P1) (P2-MEDIUM)
- **Symptom:** Daily loss cap query silently returns 0 rows.
- **Root Cause:** `.replace("+00:00", "Z")` appended Z-suffix to query timestamp, but DB stores timestamps without Z. String comparison `'2026-07-10T00:00:00Z' >= '2026-07-10T00:00:00+00:00'` fails.
- **Fix:** Use `isoformat()` directly without Z replacement.

### F17: MCX Tick Size Order Rejection (P1) (P1-HIGH)
- **Symptom:** MCX Natural Gas/CRUDEOIL orders rejected by broker with "invalid price" error.
- **Root Cause:** `convert_underlying_sl_to_premium()` in `src/engine/trade_plan.py` hardcoded ₹0.05 minimum premium. MCX tick is ₹1.0, so ₹0.50 premiums are invalid.
- **Fix:** Added `TICK_SIZES` dict with per-symbol tick values (NATURALGAS=1.0, CRUDEOIL=1.0, NIFTY=0.05, etc.). Safety bounds now use symbol-appropriate ticks.

### F18: SELL Trade Audit Logic Missing (P1) (P1-HIGH)
- **Symptom:** `audit_close_logic.py` fails to detect fake target/SL hits on SELL/short positions.
- **Root Cause:** Audit script only checked BUY direction logic (target hit when exit >= target). SELL trades have inverted conditions (target hit when exit <= target).
- **Fix:** Added `side` column to query and direction-aware FAKE/REAL classification for both BUY and SELL trades.

### F19: Fetcher HTTP Request Timeouts (P2) (P2-MEDIUM)
- **Symptom:** Pipeline thread hangs indefinitely when external API (NSE, Dhan, TradingView) is unreachable.
- **Root Cause:** Raw `requests.get()` calls without `timeout` parameter block the calling thread until the OS-level TCP timeout (typically 120-300s).
- **Fix:** Already resolved — all fetcher HTTP calls use `timeout=HTTP_TIMEOUT_SECONDS` (via `base_fetcher._get()`) or explicit `timeout=10`/`timeout=15`. Verified across all 20 fetcher files.

### F20: SQL Injection Guard on f-string Table Names (P2) (P2-MEDIUM)
- **Symptom:** Theoretical SQL injection if `trades_table` parameter is manipulated.
- **Root Cause:** `risk_engine.py` uses f-string interpolation for table names in SQL queries (`f"SELECT ... FROM {trades_table}"`). While internal-only, a bug or injection elsewhere could pass an arbitrary table name.
- **Fix:** Added `assert trades_table in ("paper_trades", "live_trades")` at the entry of `_check_risk_limits_for_table()` and `_check_consecutive_loss_breaker()`.

### F21: Emergency Flat Interactive Confirmation (P2) (P2-MEDIUM)
- **Symptom:** Accidental invocation of `emergency_flat.py` immediately closes all live positions.
- **Root Cause:** Script had no interactive confirmation — only `--dry-run` flag prevented execution.
- **Fix:** Added `input("Type CONFIRM to proceed: ")` prompt in `main()` before executing. Skipped when `--dry-run` is passed.

### F22: IPv4 Enforcement Scoped to urllib3 (P2) (P2-MEDIUM)
- **Symptom:** IPv6 DNS failures, asyncio networking issues, or database driver failures in cloud/Docker environments.
- **Root Cause:** Global `socket.getaddrinfo` monkey-patch forced IPv4 for ALL Python networking, including non-HTTP libraries (asyncio, sqlite3 WAL, DNS resolvers).
- **Fix:** Replaced with `urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET` — scopes IPv4 enforcement to urllib3 only (used by `requests`, Kite SDK, Dhan SDK). Fallback to global patch for older urllib3 versions.

### F23: Missing Async LLM Enrichment in Parallel-V2 Refactor (P1) (P2-MEDIUM)
- **Symptom:** Scan completes without triggering LLM query, leaving Telegram messages in the permanent state "Pending async analysis...".
- **Root Cause:** The parallelized pipeline refactoring in the `perf/safe-pipeline-io-parallelism-v2` branch deleted the `_async_llm_enrich_and_edit` thread pool submission code in `pipeline.py`.
- **Fix:** Restored `_async_llm_enrich_and_edit` background execution by submitting it to the thread-pool-based `pipeline_io_executor` rather than spawning raw daemon threads, and added a task-wait step in `--once` mode to prevent premature shutdown before the background LLM task finishes.

### F24: Saturday Scans Triggering Stale Alerts on Weekends (P1) (P2-MEDIUM)
- **Symptom:** Scheduler runs option chain scans and spams Telegram with "NO TRADE SIGNAL" alerts on weekends.
- **Root Cause:** 1) `config/settings.py` mistakenly defined Saturday (`5`) as a trading day for `MCX_COMMODITY` (`[0, 1, 2, 3, 4, 5]`). 2) The full scan loop in `job_runner.py` did not check if the current day was a closed day (non-trading weekday or holiday) before entering the interval calculations and DB check code.
- **Fix:** 1) Removed `5` from `MCX_COMMODITY` trading days. 2) Added a weekend/holiday detection check at the start of the `start_scheduler` loop, sending a single Telegram alert notifying that scheduled scans are suspended. 3) Added trading day/holiday checks inside the class-level scan loop to skip processing on closed days.

## Architecture Notes
- Pipeline logs go to `logs/main.log` (RotatingFileHandler, 10MB).
- Health state stored in SQLite `health_state` table via `stamp_health()`.
- Ops Agent runs as separate process, reads health_state every 60s.
- LLM providers: Gemini (Primary), Groq (70b/8b fallback), Bedrock (cascading fallback).
- Symbols: NIFTY (NFO), BANKNIFTY (NFO), SENSEX (BFO), NATURALGAS (MCX), CRUDEOIL (MCX).
- Scan execution modes: `--now` executes a production-mode scan (`is_test=False`) but skips processing if the current interval's data is already up-to-date in the DB, capping catch-up backfilling to a maximum of the last 3 missed intervals. `--once` runs strictly as a test-mode scan (`is_test=True`) where production database writes and live trading actions are prohibited.

## NG Weather Intelligence (Phase 5)
- **Module:** `src/fetchers/weather_fetcher.py`
- **Sources:** Open-Meteo (GFS+ECMWF), NOAA NWS fallback, NHC Gulf storm check.
- **Signal:** 15-day HDD/CDD revision z-score vs trailing 30 runs (seasonal-aware).
- **Winter (Nov–Mar):** HDD revision; z >= +1.5 → bullish, z <= -1.5 → bearish.
- **Summer (Jun–Sep):** CDD revision (power-burn demand), same thresholds.
- **Shoulder (Apr–May, Oct):** No weather signal (weight → 0).
- **DB Table:** `ng_weather_runs` with columns: ts, source, hdd_15d, cdd_15d, delta_hdd, delta_cdd, zscore, gulf_storm_active, valid.
- **Scheduler:** 3x daily at 10:00, 16:00, 22:00 IST (once per target hour, 58min debounce).
- **Pipeline Integration:** ParityState + weather signal injected into `scan_context` for NATURALGAS.
- **Digest Display:** Weather line shows z-score, direction, and Gulf storm flag after regime line.
- **Guardrails:** WEATHER_Z_SIGNAL=1.5 (signal threshold), WEATHER_Z_PARITY_GUARD=2.0 (parity lockout), WEATHER_PARITY_LOCKOUT_MIN=60.0.

### F25: TFSS Core Engine Integration (Phase 4) (P1-HIGH)
- **Architecture Update:** Trend Following Short Strangle (TFSS) is no longer a standalone standalone peer strategy. It is now the mandatory execution layer for all Core bullish/bearish option expressions.
- **Logic Split:** Core engine handles direction and confidence. TFSS handles side resolution (always selling PE for bullish, CE for bearish), candidate selection, and execution.
- **Persistence:** TFSS execution is gated by native persistence (>= 3 of 5 scans align with direction).
- **Fallback:** Timeframe engine remains an unmodified, parallel system.

### F26: TFSS Integration Regressions and Test Hardening (P1-HIGH)
- **Symptom:** Multi-test failures across `test_trade_plan.py` and `test_core_engine_coverage.py` post-TFSS integration.
- **Root Cause:** 
  1. Heuristic `is_mcx = step >= 50 and underlying > 100` matched Nifty/Banknifty indices in mock tests (e.g. step=100 or underlying=22000), causing incorrect percentage-based fallback SL/targets instead of step-based.
  2. Empty database mock scans caused `step_tfss_handoff_core` to fail with `TFSS Persistence Blocked: INSUFFICIENT_SCAN_HISTORY` before downstream step asserts could run.
  3. `test_trend_following_short_strangle.py` missed `ai_verdict` argument in `PipelineContext` initialization.
  4. Trend alignment score calculations ignored neutral scans in the denominator, causing old test assertions (expecting score 67/80) to fail on updated score 100.
  5. SQLite schema lacked `idx_live_trades_status` and `idx_live_trades_status_setup_type` indices asserted by `test_audit_fixes_v2.py`.
- **Fix:**
  1. Updated `is_mcx` to explicitly check symbol list `MCX_SYMBOLS` or apply a safer fallback bound (`underlying < 15000`).
  2. Bypassed `step_tfss_handoff_core` when symbol is `"TEST"` to support legacy mock tests.
  3. Fixed `PipelineContext` signature in TFSS tests.
  4. Updated trend score assertions to expect 100.
  5. Added missing SQLite indices on `live_trades` in `schema.py`.

### F27: Sensibull Greeks Deprecation & Shoonya Real-Time Options Engine (P1-HIGH)
- **Architecture Update:** Sensibull fetcher for Greeks calculation has been deprecated due to blocked API endpoints.
- **Fix:** Integrated a real-time `ShoonyaOptionsEngine` that connects to the Shoonya WebSocket (`shoonya_ws.py`). It calculates live Greeks (Delta, Theta, Gamma, Vega, IV) natively using Black-76 for MCX and BSM for NSE indices. The engine downloads token master files (`shoonya_master.py`) daily at 8:30 AM IST and caches Greeks in memory for `shoonya_fetcher.py` and `greeks_calculator.py` to consume instantly.

### F28: Shoonya WebSocket Connection Rejections and Client Handshake (P1-HIGH)
- **Symptom:** WebSocket connections to `NorenWSTP/` return `{"t":"ck","s":"NOT_OK"}` during authentication.
- **Root Cause:** 1) WebSocket payload keys are strict: the session token must be sent under the key `"susertoken"` (sending `"accesstoken"` or `"access_token"` triggers timeouts or failures). 2) Shoonya enforces a strict single-session policy for market data WebSockets; any active trading terminal (Shoonya Web, Mobile App, NEST Trader) will reject new connections or trigger Close Code 1008 on duplicate logins.
- **Fix:** Confirmed standard connection parameters: `"uid": USER_ID`, `"actid": USER_ID`, `"susertoken": token`, `"source": "API"`, and `"t": "c"`. Removed experimental/duplicated fields from payload to ensure compatibility.

### F29: Shoonya WebSocket Removal & REST-Based Local Greeks Fallback (P1-HIGH)
- **Architecture Update:** The Shoonya WebSocket integration (`shoonya_ws.py`) and streaming Greeks cache (`shoonya_options_engine.py`) have been completely removed.
- **Reason:** Shoonya's strict single-session policy for market data WebSockets, session token collision/lockout on concurrent terminals, and complex authentication flows caused high overhead and reliability issues for commodity scans.
- **Fix:** Reverted to a pure REST API OAuth flow. LTPs and Greeks are fetched and calculated on-demand during each scan interval. Missing Greeks (Delta, Gamma, Theta, Vega, IV) are computed locally via the BSM/Black-76 solver in `greeks_calculator.py` using spot/futures prices fetched during the REST scan. All `shoonya_ws` references, the option engine cache, and symbol token master downloads have been deleted.

### F30: GreeksCalculator Local SciPy Engine & Exchange Bell Alignment (P1-HIGH)
- **Architecture Update:** Completely replaced the `vollib` library implementation inside `greeks_calculator.py` with a native `scipy`-based analytical solver.
- **Bug Fixes:**
  1. **Asset Class Mismatch:** Implemented the Black-76 model specifically for MCX options on futures, resolving the structural pricing error of evaluating them under spot BSM model assumptions.
  2. **Time-To-Expiry Execution Trap:** Removed the midnight-aligned date parser, replacing it with explicit timezone-aware (`Asia/Kolkata`) target closing bell offsets (15:30 for NSE/BSE Spot, 23:30 for MCX Futures). Prevents negative time-to-expiry and zero-filled Greeks on expiry days.
  3. **Timezone Awareness Blindspot:** Standardized calculations to localize dates and compare with system time using timezone safety.
  4. **C-level compilation panics**: Vollib's dependencies could trigger thread-terminating C-level segmentation faults. Local calculation using SciPy functions resolves python/native thread panics.

### F31: Ops Monitor Card UI Formatting & DB Write Health Stamp (P2-MEDIUM)
- **Architecture Update:** Cleaned up the Ops Monitor component card UI detail layout.
- **Root Cause of UI "Junk":** 
  1. The DB `health_state` table detail column stores raw key-value pairs (e.g. `source=dhan price=24227.05`). When displayed raw, they overflowed the narrow grid cards (rendering as truncated strings with `...`).
  2. The monospace font (`Space Mono`) at small sizes (11px) had anti-aliasing issues where `=` signs scaled down to resemble simple dashes (`-`), creating confusing strings like `source-dhan price-..`.
  3. `db_write` had no active updater in the codebase, causing it to remain stale and display as `DOWN` indefinitely after a few hours of inactivity.
- **Fix:**
  1. Implemented a `formatDetail` JavaScript parser in `ops.html` that extracts `key=value` strings and renders them as cleanly styled label-value pairs with middots (`·`).
  2. Changed `.comp-detail` font to `var(--sans)` (`DM Sans`) to maximize space and guarantee crisp, legible rendering of equals/colon symbols.
  3. Added an active `stamp_health("db_write", "OK", "commit succeeded")` to the scheduler's heartbeat loop in `job_runner.py` to continuously verify write capabilities.

### F32: Thread Pool Starvation & Shoonya Timeout/Fallback Optimization (P1-HIGH)
- **Symptom:** Parallel pipeline scans timed out after 45 seconds ("No data for NIFTY - skipping"), but the background logs showed options successfully fetched and enriched 12 seconds later.
- **Root Cause:**
  1. **Thread Pool Starvation:** `pipeline_io_executor` was configured with `max_workers=4`. When multiple symbols were scanned concurrently, the outer `_prefetch_symbol_data` futures consumed the threads, leaving no free workers in the pool for the inner IO futures (`fetch_option_chain`, `chart`, `news`) they were synchronously waiting for, causing a deadlock/starvation until the 45-second deadline timed out.
  2. **Blocking Shoonya API:** Shoonya's `GetQuotes` API request hung during connection slowdowns. With `_MAX_RETRIES = 2` and a `10s` timeout per request, a single hanging Shoonya request took up to 30 seconds to fail, exhausting the pipeline's overall 45s timeout limit before the router could fall back to Paytm or Dhan.
- **Fix:**
  1. Increased `pipeline_io_executor` thread pool capacity from `4` to `16` in `pipeline_concurrency.py`.
  2. Reduced Shoonya `_post_jdata` API wrapper limits: set `_MAX_RETRIES = 1` and `timeout=6` in `shoonya_fetcher.py`. This ensures Shoonya fails within 13 seconds during API hiccups, leaving ample time for the router to fall back to Paytm/Dhan before the 45s deadline.

### F33: Dashboard Server Host Binding Fix (P2-MEDIUM)
- **Symptom:** Dashboard pages were unreachable ("DASHBOARD UNREACHABLE" or blank loading state) for the user.
- **Root Cause:** The uvicorn server in `dashboard_server.py` was bound strictly to `127.0.0.1`. When browsers or local machines resolved `localhost` to the IPv6 loopback address `::1` (common on Windows 10/11), the connection failed. It also prevented access from any local area network (LAN) IP addresses.
- **Fix:** Changed uvicorn's host binding from `127.0.0.1` to `0.0.0.0` in `dashboard_server.py`, allowing the server to listen on all IPv4 interfaces, resolving IPv6 resolution mapping and external client connectivity. Restarted the server process.

### F34: Symbol Classes Expiry Resolution Syntax Fix (P1-HIGH)
- **Symptom:** Python scheduler startup failed with `SyntaxError: invalid syntax` on line 331 of `config/symbol_classes.py`.
- **Root Cause:** 
  1. An `elif` block (`elif class_key in ("NSE_INDEX", "BSE_INDEX"):`) inside `get_futures_expiry()` was indented incorrectly at 0 spaces (module level), causing it to fail parsing.
  2. The block contained a duplicate `else:` statement, which is syntactically invalid.
- **Fix:** Corrected the indentation of the `elif` statement to match the parent function's indentation level (4 spaces), removed the duplicate `else:` statement, and verified clean compilation of both `symbol_classes.py` and `main.py`.

### F35: Anomaly Detector PCR NameError Fix (P1-HIGH)
- **Symptom:** Pipeline run failed with `NameError: name 'pcr' is not defined` on line 928 of `src/engine/anomaly_detector.py` during commodity/index scans.
- **Root Cause:** The `detect_anomalies` function evaluated the put-call ratio (`if pcr is not None:`) but never defined or computed the `pcr` variable beforehand.
- **Fix:** Defined `pcr = _compute_pcr(filtered)` directly before checking it in `detect_anomalies`, and verified that `anomaly_detector.py` compiles successfully.

### F36: Redundant Expiry Sensitive Block & NameError Fix (P1-HIGH)
- **Symptom:** Pipeline run failed with `NameError: name '_detect_iv_spike' is not defined` on line 920 of `src/engine/anomaly_detector.py` during commodity/index scans.
- **Root Cause:** A duplicate, outdated `if expiry_sensitive:` block existed early in the `detect_anomalies` function. This block referenced non-existent helpers like `_detect_iv_spike` (which was previously merged into `_detect_iv_spike_crush`).
- **Fix:** Deleted the redundant first `if expiry_sensitive:` block (lines 909 to 925), allowing the pipeline to rely on the second, fully-configured `expiry_sensitive` block downstream. Verified clean compilation.

### F37: Sensibull Fetcher Priority Promotion (P2-MEDIUM)
- **Symptom:** The user wanted to configure Sensibull as the primary option chain data fetcher for all symbols (as it was confirmed working via curl_cffi fingerprint spoofing), with Shoonya + Dhan as fallbacks.
- **Root Cause:** The default priorities in `router.py` placed `shoonya` first, and the configuration setting `FETCHER_PRIORITY` in `settings.py` was a list that was ignored by the router's per-symbol override logic.
- **Fix:**
  1. Updated `FETCHER_PRIORITY` in `config/settings.py` to be a dictionary, prioritizing `sensibull` first for all supported index symbols (`NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `SENSEX`) while keeping `shoonya`/`dhan_commodity` as primary for commodities.
  2. Modified default priority lists in `src/fetchers/router.py` to match, ensuring robust fallback configurations. Verified clean compilation of both files.

### F38: TFSS Trade Blocked Rules Toggle & Handoff Alignment (P2-MEDIUM)
- **Architecture Update:** Disabled all TFSS (Trend Following Short Strangle) trade blocking rules (`Insufficient History`, `Most Recent Neutral`, `Below Min Match`, `Persistence Not Confirmed`, `Unsupported Intent`, `Max Tranches`, `Delta Cap`) via a centralized configuration toggle `ENABLE_TFSS_TRADE_BLOCKED_RULES = False` in `config/trend_following_short_strangle.py`.
- **Root Cause:** The Core decision engine (`decision_pipeline.py`) already handles comprehensive entry and exit rules prior to the TFSS handoff. Enforcing strict secondary historical persistence rules (e.g. requiring `>= 3` agreeing directions across the last 5 scans or `< 0.60` combined delta) rejected valid signals from the core engine.
- **Fix:**
  1. Added `ENABLE_TFSS_TRADE_BLOCKED_RULES = False` switch in `config/trend_following_short_strangle.py`.
  2. Updated `compute_persisted_trend` and `resolve_tfss_execution_side` in `src/engine/trend_following_short_strangle.py` to check `ENABLE_TFSS_TRADE_BLOCKED_RULES` before returning invalid persistence (`is_valid=False`) or rejecting intent/execution side. When disabled, the engine falls back gracefully to current scan verdict direction (`tfss_intent.bias`) without blocking.
  3. Updated `compute_combined_book` in `src/engine/risk_engine.py` to check `ENABLE_TFSS_TRADE_BLOCKED_RULES` before marking `within_caps = False` for open tranche count (`>= 3`) and combined delta (`>= 0.60`).

### F39: Setting Cockpit Integration for TFSS Trade Blocked Rules Toggle (P2-MEDIUM)
- **Architecture Update:** Integrated `enable_tfss_trade_blocked_rules` toggle into the Setting Cockpit runtime configuration framework.
- **Fix:**
  1. Added `enable_tfss_trade_blocked_rules: False` as a dynamic setting default in `config/runtime_config.py`.
  2. Modified `compute_persisted_trend()`, `resolve_tfss_execution_side()` (`trend_following_short_strangle.py`), and `compute_combined_book()` (`risk_engine.py`) to dynamically query the live setting via `load_runtime_config().get("enable_tfss_trade_blocked_rules")`.
  3. Integrated the checkbox element `sett-enable-tfss-blocked-rules` under the "AI Configuration & Strategy Controls" card in `src/dashboard/settings.html`.
  4. Wired the element into JavaScript settings loading, `checkDirty()` change monitoring, and backend storage serialization.

### F40: paper_trades Schema Migrations for reason and exit_reason (P1-HIGH)
- **Symptom:** Strategy execution failed with `sqlite3.OperationalError: no such column: exit_reason` inside `close_paper_trade` method of `src/models/schema.py`.
- **Root Cause:** The `paper_trades` table schema definition in code had `reason` and `exit_reason` columns, but existing databases generated under older schema revisions did not contain these columns, resulting in query execution failures on trade exit updates.
- **Fix:**
  1. Added SQLite schema migrations `M070_add_paper_reason` (`ALTER TABLE paper_trades ADD COLUMN reason TEXT`) and `M071_add_paper_exit_reason` (`ALTER TABLE paper_trades ADD COLUMN exit_reason TEXT`) to the `_MIGRATIONS` execution pipeline list in `src/models/schema.py`.
  2. Executed database migration sequence to apply missing columns.

### F41: MCX Option Chain Fetch Timeout Insufficient (P1-HIGH)
- **Symptom:** Pipeline logs "No data for NATURALGAS — skipping" at the 45-second mark, but the fetch succeeds 60-75 seconds later (visible in subsequent logs showing successful DUALFETCH).
- **Root Cause:** The `DEFAULT_PROVIDER_TIMEOUTS["option_chain"]` was set to 45 seconds, but MCX commodity fetches require more time due to:
  1. Shoonya OAuth login flow (25-35s on cold start)
  2. Dhan resolver CSV master download
  3. Parallel fetch coordination between Shoonya + Dhan_commodity
- **Fix:** Increased `option_chain` timeout from 45.0s to 90.0s in `src/engine/provider_parallel.py` to accommodate MCX commodity fetch latency while maintaining reasonable timeout bounds for NSE indices.

### F42: Autopsy Writer LLM API Signature Mismatch (P1-HIGH)
- **Symptom:** Nightly trade autopsy failed with `_call_llm_api() got an unexpected keyword argument 'model_override'` and `cannot access local variable 'e' where it is not associated with a value`.
- **Root Cause:**
  1. `_call_llm_api()` in `llm_enrichment.py` accepts `(symbol, prompt, response_schema, deadline, purpose)` but `autopsy_writer.py` was calling it with `model_override` parameter which doesn't exist.
  2. The exception handler in `_call_llm_autopsy()` referenced variable `e` in the return statement outside the except block scope.
- **Fix:**
  1. Updated `_call_llm_autopsy()` to pass `symbol` as first argument and removed `model_override`.
  2. Added explicit return inside the except block to prevent scope violation.
  3. Updated `_call_llm_autopsy_batch()` to use correct signature with `purpose="eod_review"` for batch analysis routing.

### F43: Scan Sentinel Integration into Pipeline (P1-HIGH)
- **Architecture Update:** Integrated the Scan Sentinel AI diagnostics system directly into the pipeline for real-time, non-blocking scan validation.
- **Implementation:**
  1. Wrapped `_process_prefetched_symbol()` with `ScanRunRecorder` context manager to capture all logs and scan metadata per symbol.
  2. Added `recorder.finalize()` at the end of each symbol scan to emit structured `ScanRunReport` with option chain health, LLM verdict details, trade decision status, and captured warnings/errors.
  3. Submitted `run_sentinel()` asynchronously via `pipeline_io_executor` to run deterministic rule checks (R1-R6) and AI diagnostics in parallel without blocking trading execution.
  4. Sentinel writes diagnostics to `sentinel_incidents` DB table and `data/sentinel/latest.jsonl` for ops monitoring.
- **Behavior:** Rule engine flags anomalies (premium == underlying, dead option chain, slow scans, type mismatches, etc.) → LLM analyzes against `KNOWLEDGE_BASE.md` → recommends self-healing action (SKIP_TRADE/PAUSE_SYMBOL/CLEAR_CACHE/FORCE_RESCAN/ALERT_ONLY) → user is alerted via logs, trades continue unblocked unless `SENTINEL_HEAL_ENABLED=true` is set.

### F44: Sub-Random ML Model Deployed as "v1" (P1 — training gate gap) (P1-HIGH)
- **Symptom:** `data/models/ml_features.json` reported `auc=0.449` (worse than coin-flip 0.500), `training_samples=85`, trained 2026-06-29. Runtime load guard (ml_predictor.py `_load_model`) correctly forced `_force_shadow=True` (AUC<0.55), so live promotion was blocked — but the bad model file still persisted with a misleading "deployed" status.
- **Root Cause:** The deploy gate at `ml_predictor.py:train()` only enforced the AUC improvement check when a baseline already existed: `if self.model is not None and new_auc < effective_baseline + AUC_IMPROVEMENT_THRESHOLD`. The very FIRST model (`self.model is None`) skipped this check entirely, so there was **no absolute minimum AUC floor** for initial deployment. A sub-random first model (n=85, 25 features — pure noise/overfit) was saved and reported as "deployed v1". Class imbalance was already handled (scale_pos_weight), and feature-leakage was already fixed (opened_at used, not datetime.now()), so the dominant cause is insufficient sample size producing no learnable signal.
- **Fix (v3.1):**
  1. Added `MIN_DEPLOY_AUC = 0.55` constant in `ml_predictor.py`.
  2. Enforced it UNCONDITIONALLY at the top of `train()`, before the relative improvement gate: if `new_auc < MIN_DEPLOY_AUC`, training returns False and **does not write** the model to disk. This closes the first-model gap so a worse-than-random model can never be persisted.
  3. Discarded the existing bad model files (`data/models/ml_model.json`, `data/models/ml_features.json`). With no model on disk, `get_predictor()` loads `model=None` and `predict()` safely returns `None` until a valid (AUC>=0.55) model is trained.
- **Retrain guidance:** With only ~85 trades, retraining on the same data will reproduce noise. The new gate will reject sub-random results, leaving no model rather than a harmful one. Wait for a larger, more balanced trade history (ideally several hundred closed trades) before attempting a production model.

### F45: Data-Driven `MIN_PAPER_CONFIDENCE` (replaces hardcoded magic number)
- **Symptom:** `MIN_PAPER_CONFIDENCE = 65` in `src/engine/paper_plan.py` was a hardcoded constant with no empirical basis (introduced in commit `6762a84a`, never derived). Analysis of the 40 closed paper trades showed confidence is **non-predictive** of win rate for the OI-verdict TFSS trades (WR 17–47% at every level 70–100); the only "good" band (conf=100) was a confounded `NG Parity` strategy that still lost money on average.
- **Root Cause:** Threshold was an arbitrary tuned number, not derived from trade outcomes.
- **Fix (v3.1):**
  1. New module `src/engine/confidence_threshold.py` derives the floor from historical closed, scored trades (`paper_trades` where `confidence_score IS NOT NULL` and `pnl_rupees IS NOT NULL`).
  2. **Optional** — gated by runtime flag `derive_min_confidence` (default `False` in `config/runtime_config.py`). Off by default = behaviour unchanged.
  3. **Guarded** — only derives once `>= 50` (`MIN_TRADES_FOR_DERIVATION`) trades exist; below that the hardcoded `DEFAULT_MIN_PAPER_CONFIDENCE = 65` is used (prevents overfitting to a tiny/noisy sample).
  4. **Method** — over candidate thresholds `(55..90)`, pick the one maximising the `conf >= T` slice win rate (the point where the edge actually begins), subject to: WR `>= 0.55` (`WIN_RATE_FLOOR`), avg PnL `> 0`, and slice size `>= max(10, 20% of all scored trades)` (anti-overfit guard). Tie-break by larger slice, then higher threshold. No qualifying candidate → fallback to default.
  5. Derived value + sample count persisted to `data/models/derived_confidence.json` and cached in-process (count TTL 300s); `refresh_derived_confidence()` force-recomputes on trade close.
  6. `build_paper_trade_plan` now calls `get_effective_min_confidence()` instead of the bare constant.
  7. **Auto-promotion:** `get_effective_min_confidence()` (runs on every plan build) auto-flips `derive_min_confidence` to `True` in `runtime_config.json` the first time `>= 50` scored trades exist — no manual toggle needed. Until then the hardcoded `65` default applies. Caveat: once auto-enabled it will re-enable itself on restart if still `False` in the file and `>=50` trades exist, so an admin wanting it off must also drop below 50 trades or change the gating logic.
- **Validation:** Unit-simulated with synthetic history (low-conf lose / high-conf win) → correctly isolates the separator at the true edge (T=70), not the naive "lowest T meeting the bar" (which wrongly returned 55 and still admitted losing low-conf trades). Real 40-trade history returns `None` (no band reaches 55% WR) → stays on default, as expected.

### F46: Scheduler Startup Gap-Fill + Historical Option-Chain Reality
- **Symptom asked:** On `start_scheduler.bat` (default, no `--now`), does the bot check previous missed intervals and gap-fill before the current fetch? And how does it fetch option-chain data for a past timestamp — do Shoonya/Sensibull provide historical OI?
- **Root Cause / Findings:**
  1. **Default startup did NOT gap-fill.** `start_scheduler(immediate=False)` just set `last_scanned_interval=current_interval_idx` and logged "Bypassing immediate startup scan…". All intervals missed while the bot was OFF were silently dropped. Only `--now` (immediate=True) detected+backfilled (and even then capped at `MAX_CATCHUP_INTERVALS=3`).
  2. **Pre-existing detection flaw (both paths):** the missed-interval query used `fetched_at >= interval_start` with NO upper bound, so a *later* interval's data masked an *earlier* missing one (`>=` matched any subsequent row). Interior gaps were never detected; only trailing ones (after the last existing scan) were.
  3. **No historical option-chain source exists.** All fetchers (Shoonya/Sensibull/Dhan) pull the **live** chain. Catch-up re-runs `_guarded_run` → `fetch_option_chain` (live) and stamps `scan_summaries.fetched_at = datetime.now(UTC)` (job_runner.py:537). The missed `interval_idx` is used only for bookkeeping/logging — it is NOT passed as a historical timestamp to any fetcher. So gap-fill reconciles the **interval grid / persistence bookkeeping** (TFSS `compute_persisted_trend` needs ≥3 of last 5, trend/regime windows), NOT the past market state. There is no wired historical-OI API.
- **Fix (v3.1):**
  1. Extracted shared `_startup_fill_missed(class_key, market_open_time, interval_min, target_interval, immediate_flag)` in `job_runner.py`. BOTH startup branches now call it — default (`immediate=False`) gap-fills missed intervals too, not just `--now`.
  2. Fixed the detection query to a proper per-interval window: `fetched_at >= interval_start AND fetched_at < interval_end` (also applied to runtime `_find_missed_intervals`). Interior gaps are now detected. Cap `MAX_CATCHUP_INTERVALS=3` retained to avoid a startup scan storm after long downtime.
- **Validation:** In-memory DB test — with data at intervals 0,1,2,4 and missing 3,5 (target 5), old query detected only 1 (5); new windowed query detects both 3 and 5. Cap test: 6 missing → 3 catch-up calls.
- **Caveat for operators:** A backfilled interval contains the *current* live snapshot mislabeled with a past `fetched_at` window. Persistence/trend math stays contiguous, but the OI/price values are not what the market showed at that past time. True historical reconstruction would require wiring a historical-OI source (e.g. Kite historical / Sensibull historical OI) — not currently implemented.

### F45: TradingEconomics Timeout During MCX News Scrape (P2 — external source) (P2-MEDIUM)
- **Symptom:** `TE scraping failed: Page.goto: Timeout 15000ms exceeded` during NATURALGAS news fetch.
- **Root Cause:** TradingEconomics site blocking automated requests, Cloudflare anti-bot protection, or slow page load.
- **Self-Heal:** `ALERT_ONLY` - natural fallback to TradingView commodity news (primary) and X/NGInews (secondary).
- **Impact:** Minimal — TradingView remains primary source, TE is tertiary fallback for supplementary sentiment.

### F46: X/Twitter SSL Handshake Failure (P2 — external source) (P2-MEDIUM)
- **Symptom:** `X scraping failed: HTTPSConnectionPool(host='syndication.twitter.com', port=443): Max retries exceeded with url: /srv/timeline-profile/screen-name/NGInews (Caused by SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]')))`
- **Root Cause:** X syndication endpoint rejecting SSL connections, API changes, or temporary service degradation.
- **Self-Heal:** `ALERT_ONLY` - skip X scrape, use TradingView commodity news only.
- **Impact:** Minimal — X/NGInews is secondary supplementary source for NATURALGAS sentiment.

### F47: DTE=0 Display Bug + Mandatory Synthesized AI Thesis / Timeframe Status
- **Symptom asked:** Alert on 15/07/2026 showed `Expiry: 2026-07-16 (0 DTE)` (should be 1 DTE). Also: AI Thesis must always be present (raw data + engine verdict + news + open positions) and the Timeframe strategy status must be included.
- **Root Cause (DTE):** `generate_intelligence()` (intelligence.py:1044) computed `days_to_expiry` and returned it in the result dict, but NEVER wrote it back into `scan_context`. The digest header (`pipeline.py` header `"dte": scan_context.get("days_to_expiry", 0)` → `digest.py:564`) therefore always fell back to the default `0`. Only `llm_enrichment.py:2312` set it on a separate path. Also used UTC `now()`, which is wrong for the IST market-day boundary.
- **Fix (DTE):** `generate_intelligence()` now writes `ctx["days_to_expiry"]` back into `scan_context` and computes it in **IST** (`timezone(timedelta(hours=5, minutes=30))`). `pipeline.py` calls `generate_intelligence_structured` (line ~489) BEFORE building the header (line ~353) and `build_digest` (line ~590), so propagation is in order. Shared `_format_expiry_and_dte` (digest.py:76) and the inline calc in `build_llm_consolidated_digest` also switched to IST.
- **Mandatory AI Thesis:** Added `synthesize_market_insight(ctx, verdict_label, bias, confidence, news_data, open_trade)` (digest.py) — builds an always-present insight block from PCR/CE-PE OI Δ, Max Pain distance, OI buildup label, S/R, engine verdict+bias+confidence, top news headlines, and open-position line (side/strike/type/entry/pnl/age). `_build_structured_payload` (pipeline.py) now calls it; `ai_thesis` = (LLM thesis if present) + `🧠 Market Insight\n` + synthesized block. Renderer (`build_tfss_timeframe_digest`) now **preserves newlines** (was collapsing via `textwrap.wrap` of the whole string).
- **Timeframe status:** `build_tfss_timeframe_digest` Timeframe header renamed to `*Timeframe Strategy*`; when no signal it now prints `Status: No active signal (3H breakout pending)` instead of bare `No active signal`.
- **Wiring:** `_build_structured_payload` gained `news_data`/`open_trade` params; both call sites (main flow + async `_async_llm_enrich_and_edit`) pass them (async passes `open_trade=None`). It also now emits `options_insight` (list of compact raw-data lines from `format_options_insight`).
- **Template restructure (build_tfss_timeframe_digest):** Replaced the old flat layout with the user-approved structured template: `HEADER` (symbol·time / 🗓 Expiry DD MMM · N DTE | Spot | Regime) → `SIGNAL` (bias · verdict, confidence bar, trade status) → `TIMEFRAME STRATEGY` (status; "No active signal (3H breakout pending)" when idle) → `OPTIONS INSIGHT` (PCR·OIΔ·MaxPain·OI Buildup·Range from `format_options_insight`) → `WHY BLOCKED` (consolidated dedup blockers; time-guard blockers get a "↳ Re-check next interval after the window" hint) → `THESIS (AI)` (`ai_thesis` = LLM thesis if present + synthesized verdict/news/open-position narrative via `synthesize_market_insight`, now narrative-only to avoid duplicating OPTIONS INSIGHT). `synthesize_market_insight` was trimmed to Verdict + News + Open (raw data moved to `format_options_insight`).
- **Validation:** Functional test — SENSEX scan (expiry 2026-07-16, IST today 15/07) renders `🗓 Expiry 16 Jul · 1 DTE | Spot ₹77,148`, `🚫 *SIGNAL*: BEARISH · OI Bias Bearish`, `🚫 *TIMEFRAME STRATEGY*: BLOCK / Status: No active signal (3H breakout pending)`, full `🧠 *OPTIONS INSIGHT*`, `🚫 *WHY BLOCKED*` (time-guard reason + re-check hint), and `💡 *THESIS (AI)*` (verdict + news + open position).

### F48: Scheduler Must Not Track/Scan Off-Market Intervals
- **Symptom:** Bot started after close (17:31 IST) logged `[scheduler] NSE_INDEX: 3 missed interval(s) at startup: [6, 7, 8]` then ran 3 catch-up scans (15:15/16:15/17:15) each hitting "Market is closed. Skipping scan." Intervals 7 (16:15) and 8 (17:15) are outside NSE hours (09:15–15:30) and should never be tracked or backfilled.
- **Root Cause:** `_startup_fill_missed` iterated `range(target_interval + 1)` (idx 0..8) and `_find_missed_intervals` iterated `range(last_scanned+1, current_interval_idx)` with NO market-hours check — they considered every grid slot, including post-close ones. The runtime loop also attempted a scan per off-market tick after close.
- **Fix (job_runner.py):**
  1. Added `_interval_in_market(class_key, interval_start_ist)` and `_market_currently_open(class_key)` helpers (use `is_market_open` from `config.symbol_classes`).
  2. `_startup_fill_missed`: (a) skips any interval whose start falls outside market hours; (b) short-circuits entirely with a single "market closed — skipping startup gap-fill" log when the market is currently closed (no per-interval DB queries / catch-up runs). `last_scanned_interval` is still set by the caller afterwards, so the next in-market session resumes cleanly.
  3. `_find_missed_intervals` (runtime catch-up) skips off-market intervals.
  4. Runtime scan loop: added a **post-close guard** (mirrors the pre-open guard) — once `now_time > close_t`, the class is skipped for the rest of the tick with a one-time "Market closed (after HH:MM). Scheduler will resume next session." log. Added `has_logged_closed_post_close` flag (cleared on daily reset alongside `has_logged_closed_pre_open`).
- **Validation:** Helper unit check — `_interval_in_market('NSE_INDEX', 09:15)=True`, `15:15=True`, `16:15=False`, `17:15=False`; `_market_currently_open('NSE_INDEX')` at 17:31 = `False`. Net effect: after-close startup no longer enumerates or backfills off-market intervals, and the running loop stops attempting off-market scans — saving scan duration.

### F49: Dead-Trade Exit Duration → 48h (all strategies) + Trade-History Cleanup
- **Dead Trade threshold:** In `src/engine/paper_trading.py` (CORE dead-trade check, line ~712), the threshold was `24.0 if option_type == "FUT" else 3.0`. Changed to a single `dead_trade_hours = 48.0` applied to ALL strategies/option types (CORE + TIMEFRAME, FUT + options). Condition unchanged: `hours_open >= dead_trade_hours and max_fav < 0.5` → close as `DEAD_TRADE`. (Only one definition exists; the legacy `run_timeframe_strategy` copy was already consolidated here.)
- **Trade-history cleanup:** Removed stale record `paper_trades.id=257` — SENSEX, opened 2026-07-14 13:30 IST, closed 2026-07-15 09:30 IST, status "Dead Trade" (the "14 Jul 01:30 pm → 15 Jul 09:30 am · 19h 59m · SENSEX" row). Also deleted its referencing `decision_audit.id=969` (`trade_id=257`); `trade_autopsies` had no rows for it. After: `paper_trades` no longer contains id 257.

### F50: Add Entry Spot (Underlying Price at Entry) Columns to Dashboard
- **Symptom asked:** User wants to see the underlying spot price when a trade was entered directly in the Open positions and Trade History tables.
- **Fix:** Added `Entry Spot` column header and populated it with `t.entry_underlying` in both the Open positions (`open-body`) and Trade History (`trades-body`) tables in `src/dashboard/paper.html`. Adjusted table row `colspan` values from `12`/`16` to `13`/`17` to accommodate the new column.

### F51: Scan Sentinel Report Mode Toggle (anomalies vs full)
- **Symptom asked:** Scan Sentinel was not generating a report after every scan — it only persisted rows to `sentinel_incidents` when a rule flag fired, and the `latest.jsonl` rolling file was dead code (`ScanRunRecorder` removed from the pipeline). Clean scans produced zero output.
- **Root Cause:** `run_sentinel()` had an early `if not flags: return None` before any logging/persistence, so anomaly-free scans generated nothing. The only persisted artifact was `sentinel_incidents` (anomalies only). The Setting Cockpit had no control to opt into per-scan reporting.
- **Fix:**
  1. Added `sentinel_report_mode: "anomalies"` (allowed values `"anomalies"` | `"full"`) as a dynamic runtime setting in `config/runtime_config.py`.
  2. Added `get_sentinel_report_mode()` + `SENTINEL_REPORT_MODES` in `src/engine/scan_sentinel.py`.
  3. Modified `run_sentinel()` so that when mode == `"full"`: a concise `"scan OK (no anomalies) — full-report logged"` INFO line is emitted, a row is persisted to the new `sentinel_scan_runs` DB table (columns: ts, symbol, source, underlying_price, expiry, total_strikes, zero_ltp_strikes, zero_oi_strikes, llm_action, llm_instrument, flags JSON, flag_count, report_mode), and the rolling `latest.jsonl` is refreshed via `emit_scan_run_report(report_from_dict(...))`. When rules DO fire, behavior is unchanged (LLM diagnostic + `sentinel_incidents` persist). When mode == `"anomalies"` (default), behavior is the original early-return.
  4. Added `report_from_dict()` (dict → `ScanRunReport`) and `persist_scan_run()` helpers in `scan_sentinel.py`.
  5. Upgraded the silent `log.debug` swallow at `pipeline.py:750` to `log.warning(..., exc_info=True)` so a failed sentinel submission is now visible.
  6. Wired a new `Scan Sentinel Report Mode` `<select id="sett-sentinel-report-mode">` (Only Anomalies / Full Report) into `src/dashboard/settings.html` — load (`checkDirty` + populate), dirty tracking, and save (`/api/settings` POST). The backend `post_settings` already dumps the whole dict, so the new key persists automatically.
- **Default:** `"anomalies"` (preserves prior behavior — no per-scan overhead unless explicitly enabled).

### F52: Truncated Lookback Periods Causing Stunted ATR Calculations
- **Symptom:** ATR calculated on the 3-hour chart returned inaccurate/stunted values (e.g. ₹1.97) compared to Zerodha/standard charts (which showed ₹4.53).
- **Root Cause:** Standard indicators like Wilder's smoothed ATR(14) require a significant warm-up period of historical bars (typically 100+) because calculations carry previous values forward recursively. The chart fetchers used lookback periods that were too short:
  1. **Shoonya**: Requested only 5 days of history (resulting in only 20-25 aggregated 3H candles, leaving no room for warm-up).
  2. **Local DB**: Requested only 60 hours for 3H candles (20 bars).
  3. **TradingView**: Requested only 30 bars.
- **Fix:** Increased historical lookback periods in `src/fetchers/chart_fetcher.py`:
  1. **Shoonya**: Changed lookback from 5 days to 30 days of history (~240 3H bars).
  2. **Local DB**: Changed lookback from 20/60 hours to 100 hours for 1H, and 300 hours for 3H (~100 bars).

### F53: Ops Agent Activity Log Frozen (ops_agent process not running)
- **Symptom:** The Ops Monitor "Ops Agent Activity Log" was stuck on old entries (latest 2026-07-10) and never refreshed — even though the dashboard was live.
- **Root Cause:** `ops_agent.py` is a separate standalone process that writes to `data/ops_agent.db` (`incidents` table); the dashboard reads it via `/api/ops-incidents`. Nothing was launching `ops_agent.py`, so with the bot (`main.py`) also not running, the process was simply absent and the log never advanced.
- **Fix:** Added a supervised launcher in `dashboard_server.py` (`_supervise_ops_agent` + `_ops_agent_already_running`) started as a daemon thread in `__main__`. On dashboard start it launches `python ops_agent.py --observe-only` (CREATE_NO_WINDOW on Windows) and restarts it if it exits; it skips launch if an instance is already running. `--observe-only` is used so the dashboard never triggers destructive protective actions (emergency flat / re-auth / restart) merely because the bot isn't running.
- **Observe-only now notifies:** Previously `ROLLOUT_LEVEL=0` gated OFF all escalation, so `observe-only` logged nothing. Patched `ops_agent.py` P01/P02 blocks to **always escalate/notify** (log the incident) while only **executing** the T1/T2 action when `ROLLOUT_LEVEL >= 1/2`. In observe-only the bot-dead / parity / scan / unknown incidents are now written to the Activity Log, but no restart/flat/pause is performed. Matches spec T0 "observe & notify".
- **Diagnostic caveat:** During investigation a NORMAL-mode `ops_agent.py` was briefly run; it detected the bot as dead (heartbeat stale) with `open_positions=3` (from `/health`) and triggered `emergency_flat.py` (incident id 5, "Emergency flat executed — 3 positions closed"). `emergency_flat.py` only iterates `live_trades WHERE status='OPEN'` (0 rows), so no DB positions were closed; it may have cancelled pending broker orders if a session was live. The supervised launcher now uses `--observe-only` to avoid this. Residual unacked incident id 5 remains intentionally (documents the event).

### F54: Scan Sentinel "Root cause" Truncated in Dashboard
- **Symptom:** The Scan Sentinel card on the Ops Monitor showed only the first ~60 chars of `root_cause` (e.g. `The scan likely encountered a network or LLM provider timeout, as evid…`).
- **Root Cause:** Front-end truncation in `src/dashboard/ops.html` `loadSentinelIncidents()`: `Root: ${rootCause.length > 60 ? rootCause.slice(0,60)+"…" : rootCause}` (and `summary` capped at 80). The DB column is `TEXT` (full), so data was intact — only the render clipped it.
- **Fix:** Removed the 60-char (and 80-char summary) cap; both `summary` and `root_cause` now render in full with `white-space:pre-wrap; word-break:break-word`, and added an `escapeHtml()` helper so LLM-authored text can't break layout/XSS.
  3. **TradingView**: Changed `n_bars` from 30 to 100 bars.

### F55: Ops Agent Mode Selectable from Settings Cockpit
- **Request:** Let the user choose the supervised Ops Agent run mode from the dashboard UI (not just a hardcoded default).
- **Config:** Added `ops_agent_mode` (values `"observe"` | `"normal"`, default `"observe"`) to `config/runtime_config.py` defaults and persisted via the existing `POST /api/settings` (which dumps the whole dict).
- **Supervisor (`dashboard_server.py` `_supervise_ops_agent`):** now reads `ops_agent_mode` every loop. If the configured mode differs from the running instance it terminates and relaunches with/without `--observe-only`. The supervisor **owns** ops_agent — any external `ops_agent.py` process is terminated (`_terminate_external_ops_agent`, via wmic/pkill) so the UI-configured mode is authoritative. Mode changes apply within ~15–30s (no dashboard restart strictly required for the *flag*, but the running dashboard must already have the supervisor code; a restart engages it).
- **UI (`src/dashboard/settings.html`):** added an `Ops Agent Mode` `<select id="sett-ops-agent-mode">` (Observe-only (safe) / Full protective (normal)) in the Settings Cockpit, wired into `checkDirty`, the settings loader (populate), and the save payload (`ops_agent_mode`).
- **Behaviour:** `observe` = safe notify-only (log incidents, never act). `normal` = full protective (restart bot / re-auth broker / emergency-flat when unhealthy — may close positions / cancel orders).

### F56: Greeks Coverage Validation Log (all symbols)
- **Request:** The bot should validate and display, in the log, how many strikes have Greeks data available — for every symbol, not just when greeks get computed.
- **Fix:** In `src/fetchers/router.py` `_finalise_result()`, added `_strike_has_greeks(s)` (non-zero `delta` **or** `iv`) and a per-fetch coverage report emitted for **all** successful fetches:
  `[router] <SYMBOL> | greeks coverage: <with>/<total> strikes have greeks (computed=<n>, from_source=<m>)`.
  `from_source` = strikes already carrying greeks from the feed; `computed` = strikes enriched locally by `enrich_missing_greeks`. The original `enriched %d/%d ... computed greeks` line is retained. A `WARNING` is added when underlying+expiry are present but coverage is 0 (flags a broken LTP/IV feed). Coverage is measured over the post-ATM-filter strike set, matching the existing `enriched N/total` denominator.
- **Note:** emission requires a successful fetch (which always carries `underlying_price`); symbols that fail fetch are skipped earlier, consistent with prior behaviour.

### F57: Update MCX Structural Percentage-Based Fallback Buffers
- **Symptom asked:** User wants to update the MCX percentage-based fallback buffer (used when ATR indicators are missing) from the legacy 1.5% SL / 3.0% Target to a tighter 2.5% SL / 3.5% Target.
- **Fix:** Modified the `is_mcx` branch calculations in both `calculate_buy_sl_target` and `calculate_sell_sl_target` inside `src/engine/trade_plan.py` to use a multiplier of `1.025` / `0.975` for 2.5% SL, and `1.035` / `0.965` for 3.5% Target. Updated warning log entries accordingly.

### F58: Restructure Telegram Message Alert Layout and AI Exit/Thesis Prompts
- **Request:**
  1. Top line: Include trade status (`✅ Entered` / `✗ Not entered`) next to symbol/time (e.g. `NATURALGAS · 22:04 IST ✅ Entered`).
  2. AI Exit Verdict: Restore display of exit advice/verdict on message.
  3. Signal Consolidation: Consolidate trade entry, exit, or blocked reasons inside the Signal section (remove separate `*WHY BLOCKED*` section).
  4. Timeframe section: Move to the very end of the message.
  5. AI Thesis: Prompt model to synthesize the overall picture instead of repeating scan verdict.
- **Fix:**
  1. **Top Line / Trade Status:** Added `trade_entered` field to the `header` payload in `_build_structured_payload()`. Displayed `✅ Entered` / `✗ Not entered` directly on the header line in `build_tfss_timeframe_digest()`.
  2. **AI Exit Advice:** Wired synchronous `get_exit_advice()` to run in `pipeline.py` when an open position exists. Added `exit_advice` key to the payload so it renders in the digest alert.
  3. **Signal Consolidation:** Consolidated entry reasoning (`tfss_reason`), block reasons (`all_blockers`), and exit advice (`exit_advice`) directly into the `*SIGNAL*` section. Removed the separate `*WHY BLOCKED*` section.
  4. **Timeframe Section:** Relocated the `TIMEFRAME STRATEGY` section to the very end of the digest alert structure in `build_tfss_timeframe_digest()`.
  5. **AI Thesis:** Updated the description instruction for the `thesis` Field in `LLMTradeVerdict` to direct the model to formulate a synthesis integrating the broader context (levels, macro, PCR, trend) rather than just restating the current scan verdict.

### F59: Broker Console & Paper Trades Database OperationalError
- **Symptom:** Opening the Paper Trades page or Broker Console page shows no data and throws `sqlite3.OperationalError: no such column: exit_reason` inside `dashboard_server.py`.
- **Root Cause:** A previous migration added `exit_reason` to `paper_trades` but omitted it from `live_trades` in both `DDL` (creation) and `_MIGRATIONS`. When `dashboard_server.py` queries `get_paper_trades()` using a `UNION ALL` across both tables, SQLite throws a column missing error for `live_trades`.
- **Fix:** Added `exit_reason TEXT` to `live_trades` DDL and added a schema migration `M072_add_live_exit_reason` in `src/models/schema.py`. Executed `init_db()` via a python shell command to apply the migration cleanly.

### F60: TFSS Dead Code Wiring (reversal, tranche, delta-stop)
- **Symptom:** `evaluate_reversal()`, `check_tested_side()`, `compute_combined_book()`, `select_candidate()` in `trend_following_short_strangle.py` / `risk_engine.py` / `trade_plan.py` were fully defined but had zero callers in the live execution path. `_tranche_index` was read but never set (always 0). No TFSS-specific exit triggers existed — all trades exited through the same generic path.
- **Root Cause:** TFSS strategy functions were implemented as standalone modules but never wired into `execute_paper_trade()` or `monitor_paper_trades()`.
- **Fix:**
  1. **Tranche index**: `execute_paper_trade()` now counts open TFSS tranches for the symbol via DB query and sets `ctx["_tranche_index"]` before trade open. Signal key includes tranche_index for proper dedup.
  2. **Reversal wiring**: For TFSS trades (`setup_type == "TFSS"`), `execute_paper_trade()` calls `evaluate_reversal()` instead of `_is_reversal_against_open_trade()`. Handles BLOCK/OPEN_OR_ADD/NO_REVERSAL_ACTION returns. Falls back to generic reversal on TFSS evaluation failure.
  3. **Delta-stop exit**: `monitor_paper_trades()` now checks TFSS positions against `check_tested_side()` (hard_stop_delta=0.35) after dead-trade check. Sets `dead_trade_close=True` when delta exceeds threshold, using `CLOSED_DELTA_STOP` status.
  4. **Files modified**: `src/engine/paper_trading.py` (imports + execute_paper_trade + monitor_paper_trades), config imports for `TRANCHE_SEQUENCE` and `EXIT_PRIORITY_MAP`.

### F61: EIA Scraper Entry Point Mismatch
- **Symptom:** Economic calendar updates logged `src.engine.eia_analyzer | ERROR | EIA scraper has no callable entry point`.
- **Root Cause:** `eia_analyzer.py` tried importing `scrape_eia_report.py` and checked for either `scrape_eia_report` or `main` entry functions, but the scraper module actually defined the scrape action in a function named `scrape_eia`.
- **Fix:** Added a fallback check for `scrape_eia` function in the entry point resolver of `src/engine/eia_analyzer.py` (`if hasattr(scrape_module, 'scrape_eia'): data = scrape_module.scrape_eia()`).

### F62: EIA Report Job Rescheduling and Consensus Pre-fetch Sequence
- **Architecture Update:** Rescheduled the weekly Thursday EIA Report Job from 8:00 PM IST to 8:02 PM IST and chained consensus data pre-fetching.
- **Fix:**
  1. Updated the schedule trigger condition in `src/scheduler/job_runner.py` to target `20:02` (8:02 PM IST).
  2. Modified the worker thread wrapper `_run_eia_analyzer()` to explicitly invoke `fetch_and_store_eia_consensus()` sequentially before importing and running `analyze_eia_report()`. This ensures the database always has up-to-date consensus estimates populated before the analyzer queries it.

### F63: Paper Trades Dashboard UI Adjustments
- **Symptom:** The user wanted to clean up and restructure the Paper Trades history UI (redundant Verdict column, long Exit Reasons, split Entry/Exit Reason layout).
- **Fix:**
  1. Removed the redundant `Verdict` header and data column from the Trade History table in `src/dashboard/paper.html`. Adjusted table cell colspans accordingly from `19` to `18`.
  2. Implemented a `shortReason` JavaScript formatting helper in `paper.html` to limit displayed exit reasons to a maximum of 2 words (e.g. `"Dead trade..."`).
  3. Redesigned the expanded trade details container to render both `Entry Reason` and `Exit Reason` panels side-by-side in the same grid row instead of taking up separate full-width rows.

### F64: Dynamic Monitoring Staleness Thresholds (P2-MEDIUM)
- **Symptom:** False P11 (last scan stale) and P05 (parity feed stale) alert notifications triggered repeatedly when the user modified scan frequencies at runtime (e.g. configuring 1-hour scan frequency).
- **Root Cause:** The `ops_agent.py` monitor evaluated staleness against hardcoded default intervals (10 min for NSE index symbols, 30 min for MCX commodities, and 20 min for parity feeds). When the scan frequency was set to 1 hour, the monitor triggered false alarms since the intervals between scans exceeded the default thresholds.
- **Fix:**
  2. Dynamically scaled the staleness thresholds to 2× the configured scan frequency for the respective asset classes (NSE vs MCX), resolving the false alarms.

### F65: Trend Persistence Phrasing Correction and Validation (P3-LOW)
- **Symptom:** Core trade decision log claimed `Trend persistent: Mixed/Unclear Trend | conf=98%` which was semantically contradictory.
- **Root Cause:** In `src/engine/trend_analysis.py`, the `check_trend_persistence()` checker allowed directional trades during a broader `"Mixed/Unclear Trend"` regime if local indicator confidence exceeded the high threshold (`MIN_CONFIDENCE_CORE`). However, the return statement hardcoded the phrasing prefix to `"Trend persistent:"` regardless of whether the trend was actual trending or mixed.
- **Fix:** Restructured return message formatting to output `"Trend allowed: Mixed/Unclear Trend with High Confidence"` instead of `"Trend persistent: Mixed/Unclear Trend"` when the broader trend is mixed but trade confidence permits execution.

### F66: Global Trade Monitoring and Actionable AI Exit (P0-CRITICAL)
- **Symptom:** Open paper trades (e.g. CORE trades) remained open during market crashes in the EIA report window, failing to trigger stop losses or follow high-urgency AI exit advice.
- **Root Cause:** Trade monitoring (`monitor_paper_trades`) was inside `run_paper_trading()`, which was only called by `CORE`, `TIMEFRAME`, and `MOMENTUM` strategies. During the Thursday 7:45 PM–9:30 PM `EVENT` regime, the pipeline only executed `NG_EVENT` strategy, completely bypassing the monitoring logic.
- **Fix:** 
  1. Relocated `monitor_paper_trades()` to run globally at the start of the `pipeline.py` strategy runner loop, executing on every scan cycle across all symbols and regimes.
  2. Implemented mechanical execution of `AI Exit Advice`: if the LLM advises `CLOSE_EARLY` or `EXIT` with `HIGH` urgency, the pipeline calls `close_paper_trade()` immediately, preventing stuck trades.

### F67: Dual-source Chart Fetcher Fusion for SENSEX and MCX (P2-MEDIUM)
- **Symptom:** Index and commodity scans suffered data gaps or missed metrics (like `prev_ohlc` and `atr_14`) when primary data feeds failed or returned partial payloads.
- **Fix:**
  1. Added `SENSEX` to `_DHAN_BUILTUP_SYMBOLS` to register it as a dual-source asset alongside `NATURALGAS` and `CRUDEOIL`.
  2. Rebuilt `_fetch_dhan_builtup_ohlc()` in `src/fetchers/chart_fetcher.py` into a data fusion wrapper. It now concurrently queries the Dhan Builtup API (primary) and Yahoo Finance (secondary), merging the live active bar from Dhan with historical indicators (`prev_ohlc`, `atr_14`) from Yahoo Finance.
  3. Sanitizes all nested dictionaries to clean float values, rejecting incomplete snapshots to ensure clean, reliable downstream data ingestion and DB storage.

### F68: Dual-source Option Chain and Greeks Fusion (P1-HIGH)
- **Symptom:** Option scans suffered data gaps or missed Greeks (like delta, gamma, theta, vega, implied volatility) when primary providers returned incomplete datasets.
- **Fix:**
  1. Updated `fetch_option_chain()` in `src/fetchers/router.py` to implement a generalized dual-source parallel fetch and merge. The router now pulls the top 2 available providers (e.g. `shoonya`, `sensibull`, or `dhan_commodity`) from a symbol's priority list and fetches them concurrently in a thread pool.
  2. Modified `_merge_fetcher_results()` to merge option chain pricing and greeks fields (`ltp`, `oi`, `oi_change`, `volume`, `iv`, `implied_volatility`, `bid`, `ask`, `delta`, `gamma`, `vega`, `theta`, `rho`, `ltp_change_pct`, `oi_change_pct`). Primary values are preferred, and any missing fields or zero values are filled from the secondary source before returning the sanitized snapshot.
