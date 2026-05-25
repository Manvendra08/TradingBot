# Agent Handoff

## Current state

- Bot is stable around the live dashboard and Telegram flow.
- MCX candles for `NATURALGAS` and `CRUDEOIL` use last-closed `1H` and `3H` windows.
- Delta prev scan is fixed to use real previous scan data.
- Telegram retries are in place; HTTP fallback is available.
- Paper trading page and auto-close behavior are live.

## Working rules

- Keep changes tight and behavior-first.
- Do not reintroduce Upstox, Paytm, or NSE commodity chain.
- Keep strike fetches limited to ATM +/- 15.
- Keep NATURALGAS intelligence separate from NIFTY/BANKNIFTY intelligence.
- Keep Telegram clean and trader-friendly.

## Next session

1. Validate candle freshness after any fetcher or scheduler change.
2. Improve broader trend intelligence using more historical scan data if needed.
3. Verify Telegram output on mobile-sized layout before shipping any formatting change.
4. Keep docs in sync with the live FastAPI dashboard.
