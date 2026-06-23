# NSEBOT AGoT Playbook — Reasoning Framework for AI Sessions

> **Generated:** June 22, 2026 | **Method:** Adaptive Graph of Thoughts (AGoT)
> **Purpose:** Reasoning framework, decision trees, and debugging guide for future AI sessions.
> **⚠️ READ THIS FIRST in every new session.**

---

## 1. Session Startup Checklist

Before making any changes, complete this checklist:

```
□ Read docs/AGoT-playbook.md (this file)
□ Read docs/architecture.md (system structure)
□ Read docs/order-flow.md (signal → execution path)
□ Read docs/strategies/options-engine.md (options logic)
□ Check config/settings.py (all configurable thresholds)
□ Check data/runtime_config.json (runtime toggles)
□ Review tests/ directory (expected behavior)
□ Check logs/main.log (recent activity)
□ Check .gitignore (what's tracked)
```

---

## 2. AGoT Reasoning Framework

### The 5-Step AGoT Process

When approaching any NSEBOT problem, follow this reasoning graph:

```
Step 1: DECOMPOSE
    │   Break the problem into reasoning nodes
    │   Each node = one module or concern
    │
    ▼
Step 2: MAP DEPENDENCIES
    │   Draw edges between nodes
    │   Identify data flow direction
    │   Mark sync/async boundaries
    │
    ▼
Step 3: EVALUATE TRADE-OFFS
    │   For each node, list pros/cons of current design
    │   Identify what was optimized for
    │   Identify what was sacrificed
    │
    ▼
Step 4: IDENTIFY FAILURE MODES
    │   For each edge, ask: what if this fails?
    │   Check error handling paths
    │   Verify fallback mechanisms
    │
    ▼
Step 5: PROPOSE CHANGES
        Make minimal, targeted changes
        Preserve existing trade-offs unless explicitly changing them
        Add tests for new behavior
```

### Applying AGoT to Common Tasks

#### Task: "Fix a bug in trade execution"
```
1. DECOMPOSE: pipeline → trade_decision → risk_engine → paper_trading → schema
2. MAP: trace the exact data flow from signal to DB insert
3. TRADE-OFFS: is the bug caused by an intentional trade-off?
   (e.g., aggressive mode fires more trades but with lower quality)
4. FAILURE MODES: check each node's error handling
   (e.g., what if premium is None? what if risk check fails?)
5. PROPOSE: fix the specific node, add regression test
```

#### Task: "Add a new strategy"
```
1. DECOMPOSE: new strategy node + integration points
2. MAP: where does it fit in the pipeline?
   (before/after existing strategies? parallel or sequential?)
3. TRADE-OFFS: does it conflict with existing strategies?
   (e.g., new strategy might fire when existing one is blocked)
4. FAILURE MODES: what if the new strategy fails?
   (does it block the pipeline or fail gracefully?)
5. PROPOSE: add as a new node, integrate via pipeline.py
```

#### Task: "Optimize performance"
```
1. DECOMPOSE: identify the slow node (fetcher? LLM? DB?)
2. MAP: is it on the critical path?
3. TRADE-OFFS: what does optimization sacrifice?
   (e.g., caching improves speed but risks stale data)
4. FAILURE MODES: does optimization introduce new failure modes?
5. PROPOSE: targeted optimization with monitoring
```

---

## 3. Decision Trees for Common Problems

### Tree 1: Trade Not Firing

```
Trade not placed?
    │
    ├── Is pipeline running?
    │   ├── NO → Check scheduler (src/scheduler/job_runner.py)
    │   │        Check market hours (config/settings.py MARKET_WINDOWS)
    │   │        Check holidays (config/holidays.py)
    │   │
    │   └── YES → Continue below
    │
    ├── Is data being fetched?
    │   ├── ALL fetchers failing → Check network/API keys
    │   │                          Check FETCHER_PRIORITY in settings
    │   │                          Check logs for fetcher errors
    │   │
    │   └── Data OK → Continue below
    │
    ├── Are anomalies being detected?
    │   ├── NO anomalies → Thresholds too high?
    │   │                  Check SYMBOL_THRESHOLD_OVERRIDES
    │   │                  Check if market is flat (no OI changes)
    │   │
    │   └── Anomalies detected → Continue below
    │
    ├── Is trade decision TRIGGERED?
    │   ├── BLOCKED → Check block reason in logs
    │   │             Common blocks:
    │   │             - "Insufficient scan history" → Wait for TREND_MIN_SCANS
    │   │             - "Entry quality insufficient" → Lower thresholds (carefully)
    │   │             - "Unfavorable regime" → Check regime_detector
    │   │             - "AI VETO" → AI disagrees with trade
    │   │
    │   └── TRIGGERED → Continue below
    │
    ├── Is risk check passing?
    │   ├── BLOCKED_RISK → Check which limit is hit:
    │   │                  - Max open trades → Close some positions
    │   │                  - Daily loss cap → Wait for next day
    │   │                  - Loss cooldown → Wait for cooldown
    │   │                  - Consecutive losses → Circuit breaker active
    │   │
    │   └── Risk OK → Continue below
    │
    └── Is execution succeeding?
        ├── BLOCKED_PLAN → No valid plan (premium unavailable?)
        ├── BLOCKED_LOTS → Zero lots calculated
        └── Check paper_trading.execute_paper_trade() for errors
```

