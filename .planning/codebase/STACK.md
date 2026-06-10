# Technology Stack

## Core Language & Runtime
- **Language**: Python 3.12
- **Runtime**: CPython (standard virtual environment setup via `.venv`)

## Core Packages & Frameworks
- **FastAPI / Uvicorn**: Serves the professional TradingView-inspired HTML dashboard and REST APIs (`dashboard_server.py`) on port 8080.
- **APScheduler**: Task scheduling engine (>= 3.10.4) executing periodic market scans, data backfills, database cleanup, and scheduler loops.
- **requests**: Standard HTTP fetching library (>= 2.31.0) with retries and connection backoff for option chains and external API webhooks.
- **python-telegram-bot**: Asynchronous wrapper (== 21.5) used to format and dispatch alerts to designated chat groups.
- **yfinance**: Financial data library (>= 0.2.40) used to fetch OHLC data for indices (NIFTY, BANKNIFTY, FINNIFTY) and as a fallback for commodities.
- **tvDatafeed**: MCX-native real-time OHLC fetching library for commodities (installed directly from GitHub).
- **pytz / python-dotenv**: Timezone management (IST / UTC) and environment variable handling.

## Database Layer (SQLite)
- **Database**: File-based SQLite database persisting at `data/nsebot.db`.
- **Performance Configurations**: 
  - Write-Ahead Logging (WAL) enabled: `PRAGMA journal_mode=WAL;`
  - Thread safety: `check_same_thread=False` bypassed gracefully via context-managed database connections.
  - PRAGMAs: `PRAGMA synchronous=NORMAL;`, `PRAGMA foreign_keys=ON;`
- **Database Schema**:
  - `option_chain_snapshots`: Time-series option chain data (strikes, LTP, OI, volume, IV, bid, ask, delta) keyed to fetcher sources.
  - `underlying_price`: Spot/future price per symbol per scan.
  - `anomaly_alerts`: Alert audit trail tracking fired alerts and their telegram status.
  - `alert_dedup`: Deduplication tracking table to prevent alert fatigue.
  - `paper_trades`: Active and closed paper trading positions, tracking entry/exit underlying, premium, stop-loss, target, lots, and P&L in both points and rupees.
  - `scan_summaries`: Historic aggregate scan states (PCR, Max Pain, Support/Resistance, Trend verdict, regime, and confidence scores).
  - `snapshot_baseline`: Baselines for tracking symbol rollover/switches.

## Testing & Quality Assurance
- **pytest**: Main test runner (>= 8.2.0).
- **pytest-asyncio**: Support for testing asynchronous Telegram loop dispatches and endpoints.
- **pytest-cov**: Code coverage reporting (>= 5.0.0).
