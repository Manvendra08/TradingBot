"""Runtime-configurable settings persisted to data/runtime_config.json."""
from __future__ import annotations

import json
from pathlib import Path

from config.settings import DATA_DIR, FETCH_INTERVAL_MINUTES

RUNTIME_CONFIG_PATH = DATA_DIR / "runtime_config.json"
ALLOWED_SCAN_FREQUENCIES = [5, 15, 30, 60, 180, 1440]
MIN_SCAN_FREQUENCY = ALLOWED_SCAN_FREQUENCIES[0]
MAX_SCAN_FREQUENCY = ALLOWED_SCAN_FREQUENCIES[-1]

# Symbols shown in broker/paper lot controls (Settings → Cockpit)
TRADE_CONTROL_SYMBOLS = [
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "SENSEX",
    "NATURALGAS",
    "CRUDEOIL",
]


def default_symbol_lots(default_lot: int = 1) -> dict[str, int]:
    return {sym: default_lot for sym in TRADE_CONTROL_SYMBOLS}


def _clamp_minutes(value: int) -> int:
    v = int(value)
    if v in ALLOWED_SCAN_FREQUENCIES:
        return v
    # fallback to nearest allowed value
    return min(ALLOWED_SCAN_FREQUENCIES, key=lambda x: abs(x - v))


def load_runtime_config() -> dict:
    default_freq = _clamp_minutes(int(FETCH_INTERVAL_MINUTES))
    defaults = {
        "scan_frequency_minutes": default_freq,
        "scan_frequency_nse": default_freq,
        "scan_frequency_mcx": default_freq,
        "live_shadow_mode": True,
        "live_capital_per_trade_inr": 20000,
        "live_max_capital_utilisation_pct": 80,
        "live_max_concurrent_positions": 2,
        "live_max_daily_loss_rupees": 200000,
        "live_symbol_lots": default_symbol_lots(1),
        "paper_symbol_lots": default_symbol_lots(10),
        "paper_lots": 10,  # Global fallback when paper_symbol_lots has no entry
        "live_enabled_broker_symbols": ["NIFTY", "BANKNIFTY", "NATURALGAS", "CRUDEOIL"],
        "oi_spike_threshold_pct": 10.0,
        "price_spike_threshold_pct": 2.0,
        "dashboard_auth_enabled": False,
        "live_ai_decision_mode": "advisory",
        "live_ai_min_confidence_boost": 80,
        "live_ai_min_confidence_veto": 85,
        "live_ai_exit_advisor_enabled": False,
        "manage_direct_kite_positions": False,
        "direct_kite_initialization_mode": "fixed_pct",
        "direct_kite_default_sl_pct": 30.0,
        "direct_kite_default_tgt_pct": 50.0,
        "live_broker_disabled": False,  # Completely block all broker order placement
    }
    if not RUNTIME_CONFIG_PATH.exists():
        return defaults
    try:
        data = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
        for k, v in data.items():
            defaults[k] = v
        # Merge nested symbol-lot maps so new symbols (e.g. SENSEX) get defaults.
        for lots_key in ("live_symbol_lots", "paper_symbol_lots"):
            saved_lots = data.get(lots_key)
            if isinstance(saved_lots, dict):
                merged = default_symbol_lots(1 if lots_key == "live_symbol_lots" else 10)
                merged.update(saved_lots)
                defaults[lots_key] = merged
        defaults["scan_frequency_minutes"] = _clamp_minutes(defaults.get("scan_frequency_minutes", default_freq))
        defaults["scan_frequency_nse"] = _clamp_minutes(defaults.get("scan_frequency_nse", defaults["scan_frequency_minutes"]))
        defaults["scan_frequency_mcx"] = _clamp_minutes(defaults.get("scan_frequency_mcx", defaults["scan_frequency_minutes"]))
        return defaults
    except Exception:
        return defaults


def save_runtime_config(config: dict) -> None:
    if "scan_frequency_minutes" in config:
        config["scan_frequency_minutes"] = _clamp_minutes(config["scan_frequency_minutes"])
    if "scan_frequency_nse" in config:
        config["scan_frequency_nse"] = _clamp_minutes(config["scan_frequency_nse"])
    if "scan_frequency_mcx" in config:
        config["scan_frequency_mcx"] = _clamp_minutes(config["scan_frequency_mcx"])
        
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def get_scan_frequency_minutes() -> int:
    return load_runtime_config()["scan_frequency_minutes"]


def get_scan_frequency_nse() -> int:
    return load_runtime_config()["scan_frequency_nse"]


def get_scan_frequency_mcx() -> int:
    return load_runtime_config()["scan_frequency_mcx"]


def set_scan_frequency_minutes(minutes: int) -> int:
    val = _clamp_minutes(minutes)
    config = load_runtime_config()
    config["scan_frequency_minutes"] = val
    config["scan_frequency_nse"] = val
    config["scan_frequency_mcx"] = val
    save_runtime_config(config)
    return val


def set_scan_frequency_nse(minutes: int) -> int:
    val = _clamp_minutes(minutes)
    config = load_runtime_config()
    config["scan_frequency_nse"] = val
    save_runtime_config(config)
    return val


def set_scan_frequency_mcx(minutes: int) -> int:
    val = _clamp_minutes(minutes)
    config = load_runtime_config()
    config["scan_frequency_mcx"] = val
    save_runtime_config(config)
    return val
