# Agent Rules
- **ALWAYS use Skills and MCPs by default wherever applicable**
## Strict Graft Rule

- **ALWAYS use Graft wherever applicable**:
  - Symbol & Architecture Lookup: use `graft ask "<query>"`, `graft callers <symbol>`, or `graft skeleton <file>` for instant symbol, method, and architecture lookups instead of broad greps or reading whole files.
  - Large Codebase Exploration: run `graft build` if the AST index needs generating or refreshing ($0, local AST).
  - Blast Radius & Caller Verification: before editing or removing shared APIs, functions, or schemas, use `graft callers <symbol>` or `graft trace_calls <symbol>` to verify all downstream consumers.
  - Report Graft token savings and percentage at the end of turns (e.g. `🌱 graft saved ~N tokens (X%) this turn`).
  - Report Ponytail token savings (anti-bloat, minimal diffs, zero filler) at the end of turns (e.g. `✂️ ponytail saved ~M tokens (Y%) this turn`).

## Response Style

- Be concise. No filler.
- Do not restate the prompt unless ambiguity is risky.
- Prefer dense bullets, tables, or code over padded prose.
- Summarize resolved context instead of repeating history.

## Current repo shape

- Main app entry: `python main.py`
- One-shot scan: `python main.py --now`
- Dashboard server: `python dashboard_server.py` (FastAPI — Streamlit legacy removed)
- Paper trading page: `http://localhost:8080/paper`
- Active scan interval options: `5m`, `15m`, `30m`, `1H`, `3H`, `1D`
- Live option-chain output is limited to ATM +/- 10 strikes
- Dashboard "Recent Intelligence" section:
  - NATURALGAS: TradingView 24h news + direction scoring
  - NIFTY/BANKNIFTY: ScanX heatmap + combined direction (OI + 1H/3H sentiments)
  - Fallback 1H/3H sentiment generator from local underlying_price history
- Contributors & Top Heavyweights:
  - Consolidated 3-index Telegram alert per scan cycle (`/hw`, `/heavyweights`, `/contributors`) via [heavyweight_digest.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/alerts/heavyweight_digest.py)
  - Dedicated Contributors page: `http://localhost:8080/contributors`
- Paper trading dashboard:
  - Bloomberg/TradingView-inspired professional UI
  - 6 comprehensive KPIs: total trades, win rate, P&L, avg P&L, profit factor, streak
  - Collapsible Equity Curve (linked to Total P&L) initialized in collapsed mode with dynamic "view curve ▾" / "hide curve ▴" toggle
  - Symbol performance breakdown (win rate, avg P&L, total P&L per symbol)
  - Trade duration tracking (human-readable format)
  - Responsive design (desktop/tablet/mobile)
- Production Cloud Host (OCI):
  - Oracle Cloud Infrastructure Ubuntu instance `137.23.58.212` (user `ubuntu`, hostname `nsebot`)
  - Continuous execution managed via systemd: `nsebot.service` (scheduler & engine) and `nsebot-dashboard.service` (dashboard on port 8080)
  - Fast-forward remote deployment via `git pull origin dev` + `sudo systemctl restart nsebot.service nsebot-dashboard.service`
