"""
Tests for Decision Matrix Configuration & Hot-Reloading Loader.
Verifies fail-closed defaults, schema validation, mtime-based hot-reloading,
and atomic file persistence.
"""

import json
import os
import tempfile
import time
from pathlib import Path
import pytest

from config.decision_matrix_loader import (
    DEFAULTS,
    load_decision_matrix,
    save_decision_matrix,
    validate_decision_matrix,
    get_matrix_mode,
    get_decision_matrix_mode,
)
from src.engine.decision_matrix import (
    vol_bucket,
    delta_band,
    select_structure,
)


def test_defaults_structure_valid():
    """Verify compile-time DEFAULTS pass internal validation without errors."""
    errors = validate_decision_matrix(DEFAULTS)
    assert errors == [], f"DEFAULTS must be valid, got: {errors}"


def test_load_decision_matrix_missing_file_fallback(tmp_path):
    """Missing config file must log warning and fall back to DEFAULTS."""
    non_existent = tmp_path / "does_not_exist.json"
    cfg = load_decision_matrix(path=non_existent, force_reload=True)
    assert cfg["mode"] == "shadow"
    assert "fallback-missing" in cfg["config_version"]
    assert cfg["delta_profile"] == "aggressive"


def test_load_decision_matrix_invalid_json_fallback(tmp_path):
    """Corrupted JSON must fall back to DEFAULTS safely."""
    corrupted = tmp_path / "corrupted.json"
    corrupted.write_text("{ this is not valid json !!! }", encoding="utf-8")
    cfg = load_decision_matrix(path=corrupted, force_reload=True)
    assert cfg["mode"] == "shadow"
    assert "fallback-exception" in cfg["config_version"]


def test_validation_rejects_out_of_bounds_delta():
    """Delta values outside (0, 1] or inverted ranges must be rejected."""
    bad_cfg = json.loads(json.dumps(DEFAULTS))
    bad_cfg["delta_bands"]["aggressive"]["indices"]["CREDIT_SPREAD_SHORT"]["min"] = 0.50
    bad_cfg["delta_bands"]["aggressive"]["indices"]["CREDIT_SPREAD_SHORT"]["max"] = 0.30  # min > max!
    errors = validate_decision_matrix(bad_cfg)
    assert any("Invalid delta range" in e for e in errors)


def test_validation_rejects_danger_ceiling_below_max():
    """danger_ceiling must be >= max delta."""
    bad_cfg = json.loads(json.dumps(DEFAULTS))
    bad_cfg["delta_bands"]["aggressive"]["indices"]["CREDIT_SPREAD_SHORT"]["danger_ceiling"] = 0.20  # < max 0.35!
    errors = validate_decision_matrix(bad_cfg)
    assert any("Invalid danger_ceiling" in e for e in errors)


def test_validation_rejects_invalid_credit_rules():
    """Credit rules hierarchy reject <= half <= full must be enforced."""
    bad_cfg = json.loads(json.dumps(DEFAULTS))
    bad_cfg["credit_rules"]["reject_below_ratio"] = 0.40
    bad_cfg["credit_rules"]["min_credit_ratio_full_size"] = 0.20
    errors = validate_decision_matrix(bad_cfg)
    assert any("Invalid credit_rules hierarchy" in e for e in errors)


def test_hot_reload_on_mtime_change(tmp_path):
    """Config reloads on disk edit without process restart."""
    test_file = tmp_path / "decision_matrix.json"
    test_cfg = json.loads(json.dumps(DEFAULTS))
    test_cfg["delta_profile"] = "aggressive"
    save_decision_matrix(test_cfg, path=test_file)

    cfg1 = load_decision_matrix(path=test_file, force_reload=True)
    assert cfg1["delta_profile"] == "aggressive"

    # Modify on disk
    time.sleep(0.05)  # Ensure mtime tick
    test_cfg["delta_profile"] = "defensive"
    save_decision_matrix(test_cfg, path=test_file)

    cfg2 = load_decision_matrix(path=test_file)
    assert cfg2["delta_profile"] == "defensive"


def test_vol_bucket_fallback_on_insufficient_history():
    """When IVP history is unavailable (None), vol_bucket must fall back to HIGH (defined risk)."""
    assert vol_bucket("NIFTY", ivp=None) == "HIGH"
    assert vol_bucket("NATURALGAS", ivp=None) == "HIGH"


def test_delta_band_retrieval():
    """Verify delta_band accurately retrieves profile-specific min/max/danger_ceiling."""
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["delta_profile"] = "aggressive"
    # Indices credit spread short aggressive: min 0.30, max 0.35, danger 0.40
    mn, mx, dc = delta_band("BULL_PUT_SPREAD", "SHORT", symbol="NIFTY", cfg=cfg)
    assert mn == 0.30
    assert mx == 0.35
    assert dc == 0.40

    # MCX credit spread short aggressive: min 0.24, max 0.28, danger 0.32
    mn, mx, dc = delta_band("BULL_PUT_SPREAD", "SHORT", symbol="NATURALGAS", cfg=cfg)
    assert mn == 0.24
    assert mx == 0.28
    assert dc == 0.32


def test_get_decision_matrix_mode_and_alias():
    """Verify get_decision_matrix_mode and get_matrix_mode support symbol, cfg, and empty calls."""
    cfg = {"mode": "enforce", "overrides_by_symbol": {"NATURALGAS": {"mode": "shadow"}}}
    assert get_decision_matrix_mode(cfg=cfg) == "enforce"
    assert get_decision_matrix_mode("NATURALGAS", cfg=cfg) == "shadow"
    assert get_decision_matrix_mode("NIFTY", cfg=cfg) == "enforce"
    assert get_matrix_mode(cfg) == "enforce"
    assert get_matrix_mode(cfg, symbol="NATURALGAS") == "shadow"

