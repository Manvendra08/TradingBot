# Technology Stack

## Core
- **Language**: Python 3.9+
- **Runtime**: CPython
- **Frameworks**: 
  - **APScheduler**: Task scheduling (15-min ticks)
  - **requests**: HTTP fetching
  - **Streamlit**: Dashboard UI
  - **Plotly/Pandas**: Data visualization

## Data Persistence
- **Database**: SQLite (WAL mode enabled)
- **Schema**: Time-series snapshots of option chains, underlying prices, and alert history.

## Environment & Configuration
- **python-dotenv**: Environment variable management (`.env`)
- **Central Config**: `config/settings.py` for thresholds, URLs, and API keys.

## Notifications
- **python-telegram-bot**: v21.5 (asynchronous dispatch)

## Chrome Extension (Optional Bridge)
- **Manifest V3**: JavaScript based extension.
- **HTTP Bridge**: Python-based server (`src/extension_bridge.py`) accepting DOM-scraped JSON.

## Development & Testing
- **pytest**: Test runner
- **pytest-asyncio**: Async test support
- **pytest-cov**: Coverage reporting (50% threshold)
- **venv**: Standard Python virtual environment
