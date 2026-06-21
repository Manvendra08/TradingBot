# CLAUDE.md

Guidance for Claude Code in this repo.

## Project

NSEBOT is a local NSE option-chain monitor, signal engine, Telegram digest bot, dashboard app, paper-trading tracker, and live trading executor.

It:

- fetches option-chain data for watched symbols (NSE indices + MCX commodities)
- keeps live output limited to ATM +/- 15 strikes
- detects OI, PCR, IV, price, max-pain, and candle anomalies
- builds trader-facing Telegram digests with action-oriented AI trade plans
- auto-opens and manages paper trades from bot intelligence
- executes live trades via Zerodha Kite API with GTT SL/Target
- stores scans, alerts, paper trades, and live trades in SQLite
- serves a FastAPI dashboard plus a paper-trading page
- syncs broker positions every 5 minutes

## Current runtime

- Scheduler interval is user-selectable from the dashboard.
- Available scan frequencies: `5m`, `15m`, `30m`, `1H`, `3H`, `1D`.
- `python main.py` runs the bot, `python main.py --now` runs a one-shot scan.
- `python dashboard_server.py` serves the dashboard.
- Paper trading page: `http://localhost:8080/paper`.

## LLM Provider Stack

- **Primary:** OpenRouter (`openrouter/free`) — `https://openrouter.ai/api/v1/chat/completions`
- **Fallback 1:** Gemini (`gemini-2.5-flash` / `gemini-2.0-flash`) via Google GenAI SDK
- **Fallback 2:** Groq (`llama-3.3-70b-versatile` / `llama-3.1-8b-instant`) via OpenAI-compatible API
- Default timeout: 30 seconds per provider
- Array-wrapped JSON responses are automatically unwrapped

## LLM Schema (v2.0 — Action-Oriented)

The LLM returns structured trade plans with these fields:
- `action`: GO_LONG / GO_SHORT / NO_TRADE
- `instrument`: Exact contract (e.g., "NIFTY 24500 CE 27Jun")
- `entry_trigger`: Specific entry condition
- `stop_loss`, `target_1`, `target_2`: Concrete levels
- `risk_reward`, `thesis`, `invalidation`, `risk_rating`, `catalyst`

AI decision modes: `advisory` (info only), `boost_only` (promote blocked trades), `full` (can veto approved trades).

## Trade Planning Architecture

- **Unified module:** `src/engine/trade_plan.py` — single source of truth for SL/Target, premium resolution, verdict parsing
- **ATR-based SL/Target:** Uses ATR(14) × 1.5 for SL, × 2.0 for Target on underlying
- **Premium staleness guard:** DB fallback rejects snapshots older than 15 minutes
- **Breakout buffer:** `max(ATR_14 * 0.5, underlying * 0.003)` — ATR-proportional, not fixed 0.1%
- **MCX liquidity check:** Options used only if volume ≥ 500 AND OI ≥ 2000; otherwise FUT

## Risk & Decision Engine

- **Chart conflict:** Soft penalty (-20 entry quality) instead of hard block for core OI trades
- **Historical OI trend:** Last 10 scans with PCR trend, OI trend, price impact analysis fed to LLM
- **Regime detector:** Time-weighted decay using `√(index_w × time_w)` — old scans have lower weight
- **Live reversal guard:** 3 guards matching paper (confidence ≥ 75, entry_quality ≥ 60, trend_alignment ≤ 40)
- **Paper premium monitoring:** `monitor_paper_trades()` checks both underlying AND premium SL/Target
- **Signal key dedup:** Live and paper use same format `{symbol}:{option_type}:{strike}:{date}`
- **CLOSE_EARLY safety:** Skipped if current LTP unavailable (no zero-P&L exits)
- **SELL margin multiplier:** 12× (increased from 10× to match actual SPAN+exposure)

## Live Trading

- **Timeframe strategy:** Fully implemented (not a stub) — 3H breakout entries, 1H crossover exits
- **Position sync:** `sync_direct_kite_positions()` runs every 5 min + on every scan cycle
- **Exit monitoring:** `_check_live_exits()` runs every 2 min for premium-poll trades
- **SL/Target unified:** Same ATR-based calculation as paper via `trade_plan.py`

