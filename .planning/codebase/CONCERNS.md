# Technical Concerns

## Technical Debt
- **Mock Complexity**: Some tests in `test_engine.py` are skipped or fragile due to complex mock signatures for database queries.
- **NSE Scraper Fragility**: NSE public JSON endpoints frequently change headers/cookies, requiring ongoing maintenance.
- **Blocking Scheduler**: `main.py` uses a blocking scheduler, which might be problematic for long-running deployments without process management (e.g. systemd/pm2).

## Performance
- **SQLite Locking**: While WAL mode is enabled, heavy concurrent write bursts from multiple symbols might still cause occasional locking during dashboard reads.

## Security
- **Credential Storage**: Credentials stored in `.env` or system environment. No encryption for local SQLite database.
- **NSE Rate Limits**: Scraper lacks sophisticated proxy/rotation, making it vulnerable to IP bans if frequency is increased.

## Fragility
- **Data Source Normalization**: The "Router" pattern assumes all fetchers return perfectly normalized JSON. Any schema change in Dhan or NSE APIs will break the pipeline for that symbol.
