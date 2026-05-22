# Testing Practices

## Framework
- **pytest**: Primary testing framework.
- **pytest-cov**: Used for coverage tracking.

## Coverage
- **Threshold**: 50% minimum coverage required.
- **Config**: Defined in `pytest.ini`.

## Test Structure
- `tests/conftest.py`: Shared fixtures (database initialization, mock data).
- `tests/test_engine.py`: Unit tests for anomaly detection logic.
- `tests/test_integration.py`: Pipeline and persistence testing.

## Patterns
- **Mocking**: Extensive use of `unittest.mock.patch` for external APIs (Dhan, NSE) and database calls in unit tests.
- **Fixtures**: `_make_strike` and `_make_oc` helpers for generating consistent test data.

## Running Tests
```bash
pytest
pytest --cov=src tests/
```
