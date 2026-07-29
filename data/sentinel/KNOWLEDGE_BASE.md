# NSEBOT Architecture — Scan Sentinel Knowledge Base

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

### F5: Fetcher Degradation & Fast Dual-Fetch Failover (P1-HIGH)
- **Symptom:** Primary fetcher hangs or fails (e.g. Shoonya auth or Dhan timeout).
- **Root Cause:** Broker API rate-limits, session expiration, or network latency.
- **Self-Heal:** `router.py` executes non-blocking parallel fetch loop across dual sources. Primary returns in 12s; if primary fails, cascades to secondary pairs before falling back to any single completed source.
- **Caveat — Shoonya Sequential Call Avalanche:** Shoonya `fetch_option_chain()` makes 3–4 sequential API calls (`_search_scrip`, `_get_quotes`, `_get_option_chain` + per-strike fallbacks), each with 6s timeout + 1 retry. Worst-case wall time (~22.5s+) exceeds the router's 12s deadline. The `0.1s` fallback timeout when the primary source succeeds can also produce false "fetch failed/timed out" warnings. Session-quota re-login (Playwright OAuth, 60–75s) guarantees a timeout if it triggers during a fetch cycle. No per-source timeout configuration exists — all Shoonya timeouts are hardcoded.

### F6: Zero OI & Illiquid Option Chain Anomaly (P1-HIGH)
- **Symptom:** >80% of strikes return `oi=0` and `volume=0`.
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
- **Chain Priorities:**
  - **NSE & BSE:** OpenCode Zen -> Groq -> GitHub Models -> NVIDIA NIM -> Bedrock -> OpenRouter -> Gemini
  - **MCX Symbols:** GitHub Models -> Groq -> OpenCode Zen -> AnyAPI Free -> Bedrock Mantle -> NVIDIA NIM -> Bedrock -> OpenRouter -> Gemini -> SambaNova
- **Self-Heal:** 12-second provider timeout. HTTP 500, empty content, or network exceptions trigger an automatic 10-minute cooldown (`_PROVIDER_COOLDOWN_UNTIL[key] = now + 600.0`), skipping failing providers instantly on subsequent ticks.

### F9: Alert Payload Execution Discrepancy Guard (P1-HIGH)
- **Symptom:** Alert reports `🟢 Entered` for setups blocked by Risk Engine.
- **Root Cause:** Payload generator previously checked raw signal trigger status rather than DB commitment.
- **Self-Heal:** `_build_structured_payload()` requires actual DB row existence (`db_entered`), timeframe execution (`tf_entered`), or paper runner confirmation (`paper_opened`). Non-entered signals report `✗ Not entered` with the exact Risk Engine block reason.

### F10: Friday Mandatory Exit Window (P0-CRITICAL)
- **Symptom:** Open position carried over the weekend exposing account to gap risk.
- **Self-Heal:** Range check `"15:25" <= current_time <= "15:30"` IST (MCX `"23:25" <= current_time <= "23:30"`) forces square-off of open positions on Fridays.

### F11: Natural Gas Session Regime Routing (P1-HIGH)
- **Symptom:** Static NG strategy logic misaligned with market session hours.
- **Self-Heal:** `ng_session_router.py` dynamically routes NATURALGAS to `NG_PARITY`, `NG_EVENT`, or `NG_MOMENTUM` based on time of day. Daily loss cap checks are disabled to allow valid setups.

### F133: Missing import time in Database get_conn Retry Loop (P0-CRITICAL)
- **Symptom:** Unhandled exception `NameError: name 'time' is not defined` raised in `src/models/schema.py` line 483 when `get_conn()` encountered a temporary SQLite database lock (`sqlite3.OperationalError: database is locked`).
- **Root Cause:** In `src/models/schema.py`, the lock-retry loop attempted `time.sleep(0.15 * (attempt + 1))`, but `import time` was missing from the module-level imports.
- **Fix:** Added `import time` to `src/models/schema.py` line 30. The lock retry handler in `get_conn()` now successfully executes `time.sleep()` during lock contention backoffs.

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
