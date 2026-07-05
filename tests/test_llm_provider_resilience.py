import time
from unittest.mock import MagicMock, patch

import pytest

from src.engine.llm_enrichment import (
    LLMTradeVerdict,
    _call_llm_api,
    _parse_retry_after_seconds,
    _provider_cooldown_key,
    _PROVIDER_COOLDOWN_UNTIL,
    _register_provider_failure,
)


from src.engine import llm_enrichment as llm_mod


@pytest.fixture(autouse=True)
def reset_llm_runtime_state():
    llm_mod._CONSECUTIVE_FAILURES = 0
    llm_mod._CIRCUIT_OPEN_UNTIL = 0.0
    llm_mod._API_QUOTA_EXHAUSTED_UNTIL = 0.0
    llm_mod._PROVIDER_COOLDOWN_UNTIL.clear()
    yield
    llm_mod._CONSECUTIVE_FAILURES = 0
    llm_mod._CIRCUIT_OPEN_UNTIL = 0.0
    llm_mod._API_QUOTA_EXHAUSTED_UNTIL = 0.0
    llm_mod._PROVIDER_COOLDOWN_UNTIL.clear()


class TestProviderCooldownHelpers:
    def test_parse_retry_after_minutes_seconds(self):
        assert _parse_retry_after_seconds("Please try again in 1m45.408s") == pytest.approx(105.408)

    def test_parse_retry_after_seconds_only(self):
        assert _parse_retry_after_seconds("retry in 90s") is None
        assert _parse_retry_after_seconds("try again in 90s") == 90.0

    def test_register_provider_failure_402(self):
        provider = {"env_key": "OPENROUTER_API_KEY", "model": "openai/gpt-oss-120b", "name": "OR Paid"}
        key = _provider_cooldown_key(provider)
        _PROVIDER_COOLDOWN_UNTIL.pop(key, None)
        now = time.time()
        _register_provider_failure(provider, 402, "requires more credits", now)
        assert _PROVIDER_COOLDOWN_UNTIL[key] == pytest.approx(now + 86400.0)

    def test_register_provider_failure_tpd_429(self):
        provider = {"env_key": "GROQ_API_KEY", "model": "openai/gpt-oss-120b", "name": "Groq"}
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
        "choices": [{
            "message": {
                "content": '{"action":"NO_TRADE","confidence":50,"signal_chain":"OI: flat\\nPrice: flat\\nChart: flat","instrument":"NIFTY","entry_trigger":"wait","entry_premium_range":"0-0","stop_loss":"n/a","target_1":"n/a","target_2":"n/a","risk_reward":"n/a","thesis":"wait","invalidation":"n/a","risk_rating":"LOW","catalyst":"none"}'
            }
        }]
    }

    captured = {}

    def _capture_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return mock_resp

    import os
    os.environ["OPENROUTER_API_KEY"] = "fake-key"
    for k in ("OPENCODE_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(k, None)
    _PROVIDER_COOLDOWN_UNTIL.clear()

    try:
        with patch("requests.Session.post", side_effect=_capture_post):
            result = _call_llm_api("NIFTY", "test prompt", LLMTradeVerdict, purpose="live_verdict")
        assert result is not None
        assert captured["json"]["max_tokens"] == 2048
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)


class TestOpenCodeZenProviderResilience:
    def test_opencode_provider_cooldown_key(self):
        provider = {"env_key": "OPENCODE_API_KEY", "model": "opencode/big-pickle", "name": "OpenCode Zen"}
        assert _provider_cooldown_key(provider) == "opencode_zen:opencode/big-pickle"

    def test_opencode_register_provider_failure_does_not_cooldown_key(self):
        provider = {"env_key": "OPENCODE_API_KEY", "model": "opencode/big-pickle", "name": "OpenCode Zen"}
        key = "opencode_zen:opencode/big-pickle"
        env_key = "OPENCODE_API_KEY"

        _PROVIDER_COOLDOWN_UNTIL.clear()
        now = time.time()
        _register_provider_failure(provider, 429, "rate limit exceeded", now)

        # The specific model variant is cooldown-flagged for 600s
        assert _PROVIDER_COOLDOWN_UNTIL[key] == pytest.approx(now + 600.0)
        # But the environment key is NOT cooldown-flagged
        assert env_key not in _PROVIDER_COOLDOWN_UNTIL

    def test_opencode_calls_inject_user_agent_header(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"action":"NO_TRADE","confidence":50,"signal_chain":"OI: flat\\nPrice: flat\\nChart: flat","instrument":"NIFTY","entry_trigger":"wait","entry_premium_range":"0-0","stop_loss":"n/a","target_1":"n/a","target_2":"n/a","risk_reward":"n/a","thesis":"wait","invalidation":"n/a","risk_rating":"LOW","catalyst":"none"}'
                }
            }]
        }

        captured_headers = {}

        def _capture_post(url, headers=None, json=None, timeout=None):
            captured_headers.update(headers or {})
            return mock_resp

        import os
        os.environ["OPENCODE_API_KEY"] = "fake-opencode-key"
        # clear other keys so it tries OpenCode
        for k in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "SAMBANOVA_API_KEY", "GITHUB_TOKEN"):
            os.environ.pop(k, None)

        try:
            with patch("requests.Session.post", side_effect=_capture_post):
                _call_llm_api("NIFTY", "test prompt", LLMTradeVerdict, purpose="live_verdict")
            
            assert "User-Agent" in captured_headers
            assert "Mozilla/5.0" in captured_headers["User-Agent"]
        finally:
            os.environ.pop("OPENCODE_API_KEY", None)

    def test_opencode_unauthorized_skips_group(self):
        mock_unauthorized = MagicMock()
        mock_unauthorized.status_code = 401
        mock_unauthorized.text = "unauthorized"

        # Mock post to fail with 401 for the first model, and verify it does NOT try subsequent OpenCode models
        # because it breaks out of the group completely.
        call_count = 0
        def _capture_post(url, headers=None, json=None, timeout=None):
            nonlocal call_count
            call_count += 1
            return mock_unauthorized

        import os
        os.environ["OPENCODE_API_KEY"] = "fake-opencode-key"
        for k in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "SAMBANOVA_API_KEY", "GITHUB_TOKEN"):
            os.environ.pop(k, None)

        try:
            with patch("requests.Session.post", side_effect=_capture_post):
                _call_llm_api("NIFTY", "test prompt", LLMTradeVerdict, purpose="live_verdict")
            
            # Since 401 unauthorized was returned, we break out of the OpenCode group immediately, so only 1 POST call is made.
            # If it fell through to the next model in the pool, it would have made multiple calls.
            assert call_count == 1
        finally:
            os.environ.pop("OPENCODE_API_KEY", None)

