# NSEBOT - Pending Tasks

- [ ] Implement lazy/safe Kite instrument caching (TTL + refresh guard) to stop SSL-failure spam
- [ ] Rate-limit resolve_instrument cache-miss warnings
- [ ] Remove synchronous instrument cache refresh from `src/engine/live_trading.py` during Kite client initialization
- [x] Re-run `python dashboard_server.py` to confirm dashboard starts cleanly without Kite instrument-fetch stack traces
