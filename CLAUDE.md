# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Strict Tooling & Operational Discipline

- **STRICT RULE: ALWAYS use Graft for codebase exploration.**
  - Symbol & Architecture Lookup: Use `graft ask "<query>"`, `graft callers <symbol>`, or `graft skeleton <file>` for instant symbol, method, and architecture lookups instead of broad greps or reading whole files.
  - Large Codebase Exploration: Run `graft build` if the AST index needs generating or refreshing ($0, local AST).
  - Blast Radius & Caller Verification: Before editing or removing shared APIs, functions, or schemas, use `graft callers <symbol>` or `graft trace_calls <symbol>` to verify all downstream consumers.
  - **Graft before any search or read.** Run `graft ask` / `graft skeleton` / `graft callers` first. Do not use broad Grep, Glob, or whole-file reads for symbol or architecture lookups. Subagents must also start with Graft.
- **Targeted Testing Only:** Avoid running the full `pytest tests/ -v` test suite on routine changes. Always run targeted test files (e.g. `pytest tests/test_broker_gate.py -v`). Full suite takes significant time and exhausts database connection pools.
- **Windows Shell & Quoting Discipline:** On Windows, never use multi-line bash heredocs (`cat << 'EOF' > file.py`, `python << 'PY_EOF'`) or stdin gymnastics. Use file editing/writing tools directly or run standalone `.py` scripts.
- **Mandatory Turn Footer:** End every task turn with this four-line footer (report estimates with "~", use "none" if not applicable):
  - `🌱 Graft saved ~N tokens this turn across [areas]`
  - `✂️ Ponytail saved ~N tokens (~N lines avoided) via [what was avoided]`
  - `🛠️ Skills used: [skills actually invoked via the Skill tool]`
  - `📊 Total tokens consumed this turn: ~N tokens`

---

## Development & Operational Commands

### Running Bot & Local Services
- **Continuous Bot Scheduler:** `python main.py`
- **Immediate One-Shot Scan:** `python main.py --now`
- **One-Shot Scan for Specific Symbols:** `python main.py --now --symbols NIFTY BANKNIFTY NATURALGAS`
- **FastAPI Web Dashboard:** `python dashboard_server.py` (Serves on `http://localhost:8080`)
  - Main Cockpit & Option Chain: `/`
  - Paper Trading Console: `/paper`
  - Broker Live Console: `/broker`
  - AI Insights & Decision Audit: `/ai`
  - Contributors & Heavyweights: `/contributors`
  - Market Summary Memorandum: `/report`
  - Settings & Strategy Cockpit: `/settings`
  - Ops Agent & Sentinel Health: `/ops`
- **Emergency Flatten (Close all positions & cancel GTTs):** `python emergency_flat.py`
- **Decision Matrix Replay / Backtesting:** `python scripts/replay_decision_matrix.py`
- **Ops Agent Daemon:** `python ops_agent.py`

### Production Server (Oracle Cloud Infrastructure — OCI)
- **Host:** `137.23.58.212` | **User:** `ubuntu` | **Key:** `~/.ssh/id_ed25519`
- **App Directory:** `/home/ubuntu/nsebot` | **Virtualenv:** `/home/ubuntu/nsebot/.venv`
- **Deploy & Restart Services from Local:**
  ```bash
  ssh -i ~/.ssh/id_ed25519 ubuntu@137.23.58.212 "cd /home/ubuntu/nsebot && git pull origin dev && sudo systemctl restart nsebot.service nsebot-dashboard.service"
  ```
- **Service Status & Health:**
  ```bash
  ssh -i ~/.ssh/id_ed25519 ubuntu@137.23.58.212 "sudo systemctl status nsebot.service nsebot-dashboard.service --no-pager"
  ```
- **Tail Engine Logs:**
  ```bash
  ssh -i ~/.ssh/id_ed25519 ubuntu@137.23.58.212 "sudo journalctl -u nsebot.service -f"
  ```
- **Tail Dashboard Logs:**
  ```bash
  ssh -i ~/.ssh/id_ed25519 ubuntu@137.23.58.212 "sudo journalctl -u nsebot-dashboard.service -f"
  ```

