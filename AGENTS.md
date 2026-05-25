# Agent Rules

## Response Style

- Be concise. No filler.
- Do not restate the prompt unless ambiguity is risky.
- Prefer dense bullets, tables, or code over padded prose.
- Summarize resolved context instead of repeating history.

## Current repo shape

- Main app entry: `python main.py`
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
