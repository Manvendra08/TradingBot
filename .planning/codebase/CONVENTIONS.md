# Coding Conventions

## Language & Style
- **Python**: Standard PEP 8 style.
- **Type Hinting**: Used in newer modules (v2.6+) for function signatures.
- **Documentation**: Module-level docstrings and function docstrings for complex logic.

## Patterns
- **Error Handling**: Extensive use of try-except blocks in fetchers and pipeline to prevent total system failure on single-symbol errors.
- **Logging**: Hierarchical logging using `logging` module. Different log files for `main` and `bridge`.
- **Async/Await**: Used in Telegram dispatcher and some test helpers.

## Database
- **Naming**: `snake_case` for table and column names.
- **Time**: All timestamps stored as ISO8601 strings in UTC.
- **WAL Mode**: Enabled for SQLite to allow concurrent read/write from dashboard and scheduler.

## Anomaly Detection
- **Pure Functions**: Detection logic should not modify state or perform IO.
- **Rule Return**: Detection rules return `(alert_type, context)` tuples.