- Keep docs aligned with the live FastAPI dashboard, not older Streamlit references
- News sources: ICICIDirect + NewsAPI for NIFTY/BANKNIFTY; TradingView for MCX (Way2Wealth removed)
- Multi-leg execution engine: [multileg_live_trading.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_live_trading.py) & [multileg_paper_trading.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_paper_trading.py) supporting Iron Condor, Strangles, Straddles, Spreads, and Jade Lizards
- Centralized Fail-Closed Broker Gate: [broker_gate.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/broker_gate.py) gates all live Kite orders (shadow mode, broker disabled, trading paused, market hours) while decoupling paper lot sizing
- Fail-Closed Runtime Config: [runtime_config.py](file:///c:/Users/manve/VibeProjects/NSEBOT/config/runtime_config.py) provides safe fallback defaults and atomic file persistence
- Immutable Scan Snapshots: [scan_snapshot.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/models/scan_snapshot.py) with SHA-256 data hashing and `MappingProxyType` immutability; `snapshot_id` provenance tracked in `paper_trades`, `live_trades`, and `multi_leg_trades`
- Strict LLM Execution Parser & Validator: [execution_parser.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/execution_parser.py) and [multileg_validator.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_validator.py) enforce 1-6 legs, strike bounds, and alignment with canonical engine verdicts
- Sequential Leg Fills & Atomic Rollback: Live orders verify fills with `confirm_order_fill()`; on partial failure, `_rollback_placed_legs()` flattens open positions immediately

## Timeframe Role Separation (strict — do not cross-use)

- **3H candles**: entry timing ONLY — breakout/breakdown confirmation combined with OI buildup classification. Never used for trend/signal generation.
- **1H candles**: exit timing ONLY — strategy-level exit trigger. Never used for entries, trend, or signal generation.
- 3H and 1H are **NOT** cross-checked against each other. They serve independent, non-overlapping functions.

## New Price Discovery Rules & Timings (SEBI/NSE Mandate)

All bot decision paths, time guards, and LLM reasoning engines MUST adhere strictly to these timings and rules:

### F&O Pre-Open Session (09:00 AM – 09:15 AM IST)
Pre-open session uses a call auction mechanism to discover a single equilibrium opening price for eligible index and single-stock futures:
- **09:00 AM – 09:08 AM IST**: Order entry, modification, and cancellation window (with random closure during the final minute, around 09:07–09:08 AM).
- **09:08 AM – 09:12 AM IST**: Order matching and opening price determination.
- **09:12 AM – 09:15 AM IST**: Buffer period before continuous market trading begins at 09:15 AM.

### Cash & F&O Closing / Call Auction Session (CAS) (03:15 PM – 03:40 PM IST)
- **3:15 PM IST**: Continuous regular trading for F&O-eligible stocks in the cash market ends (instead of 3:30 PM).
- **3:15 PM – 3:20 PM IST**: Transition period — orders are frozen, reference prices are published, and non-equilibrium/out-of-bound pending orders are automatically canceled.
- **3:20 PM – 3:25 PM IST**: Order Entry Session 1 — both market and limit orders are permitted.
- **3:25 PM – 3:30 PM IST**: Order Entry Session 2 — only limit orders are allowed; closes randomly between 3:28 PM and 3:30 PM.
- **3:30 PM – 3:35 PM IST**: Order matching occurs at a single equilibrium price (where maximum share volume trades), declared as the official cash closing price.
- **3:40 PM IST**: Derivative F&O contracts continue trading until this time, providing traders/bot an extra 10 minutes past the old market close (15:30) to hedge or adjust positions.
- **Non-F&O Stocks**: Completely unaffected by CAS; continuous normal trading continues until 3:30 PM IST.
- **Mandatory Friday Exits**: Handled between 15:35–15:40 IST for NSE F&O (23:25–23:30 MCX) to square off weekend gap risk before derivatives halt.

## AI / LLM state (v3.0)

- `live_ai_decision_mode = full` (default in `runtime_config.json`; governs live AI decisions). NOTE: `settings.py AI_DECISION_MODE` (env `AI_DECISION_MODE`, default `empirical`) is legacy and NOT used by the live decision path.
- LLM enrichment is **engine-aligned**: OI engine decides direction; LLM provides execution detail only
- **NSE/BSE Primary LLM Chain:** OmniRouter (Claude-Models Combo → Claude/Antigravity → claude/Free → GPT 5.5 CX → KR Models) → Groq → Gemini → OpenCode Zen → NVIDIA NIM → OpenRouter.
- **MCX Primary LLM Chain:** OmniRouter (Claude-Models Combo → Claude/Antigravity → claude/Free → GPT 5.5 CX → KR Models) → Groq → Gemini → OpenCode Zen → NVIDIA NIM → OpenRouter.
- Multi-Leg entry advisor is skipped when open books for the symbol >= 5; exit advisor monitors existing open books
- Chart conflict (1H vs 3H): NO penalty for OI-based trades; a 1H opposing a completed 3H = entry timing signal
- MCX confidence floor: 72% for NATURALGAS/CRUDEOIL/GOLD/SILVER (vs 70% NSE)
- JSON parse hardening: `_extract_json()` handles fences, prose wrappers, control chars — eliminates per-cycle OpenRouter parse failures
- HOLDING alerts now show position age (`entered 47m ago`) to disambiguate from new signals
- **Deterministic Decision Matrix (v2.0 Enforce Mode):** Operating under `"mode": "enforce"` in [decision_matrix.json](file:///c:/Users/manve/VibeProjects/NSEBOT/config/decision_matrix.json). Allowed structures are strictly bounded by direction × IV percentile bucket. LLMs cannot select structures outside the regime cell.
- **AI Exit Auto-Exits & Gating:** Autonomous AI exits (`CLOSED_AI_EXIT`) and leg adjustments (`ADJUST`) are gated by [exit_evaluator.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/exit_evaluator.py). AI cannot close prematurely; it is restricted to tightening conditions ($\ge 30\%$ credit profit or $\le -50\%$ stop loss ratio).
- **TFSS Multi-Leg Strangle Book (v4.0):** Active TFSS strangles are grouped via `leg_group_id` (`{symbol}:{today_date}:TFSS`). Supports up to 6 open legs (3 per side) per symbol-day. Tranche scale-down (`ENABLE_TFSS_TRANCHE_SCALING = False`) is disabled per requirement so every trade places at 100% full base size in market direction (Bullish → Sell PE, Bearish → Sell CE). Risk Engine checks combined margin (cap ₹600k), combined net delta (cap 0.60), and max tranches (6) before allowing new entries. Exit logic & `EXIT_PRIORITY_MAP` remain fully active (Delta-stop exits close the tested side selectively, leaving the opposite side active).

## Token Efficiency

- Keep only active task context, unresolved decisions, and hard constraints.
- Reference existing files or prior context instead of re-pasting.
- Compress tool output before using it.
- Match response length to task complexity.

## Browser Automation

Use `agent-browser` for browser automation in this workspace.

Primary workflow:

1. `agent-browser open <url>`
2. `agent-browser snapshot -i`
3. Interact with refs like `@e1`, `@e2` via `click`, `fill`, `type`, `get text`
4. Re-run `snapshot -i` after page changes
5. Use `screenshot` when visual confirmation matters
6. Close sessions with `agent-browser close`

Rules:

- Prefer snapshot refs over CSS selectors.
- Use `agent-browser --help` for command discovery.
- If `agent-browser` is not on PATH in PowerShell, use:
  - `C:\Users\manve\AppData\Roaming\npm\agent-browser.cmd`
- Ensure write permissions on `C:\Users\manve\.agent-browser` (grant CodexSandboxUsers Modify access if needed)
- Validated: NATURALGAS news section, NIFTY/BANKNIFTY heatmap sections working correctly

## Planning & Execution Rules

- Do NOT create `implementation_plan.md` or `walkthrough.md` documents.
- Do NOT run tests (e.g., pytest) unless the user explicitly approves.

Follow below instructions before starting work:

1. **NO GUESSING** — If I lack information or a library function is uncertain, do not invent syntax. State clearly what is missing.

2. **THINK BEFORE WRITING** — Wrap step-by-step logic, edge-case analysis, and architectural plan in `<thinking>` tags before outputting code.

3. **VERIFY EXAMPLES** — Ensure all code snippets use exact syntax of the specific version requested. Never mix versions.

4. **TYPE SAFETY** — Always write strictly typed code with explicit error handling and input validation.

5. **NO SHORTCUTS** — Provide full, runnable code blocks. No placeholders like `// implement here`.

6. **Use required MCP** before starting any work.

7. **Review previous code line-by-line** for deprecated methods, unhandled edge cases, or logic bugs before fixing.
8. **Update KNOWLEDGE_BASE.md**: Update `data/sentinel/KNOWLEDGE_BASE.md` only for **major changes/fixes** in the app or when changes are relevant to **Sentinel scan diagnostics**. Do not update for routine architecture/pipeline changes.

## Key Architectural Decisions (NSEBOT)

- **Schema Migrations:** Managed inside `_MIGRATIONS` in [schema.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/models/schema.py) up to M113 (`M111_add_snapshot_id_to_paper_trades`, `M112_add_snapshot_id_to_live_trades`, `M113_add_snapshot_id_to_ml_trades`). SQLite uses WAL (Write-Ahead Logging) mode with `timeout=60.0` to safely handle concurrent, multi-threaded engine and dashboard server writes.
- **Fail-Closed Broker Invariants:** All live Kite order actions are strictly authorized via [broker_gate.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/broker_gate.py) (`authorize_broker_execution`). Paper trading calculations remain completely decoupled from broker gate states.
- **Deterministic Decision Matrix (v2.0 Enforce Mode):** Driven by [decision_matrix.json](file:///c:/Users/manve/VibeProjects/NSEBOT/config/decision_matrix.json) and [decision_matrix.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/decision_matrix.py). Permitted options structures (Iron Condor, Short Strangle, Bull Put Spread, Bear Call Spread, Jade Lizard) are strictly determined by direction and IV percentile volatility bucket. Validator rejects proposals outside the cell.
- **Delta Profiles & Candidate Strike Pre-Filtering:** Delta bands (Aggressive default vs Defensive) have hard danger ceilings. In [multileg_strategy.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_strategy.py) (`get_candidate_strikes`), candidate strikes are pre-filtered against `danger_ceiling_short` (e.g. >0.32 or >0.25 delta), completely preventing danger ceiling rejections.
- **Hedge Drag Ceiling & Credit-to-Width:** `MAX_HEDGE_COST_RATIO` in [multileg_strategies.py](file:///c:/Users/manve/VibeProjects/NSEBOT/config/multileg_strategies.py) is calibrated to 0.60 for indices (0.55 for Natural Gas). In `validate_legs()`, hedge debit up to 60% of gross credit is accepted provided net credit is $\ge 25\%$ of wing width.
- **Directional Engine Alignment & Confidence Blending:** Directional strategies resolve engine direction via `is_bullish_engine` / `is_bearish_engine`. When engine conviction is $\ge 60\%$ and LLM execution confidence $\ge \text{conf\_floor}$, confidence is blended (`0.35 * engine + 0.65 * llm`) in [multileg_paper_trading.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_paper_trading.py) and [multileg_live_trading.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_live_trading.py) to prevent false rejections of 60–69% signals.
- **Strategy Routing & Models:** Plumbed dynamically via [strategy_registry.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/strategy_registry.py). Custom session overrides redirect `NATURALGAS` to specialized strategies (`NG_PARITY`, `NG_EVENT`, `NG_MOMENTUM`) based on time/regime rather than standard `CORE` logic.
- **Multi-Leg & TFSS Option Execution:** Multi-leg strategies and TFSS are executed with strict leg validation, margin checks, net delta caps (`0.60`), and atomic database operations (`insert_multileg_trade_atomically`, `close_book`, `execute_multi_leg_adjustment`). Live execution features sequential Kite fills and automatic rollback (`_rollback_placed_legs`).
- **Exit Logic Precedents & Safe Leg Ordering:**
  1. *Priority Map:* Evaluated via [exit_evaluator.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/exit_evaluator.py): `HARD_STOP` $\to$ `TESTED_DELTA` $\to$ `PROFIT_TARGET` $\to$ `TIME_DECAY` $\to$ `CAS_CLOSE` $\to$ `FRIDAY_CLOSE` $\to$ `AI_TIGHTEN`.
  2. *AI Tightening Authority:* AI closes only fire on true tightening conditions ($\ge 30\%$ credit profit or $\le -50\%$ stop loss ratio). Premature closes are deterministically blocked.
  3. *Safe Leg Ordering:* Short sold legs are closed first via `order_legs_for_exit()` to immediately release SPAN margin and eliminate naked risk before long hedges are closed.
  4. *Mechanical Exits:* Checked continuously on every scan tick (based on SL/Target premium thresholds, trailing stops, or time-of-day guards).
  5. *Friday Exits:* Mandatory square-off is executed between 15:35–15:40 IST (NSE F&O close; 23:25–23:30 MCX) to avoid weekend gap risks.
  6. *Daily Loss Cap:* Natural Gas blocks new entries for the day after 5 SL hits.
- **Top Heavyweights Net Impact & Contributors Parity:** [heavyweight_digest.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/alerts/heavyweight_digest.py) generates a single consolidated 3-index Telegram alert per scan during market hours (09:15–15:40 IST). Parity maintained with the Contributors page on the FastAPI dashboard.
- **OCI Production Cloud Architecture:** Running 24/7 on Ubuntu instance `137.23.58.212` (`nsebot`) via dual systemd units: `nsebot.service` (trading scheduler/engine) and `nsebot-dashboard.service` (dashboard server). Remote updates are deployed via `git pull origin dev` followed by service restarts.
- **ML Predictor Quality Gate & Retraining Loop:** Configured with `ml_predictor_mode = shadow` in `data/runtime_config.json`. The sample qualification threshold is set to `MIN_SAMPLES_FOR_LIVE = 250` (samples > 200, CV AUC > 0.68). Multi-leg trade closures (`close_book()` in [schema.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/models/schema.py)) automatically trigger model retraining events.
- **Natural Gas EIA Macro Stance Dynamic Override:** While weekly EIA storage stance (`BULLISH_TIGHTENING` / `BEARISH_LOOSENING`) provides macro direction in [ng_momentum_strategy.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/ng_momentum_strategy.py), intraday price trend (NYMEX 1H breakdown) and OI conviction (`Call Writing`, `Short Buildup`) override stale EIA bias to permit short momentum entries.
- **Shadow Mode Pipeline Decoupling:** [pipeline.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/pipeline.py) invokes live shadow trading when `live_shadow_mode=True`, completely decoupled from whether live broker order execution (`is_broker_trade_enabled()`) is enabled.
- **Multi-Leg Risk Profile & Stop-Loss Calculation:** For undefined-risk short straddles/strangles, max loss is computed based on strategy stop-loss thresholds rather than arbitrary `underlying * 0.5`, eliminating false trade rejections in [multileg_strategy.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_strategy.py) and [entry_quality.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/entry_quality.py).
- **Secondary Chain Fetcher Fallbacks:** Option chain fallback router in [router.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/fetchers/router.py) integrates [niftytrader_fetcher.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/fetchers/niftytrader_fetcher.py), while [shoonya_fetcher.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/fetchers/shoonya_fetcher.py) bulk fetching is guarded against interpreter shutdown concurrency faults.