### Tree 2: Trade Closed Unexpectedly

```
Trade closed unexpectedly?
    │
    ├── Check exit_reason in DB
    │   ├── CLOSED_SL → Stop loss was hit
    │   │               Was SL too tight?
    │   │               Was it a premium SL or underlying SL?
    │   │
    │   ├── CLOSED_TARGET → Target was hit (this is good!)
    │   │
    │   ├── CLOSED_REVERSAL → Reversal signal fired
    │   │                     Check reversal guards:
    │   │                     - confidence >= 75?
    │   │                     - entry_quality >= 60?
    │   │                     - trend_alignment <= 40?
    │   │
    │   ├── TF-1H-Cross → Timeframe exit (1H crossover)
    │   │                 Was the crossover size > 2x buffer?
    │   │                 Was OI bias confirming?
    │   │
    │   ├── Dead Trade → 3 hours passed, max_R < 0.5
    │   │                Was the trade actually dead?
    │   │                Consider increasing time window
    │   │
    │   ├── LLM_REVERSAL → AI exit advisor closed trade
    │   │                  Check AI confidence threshold
    │   │                  Check if AI bias actually contradicted
    │   │
    │   └── AI_CLOSE_EARLY → AI exit advisor with HIGH urgency
    │                        Was LTP available? (FIX #9)
    │                        Was the reasoning valid?
    │
    └── Check if the exit was correct given market conditions
```

### Tree 3: Data Quality Issues

```
Data quality problem?
    │
    ├── Underlying price is None or stale
    │   ├── All fetchers returning None → API down?
    │   ├── is_fallback=True → Using prev_price
    │   │                     regime_detector excludes these rows
    │   │                     Check why current price unavailable
    │   │
    │   └── Price seems wrong → Check symbol mapping
    │                          Check strike step size
    │                          Check if market is open
    │
    ├── Option chain is empty or zero-filled
    │   ├── Fetcher returned empty → Try next fetcher
    │   ├── ATM filter removed all strikes → Increase STRIKES_AROUND_ATM
    │   └── Market closed → Normal behavior outside hours
    │
    ├── Premium is None for a strike
    │   ├── Strike not in option chain → Filtered out by ATM filter
    │   ├── LTP is 0 → Illiquid strike
    │   └── Strike too far from ATM → MAX_LEVEL_DISTANCE_STEPS fallback
    │
    └── Chart data missing
        ├── chart_fetcher crashed → Check logs
        ├── Server-side fetch failed → Network issue
        └── No chart data for symbol → Not all symbols have charts
```

---

## 4. Trade-off Evaluation Matrix

### How to Evaluate Design Changes

Before making any change, evaluate its impact on these dimensions:

| Dimension | Questions to Ask | Weight |
|-----------|-----------------|--------|
| **Correctness** | Does this change produce correct results? | CRITICAL |
| **Safety** | Could this cause financial loss? | CRITICAL |
| **Reliability** | Will this work under all conditions? | HIGH |
| **Performance** | Does this slow down the pipeline? | MEDIUM |
| **Maintainability** | Is this easy to understand and modify? | MEDIUM |
| **Testability** | Can we write tests for this? | MEDIUM |
| **Simplicity** | Is this the simplest solution? | LOW |

### Trade-off Patterns in NSEBOT

