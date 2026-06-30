# NSEBOT Architecture Documentation

## 1. System Overview
NSEBOT is a sophisticated, multi-phase algorithmic trading system designed for the Indian National Stock Exchange (NSE) and Multi Commodity Exchange (MCX). It utilizes a hybrid data ingestion approach, combining official broker APIs with web scraping to capture real-time Option Chain (OI) data, technical indicators, and news sentiment. The bot operates in both **Paper Trading** (simulation) and **Live Trading** modes, featuring an integrated AI "Brain" that provides trade recommendations and exit advice.

### Key Architectural Pillars:
*   **Modular Core:** Separated into logical layers (`engine`, `fetchers`, `services`, `models`) for maintainability.
*   **Robust Testing:** A comprehensive suite of 25+ test files ensures regression-free development.
*   **AI Integration:** Uses LLMs (via OpenRouter/Groq/Gemini) for deep market context analysis and trade plan generation.
*   **Operational Tooling:** Custom scripts for database maintenance, broker authentication, and UI management.

---

## 2. Module Responsibilities

| Module | Path | Primary Responsibility |
| :--- | :--- | :--- |
| **Engine** | `src/engine/` | The core logic layer containing the anomaly detector, risk engine, trade decision maker, and intelligence generator. |
| **Fetchers** | `src/fetchers/` | Data ingestion from multiple sources including Dhan, Shoonya, Paytm, NSE Public, and Yahoo Finance. |
| **Models** | `src/models/` | Database schema definition (`schema.py`) and lightweight data-access helpers for SQLite. |
| **Scheduler** | `src/scheduler/` | Manages the pipeline execution loop, ML training jobs, and EIA report analysis triggers. |
| **Dashboard** | `src/dashboard/` | FastAPI-based web UI for monitoring trades, configuring settings, and viewing AI insights. |
| **Intelligence** | `src/intelligence/` | Advanced analytics including Trade History Analysis and ML Success Prediction. |
| **Services** | `src/services/` | Broker-specific integrations, primarily handling Zerodha Kite Connect authentication. |

---

## 3. Data Flow & Pipeline Logic

The system follows a strict **Data → Intelligence → Decision → Execution** flow:

1.  **Ingestion:** `fetchers/router.py` attempts to fetch option chain data based on a priority list (e.g., Shoonya → Paytm → NSE Public).
2.  **Anomaly Detection:** `anomaly_detector.py` identifies spikes in OI, price, or volume and classifies them (e.g., Long Buildup, Short Covering).
3.  **Intelligence Generation:** `intelligence.py` combines scan context, alerts, and chart data to produce a structured verdict (Bullish/Bearish/Neutral) with a confidence score.
4.  **Trade Decision:** `trade_decision.py` applies filters (Regime, Trend Alignment, Entry Quality) to decide if a trade should be triggered.
5.  **Execution:** 
    *   **Paper:** `paper_trading.py` simulates entries/exits using premium-based SL/Target logic.
    *   **Live:** `live_trading.py` interacts with Zerodha Kite Connect to place actual orders and manage GTT (Good Till Triggered) exits.

---

## 4. Broker Integrations

*   **Zerodha (Kite Connect):** The primary live trading broker. Requires IP whitelisting and uses a resilient TLS adapter for API stability.
*   **Dhan:** Used as a secondary data source and for MCX commodity futures token resolution.
*   **Shoonya (Finvasia):** The primary source for chart data (GetTimePriceSeries) due to its reliability and lack of CAPTCHA risks.
*   **Chrome Extension Bridge:** A custom bridge reads DOM elements from TradingView and Dhan to supplement data where APIs are limited.

---

## 5. Configuration & Environment

Key configuration points are managed via `config/settings.py` and environment variables (`.env`):

*   **Market Windows:** Defines operating hours for NSE (09:15–15:30) and MCX (09:00–23:30).
*   **Risk Engine:** Configurable limits for daily loss caps, max concurrent positions, and cooldown periods.
*   **AI Decision Mode:** Controls how the AI influences trades (`advisory`, `boost_only`, or `full`).
*   **Fetcher Priority:** Determines the order in which data sources are queried for each symbol class.

---

## 6. Technical Debt & Cleanup Status

Recent high-priority cleanup has been performed:
*   **Database Consolidation:** Merged redundant databases; `nsebot.db` is now the single source of truth.
*   **Virtual Environments:** Consolidated multiple `.venv` folders into a single active environment.
*   **Cache Management:** Implemented automated cleanup for Yahoo Finance cache bloat.
*   **Scratch Directory:** Archived 164+ debug files to keep the root directory clean.

For future sessions, always refer to `docs/AGoT-playbook.md` for the reasoning framework and debugging guides.
