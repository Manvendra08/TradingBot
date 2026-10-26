import pytest
from unittest.mock import MagicMock, patch
from src.fetchers.shoonya_fetcher import ShoonyaFetcher


def test_exchange_for_token_reactive_ip_update(monkeypatch):
    fetcher = ShoonyaFetcher()

    call_count = {"post": 0, "update": 0, "reset": 0}

    def mock_post_jdata(url, payload, **kw):
        call_count["post"] += 1
        if call_count["post"] == 1:
            return {"stat": "Not_Ok", "emsg": "Invalid Input : INVALID_IP", "uid": "FA369105"}
        # second call succeeds
        return {
            "stat": "Ok",
            "susertoken": "mock_suser_token_12345",
            "actid": "FA369105",
        }

    def mock_update_ip(headless=True):
        call_count["update"] += 1
        return True, "IP updated"

    def mock_reset():
        call_count["reset"] += 1

    monkeypatch.setattr("src.fetchers.shoonya_fetcher._post_jdata", mock_post_jdata)
    monkeypatch.setattr(
        "src.fetchers.shoonya_ip_updater.update_shoonya_portal_ip", mock_update_ip
    )
    monkeypatch.setattr("src.fetchers.shoonya_ip_guard.reset_shoonya_ip_skip", mock_reset)

    token = fetcher._exchange_for_token("test_auth_code")
    assert token == "mock_suser_token_12345"
    assert call_count["update"] == 1
    assert call_count["reset"] == 1
    assert call_count["post"] == 2


def test_login_flow_retries_after_reactive_ip_update(monkeypatch):
    fetcher = ShoonyaFetcher()

    # Bypass filelock and token cache
    monkeypatch.setattr(fetcher, "_load_cached_token", lambda: None)
    monkeypatch.setattr(fetcher, "_save_token", lambda: None)
    fetcher.access_token = None
    fetcher.user_id = "FA369105"
    fetcher.password = "dummy"
    fetcher.totp_key = "dummy"
    fetcher.secret_code = "dummy"

    codes = ["code1", "code2"]
    monkeypatch.setattr(
        fetcher, "_get_auth_code_playwright", lambda: codes.pop(0) if codes else None
    )

    exchange_calls = []

    def mock_exchange(auth_code):
        exchange_calls.append(auth_code)
        if len(exchange_calls) == 1:
            # Simulate first code expired/failed during update, setting _just_updated_ip
            fetcher._just_updated_ip = True
            return None
        return "new_access_token_6789"

    monkeypatch.setattr(fetcher, "_exchange_for_token", mock_exchange)
    monkeypatch.setattr("src.fetchers.shoonya_ip_guard.shoonya_should_skip", lambda: False)
    monkeypatch.setattr("src.fetchers.shoonya_ip_guard.reset_shoonya_ip_skip", lambda: None)

    ok = fetcher.login()
    assert ok is True
    assert fetcher.access_token == "new_access_token_6789"
    assert exchange_calls == ["code1", "code2"]
