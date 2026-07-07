import time
from unittest.mock import MagicMock, patch

import pytest

from src.engine import llm_enrichment as llm_mod
from src.engine.llm_enrichment import (
    _PROVIDER_COOLDOWN_UNTIL,
    LLMTradeVerdict,
    _call_llm_api,
    _parse_retry_after_seconds,
    _provider_cooldown_key,
    _register_provider_failure,
)


@pytest.fixture(autouse=True)
def reset_llm_runtime_state():
    import os
    llm_mod._CONSECUTIVE_FAILURES = 0
    llm_mod._CIRCUIT_OPEN_UNTIL = 0.0
    llm_mod._API_QUOTA_EXHAUSTED_UNTIL = 0.0
    llm_mod._PROVIDER_COOLDOWN_UNTIL.clear()

    # Save and pop keys to prevent real API calls in tests
    saved = {}
    for k in (
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GITHUB_TOKEN",
    ):
        if k in os.environ:
            saved[k] = os.environ[k]
            del os.environ[k]

    yield

    llm_mod._CONSECUTIVE_FAILURES = 0
    llm_mod._CIRCUIT_OPEN_UNTIL = 0.0
    llm_mod._API_QUOTA_EXHAUSTED_UNTIL = 0.0
    llm_mod._PROVIDER_COOLDOWN_UNTIL.clear()

    # Restore keys
    for k, v in saved.items():
        os.environ[k] = v


class TestProviderCooldownHelpers:
    def test_parse_retry_after_minutes_seconds(self):
        assert _parse_retry_after_seconds(
            "Please try again in 1m45.408s"
        ) == pytest.approx(105.408)

    def test_parse_retry_after_seconds_only(self):
        assert _parse_retry_after_seconds("retry in 90s") is None
        assert _parse_retry_after_seconds("try again in 90s") == 90.0

    def test_register_provider_failure_402(self):
        provider = {
            "env_key": "OPENROUTER_API_KEY",
            "model": "openai/gpt-oss-120b",
            "name": "OR Paid",
        }
        key = _provider_cooldown_key(provider)
        _PROVIDER_COOLDOWN_UNTIL.pop(key, None)
        now = time.time()
        _register_provider_failure(provider, 402, "requires more credits", now)
        assert _PROVIDER_COOLDOWN_UNTIL[key] == pytest.approx(now + 86400.0)

    def test_register_provider_failure_tpd_429(self):
        provider = {
            "env_key": "GROQ_API_KEY",
            "model": "openai/gpt-oss-120b",
            "name": "Groq",
        }
        key = _provider_cooldown_key(provider)
        _PROVIDER_COOLDOWN_UNTIL.pop(key, None)
        now = time.time()
        body = "tokens per day (TPD): Limit 200000. Please try again in 1m45s"
        _register_provider_failure(provider, 429, body, now)
        assert _PROVIDER_COOLDOWN_UNTIL[key] >= now + 3600.0


def test_call_llm_api_sets_max_tokens():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"action":"NO_TRADE","confidence":50,"signal_chain":"OI: flat\\nPrice: flat\\nChart: flat","instrument":"NIFTY","entry_trigger":"wait","entry_premium_range":"0-0","stop_loss":"n/a","target_1":"n/a","target_2":"n/a","risk_reward":"n/a","thesis":"wait","invalidation":"n/a","risk_rating":"LOW","catalyst":"none"}'
                }
            }
        ]
    }

    captured = {}

    def _capture_post(*args, **kwargs):
        captured["json"] = kwargs.get("json")
        return mock_resp

    import os

    os.environ["OPENROUTER_API_KEY"] = "fake-key"
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(k, None)
    _PROVIDER_COOLDOWN_UNTIL.clear()

    try:
        with patch("requests.Session.post", side_effect=_capture_post):
            result = _call_llm_api(
                "NIFTY", "test prompt", LLMTradeVerdict, purpose="live_verdict"
            )
        assert result is not None
        assert captured["json"]["max_tokens"] == 2048
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)
