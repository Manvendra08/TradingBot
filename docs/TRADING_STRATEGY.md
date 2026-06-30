# NSEBOT Trading Strategy Overview

## 1. Core Logic: The Price × OI Matrix
The bot's primary signal generation is based on the relationship between price movement and Open Interest (OI) changes:

| Price | OI | Verdict | Interpretation |
| :--- | :--- | :--- | :--- |
| **Up** | **Up** | Long Buildup | Bullish — Fresh longs entering the market. |
| **Down** | **Up** | Short Buildup | Bearish — Fresh shorts entering the market. |
| **Up** | **Down** | Short Covering | Weak Bullish — Shorts exiting, not fresh buying. |
| **Down** | **Down** | Long Unwinding | Weak Bearish — Longs exiting, not aggressive selling. |

---

## 2. Risk Management Framework

### Entry Quality Scoring
Before any trade is executed, `entry_quality.py` calculates a score (0-100) based on:
*   **Price Location:** Is the underlying near support or resistance?
*   **Risk-Reward Ratio:** Is the target at least as far as the stop-loss?
*   **Liquidity:** Is the bid-ask spread within acceptable limits (<5%)?
*   **Chasing Penalty:** Deductions for entering after a large move (>1.5%).

### Stop-Loss & Target Calculation
*   **Options:** SL and Targets are calculated in premium terms but monitored via underlying price levels to account for time decay (Theta).
*   **Futures:** Uses ATR-based dynamic stops (1.5x ATR) with a minimum floor of 0.3% of the underlying price.

### Circuit Breakers
*   **Consecutive Losses:** Halts all trading if 3 losses occur within 30 minutes across any symbol.
*   **Daily Loss Cap:** Stops trading once the realized loss hits ₹200,000.
*   **Kill Switch:** An immediate global halt toggleable via the dashboard.

---

## 3. Execution Modes

### Paper Trading (Research Mode)
*   **Purpose:** To validate strategies without financial risk.
*   **Logic:** Simulates fills using current LTP and manages exits via "premium polling" every scan cycle.
*   **ML Features:** Captures 15+ feature columns (PCR, RSI, Regime) at the moment of trade entry for model training.

### Live Trading
*   **Broker:** Zerodha Kite Connect.
*   **Order Types:** 
    *   **Entry:** Limit orders with a dynamic slippage buffer (0.2% for futures, 5% for options).
    *   **Exit:** GTT (Good Till Triggered) orders for automated SL/Target management.
*   **Shadow Mode:** A safety feature that logs trades to the database and sends Telegram alerts without placing actual broker orders.

---

## 4. AI & LLM Enhancement

### Advisory vs. Full Control
The AI operates in one of three modes defined in `settings.py`:
1.  **Advisory:** Logs insights but does not change trade outcomes.
2.  **Boost Only:** Can promote a "Blocked" trade to "Experimental" if confidence is high.
3.  **Full:** Can both promote trades and veto rule-engine signals.

### Exit Advisor
A specialized LLM agent monitors open positions and suggests:
*   **Trail SL:** Locking in profits by moving the stop-loss up/down.
*   **Close Early:** Exiting before SL hit if the thesis is broken.
*   **Extend Target:** Moving profit targets further out during strong momentum.

---

## 5. Operational Workflows

### Market Hours & Scheduling
*   **NSE Indices:** 09:15 AM – 03:30 PM IST.
*   **MCX Commodities:** 09:00 AM – 11:30 PM IST.
*   **Scan Frequency:** Configurable per symbol class (e.g., 5 mins for NSE, 15 mins for MCX).

### Emergency Procedures
*   **Data Failure:** If all fetchers fail, the bot sends a Telegram alert and skips the scan.
*   **Stale Prices:** If the underlying price is missing, the bot falls back to the previous known price but flags the scan as `is_fallback=True` to prevent regime detection errors.
*   **API Limits:** Implements circuit breakers for LLM calls to prevent quota exhaustion during high-volatility periods.

---

## 6. Performance Monitoring
*   **Trade DNA:** Matches current setups against historical trades to find similar patterns.
*   **Edge Monitor:** Tracks win-rate trends and triggers ML retraining if the strategy's edge declines below 60%.
*   **Dashboard:** A FastAPI + HTML interface provides real-time views of P&L, active trades, and AI confidence scores.
