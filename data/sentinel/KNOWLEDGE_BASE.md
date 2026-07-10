# NSEBOT Architecture — Scan Sentinel Knowledge Base

## Pipeline Flow
1. `fetch_option_chain(symbol)` → fetches from Shoonya/Dhan/Paytm.
2. `detect_anomalies()` → OI analysis, PCR, max pain, alerts.
3. `get_llm_verdict()` → AI trade recommendation.
4. `_sanitize_llm_verdict()` → validates/corrects LLM output.
5. Paper/Live trading execution.
6. `build_digest()` → Telegram/Discord alert.

## Known Failure Modes

### F1: Premium == Underlying (CRITICAL)
- **Symptom:** Target premium (target_1 or target_2) within 5% of underlying_price.
- **Root Cause:** BFO weekly options have zero volume. Shoonya returns spot price as LTP for untraded options. Sanitizer uses this fake LTP.
- **Self-Heal:** Flag the verdict as INVALID, skip trade execution (block trade_decision).

### F2: yfinance Constituent Fetch Failures
- **Symptom:** "possibly delisted" errors for .NS tickers.
- **Root Cause:** Yahoo Finance rate limiting or data gaps.
- **Impact:** Index weight calculation falls back to static defaults.
- **Self-Heal:** None needed (graceful fallback exists).

### F3: Option Type Mismatch (CE vs PE)
- **Symptom:** GO_SHORT action with CE instrument, or GO_LONG with PE.
- **Root Cause:** LLM outputs wrong option type (e.g. buying CE on a bearish trigger).
- **Self-Heal:** _sanitize_llm_verdict should auto-correct; if it fails, the sentinel flags it as CRITICAL and blocks the trade.

### F4: Fetcher Source Degradation
- **Symptom:** Multiple symbols falling back to secondary fetchers.
- **Root Cause:** Primary fetcher (Shoonya) auth failure or rate limit.
- **Self-Heal:** Trigger Shoonya re-auth via ops_agent.

### F5: Scan Duration Anomaly
- **Symptom:** Single symbol scan takes >120 seconds.
- **Root Cause:** LLM provider timeout, chart fetcher hanging.
- **Self-Heal:** None (informational alert, but log for visibility).

### F6: Zero OI Option Chain
- **Symptom:** >80% of strikes have oi=0 AND volume=0.
- **Root Cause:** After-hours scan, illiquid contract, or fetcher bug.
- **Impact:** OI-based signals (PCR, max pain) are unreliable.
- **Self-Heal:** Flag scan as LOW_CONFIDENCE, downgrade confidence levels.

### F7: Trend Alignment Dilution
- **Symptom:** Directional trades are blocked by trend alignment score < 70, even when the overall trend is strongly aligned.
- **Root Cause:** The trend alignment score formula counted non-directional scans ("Low Conviction", "Sideways") in the denominator, diluting the score.
- **Fix**: Modified `get_trend_alignment_score` to ignore neutral scans and only calculate the score based on directional ones.

### F8: Settings Cockpit Option Mismatches
- **Symptom:** Settings cockpit unsaved changes banner doesn't disappear after clicking "Save Now".
- **Root Cause:** Discrepancy between HTML options (missing `boost_only` option) and the default backend `runtimeConfig` settings, causing a perpetual dirty check mismatch.
- **Fix**: Added the missing option `boost_only` in settings UI and resolved the JS handler visibility checks.

### F9: Paper Trading Write Lock Contention (Windows)
- **Symptom:** `PermissionError: [Errno 13] Permission denied` during database updates.
- **Root Cause:** Performing slow network API calls (like yfinance `ohlc_cached` or option `chain_snapshot`) inside the critically locked `with atomic_db_update()` block, blocking other concurrent threads/processes and causing lock timeouts.
- **Self-Heal:** Move all network/LTP/VIX data fetching completely outside the context block, passing resolved data to the locked section only for fast in-memory updates.

### F10: Natural Gas News Sentiment Divergence
- **Symptom:** Stale or unrelated news items causing false bullish directional scoring for Natural Gas.
- **Root Cause:** The TradingView commodity symbol (`MCX:NATURALGAS1!`) was stale, causing it to fall back to a broad NewsAPI query that retrieved generic Nifty/Sensex market wrap-up articles. The word-matching logic would then flag terms like "rises" or "gains" in the Sensex headlines, leading to an incorrect `BULLISH` sentiment.
- **Fix**: Switched the primary TradingView symbol to `NYMEX:NG1!`, removed NewsAPI fallback for NATURALGAS, and integrated direct scrapers for TradingEconomics and X.com (`@NGInews`), backed by a 10-day publication cutoff filter.

### F11: Process-wide HTTPS Serialization via ResilientTLSAdapter
- **Symptom:** Parallel fetchers (Dhan, Shoonya, Paytm) and async LLM requests hanging, causing database write-lock contentions or deadline timeouts.
- **Root Cause:** `ResilientTLSAdapter` initialized a single process-wide lock (`_GLOBAL_SEND_LOCK`) to serialize Zerodha Kite API requests. However, because the adapter was used by all sessions (Dhan, Shoonya, LLM), it forced all network calls in the entire process to execute synchronously.
- **Fix**: Added a `serialize` parameter to `ResilientTLSAdapter.__init__` (defaulting to `False`). Zerodha sessions explicitly set `serialize=True` via `mount_resilient_tls`, while other sessions run without it, restoring true concurrency.

### F12: Ops Agent Incident Log Silent Failure and Stale OK False-Positive
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