### Testing Commands
- **Run a single test file:** `pytest tests/test_decision_matrix.py -v`
- **Run a specific test method:** `pytest tests/test_trade_plan.py -k test_unified_sl_target -v`
- **Run related tests with pattern:** `pytest tests/test_multileg*.py -v`
- **Run with stdout enabled:** `pytest tests/test_broker_gate.py -s -v`

### Graft Index Commands
- **Rebuild AST index:** `graft build`
- **Repository map & orientation:** `graft map` (CLI) or MCP tool `graft_repo_map`
- **Search symbol or concept:** `graft ask "<query>"`
- **File API skeleton:** `graft skeleton <file_path>`
- **Direct & transitive callers:** `graft callers <symbol>`
- **Blast radius of changes:** `graft blast`

---

## High-Level Architecture & Core Flow

### 1. Central Scan Flow
```
main.py -> scheduler (job_runner.py) -> pipeline.py -> fetchers (router.py) ->
anomaly_detector.py -> intelligence.py + decision_matrix.py -> llm_enrichment.py ->
strategy_registry.py -> paper_trading.py / live_trading.py ->
alerts/digest.py + alerts/heavyweight_digest.py -> telegram_dispatcher.py
```

### 2. Timeframe Separation Rules
- **3H Candles:** Entry timing **ONLY** (breakout/breakdown confirmation with OI trend). Never used for exit timing or trend generation.
- **1H Candles:** Exit timing **ONLY** (strategy exit trigger). Never used for entries or trend generation.
- 3H and 1H candles are **strictly decoupled** and never cross-checked against each other.