#### Pattern 1: Strictness vs Opportunity
```
STRICT (current default):
  + Protects capital
  + Fewer false positives
  - Misses some good trades
  - Lower trade frequency

RELAXED:
  + More trades
  + Captures marginal opportunities
  - Higher drawdown risk
  - More false positives

RECOMMENDATION: Keep strict defaults. Use PAPER_RESEARCH_MODE
for relaxed testing. Never relax live trading without extensive
paper trading validation.
```

#### Pattern 2: AI Integration Depth
```
ADVISORY (current default):
  + Safe — AI never blocks trades
  + Good for building trust in AI
  - Misses AI-driven improvements

BOOST_ONLY:
  + Captures marginal setups
  + AI promotes but never vetoes
  - May promote weak trades

FULL:
  + AI can prevent bad trades
  + Maximum AI utilization
  - Over-reliance on AI
  - False vetoes possible

RECOMMENDATION: Start with ADVISORY. Move to BOOST_ONLY only
after 30+ days of paper trading with AI tracking. FULL mode
requires extensive validation and should only be used with
high-confidence AI models.
```

#### Pattern 3: Multi-Source Data Ingestion
```
SINGLE SOURCE:
  + Simple
  + Easy to debug
  - Single point of failure

MULTI-SOURCE (current):
  + Resilient — fallback when primary fails
  + Can compare data quality across sources
  - Complex — multiple failure modes
  - Maintenance burden — each fetcher needs updates

RECOMMENDATION: Keep multi-source for production. For
development/testing, single source is fine. Deprecate
DOM scraping (Chrome extension) in favor of APIs.
```

#### Pattern 4: Synchronous vs Asynchronous Pipeline
```
SYNCHRONOUS (current):
  + Simple to reason about
  + Easy to debug
  + No race conditions
  - Slow — each symbol processed sequentially
  - One slow fetcher blocks everything

ASYNCHRONOUS:
  + Fast — parallel processing
  + Better resource utilization
  - Complex — race conditions, deadlocks
  - Harder to debug
  - Need proper error handling per task

RECOMMENDATION: Keep synchronous for now. If performance
becomes an issue, consider parallelizing across symbols
(not within a symbol's pipeline).
```

---

## 5. Known Failure Modes & Mitigations

### Failure Mode 1: Fetcher Cascade Failure
```
SYMPTOM: All fetchers fail simultaneously
CAUSE: Network outage, API rate limiting, or upstream service down
MITIGATION:
  - Each fetcher has independent timeout (HTTP_TIMEOUT_SECONDS = 15)
  - Router tries all sources before giving up
  - Telegram alert sent: "ALL data fetchers failed"
  - Pipeline continues with next symbol
RECOVERY: Wait for network/API restoration. Check logs for specific errors.
```

### Failure Mode 2: Stale Price Fallback
```
SYMPTOM: underlying_price uses prev_price, is_fallback=True
CAUSE: Current price unavailable from all fetchers
IMPACT: regime_detector excludes this row from trend analysis
MITIGATION:
  - is_fallback flag prevents stale data from corrupting trend
  - Telegram alert includes fallback warning
  - Next scan will attempt fresh fetch
RECOVERY: Usually resolves on next scan. If persistent, check fetcher health.
```

### Failure Mode 3: Premium Unavailable
```
SYMPTOM: "BLOCKED_PLAN: Option premium unavailable for CE strike X"
CAUSE: Option chain doesn't include the selected strike
IMPACT: Trade cannot be placed
MITIGATION:
  - Strike selection uses MAX_LEVEL_DISTANCE_STEPS fallback to ATM
  - If premium still unavailable, trade is blocked (safe)
  - Log warning for visibility
RECOVERY: Increase STRIKES_AROUND_ATM or check if strike is valid.
```

### Failure Mode 4: AI Veto False Positive
```
SYMPTOM: Valid trade blocked by AI veto
CAUSE: AI model disagrees with rule-based verdict, confidence >= 85
IMPACT: Missed trading opportunity
MITIGATION:
  - AI_DECISION_MODE defaults to "advisory" (no veto)
  - Veto only fires in "full" mode with high confidence
  - AI_MIN_CONFIDENCE_VETO = 85 (high bar)
RECOVERY: Switch to "advisory" or "boost_only" mode. Retrain AI model.
```

### Failure Mode 5: Consecutive Loss Circuit Breaker
```
SYMPTOM: All new trades blocked across all symbols
CAUSE: 3+ losing trades in 30 minutes
IMPACT: Trading halted until window clears
MITIGATION:
  - Protects against tilt and revenge trading
  - Rolling window moves past losses after 30 minutes
  - Telegram alert should be sent (check dispatcher)
RECOVERY: Wait for window to clear. Review losing trades for pattern.
```

