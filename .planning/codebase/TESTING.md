# Testing Practices

## Framework & Configuration
- **pytest**: The primary test runner.
- **pytest-cov**: Active for coverage assertion.
- **pytest.ini**: Configures the default test flags and coverage thresholds:
  ```ini
  [pytest]
  testpaths = tests
  python_files = test_*.py
  python_classes = Test*
  python_functions = test_*
  addopts = -v --tb=short --cov=src --cov-report=term-missing --cov-fail-under=50
  asyncio_mode = auto
  ```

## Test Database Isolation (conftest.py)
All tests run with isolated environment state, governed by fixtures in `tests/conftest.py`:
- `isolated_db` (autouse, session-scoped): Creates a temporary DB file via `tempfile.NamedTemporaryFile`, runs schema initialization via `init_db()`, patches both `src.models.schema.DB_PATH` and `config.settings.DB_PATH` to point to it, and unlinks it after the session concludes.
- `no_telegram` (autouse, function-scoped): Automatically mocks the `send_alert` dispatch function to prevent tests from triggering actual external Telegram API calls.
- `sample_oc_nifty` & `sample_oc_banknifty`: Shared fixtures that supply realistic, pre-formed option chain states (including strike rows, LTPs, volumes, and IVs) to mock detection and strategy runs.

## Mocking & Seeding Patterns
- **API Mocking**: Extensive usage of `unittest.mock.patch` to stub external modules like `requests.Session` (fetchers) or optional visualization libraries like `tvDatafeed`.
- **Database Seeding**: Tests often mock or directly seed the in-memory/temp database (e.g. inserting historical `scan_summaries` or `paper_trades`) prior to calling calculation functions to verify time-series and stateful rules like regime or trend persistence.

## Test Files Reference
- `tests/conftest.py`: Session setup, DB isolation, and mock option-chains.
- `tests/test_base_fetcher.py`: Validation of the base scraper / fetcher class mechanics.
- `tests/test_chart_fetcher.py`: Tests candle fetching, data parsing, and time aggregations.
- `tests/test_core_engine_coverage.py`: Targets 100% coverage across core indicators:
  - `regime_detector.py` (crossover detections, no-trade conditions)
  - `entry_quality.py` (spread ranges, chase buffers, risk/reward profiles)
  - `risk_engine.py` (max daily trade counts, loss limits, and cool-down states)
  - `trend_analysis.py` (alignment percentages, reversal triggers, and momentum scores)
  - `trade_decision.py` (aggressive/conservative trading configurations)
- `tests/test_engine.py`: Unit tests for anomaly engine evaluations.
- `tests/test_entry_quality.py`: Individual test coverage of risk margins.
- `tests/test_formatting.py`: Formatting helpers validations.
- `tests/test_headless_fetchers.py`: Stubs and checks Dhan/nse fetchers execution loop.
- `tests/test_integration.py`: End-to-end flow from scan data insertion to paper trade updates.
- `tests/test_operational_robustness.py`: Checks holiday checking rules (NSE/MCX calendar), timeout watchdogs, and fetcher session cookie warmups.
- `tests/test_phase2_regression.py`: Checks regression metrics from Phase 2 features.
- `tests/test_regime_detector.py`: Tests regime boundary transitions.
- `tests/test_telegram_formatter.py`: HTML telegram digest formatting validations.
- `tests/test_timeframe_strategy.py`: Crossover trading rules (3h entry / 1h exit), breakout buffers, and pyramiding sizing scales.
- `tests/test_trend_analysis.py`: Reversal validation and trend-persistence tests.
- `tests/test_trend_oi_fixes.py`: Special trend and OI indicators assertions.

## Running Tests
Run the test suite from the virtual environment:
```bash
.venv\Scripts\pytest
```
To run a single test module:
```bash
.venv\Scripts\pytest tests/test_timeframe_strategy.py
```
