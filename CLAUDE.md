# CLAUDE.md

Guidance for Claude Code in this repo.

## Project

NSEBOT is a local NSE option-chain monitor, signal engine, Telegram digest bot, dashboard app, and paper-trading tracker.

It:

- fetches option-chain data for watched symbols
- keeps live output limited to ATM +/- 15 strikes
- detects OI, PCR, IV, price, max-pain, and candle anomalies
- builds trader-facing Telegram digests
- auto-opens and manages paper trades from bot intelligence
- stores scans, alerts, and paper trades in SQLite
- serves a FastAPI dashboard plus a paper-trading page

## Current runtime

- Scheduler interval is user-selectable from the dashboard.
- Available scan frequencies: `5m`, `15m`, `30m`, `1H`, `3H`, `1D`.
- `python main.py` runs the bot, `python main.py --now` runs a one-shot scan.
- `python dashboard_server.py` serves the dashboard.
- Paper trading page: `http://localhost:8080/paper`.

## Current behavior

- Telegram digest now includes verdict, action, key levels, candles, OI pulse, top signals, balance, and trend.
- Duplicate chart blocks were removed from the Telegram message.
- Candle values shown in Telegram are last closed candles only.
- MCX `NATURALGAS` and `CRUDEOIL` candles are sourced from Dhan built-up data and aggregated into the last closed `1H` and `3H` windows.
- `Delta prev scan` now compares against the previous scan properly instead of always reading as flat.
- Telegram send now has timeout retry plus HTTP fallback.
- Paper trades can be auto-closed on strong opposite verdicts.

## Current intelligence routing

- `NATURALGAS`: news-only intelligence
- `NIFTY` / `BANKNIFTY`: heatmap-only intelligence
- No cross-mixing between commodity news and index heatmap context

## Hard constraints

- Do not reintroduce Upstox, Paytm, or NSE commodity chain routes.
- Keep option-chain fetches within ATM +/- 15 strikes.
- Keep Telegram text clean, short, and trader-readable.
- Keep docs aligned with the FastAPI dashboard, not old Streamlit references.

## Main flow

`main.py -> scheduler -> pipeline -> fetch -> detect -> digest -> telegram -> paper trade`

## Important modules

- `src/fetchers/router.py` - source routing and ATM strike filtering
- `src/fetchers/chart_fetcher.py` - candle sourcing and aggregation
- `src/engine/pipeline.py` - orchestrates the scan
- `src/engine/anomaly_detector.py` - computes alerts and scan context
- `src/engine/intelligence.py` - verdict, trend, trade guidance
- `src/alerts/digest.py` - Telegram message builder
- `src/alerts/telegram_dispatcher.py` - Telegram delivery and retries
- `src/engine/paper_trading.py` - automatic paper trade lifecycle
- `src/models/schema.py` - SQLite tables and helpers
- `dashboard_server.py` - FastAPI dashboard API and pages

## Next session context

- Verify candles are still last-closed after any fetcher changes.
- Keep `Delta prev scan` using actual prior scan data.
- If trend work continues, expand broader intelligence with more historical scan context instead of current-scan-only heuristics.
- Validate dashboard and Telegram text after edits.
- Preserve symbol segregation in intelligence.
