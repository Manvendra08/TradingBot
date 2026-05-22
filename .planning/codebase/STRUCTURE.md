# Directory Structure

## Root Layout
- `main.py`: Main entry point (scheduler, one-shot, bridge).
- `config/`: Configuration and logging setup.
- `src/`: Core application source code.
- `tests/`: Unit and integration tests.
- `data/`: SQLite database storage.
- `logs/`: Rotating application logs.
- `chrome_extension/`: Chrome Extension source (JS/JSON).

## src/ Internal Structure
- `src/alerts/`: Telegram dispatching, deduplication, and digest building.
- `src/dashboard/`: Streamlit app logic.
- `src/engine/`: Pipeline orchestration and anomaly detection rules.
- `src/fetchers/`: Data source adapters (Dhan, NSE, Upstox).
- `src/models/`: Database schema and CRUD operations.
- `src/scheduler/`: APScheduler configuration and market hour guards.

## Key Files
- `config/settings.py`: The single source of truth for all thresholds and credentials.
- `src/engine/anomaly_detector.py`: Contains the core mathematical rules for detection.
- `src/fetchers/router.py`: Handles the fallback logic between APIs.
