# Agent Rules

## Response Style

- Be concise. No filler.
- Do not restate the prompt unless ambiguity is risky.
- Prefer dense bullets, tables, or code over padded prose.
- Summarize resolved context instead of repeating history.

## Current repo shape

- Main app entry: `python main.py`
- One-shot scan: `python main.py --now`
- Dashboard server: `python dashboard_server.py`
- Paper trading page: `http://localhost:8080/paper`
- Active scan interval options: `5m`, `15m`, `30m`, `1H`, `3H`, `1D`
- Live option-chain output is limited to ATM +/- 15 strikes
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

## AI / LLM state (v3.0)

- `AI_DECISION_MODE = boost_only` (default in `settings.py`; override via env `AI_DECISION_MODE`)
- LLM enrichment is **engine-aligned**: OI engine decides direction; LLM provides execution detail only
- Direction inversion guard: `_enforce_engine_alignment()` in `llm_enrichment.py` — no model can flip the engine's directional call
- Entry advisor is skipped when a position is already open; only exit advisor runs
- Chart conflict (1H vs 3H): NO penalty for OI-based trades; a 1H opposing a completed 3H = entry timing signal
- MCX confidence floor: 72% for NATURALGAS/CRUDEOIL/GOLD/SILVER (vs 70% NSE)
- JSON parse hardening: `_extract_json()` handles fences, prose wrappers, control chars — eliminates per-cycle OpenRouter parse failures
- HOLDING alerts now show position age (`entered 47m ago`) to disambiguate from new signals

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