### 3. Option Chain Ingestion & Strike Filtering
- Dynamic strike window: Strictly limited to **ATM +/- 10 strikes** across NSE indices (`NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `SENSEX`) and MCX commodities (`NATURALGAS`, `CRUDEOIL`, `GOLD`, `SILVER`).
- Ingestion routes through `src/fetchers/router.py` (NSE Python API, Shoonya Noren API, NiftyTrader fallback, Dhan built-up data for MCX).

### 4. Deterministic Decision Matrix & Exit Evaluator (v2.0 Enforce Mode)
- **Config Loader (`config/decision_matrix_loader.py`):** Loads `config/decision_matrix.json` (`"mode": "enforce"`) with thread-safe hot-reloading and fail-closed fallback to code defaults (`DEFAULTS`).
- **Decision Matrix Engine (`src/engine/decision_matrix.py`):**
  - Evaluates IV percentile (`atm_iv_percentile`) and assigns volatility buckets (`vol_bucket`: Low < 25, Mid 25–70, High > 70).
  - Selects optimal options structures (Iron Condor, Short Strangle, Bull Put Spread, Bear Call Spread, Jade Lizard) deterministically from the regime cell.
  - Enforces MCX strike liquidity guards (`is_mcx_strike_liquid`), NYMEX trend alignment (`nymex_trend`), and churn guards (`check_churn_guards`).
- **Aggressive Delta Profile & Strike Pre-Filtering:**
  - Delta bands configured per structure and asset class (default: Aggressive).
  - Candidate strike generation in `src/engine/multileg_strategy.py` (`get_candidate_strikes`) strictly pre-filters strikes against `danger_ceiling_short` (e.g., > 0.32 or > 0.25 delta), ensuring LLMs never receive or propose strikes breaching risk limits.
  - Hedge drag ceiling (`MAX_HEDGE_COST_RATIO` in `config/multileg_strategies.py`) raised to 0.60 for indices (0.55 for Natural Gas) when net credit $\ge 25\%$ of wing width.
- **Directional Engine Alignment & Confidence Blending:**
  - In `src/engine/multileg_validator.py`, direction is resolved from `is_bullish_engine` / `is_bearish_engine` or directional strategy type (`BULL_PUT_SPREAD`, `BEAR_CALL_SPREAD`), preventing false NEUTRAL regime mismatches.
  - In `multileg_paper_trading.py` and `multileg_live_trading.py`, confidence is blended (`0.35 * engine + 0.65 * llm`) when engine conviction is $\ge 60\%$ and LLM $\ge \text{conf\_floor}$, eliminating false rejections on 60–69% signals.
- **Exit Evaluator (`src/engine/exit_evaluator.py`):**
  - Priority-based exit decision engine (`evaluate_exit_priority`): `HARD_STOP` $\to$ `TESTED_DELTA` $\to$ `PROFIT_TARGET` $\to$ `TIME_DECAY` $\to$ `CAS_CLOSE` $\to$ `FRIDAY_CLOSE` $\to$ `AI_TIGHTEN`.
  - AI exits only fire on valid tightening conditions ($\ge 30\%$ credit profit or $\le -50\%$ stop loss ratio); premature exits are blocked.
  - Safe exit leg ordering (`order_legs_for_exit`): Closes short sold legs first to immediately release SPAN margin and eliminate naked risk before closing long hedges.

### 5. Engine-Aligned LLM Enrichment (v3.0)
- **Deterministic OI Authority:** The OI engine (`intelligence.py._price_oi_verdict`) holds primary authority over trade direction.
- **Strict Guardrail (`_enforce_engine_alignment`):** The LLM receives the `ENGINE DECISION` and may enrich execution parameters or downgrade to `NO_TRADE`. Direction flips (e.g. `GO_LONG` on a `BEARISH` engine verdict) are deterministically blocked and converted to `NO_TRADE`.
- **Position-Aware Bypass:** If an open position exists for a symbol, entry LLM evaluation is bypassed; the exit advisor runs instead.
- **Purpose-Routed Multi-Tier Failover:** Managed via `src/engine/llm_enrichment.py` across five pipelines (`live_verdict`, `eod_review`, `formatting`, `sentinel_diagnostic`, `eia_analysis`). Primary tier routes to local OmniRouter (`localhost:20128`), failing over sequentially through GitHub Models, Groq, Gemini SDK, OpenCode Zen, NVIDIA NIM, Bedrock SDK, and OpenRouter.

### 6. Strategy Registry & Execution Engine
- **Strategy Registry (`src/engine/strategy_registry.py`):** Dispatches active strategies per symbol. Hot-reloaded without restart via `config/runtime_config.py` and dashboard settings.
  - **CORE:** Directional momentum buying (BUY CE/PE).
  - **TIMEFRAME:** 3H breakout entries, 1H crossover exits.
  - **TFSS (Trend-Following Short Strangle):** Multi-tranche premium selling (up to 6 legs) tracked by `leg_group_id` with dynamic lot scaling (`50% -> 30% -> 20%`).
  - **MULTILEG:** Multi-leg short options selling (Iron Condor, Strangle, Straddle, Spreads, Jade Lizard).
  - **TIMEFRAME:** 3H entry / 1H exit with dual entry paths: (1) dedicated reversal entry (requires underlying continuous trend + close above/below previous 3H candle) and (2) high-conviction continuation breakout (requires 3H range breakout + 2x OI differential multiplier via `TF_CONTINUATION_OI_MULTIPLIER`).
- **Centralized Broker Authorization Gate (`src/engine/broker_gate.py`):**
  - All live Kite executions (entry, exit, adjust, reconcile) route through `authorize_broker_execution`.
  - Strictly fail-closed: Enforces shadow mode, broker disabled switch, emergency pause, market open hours, and symbol whitelists.
  - Paper trading operates independently even when broker execution is disabled.
- **Atomic Multi-Leg Execution (`src/engine/multileg_live_trading.py`):**
  - Places live legs sequentially, confirming order fills via `confirm_order_fill()`.
  - If any leg fails or rejects, `_rollback_placed_legs()` immediately flattens open legs with opposite market orders.
- **Unified Trade Plan (`src/engine/trade_plan.py`):**
  - Single source of truth for ATR-based SL/Target calculations across both paper and live trading. Never duplicate SL/Target logic in individual strategies.

### 7. Provenance & Persistence
- **Immutable Scan Snapshots (`src/models/scan_snapshot.py`):** Context frozen at scan time with SHA-256 hash digests and `MappingProxyType` deep immutability.
- **Database Schema (`src/models/schema.py`):** SQLite running in WAL mode. Migrations M101–M113 link all paper, live, and multi-leg trades to exact `snapshot_id` provenance.
- **Fail-Closed Runtime Configuration (`config/runtime_config.py`):** Atomic file persistence to `data/runtime_config.json` with safe fallback defaults (`get_fail_closed_defaults`).

### 8. Heavyweights Net Impact Digest & Contributors Dashboard
- **Consolidated Scan Digest (`src/alerts/heavyweight_digest.py`):**
  - Aggregates heavyweight point movement across `NIFTY`, `BANKNIFTY`, and `SENSEX`.
  - Dispatches a single combined Telegram alert per scan during market hours (09:15–15:40 IST) with 5-scan velocity tracking and options strategy cues.
  - Responsive commands: `/hw`, `/heavyweights`, `/contributors`.
- **Contributors Dashboard (`src/dashboard/contributors.html`):**
  - Real-time web view of top heavyweight movers, point drag, and pullers vs draggers breakdown.
  - **Top Heavyweights Net Point Movement Table:** Compact 2-line headers (`13:17 BASE`, `13:47 Δ`, etc.) eliminating horizontal scrollbar (all 11 columns fit cleanly in 1920px container); center-zero diverging mini-bars (red growing left, green growing right); visually shaded baseline column.
  - **Institutional Execution Dossier:** State machine automatically trips `THESIS INVALIDATED — STAND DOWN` in high-contrast red when 15m scan delta impulse exceeds ±25 pts or boundaries breach; surfaces tripped condition with exact root cause; parses LLM prop desk synthesis cleanly into status badges.
- **Swiss Modern Flat Architecture Design System (`theme.css`):**
  - Professional, glare-free flat styling: solid 3-tier elevation (`#090b0e`, `#10141a`, `#161c24`), crisp 1px borders (`var(--border)`), tabular numbers (`font-variant-numeric: tabular-nums`), and standardized 32×32px theme toggles across all 8 web templates.
  - Paper trading collapsible equity curve features dynamic, precision-aware Y-axis tick formatting (`maxTicksLimit: 6`, fractional `k`/`L`) eliminating duplicate label collisions; holding durations sanitize sub-second trades to `—`.

### 9. Weekend & Market Session Guardrails
- **Kite Token Expiry Alerts (`src/engine/live_trading.py`):** Token expiry alerts (`check_token_expiry_with_open_positions()`) and 15-minute exit checks (`check_all_live_exits_every_2_min()`) bypass execution on weekends (`now_ist.weekday() >= 5`) and when markets for held symbols are closed. Paper trades (`trade_mode = 'PAPER'`) and shadow positions are strictly decoupled from live broker alert counts.
- **Nightly Telegram Database Backup (`src/scheduler/job_runner.py` & `src/utils/gdrive_backup.py`):** Daily 23:56 PM IST database backup is strictly guarded to Monday–Friday (`is_weekday = now_ist.weekday() < 5`). Defensive guard inside `backup_db_to_telegram()` automatically skips on Saturday and Sunday unless explicitly forced.

---

## Critical Invariants & Constraints

1. **All SL/Target logic must live in `trade_plan.py`:** Do not implement ad-hoc SL/Target math in strategy runners.
2. **Never allow LLM direction flips:** Engine OI direction is authoritative.
3. **Keep option-chain queries within ATM +/- 10 strikes:** Hard performance and payload constraint.
4. **No Streamlit:** Dashboard is strictly FastAPI (`dashboard_server.py`).
5. **No obsolete broker/source routes:** Upstox, Paytm, and NSE commodity chain routes are decommissioned.
6. **Symbol-specific intelligence isolation:** `NATURALGAS` uses news-only intelligence; `NIFTY`/`BANKNIFTY` use heatmap-only intelligence. Never cross-mix.
7. **No weekend alert spam or DB backups:** Token expiry warnings and nightly backups never fire on Saturdays or Sundays. Paper positions must never trigger live broker alerts.