## Current behavior

- Telegram digest includes action-oriented AI trade plan with specific levels
- Duplicate chart blocks removed from Telegram messages
- Candle values shown are last closed candles only
- MCX `NATURALGAS` and `CRUDEOIL` candles sourced from Dhan built-up data
- `Delta prev scan` compares against actual previous scan
- Telegram send has timeout retry plus HTTP fallback
- Paper trades auto-close on strong opposite verdicts
- Transaction costs (STT + brokerage) applied in paper P&L

## Intelligence routing

- `NATURALGAS`: news-only intelligence
- `NIFTY` / `BANKNIFTY`: heatmap-only intelligence
- No cross-mixing between commodity news and index heatmap context
- EIA report awareness built into macro context (CRUDEOIL Wed 8PM, NATURALGAS Thu 8:30PM IST)

## Hard constraints

- Do not reintroduce Upstox, Paytm, or NSE commodity chain routes.
- Keep option-chain fetches within ATM +/- 15 strikes.
- Keep Telegram text clean, short, and trader-readable.
- Keep docs aligned with the FastAPI dashboard, not old Streamlit references.
- All SL/Target changes must go through `trade_plan.py` — never duplicate logic.

## Main flow

`main.py -> scheduler -> pipeline -> fetch -> detect -> digest -> telegram -> paper/live trade`

## Important modules

- `src/fetchers/router.py` - source routing and ATM strike filtering
- `src/fetchers/chart_fetcher.py` - candle sourcing and aggregation
- `src/engine/pipeline.py` - orchestrates the scan (includes fetch-failure alerts, CLOSE_EARLY safety)
- `src/engine/anomaly_detector.py` - computes alerts and scan context
- `src/engine/intelligence.py` - verdict, trend, trade guidance (confidence caps consolidated)
- `src/engine/trade_plan.py` - **unified** SL/Target, premium resolution, verdict parsing
- `src/engine/trade_decision.py` - trade decision with AI bias mapping, chart conflict soft penalty
- `src/engine/llm_enrichment.py` - LLM v2.0 action-oriented schema, historical OI, macro context
- `src/engine/live_trading.py` - live execution with timeframe strategy, reversal guards, position sync
- `src/engine/paper_trading.py` - paper trade lifecycle with premium monitoring
- `src/engine/regime_detector.py` - time-weighted regime detection
- `src/engine/capital_allocator.py` - position sizing with 12× SELL margin
- `src/alerts/digest.py` - Telegram message builder (new schema aware)
- `src/alerts/telegram_dispatcher.py` - Telegram delivery and retries
- `src/models/schema.py` - SQLite tables and helpers (transaction costs applied)
- `src/scheduler/job_runner.py` - scheduler loop, live exit monitoring, position sync timer
- `dashboard_server.py` - FastAPI dashboard API and pages
- `config/holidays.py` - 2026 Indian market holiday calendar (NSE & MCX)

## Testing

Run all tests: `pytest tests/ -v`

Key test files:
- `tests/test_trade_plan.py` - unified trade plan module (C4)
- `tests/test_llm_schema_v2.py` - new LLM schema, historical OI, action→bias mapping
- `tests/test_audit_fixes.py` - C1, C3, C5, H1, H4, M1-M5 regression tests
- `tests/test_core_engine_coverage.py` - regime, entry quality, risk, trend, trade decision
- `tests/test_live_trading_p0.py` - live trading critical paths
- `tests/test_timeframe_strategy.py` - timeframe strategy entries/exits/pyramiding

## Next session context

- Verify candles are still last-closed after any fetcher changes.
- Keep `Delta prev scan` using actual prior scan data.
- Validate dashboard and Telegram text after edits.
- Preserve symbol segregation in intelligence.
- Monitor `AI_VETOED` soft_conflict tags in logs for overly conservative LLM behavior.
- All trade plan changes must go through `trade_plan.py`.
