"""
pytest conftest.py — shared fixtures for all tests.
Sets up an isolated in-memory SQLite DB per test session.
"""
import os
import pytest
import tempfile
from unittest.mock import patch


@pytest.fixture(scope="session", autouse=True)
def isolated_db():
    """
    Redirect all DB operations to a temp file for the test session.
    Ensures tests never touch the real data/nsebot.db.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    with patch("src.models.schema.DB_PATH", tmp_path), \
         patch("config.settings.DB_PATH", tmp_path):
        from src.models.schema import init_db
        init_db()
        yield tmp_path

    try:
        os.unlink(tmp_path)
        os.unlink(tmp_path + "-shm")
        os.unlink(tmp_path + "-wal")
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """Suppress all Telegram sends in tests."""
    monkeypatch.setattr(
        "src.alerts.telegram_dispatcher.send_alert",
        lambda alert: False
    )


@pytest.fixture
def sample_oc_nifty():
    """Realistic-ish NIFTY option chain snapshot for tests."""
    strikes = []
    for s in range(21700, 22400, 100):
        strikes.append({
            "strike": float(s), "option_type": "CE",
            "ltp": max(1.0, float(22000 - s + 150)),
            "oi": 100_000 + (22000 - s) * 10,
            "oi_change": 500, "volume": 2000,
            "iv": 15.0 + abs(22000 - s) / 100,
            "bid": max(0.5, float(22000 - s + 149)),
            "ask": max(1.5, float(22000 - s + 151)),
        })
        strikes.append({
            "strike": float(s), "option_type": "PE",
            "ltp": max(1.0, float(s - 22000 + 150)),
            "oi": 100_000 + (s - 22000) * 10,
            "oi_change": -300, "volume": 1800,
            "iv": 15.0 + abs(22000 - s) / 100,
            "bid": max(0.5, float(s - 22000 + 149)),
            "ask": max(1.5, float(s - 22000 + 151)),
        })
    return {
        "symbol":           "NIFTY",
        "underlying_price": 22000.0,
        "expiry":           "2025-06-26",
        "strikes":          strikes,
        "source":           "test",
    }


@pytest.fixture
def sample_oc_banknifty():
    strikes = []
    for s in range(51000, 53200, 200):
        for ot in ("CE", "PE"):
            strikes.append({
                "strike": float(s), "option_type": ot,
                "ltp": 200.0, "oi": 50_000, "oi_change": 0,
                "volume": 500, "iv": 18.0, "bid": 199.0, "ask": 201.0,
            })
    return {
        "symbol":           "BANKNIFTY",
        "underlying_price": 52000.0,
        "expiry":           "2025-06-26",
        "strikes":          strikes,
        "source":           "test",
    }
