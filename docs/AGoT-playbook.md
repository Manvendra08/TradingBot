# NSEBOT: Adaptive Graph of Thoughts (AGoT) Playbook

## 1. Introduction to AGoT in NSEBOT
The **Adaptive Graph of Thoughts (AGoT)** is a reasoning framework used to analyze the NSEBOT codebase. Instead of linear thinking, AGoT decomposes the architecture into a graph of interconnected nodes (modules), evaluates design trade-offs, and identifies failure modes. This document serves as the primary guide for future AI sessions to maintain and optimize the bot.

---

## 2. The Reasoning Graph: Core Nodes

| Node | Module | Function | Key Trade-off |
| :--- | :--- | :--- | :--- |
| **Ingestion** | `fetchers/router.py` | Multi-source data fallback chain. | **Resilience vs. Complexity:** More sources mean higher uptime but harder debugging. |
| **Detection** | `anomaly_detector.py` | Identifies OI/Price spikes and buildups. | **Sensitivity vs. Noise:** Tighter thresholds catch more moves but increase false positives. |
| **Intelligence** | `intelligence.py` | Generates verdicts and confidence scores. | **Aggressiveness vs. Safety:** Higher confidence floors reduce trades but improve quality. |
| **Decision** | `trade_decision.py` | Applies regime and trend filters. | **Opportunity vs. Risk:** "Hybrid" mode balances momentum with reversal detection. |
| **Risk** | `risk_engine.py` | Enforces daily limits and cooldowns. | **Protection vs. Flexibility:** Strict caps prevent blow-ups but may block recovery trades. |
| **Execution** | `live_trading.py` | Broker interaction and order management. | **Speed vs. Reliability:** GTT orders are reliable but less flexible than manual polling. |

---

## 3. Decision Trees for Common Problems

### 🛑 Trade Not Firing?
1.  **Check Regime:** Is `detect_market_regime()` returning `NO_TRADE`? (Requires 5+ scans).
2.  **Check Confidence:** Is the score below `MIN_CONFIDENCE_CORE` (70)?
3.  **Check Entry Quality:** Is the R:R ratio poor or the bid-ask spread too wide?
4.  **Check AI Veto:** If in `full` mode, did the LLM disagree with high confidence?

### ⚠️ Unexpected Trade Closure?
1.  **Check SL/Target:** Did the underlying price hit the calculated level?
2.  **Check Reversal Guard:** Did a new signal with >75% confidence contradict the open trade?
3.  **Check Circuit Breaker:** Were there 3 consecutive losses in the last 30 minutes?
4.  **Check AI Exit Advisor:** Did the LLM suggest an early close due to thesis breakdown?

### 📉 Data Quality Issues?
1.  **Check Fetcher Source:** Which source provided the data? (Shoonya is most reliable for charts).
2.  **Check Fallback Flag:** Is `is_fallback=True` in the scan summary? (Indicates stale price usage).
3.  **Check DOM Bridge:** If using the Chrome extension, has the TradingView/Dhan UI changed?

---

## 4. Session Startup Checklist
For every new AI session working on NSEBOT:
1.  [ ] Read `docs/ARCHITECTURE.md` for system structure.
2.  [ ] Read `docs/TRADING_STRATEGY.md` for trading logic.
3.  [ ] Check `config/settings.py` for current thresholds.
4.  [ ] Verify `data/nsebot.db` is the active database.
5.  [ ] Ensure only one `.venv` folder exists in the root.

---

## 5. Debugging Guide

### Log Analysis
*   **`[engine]`**: Look for "Trade blocked" or "Entry quality LOW" messages.
*   **`[llm]`**: Check for "Circuit breaker OPEN" or "Quota exhausted" warnings.
*   **`[chart]`**: Monitor for "tvdatafeed init failed" or "pure-HTTP Yahoo Finance query failed".

### Database Queries for Diagnosis
```sql
-- Check recent scan summaries for a symbol
SELECT * FROM scan_summaries WHERE symbol='NIFTY' ORDER BY fetched_at DESC LIMIT 5;

-- Check why a trade was blocked
SELECT * FROM paper_trades WHERE status='BLOCKED' ORDER BY opened_at DESC LIMIT 5;

-- Check fetcher reliability
SELECT fetcher_source, COUNT(*) FROM option_chain_snapshots GROUP BY fetcher_source;
```

---

## 6. Key Thresholds & Locations

| Parameter | Default Value | File Location |
| :--- | :--- | :--- |
| `MIN_CONFIDENCE_CORE` | 70 | `config/settings.py` |
| `MAX_DAILY_LOSS_RUPEES` | 200,000 | `config/settings.py` |
| `TREND_FILTER_MODE` | "hybrid" | `config/settings.py` |
| `REVERSAL_MIN_CONFIDENCE` | 75 | `config/settings.py` |
| `AI_DECISION_MODE` | "boost_only" | `config/settings.py` |

---

## 7. Future Development Roadmap
*   **Phase 5:** Automated MCX contract rollover (currently requires manual ID updates).
*   **Phase 6:** Integration of multi-timeframe chart patterns into the LLM prompt.
*   **Phase 7:** Migration from DOM scraping to 100% API-based data ingestion.