### F14: Friday Auto-Exit Shadow Mode Default (P0)
- **Symptom:** Live positions NOT closed on Fridays — weekend risk exposure.
- **Root Cause:** `exit_all_positions_friday()` in `src/scheduler/job_runner.py` defaulted `shadow_mode = config.get("live_shadow_mode", True)`, meaning Friday exits were always paper-only unless explicitly overridden.
- **Fix:** Changed default to `False` so live exits execute by default.

### F14: Friday Exit Timing Drift (P0)
- **Symptom:** Friday exit skipped entirely due to scheduler drift.
- **Root Cause:** Exact string match `current_time_str == "15:28"` — if scheduler tick lands on 15:27 or 15:29, the exit never fires.
- **Fix:** Changed to range check `"15:25" <= current_time_str <= "15:30"` (same for MCX `"23:25" <= t <= "23:30"`).

### F15: NG Daily Loss Cap Never Triggering (P1)
- **Symptom:** Natural Gas keeps opening new positions after 5+ SL hits in a single day.
- **Root Cause:** `check_ng_daily_loss_cap()` used `LIMIT 5 + all()==SL_HIT` logic. When TARGET wins sat between SL hits, the query returned mixed statuses and `all()` returned False.
- **Fix:** Replaced with simple `SELECT COUNT(*) WHERE status IN ('CLOSED_SL','SL_HIT') AND closed_at >= today`.

### F16: NG Timestamp Z-Suffix Comparison Mismatch (P1)
- **Symptom:** Daily loss cap query silently returns 0 rows.
- **Root Cause:** `.replace("+00:00", "Z")` appended Z-suffix to query timestamp, but DB stores timestamps without Z. String comparison `'2026-07-10T00:00:00Z' >= '2026-07-10T00:00:00+00:00'` fails.
- **Fix:** Use `isoformat()` directly without Z replacement.

### F17: MCX Tick Size Order Rejection (P1)
- **Symptom:** MCX Natural Gas/CRUDEOIL orders rejected by broker with "invalid price" error.
- **Root Cause:** `convert_underlying_sl_to_premium()` in `src/engine/trade_plan.py` hardcoded ₹0.05 minimum premium. MCX tick is ₹1.0, so ₹0.50 premiums are invalid.
- **Fix:** Added `TICK_SIZES` dict with per-symbol tick values (NATURALGAS=1.0, CRUDEOIL=1.0, NIFTY=0.05, etc.). Safety bounds now use symbol-appropriate ticks.

### F18: SELL Trade Audit Logic Missing (P1)
- **Symptom:** `audit_close_logic.py` fails to detect fake target/SL hits on SELL/short positions.
- **Root Cause:** Audit script only checked BUY direction logic (target hit when exit >= target). SELL trades have inverted conditions (target hit when exit <= target).
- **Fix:** Added `side` column to query and direction-aware FAKE/REAL classification for both BUY and SELL trades.

### F19: Fetcher HTTP Request Timeouts (P2)
- **Symptom:** Pipeline thread hangs indefinitely when external API (NSE, Dhan, TradingView) is unreachable.
- **Root Cause:** Raw `requests.get()` calls without `timeout` parameter block the calling thread until the OS-level TCP timeout (typically 120-300s).
- **Fix:** Already resolved — all fetcher HTTP calls use `timeout=HTTP_TIMEOUT_SECONDS` (via `base_fetcher._get()`) or explicit `timeout=10`/`timeout=15`. Verified across all 20 fetcher files.

### F20: SQL Injection Guard on f-string Table Names (P2)
- **Symptom:** Theoretical SQL injection if `trades_table` parameter is manipulated.
- **Root Cause:** `risk_engine.py` uses f-string interpolation for table names in SQL queries (`f"SELECT ... FROM {trades_table}"`). While internal-only, a bug or injection elsewhere could pass an arbitrary table name.
- **Fix:** Added `assert trades_table in ("paper_trades", "live_trades")` at the entry of `_check_risk_limits_for_table()` and `_check_consecutive_loss_breaker()`.

### F21: Emergency Flat Interactive Confirmation (P2)
- **Symptom:** Accidental invocation of `emergency_flat.py` immediately closes all live positions.
- **Root Cause:** Script had no interactive confirmation — only `--dry-run` flag prevented execution.
- **Fix:** Added `input("Type CONFIRM to proceed: ")` prompt in `main()` before executing. Skipped when `--dry-run` is passed.

### F22: IPv4 Enforcement Scoped to urllib3 (P2)
- **Symptom:** IPv6 DNS failures, asyncio networking issues, or database driver failures in cloud/Docker environments.
- **Root Cause:** Global `socket.getaddrinfo` monkey-patch forced IPv4 for ALL Python networking, including non-HTTP libraries (asyncio, sqlite3 WAL, DNS resolvers).
- **Fix:** Replaced with `urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET` — scopes IPv4 enforcement to urllib3 only (used by `requests`, Kite SDK, Dhan SDK). Fallback to global patch for older urllib3 versions.

## Architecture Notes
- Pipeline logs go to `logs/main.log` (RotatingFileHandler, 10MB).
- Health state stored in SQLite `health_state` table via `stamp_health()`.
- Ops Agent runs as separate process, reads health_state every 60s.
- LLM providers: Gemini (Primary), Groq (70b/8b fallback), Bedrock (cascading fallback).
- Symbols: NIFTY (NFO), BANKNIFTY (NFO), SENSEX (BFO), NATURALGAS (MCX), CRUDEOIL (MCX).
