# Agent Handoff

## Current state

- Bot is stable around the live dashboard, Telegram flow, and live trading.
- **Audit completed:** 15 findings identified (5 Critical, 5 High, 5 Medium). All Critical and High fixes applied.
- **LLM v2.0:** Action-oriented schema (GO_LONG/GO_SHORT/NO_TRADE) with specific trade levels.
- **Unified trade plan:** `src/engine/trade_plan.py` is single source of truth for SL/Target across paper and live.
- **Live timeframe strategy:** Fully implemented (no longer a stub).
- **Historical OI + price impact:** Fed to LLM for better context-aware decisions.
- **Chart conflict:** Soft penalty instead of hard block for core OI trades.
- **Premium staleness guard:** DB fallback rejects snapshots >15 min old.
- **Position sync:** Kite positions synced every 5 min + per scan cycle.
- **Live exit monitoring:** Premium-poll exits checked every 2 min between scans.
- MCX candles for `NATURALGAS` and `CRUDEOIL` use last-closed `1H` and `3H` windows.
- Delta prev scan uses real previous scan data.
- Telegram retries in place; HTTP fallback available.
- Paper trading page and auto-close behavior are live.
- Transaction costs (STT + brokerage) applied in paper P&L.

## Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| Unified `trade_plan.py` | Prevents paper/live SL/Target divergence |
| Chart conflict = soft penalty | Core OI trades shouldn't be blocked by timeframe disagreement |
| ATR-based breakout buffer | Fixed 0.1% was noise-level for all symbols |
| Time-weighted regime decay | Old scans shouldn't have same weight as recent ones |
| CLOSE_EARLY skip on missing LTP | Zero-P&L exits are worse than waiting one scan |
| SELL margin 12× | Matches actual SPAN+exposure requirements |
| Signal key without verdict text | Prevents duplicate trades when verdict label changes |

## Working rules

- Keep changes tight and behavior-first.
- Do not reintroduce Upstox, Paytm, or NSE commodity chain.
- Keep strike fetches limited to ATM +/- 15.
- Keep NATURALGAS intelligence separate from NIFTY/BANKNIFTY intelligence.
- Keep Telegram clean and trader-friendly.
- **All SL/Target changes must go through `trade_plan.py`** — never duplicate logic.
- Test with `pytest tests/ -v` before committing.

## Known Remaining Issues

| # | Severity | Issue | Status |
|---|----------|-------|--------|
| C2 | 🔴 CRITICAL | Live risk limits missing 4 of 6 safeguards | Deferred (user request) |
| H2 | 🟠 HIGH | Two strategies can double-trade same symbol | Not yet fixed |
| M4 | 🟡 MEDIUM | AI CLOSE_EARLY fallback = zero P&L | ✅ Fixed |
| L4 | 🔵 LOW | Lazy imports inside functions | Low priority refactor |

## Next session priorities

1. **Fix C2:** Align live risk limits with `risk_engine.py` (daily loss cap, cooldown, circuit breaker).
2. **Fix H2:** Add cross-strategy dedup to prevent double-trading same symbol.
3. Validate candle freshness after any fetcher or scheduler change.
4. Monitor `AI_VETOED` tags in logs for LLM over-conservatism.
5. Verify Telegram output on mobile-sized layout before shipping formatting changes.
6. Keep docs in sync with the live FastAPI dashboard.

## Key Files Modified This Session

- `src/engine/trade_plan.py` — NEW: unified trade planning module
- `src/engine/live_trading.py` — C1 reversal guards, C3 timeframe strategy, C4 unified SL, H1 signal key
- `src/engine/paper_trading.py` — C5 premium monitoring, M1 ATR buffer, M2 plan SL usage
- `src/engine/llm_enrichment.py` — v2.0 schema, historical OI, token optimization
- `src/engine/trade_decision.py` — Chart conflict soft penalty, action→bias mapping
- `src/engine/regime_detector.py` — M3 time-weighted decay
- `src/engine/capital_allocator.py` — M5 margin multiplier 12×
- `src/engine/pipeline.py` — M4 CLOSE_EARLY safety, L3 position sync
- `src/scheduler/job_runner.py` — H4 live exit monitoring, L3 sync timer
- `src/alerts/digest.py` — New schema rendering
- `tests/test_trade_plan.py` — NEW: trade plan tests
- `tests/test_llm_schema_v2.py` — NEW: LLM v2.0 tests
- `tests/test_audit_fixes.py` — NEW: audit fix regression tests
- `CLAUDE.md` — Updated with full architecture documentation
- `agent.md` — This file
