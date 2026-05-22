# CLAUDE.md

Guidance for Claude Code in this repo.

## Project

NSEBOT is a local NSE option-chain monitor and paper-trading assistant. It:

- fetches option-chain data for indices and MCX commodities
- filters strikes to ATM +/- 15 only
- detects OI / PCR / IV / price anomalies
- builds Telegram digests with candles, key levels, trend, and OI pulse
- auto-triggers paper trades from bot intelligence
- stores everything in SQLite
- serves a plain FastAPI dashboard plus a paper-trading page

## Current runtime

- Scheduler interval is user-selectable from the dashboard.
- Available scan frequencies: `5m`, `15m`, `30m`, `1H`, `3H`, `1D`.
- The current dashboard server is `python dashboard_server.py`.
- The paper trading page is `http://localhost:8080/paper`.
- Dashboard now includes "Recent Intelligence" section with symbol-specific data:
  - **NATURALGAS**: TradingView 24h news feed + direction scoring
  - **NIFTY/BANKNIFTY**: ScanX heatmap (live payload from ow-scanx-analytics.dhan.co) + combined direction
  - Fallback 1H/3H sentiment generator from local underlying_price history when live chart data unavailable

## Core commands

```bash
pip install -r requirements.txt
python main.py
python main.py --now
python dashboard_server.py
pytest
```

## Main flow

`main.py -> scheduler -> pipeline -> fetch -> detect -> digest -> telegram -> paper trade`

### Important modules

- `src/fetchers/router.py` - source routing and ATM strike filtering
- `src/engine/pipeline.py` - orchestrates the scan
- `src/engine/anomaly_detector.py` - computes alerts and scan context
- `src/engine/intelligence.py` - verdict, trend, trade guidance
- `src/alerts/digest.py` - Telegram message builder
- `src/engine/paper_trading.py` - automatic paper trade lifecycle
- `src/models/schema.py` - SQLite tables and helpers
- `dashboard_server.py` - FastAPI dashboard API and pages
  - `/api/intelligence_summary` - returns symbol-specific intelligence (news for NATURALGAS, heatmap for NIFTY/BANKNIFTY)

## Data source notes

- Do not reintroduce Upstox or Paytm into the live route.
- Commodity routing currently relies on the commodity fetch path already in the repo plus Moneycontrol fallback.
- Keep the live option-chain output limited to ATM +/- 15 strikes.

## Editing rules

- Keep changes tight and behavior-first.
- Prefer existing patterns in the repo.
- Do not expand docs with stale routes or old Streamlit commands.
- If you touch Telegram formatting, verify the final text is clean on mobile and desktop.
- Symbol segregation in intelligence: NATURALGAS shows news only, NIFTY/BANKNIFTY show heatmap only. No cross-mixing.
- Browser automation validation: Use `agent-browser` with snapshot refs; ensure write permissions on `C:\Users\manve\.agent-browser`.
