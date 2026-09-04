import json
import pytest
from config.runtime_config import load_runtime_config, save_runtime_config, RUNTIME_CONFIG_PATH

def test_load_runtime_config_corrupt_file_returns_fail_closed_defaults(tmp_path, monkeypatch):
    bad_config_file = tmp_path / "runtime_config.json"
    bad_config_file.write_text("{ invalid json ...", encoding="utf-8")
    monkeypatch.setattr("config.runtime_config.RUNTIME_CONFIG_PATH", bad_config_file)

    cfg = load_runtime_config()
    assert cfg["live_shadow_mode"] is True
    assert cfg["live_broker_disabled"] is True
    assert cfg["trading_paused"] is True
    assert cfg["live_ai_exit_advisor_enabled"] is False

def test_save_runtime_config_validates_types(tmp_path, monkeypatch):
    config_file = tmp_path / "runtime_config.json"
    monkeypatch.setattr("config.runtime_config.RUNTIME_CONFIG_PATH", config_file)

    with pytest.raises(ValueError, match="live_ai_decision_mode"):
        save_runtime_config({"live_ai_decision_mode": "invalid_mode"})
