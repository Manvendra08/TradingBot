import pytest
from src.engine.broker_gate import authorize_broker_execution, ExecutionAuthorization

def test_authorize_broker_execution_blocked_when_shadow_mode_true(monkeypatch):
    config = {
        "live_shadow_mode": True,
        "live_broker_disabled": False,
        "trading_paused": False,
        "live_enabled_broker_symbols": ["NIFTY"]
    }
    monkeypatch.setattr("config.runtime_config.load_runtime_config", lambda: config)

    auth = authorize_broker_execution("NIFTY", operation="ENTRY")
    assert isinstance(auth, ExecutionAuthorization)
    assert auth.is_authorized is False
    assert auth.is_shadow is True
    assert "shadow mode" in auth.reason.lower()

def test_authorize_broker_execution_blocked_when_trading_paused(monkeypatch):
    config = {
        "live_shadow_mode": False,
        "live_broker_disabled": False,
        "trading_paused": True,
        "live_enabled_broker_symbols": ["NIFTY"]
    }
    monkeypatch.setattr("config.runtime_config.load_runtime_config", lambda: config)

    auth = authorize_broker_execution("NIFTY", operation="EXIT")
    assert auth.is_authorized is False
    assert "paused" in auth.reason.lower()

def test_authorize_broker_execution_blocked_when_broker_disabled(monkeypatch):
    config = {
        "live_shadow_mode": False,
        "live_broker_disabled": True,
        "trading_paused": False,
        "live_enabled_broker_symbols": ["NIFTY"]
    }
    monkeypatch.setattr("config.runtime_config.load_runtime_config", lambda: config)

    auth = authorize_broker_execution("NIFTY", operation="ENTRY")
    assert auth.is_authorized is False
    assert "disabled" in auth.reason.lower()

def test_authorize_broker_execution_blocked_when_symbol_not_enabled(monkeypatch):
    config = {
        "live_shadow_mode": False,
        "live_broker_disabled": False,
        "trading_paused": False,
        "live_enabled_broker_symbols": ["BANKNIFTY"]
    }
    monkeypatch.setattr("config.runtime_config.load_runtime_config", lambda: config)

    auth = authorize_broker_execution("NIFTY", operation="ENTRY")
    assert auth.is_authorized is False
    assert "nifty" in auth.reason.lower()

def test_authorize_broker_execution_blocked_when_market_closed(monkeypatch):
    config = {
        "live_shadow_mode": False,
        "live_broker_disabled": False,
        "trading_paused": False,
        "live_enabled_broker_symbols": ["NIFTY"]
    }
    monkeypatch.setattr("config.runtime_config.load_runtime_config", lambda: config)
    monkeypatch.setattr("src.engine.broker_gate._is_market_open", lambda sym: False)

    auth = authorize_broker_execution("NIFTY", operation="ENTRY")
    assert auth.is_authorized is False
    assert "market closed" in auth.reason.lower()

def test_authorize_broker_execution_success(monkeypatch):
    config = {
        "live_shadow_mode": False,
        "live_broker_disabled": False,
        "trading_paused": False,
        "live_enabled_broker_symbols": ["NIFTY"]
    }
    monkeypatch.setattr("config.runtime_config.load_runtime_config", lambda: config)
    monkeypatch.setattr("src.engine.broker_gate._is_market_open", lambda sym: True)

    auth = authorize_broker_execution("NIFTY", operation="ENTRY")
    assert auth.is_authorized is True
    assert auth.is_shadow is False
    assert auth.reason == "AUTHORIZED"
