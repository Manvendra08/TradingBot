# Agent Rules

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
- Paper trading dashboard (Phase 1 complete):
  - Bloomberg/TradingView-inspired professional UI
  - 6 comprehensive KPIs: total trades, win rate, P&L, avg P&L, profit factor, streak
  - Symbol performance breakdown (win rate, avg P&L, total P&L per symbol)
  - Trade duration tracking (human-readable format)
  - Enhanced equity curve with smooth animations
  - Color-coded status badges and P&L display
  - Responsive design (desktop/tablet/mobile)
- Keep docs aligned with the live FastAPI dashboard, not older Streamlit references
- News sources: ICICIDirect + NewsAPI for NIFTY/BANKNIFTY; TradingView for MCX (Way2Wealth removed)
- Vendored `src/tvdatafeed/` deleted — use pip-installed package
- Dependencies trimmed: ~877 MB of unused packages removed (scipy, pandas, Twisted, numba, etc.)
- `APScheduler` and `dhanhq` removed from requirements.txt (unused)

## Timeframe Role Separation (strict — do not cross-use)

- **3H candles**: entry timing ONLY — breakout/breakdown confirmation combined with OI buildup classification. Never used for trend/signal generation.
- **1H candles**: exit timing ONLY — strategy-level exit trigger. Never used for entries, trend, or signal generation.
- 3H and 1H are **NOT** cross-checked against each other. They serve independent, non-overlapping functions.

## AI / LLM state (v3.0)

- `live_ai_decision_mode = full` (default in `runtime_config.json`; governs live AI decisions). NOTE: `settings.py AI_DECISION_MODE` (env `AI_DECISION_MODE`, default `empirical`) is legacy and NOT used by the live decision path.
- LLM enrichment is **engine-aligned**: OI engine decides direction; LLM provides execution detail only
- Direction inversion guard: `_enforce_engine_alignment()` in `llm_enrichment.py` — no model can flip the engine's directional call
- Entry advisor is skipped when a position is already open; only exit advisor runs
- Chart conflict (1H vs 3H): NO penalty for OI-based trades; a 1H opposing a completed 3H = entry timing signal
- MCX confidence floor: 72% for NATURALGAS/CRUDEOIL/GOLD/SILVER (vs 70% NSE)
- JSON parse hardening: `_extract_json()` handles fences, prose wrappers, control chars — eliminates per-cycle OpenRouter parse failures
- HOLDING alerts now show position age (`entered 47m ago`) to disambiguate from new signals
- **AI Exit Advice is Advisory Only:** Auto-exits (`CLOSED_AI_EXIT`) from AI exit advice are disabled; high-urgency exit suggestions are purely logged as advisory recommendations.
- **TFSS Multi-Leg Strangle Book (v4.0):** Active TFSS strangles are grouped via `leg_group_id` (`{symbol}:{today_date}:TFSS`). Supports up to 6 open legs (3 per side) per symbol-day. Lot sizing scales dynamically by tranche (`50% -> 30% -> 20%`) via `TRANCHE_SEQUENCE`. Risk Engine checks combined margin (cap ₹600k), combined net delta (cap 0.60), and max tranches (6) before allowing new tranche entries. Delta-stop exits close the tested side selectively (prioritized via `EXIT_PRIORITY_MAP`), leaving the opposite untested side active.

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
8. **Update KNOWLEDGE_BASE.md**: Always update the Scan Sentinel grounded codebase knowledge base at `data/sentinel/KNOWLEDGE_BASE.md` when making changes to codebase architecture, pipeline flow, or when introducing new features relevant to Scan Sentinel agentic AI diagnostics.

## Key Architectural Decisions (NSEBOT)

- **Schema Migrations:** Managed inside `_MIGRATIONS` in [schema.py](file:///c:/Users/manve/Downloads/NSEBOT/src/models/schema.py). SQLite uses WAL (Write-Ahead Logging) mode with `timeout=30.0` to safely handle concurrent, multi-threaded engine and dashboard server writes.
- **Strategy Routing & Models:** Plumbed dynamically via [strategy_registry.py](file:///c:/Users/manve/Downloads/NSEBOT/src/engine/strategy_registry.py). Custom session overrides redirect `NATURALGAS` to specialized strategies (`NG_PARITY`, `NG_EVENT`, `NG_MOMENTUM`) based on time/regime rather than standard `CORE` logic.
- **TFSS Option Execution:** The Trend-Following Strangle System (TFSS) acts as the execution layer for `CORE` signals (sells PE for bullish, CE for bearish). Groups multi-leg strangles via `leg_group_id` (`{symbol}:{date}:TFSS`), caps tranches at 6 per symbol-day, and limits combined net delta to `0.60`.
- **Exit Logic Precedents:**
  1. *AI Exit Advice:* Strictly **advisory-only** (auto-exits are disabled; high-urgency suggestions are logged only to prevent false liquidations).
  2. *Mechanical Exits:* Checked continuously on every scan tick (based on SL/Target premium thresholds, trailing stops, or time-of-day guards).
  3. *Friday Exits:* Mandatory square-off is executed between 15:25–15:30 IST (23:25–23:30 MCX) to avoid weekend gap risks.
  4. *Daily Loss Cap:* Natural Gas blocks new entries for the day after 5 SL hits (query checks count of `CLOSED_SL` or `SL_HIT`).
