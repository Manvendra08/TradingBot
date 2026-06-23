"""
pytest conftest.py — shared fixtures for all tests.
Sets up an isolated in-memory SQLite DB per test session.
"""
import os
import sys
import pytest
import tempfile
from unittest.mock import patch, MagicMock

import httpx
_orig_client_init = httpx.Client.__init__

def _patched_client_init(self, *args, **kwargs):
    app = kwargs.pop("app", None)
    if app is not None and "transport" not in kwargs:
        if hasattr(httpx, "ASGITransport"):
            kwargs["transport"] = httpx.ASGITransport(app=app)
        else:
            kwargs["app"] = app
    _orig_client_init(self, *args, **kwargs)

httpx.Client.__init__ = _patched_client_init


# Mock tvDatafeed if not installed, preventing import and patching errors in test runs
try:
    import tvDatafeed
except ImportError:
    class FakeInterval:
        in_1_minute = "1m"
        in_3_minute = "3m"
        in_5_minute = "5m"
        in_15_minute = "15m"
        in_30_minute = "30m"
        in_45_minute = "45m"
        in_1_hour = "1h"
        in_2_hour = "2h"
        in_3_hour = "3h"
        in_4_hour = "4h"
        in_daily = "1d"
        in_weekly = "1w"

    mock_tv = MagicMock()
    mock_tv.TvDatafeed = MagicMock
    mock_tv.Interval = FakeInterval
    sys.modules["tvDatafeed"] = mock_tv


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
        import sys
        for mod_name in ["src.fetchers.chart_fetcher", "dashboard_server", "src.dashboard.app"]:
            if mod_name in sys.modules:
                setattr(sys.modules[mod_name], "DB_PATH", tmp_path)
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
    """Suppress all Telegram and Discord sends in tests."""
    monkeypatch.setattr(
        "src.alerts.telegram_dispatcher.send_alert",
        lambda alert: False
    )
    monkeypatch.setattr(
        "src.alerts.telegram_dispatcher.send_text",
        lambda text: False
    )
    monkeypatch.setattr(
        "src.alerts.discord_dispatcher.send_to_discord",
        lambda text, timeout_seconds=10: False
    )


@pytest.fixture(autouse=True)
def mock_llm_calls(request):
    """Globally mock LLM enrichment calls to avoid external API token usage in tests, except when testing the API client itself."""
    if (request.cls and request.cls.__name__ == "TestOpenRouterArrayUnwrap") or request.node.name == "test_llm_alternative_fallbacks":
        yield
        return

    from src.engine.llm_enrichment import LLMTradeVerdict, LLMExitAdvice, LLMStrategyOptimization

    def mock_call(symbol, prompt, response_schema=None, deadline=None):
        schema = response_schema or LLMTradeVerdict
        if schema == LLMExitAdvice:
            return LLMExitAdvice(
                action="HOLD",
                new_sl_premium=None,
                new_target_premium=None,
                reasoning="Mocked exit advice reasoning.",
                urgency="LOW"
            )
        elif schema == LLMStrategyOptimization:
            return LLMStrategyOptimization(
                suggested_config_changes={},
                analysis="Mocked strategy optimization analysis."
            )
        else:
            return LLMTradeVerdict(
                action="NO_TRADE",
                confidence=50,
                instrument="NIFTY 22000 CE 26Jun",
                entry_trigger="Mock trigger",
                entry_premium_range="100-110",
                stop_loss="Premium 80",
                target_1="Premium 150",
                target_2="Premium 200",
                risk_reward="1:2",
                thesis="Mocked thesis.",
                invalidation="Mock invalidation",
                risk_rating="LOW",
                catalyst="Mock catalyst"
            )

    with patch("src.engine.llm_enrichment._call_llm_api", side_effect=mock_call):
        yield


# Block real external LLM HTTP calls in tests as a fallback safety net
import requests
_orig_post = requests.Session.post

def _patched_post(self, url, *args, **kwargs):
    if "openrouter.ai" in str(url):
        raise RuntimeError("Real OpenRouter call blocked in tests to prevent token usage")
    return _orig_post(self, url, *args, **kwargs)

requests.Session.post = _patched_post

try:
    from google import genai
    class MockGeminiModels:
        def generate_content(self, *args, **kwargs):
            raise RuntimeError("Real Gemini call blocked in tests to prevent token usage")
    class MockGeminiClient:
        def __init__(self, *args, **kwargs):
            self.models = MockGeminiModels()
    genai.Client = MockGeminiClient
except ImportError:
    pass


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


@pytest.fixture(autouse=True)
def mock_runtime_config_frequencies():
    """Mock scan frequencies to default 5 min to ensure tests are isolated from host configuration file."""
    with patch("src.engine.paper_trading.get_scan_frequency_nse", return_value=5), \
         patch("src.engine.paper_trading.get_scan_frequency_mcx", return_value=5), \
         patch("src.engine.paper_trading.get_scan_frequency_minutes", return_value=5), \
         patch("config.runtime_config.get_scan_frequency_nse", return_value=5), \
         patch("config.runtime_config.get_scan_frequency_mcx", return_value=5), \
         patch("config.runtime_config.get_scan_frequency_minutes", return_value=5):
        yield


@pytest.fixture(scope="session", autouse=True)
def isolated_runtime_config():
    """
    Redirect all runtime config operations to a temp file for the test session.
    """
    from pathlib import Path
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    with patch("config.runtime_config.RUNTIME_CONFIG_PATH", tmp_path):
        yield tmp_path

    try:
        os.unlink(str(tmp_path))
    except FileNotFoundError:
        pass

