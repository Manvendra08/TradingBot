# Coding Conventions

## Language & Style
- **Python**: Direct alignment with PEP 8 standards. Focus on clean code, short methods, and explicit control flows.
- **Type Annotations**:
  - Enforce explicit type annotations on all new functions and public APIs (v2.6+ modules).
  - Use type unions (`str | None`) and runtime hints.
  - Imports from `__future__ import annotations` are encouraged for clean, forward-compatible type hinting.

## Timezone Boundaries (Asia/Kolkata)
- **Primary Reference Timezone**: Indian Standard Time (IST), designated as `"Asia/Kolkata"`.
- **Database Storage**: All database dates/times MUST be stored as UTC ISO8601 strings, captured using `datetime.now(timezone.utc).isoformat()`.
- **Timezone Crossovers**:
  - Convert UTC strings to local IST when building user-facing Telegram alerts or dashboard displays.
  - Define IST explicitly using either `pytz.timezone("Asia/Kolkata")` or standard library equivalents `timezone(timedelta(hours=5, minutes=30))`.
  - Always localise naive datetimes before executing time comparison logic.
- **Market Hours Enforcement**:
  - Auto-trading and active scans perform checking via localized IST datetimes using `_is_market_open(symbol)` against symbol-specific market windows.
  - Market holidays are evaluated per-symbol (e.g., NIFTY vs. MCX NATURALGAS) using the `config/holidays.py` calendar, which supports partial session breaks (morning vs. evening closures) for MCX commodities.

## Architecture & Database Patterns
- **SQLite & Concurrent Access**:
  - SQLite must run with Write-Ahead Logging (WAL) enabled to support asynchronous reading from the FastAPI server while the scheduler executes writes.
  - Use `src/models/schema.py` as the unified data access interface, always wrapping operations in the `get_conn()` context manager.
- **Pure Functions in Detection**:
  - Core anomaly detection rules must act as pure functions. They compute and return verdicts, scores, and context without mutating global state or performing network / filesystem IO.

## Error Boundaries & Logging
- **System Robustness**:
  - Wrap individual fetching pipelines (Dhan, NSE, etc.) in robust try-except blocks. Prevent fetcher-specific HTTP/Scraping issues from triggering a cascading failure of the scheduler pipeline.
- **Logging Standards**:
  - Enforce hierarchical python logging via standard `logging.getLogger(__name__)`.
  - Avoid using raw `print` statements. Production logs are written to system-level log handlers.