### Failure Mode 6: MCX Contract Rollover
```
SYMPTOM: MCX fetcher returns empty or wrong data
CAUSE: Monthly contract expired, DHAN_SECURITY_IDS not updated
IMPACT: No MCX trades until IDs updated
MITIGATION:
  - FIX #15 comment in settings.py reminds to update
  - DHAN_FALLBACK_EXPIRIES provides temporary fallback
  - Monthly maintenance task should update IDs
RECOVERY: Download Dhan instrument master, update DHAN_SECURITY_IDS.
```

### Failure Mode 7: Zerodha IP Whitelist
```
SYMPTOM: KiteConnect fails with "unauthorized IP" error
CAUSE: Public IP not whitelisted on Zerodha developer console
IMPACT: Live trading cannot execute orders
MITIGATION:
  - _handle_kite_ip_error() auto-detects public IP
  - Logs detailed instructions for IP whitelisting
  - Paper trading continues unaffected
RECOVERY: Whitelist IP on Zerodha developer console (free, 1 change/week).
```

### Failure Mode 8: Dead Trade Exit Premature
```
SYMPTOM: Trade closed after 3 hours with small profit
CAUSE: max_favorable_R < 0.5 threshold too aggressive
IMPACT: Exits potentially profitable trades early
MITIGATION:
  - 3-hour window is conservative
  - 0.5R threshold catches truly dead trades
  - Only fires if trade hasn't moved meaningfully
RECOVERY: Increase time window or R threshold if too many premature exits.
```

---

## 6. Debugging Guide

### Log Analysis
```bash
# Recent pipeline runs
grep "Pipeline run" logs/main.log | tail -5

# Blocked trades
grep "BLOCKED" logs/main.log | tail -20

# Fetcher failures
grep "fetcher.*failed\|ALL fetchers" logs/main.log | tail -10

# AI verdicts
grep "AI verdict" logs/main.log | tail -10

# Risk blocks
grep "circuit breaker\|risk.*limit\|cooldown" logs/main.log | tail -10

# Trade executions
grep "paper trade.*opened\|paper trade.*closed" logs/main.log | tail -20
```

### Database Queries
```sql
-- Open trades
SELECT * FROM paper_trades WHERE status = 'OPEN';

-- Recent trades
SELECT * FROM paper_trades ORDER BY opened_at DESC LIMIT 10;

-- Scan history
SELECT symbol, fetched_at, underlying, is_fallback
FROM scan_summaries WHERE symbol = 'NIFTY'
ORDER BY fetched_at DESC LIMIT 10;

-- Daily P&L
SELECT DATE(closed_at) as day, SUM(pnl_rupees) as pnl
FROM paper_trades WHERE status != 'OPEN'
GROUP BY DATE(closed_at) ORDER BY day DESC;

-- Win rate
SELECT
    COUNT(*) as total,
    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
    ROUND(100.0 * SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate
FROM paper_trades WHERE status != 'OPEN';
```

### Runtime Config Changes
```bash
# Enable live trading
# Edit data/runtime_config.json:
{
    "live_trading_enabled": true,
    "live_ai_decision_mode": "advisory"
}

# Disable LLM enrichment (save API quota)
# Set in .env:
DISABLE_LLM_ENRICHMENT=true

# Enable research mode (allow experimental trades)
# Set in .env:
PAPER_RESEARCH_MODE=true
```

---

## 7. Testing Strategy

### Test Priorities
1. **P0 (Critical):** `test_live_trading_p0.py` — Must pass before any live trading
2. **P1 (Important):** `test_risk_metrics.py`, `test_engine.py` — Core logic
3. **P2 (Standard):** `test_entry_quality.py`, `test_trade_plan.py` — Strategy logic
4. **P3 (Nice-to-have):** `test_formatting.py`, `test_telegram_formatter.py` — Display

### Running Tests
```bash
# All tests
pytest tests/

# Specific test file
pytest tests/test_live_trading_p0.py -v

# With coverage
pytest tests/ --cov=src --cov-report=html

# Fast (skip slow tests)
pytest tests/ -m "not slow"
```

