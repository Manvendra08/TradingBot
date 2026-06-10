# Directory Structure

This document outlines the organization of the NSEBOT project codebase, detailing folders, key modules, and their design responsibilities.

---

## 1. Directory Tree Overview

```
NSEBOT/
├── config/              # Global environment, thresholds, and logging settings
├── data/                # Database file storage (SQLite)
├── logs/                # Rotating runtime log storage
├── src/                 # Application source directory
│   ├── alerts/          # Telegram alerts, digest compiling, and deduplication
│   ├── dashboard/       # Dashboard pages, API endpoints, and web server routes
│   ├── engine/          # Processing pipeline, anomaly rules, strategies, and paper trading
│   ├── fetchers/        # Dhan, NSE, Paytm, Upstox, and Chart data adapters
│   ├── models/          # SQLite database schema and persistence layer
│   ├── scheduler/       # Market hour guards and APScheduler/watchdog loop runner
│   └── utils/           # Shared formatters and formatting utilities
├── tests/               # Unit, regression, and system integration tests
└── tools/               # External scraper scripts and automation tools
```

---

## 2. Directory Mapping & Key Modules

### 2.1 Config (`config/`)
Contains static and runtime configurations of the application.
- **`settings.py`**: The single source of truth for thresholds, watch symbols, lot sizes, API credentials, and Telegram chat IDs.
- **`runtime_config.py`**: Handles dynamic parameters that can be changed during execution without restarting the bot (e.g., active scan intervals like `5m`, `15m`, `30m`, `1H`, `3H`, `1D`).
- **`symbol_classes.py`**: Defines standard strike step gaps and specific market windows (NSE index options, MCX commodity hours).
- **`holidays.py`**: Maintains lists of trading holidays to prevent execution during market closures.
- **`logging_config.py`**: Sets up logging streams, rotators, and formatting rules.

### 2.2 Fetchers (`src/fetchers/`)
Adapters wrapping various API sources.
- **`router.py`**: The central data-fetch router. Coordinates API fallback routes (e.g., trying Dhan $\rightarrow$ NSE $\rightarrow$ Paytm $\rightarrow$ Upstox) to ensure data is retrieved even during high-traffic API timeouts.
- **`base_fetcher.py`**: Abstract base class defining interface contracts for fetchers.
- **`chart_fetcher.py`**: Server-side client fetching intraday candle data (1h and 3h bars) to extract indicator states without running a browser engine.
- **`dhan_fetcher.py` / `dhan_commodity_fetcher.py`**: Adapters for Dhan's official REST API.
- **`dhan_headless_fetcher.py` / `dhan_headless_naturalgas.py`**: Headless web scrapers utilizing Selenium/Playwright as fallbacks.
- **`nse_fetcher.py`**: Directly fetches raw option chains from the official NSE website using custom headers and cookies.
- **`paytm_fetcher.py` / `upstox_fetcher.py`**: Secondary REST integrations for Paytm Money and Upstox.

### 2.3 Engine (`src/engine/`)
Houses core analytics, decision rules, and execution engines.
- **`pipeline.py`**: Pipeline Orchestrator wrapping the step-by-step logic.
- **`anomaly_detector.py`**: Core mathematical rules detecting abnormal OI adjustments, high-volume strikes, and target premium movements.
- **`intelligence.py`**: Generates natural language analysis, direction scoring, and options sentiment digests.
- **`verdict_sets.py` / `trade_decision.py`**: Resolves final sentiment directions (bullish/bearish) and checks momentum filters.
- **`risk_engine.py`**: Checks risk controls (e.g., maximum concurrent positions or daily loss limits) before execution.
- **`paper_plan.py`**: Automatically structures trade parameters (side, strike, option type, target, and stop-loss levels).
- **`paper_trading.py`**: Runs both the core verdict trading logic and the secondary 3H/1H crossover strategy. Manages entry triggers, premium tracking, exits, and pyramiding.
- **`entry_quality.py` / `regime_detector.py` / `trend_analysis.py`**: Auxiliary rule classes defining trade entry quality scoring, trend alignment, and current market regimes.
- **`scan_summary.py`**: Formats and saves high-level metrics of each run for historical trend scans.

### 2.4 Alerts (`src/alerts/`)
Notification formatting, routing, and filtering layer.
- **`telegram_dispatcher.py`**: Async client wrapper sending markdown payloads to Telegram chats.
- **`dedup.py`**: Temporal deduplication filter preventing repetitive alerts from spamming channels.
- **`digest.py`**: Groups multiple raw signals and trade decisions into a unified periodic markdown message.

### 2.5 Models (`src/models/`)
Persistence mapping.
- **`schema.py`**: Initializes the SQLite database and executes migrations. Handles inserts and query lookups for snapshots, underlying prices, active paper trades, and historical alerts.

### 2.6 Scheduler (`src/scheduler/`)
Scheduler and loop management.
- **`job_runner.py`**: Implements the main multi-thread loop, market-hour validation, and task timeouts.

---

## 3. Web & Bridge Files
- **`dashboard_server.py`**: Serves the professional paper-trading dashboard UI, symbols panel, real-time equity curves, and recent intelligence summaries using a FastAPI server.
- **`src/extension_bridge.py`**: Integrates as a lightweight HTTP server receiving real-time payloads pushed from the Chrome extension.

---

## 4. Test Suite (`tests/`)
Comprehensive tests ensuring algorithmic stability:
- **`test_base_fetcher.py` / `test_chart_fetcher.py`**: Unit tests validating that REST request interfaces and indicator parses function properly.
- **`test_engine.py` / `test_core_engine_coverage.py`**: Validates mathematical thresholds, anomaly detections, and rule triggers.
- **`test_entry_quality.py` / `test_regime_detector.py` / `test_trend_analysis.py`**: Verifies that market context classifiers yield correct results.
- **`test_timeframe_strategy.py`**: Validates the 3H/1H crossover strategy including entry breakouts, exit crossovers, pyramiding scaling, and dead-trade exits.
- **`test_formatting.py` / `test_telegram_formatter.py`**: Ensures that Telegram output matches strict markdown templates.
- **`test_integration.py` / `test_operational_robustness.py`**: End-to-end integration tests confirming the pipeline runs end-to-end and survives API fetch failures.
