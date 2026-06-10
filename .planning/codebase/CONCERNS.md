# Technical Concerns

This document details current blockers, data delays, systemic trading risks, architectural limits, and recently implemented robustness guards within the NSEBOT trading system.

---

## 1. Current Blockers & Technical Debt

* **Mock Complexity in Tests**: Tests in `tests/test_engine.py` and other verification suites are prone to fragility. The extensive mock signatures required for simulating complex database queries, underlying price transitions, and fetcher responses make tests harder to maintain.
* **NSE Rate Limiting & Blocking**: The scraper lacks proxy rotation or intelligent IP shifting. It is highly susceptible to rate-limiting or temporary IP bans from the NSE public servers if fetch frequencies are increased below the standard thresholds.
* **Lack of Dynamic Margin Checks**: The paper trading risk engine checks static count limits but lacks integration with actual or simulated margin calculators, making it blind to leverage or option writing margin requirements.

---

## 2. Dhan 15-Minute Commodity Options Delay

* **Public Cache Latency**: The `DhanCommodityFetcher` scraper parses public Dhan commodity option-chain pages and the ScanX API. Unauthenticated public feeds on Dhan are subject to caching, causing a delay of up to **15 minutes** in option premium LTP and bid/ask quotes.
* **Aggregate Built-up Intervals**: The MCX futures underlying spot/future price is fetched from Dhan's built-up endpoint (`https://openweb-ticks.dhan.co/builtup`) utilizing a static `"Timeinterval": "15"` payload parameter. This aggregates live tick data into 15-minute intervals, introducing lag for volatility-sensitive trend indicators during fast intraday moves.

---

## 3. Overnight Gap Risks for Options Writers

* **Exposures to Opening Gaps**: The paper trading engine executes options shorts (CE/PE writing). Holding written options positions overnight exposes the virtual portfolio to overnight index/commodity gaps (sudden price jumps or drops at market open).
* **Stop-Loss Evasion**: Because the system is idle and markets are closed overnight, stop-losses cannot trigger to mitigate losses. A massive gap-up or gap-down at the 09:15 market open will bypass stop-loss thresholds entirely, executing exits at opening prices that may result in catastrophic, multi-multiplier losses.

---

## 4. Lacking Automated Contract Rollovers

* **Manual Intervention Needed**: The scheduler calls `delete_expired_contracts()` on startup to clean up historical database records, but the trading engine has **no automated contract rollover mechanism**.
* **Impact on Long-running Trades**: If a timeframe-based or trend-based position is open and the option contract expires, the system does not automatically close and re-establish (roll over) the position in the next nearest weekly or monthly active expiry series. The position is simply left to expire, requiring manual database or configuration adjustments.

---

## 5. Position Sizing Limits

* **Static Lot Sizing**: Lot sizing is determined by static lot sizes defined in `config/settings.py` for each symbol (e.g., NIFTY, BANKNIFTY) rather than dynamically adjusting based on portfolio equity or volatility (e.g., ATR-based sizing).
* **Pyramiding Scaling Limits**: The timeframe strategy allows a maximum of 3 open pyramiding levels in a single direction. Sizing is capped and scaled down progressively to mitigate exposure on late entries:
  * **Level 1**: $100\%$ of default lots.
  * **Level 2**: $75\%$ of default lots.
  * **Level 3**: $50\%$ of default lots.
* **Profitability Gate**: Pyramiding entries are blocked unless the preceding trade in the sequence is currently profitable.

---

## 6. Robustness Guards (Recently Implemented)

### 6.1. Scheduler Watchdog
* **Thread-level Timeout Guard**: Built into `src/scheduler/job_runner.py` via the `run_with_timeout` utility.
* **Full Scan Watchdog**: Wraps the full scan loop in a daemon thread with a strict **300-second (5-minute)** timeout. If a fetcher or scraper hangs, the watchdog logs the failure, bypasses the hung thread, sends an automated alert via Telegram, and maintains scheduler uptime.
* **Live CMP Watchdog**: Wraps the active symbol price check in a **90-second** timeout.

### 6.2. 2026 Holiday Checks
* **Calendar Guarding**: Implemented in `config/holidays.py` to prevent useless API requests and invalid alerts during exchange holidays.
* **Full & Session Holidays**: Supports complete day closures for NSE and MCX, as well as complex partial MCX schedules:
  * **Morning Closures**: MCX is closed until 17:00 IST on partial holidays.
  * **Evening Closures**: MCX is closed after 17:00 IST on New Year's Day.

### 6.3. NSE Cookie Refresh Handshake
* **Auto-refresh & Retry**: In `src/fetchers/nse_fetcher.py`, the public scraper clears and refreshes its HTTP cookies up to 3 times before attempting any option chain fetches.
* **Automatic Reset**: If a fetch request fails or raises a network resolution exception, the internal state resets (`_session_warmed = False`), forcing a clean cookie handshake on the next loop cycle to maintain connections.

### 6.4. Scan Failure & API Fallback Router
* **Fetcher Fallbacks**: The router tries primary APIs (e.g. Dhan, Upstox) first and gracefully falls back to public NSE scraping.
* **Dhan Scraper Redundancy**: The `DhanCommodityFetcher` attempts to parse JSON out of the Next.js `__NEXT_DATA__` script tag first. If it is empty, it falls back to an HTML table parsing routine, and finally defaults to a raw ScanX POST API payload scan over nearest Julian expiries, handling connection timeouts and name resolution failures gracefully.