### Adding New Tests
```python
# 1. Create test file: tests/test_your_feature.py
# 2. Use pytest fixtures from conftest.py
# 3. Test both happy path and error cases
# 4. Mock external dependencies (API calls, DB)
# 5. Add to CI pipeline
```

---

## 8. Common Code Patterns

### Adding a New Fetcher
```python
# 1. Create src/fetchers/my_fetcher.py
class MyFetcher(BaseFetcher):
    def fetch_option_chain(self, symbol: str) -> dict | None:
        # Fetch data
        # Normalize to standard schema
        # Return dict with: symbol, underlying_price, expiry, strikes[]
        pass

# 2. Register in src/fetchers/router.py
_FETCHERS["my_fetcher"] = MyFetcher

# 3. Add to FETCHER_PRIORITY in config/settings.py
FETCHER_PRIORITY = ["nse_public", "my_fetcher", ...]

# 4. Add tests in tests/test_my_fetcher.py
```

### Adding a New Strategy
```python
# 1. Create strategy logic in src/engine/my_strategy.py
def run_my_strategy(symbol, scan_context, ...) -> dict | None:
    # Entry/exit logic
    # Return {"action": "EXECUTED", "trade": {...}} or None
    pass

# 2. Integrate in src/engine/pipeline.py
# After run_paper_trading() and run_timeframe_strategy():
my_report = run_my_strategy(symbol, scan_context, digest_id, intel)

# 3. Add to digest building
# 4. Add tests in tests/test_my_strategy.py
```

### Adding a New Risk Check
```python
# 1. Add check in src/engine/risk_engine.py
# Inside _check_risk_limits_for_table():
# 7. My new check
my_check_ok, my_reason = _check_my_limit(conn, trades_table, label)
if not my_check_ok:
    return False, my_reason

# 2. Add configuration in config/settings.py
MY_NEW_LIMIT = int(os.environ.get("MY_NEW_LIMIT", "10"))

# 3. Add tests in tests/test_risk_metrics.py
```

---

## 9. Session Handoff Template

When ending a session, document your work using this template:

```markdown
## Session Summary — [Date]

### Changes Made
- File: [path] — [description]
- File: [path] — [description]

### Tests Added/Modified
- test_xxx.py — [what it tests]

### Known Issues
- [issue description + workaround]

### Next Steps
- [what should be done next]

### Configuration Changes
- [any .env or runtime_config changes]

### AGoT Notes
- [any trade-off decisions made and why]
- [any failure modes discovered]
```

---

## 10. Quick Reference Card

### Key Files
| File | Purpose |
|------|---------|
| `src/engine/pipeline.py` | Main orchestration |
| `src/engine/trade_decision.py` | Trade scoring |
| `src/engine/risk_engine.py` | Risk checks |
| `src/engine/paper_trading.py` | Paper execution |
| `src/engine/live_trading.py` | Live execution |
| `src/engine/paper_plan.py` | Strike selection |
| `src/engine/trade_plan.py` | SL/Target calculation |
| `src/fetchers/router.py` | Data fetching |
| `config/settings.py` | All thresholds |
| `data/runtime_config.json` | Runtime toggles |

### Key Thresholds
| Parameter | Value | Location |
|-----------|-------|----------|
| Min Confidence (CORE) | 70% | settings.py |
| Min Entry Quality (CORE) | 60/100 | settings.py |
| Reversal Min Confidence | 75% | settings.py |
| Max Open Trades | 5 | settings.py |
| Daily Loss Cap | ₹200,000 | settings.py |
| Loss Cooldown | 30 min | settings.py |
| Consecutive Loss Limit | 3 in 30 min | risk_engine.py |
| Scan Frequency | 5 min | runtime_config |
| STRIKES_AROUND_ATM | 10 | settings.py |
| AI Decision Mode | advisory | settings.py |

### Runtime Toggles
| Toggle | Effect |
|--------|--------|
| `live_trading_enabled` | Enable real orders |
| `live_ai_decision_mode` | AI influence (advisory/boost_only/full) |
| `live_ai_exit_advisor_enabled` | AI exit advice |
| `scan_frequency_nse` | NSE scan interval |
| `scan_frequency_mcx` | MCX scan interval |

---

**Remember:** When in doubt, choose the safer option. Paper trading exists for a reason. Always test changes in paper mode before enabling live trading.

**Last Updated:** June 22, 2026
**Framework:** Adaptive Graph of Thoughts (AGoT)
**Version:** 1.0
