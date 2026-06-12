"""Runtime-configurable settings persisted to data/runtime_config.json."""
from __future__ import annotations

import json
from pathlib import Path

from config.settings import DATA_DIR, FETCH_INTERVAL_MINUTES

RUNTIME_CONFIG_PATH = DATA_DIR / "runtime_config.json"
ALLOWED_SCAN_FREQUENCIES = [5, 15, 30, 60, 180, 1440]
MIN_SCAN_FREQUENCY = ALLOWED_SCAN_FREQUENCIES[0]
MAX_SCAN_FREQUENCY = ALLOWED_SCAN_FREQUENCIES[-1]


def _clamp_minutes(value: int) -> int:
    v = int(value)
    if v in ALLOWED_SCAN_FREQUENCIES:
        return v
    # fallback to nearest allowed value
    return min(ALLOWED_SCAN_FREQUENCIES, key=lambda x: abs(x - v))


def load_runtime_config() -> dict:
    default_freq = _clamp_minutes(int(FETCH_INTERVAL_MINUTES))
    if not RUNTIME_CONFIG_PATH.exists():
        return {
            "scan_frequency_minutes": default_freq,
            "scan_frequency_nse": default_freq,
            "scan_frequency_mcx": default_freq,
        }
    try:
        data = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
        minutes = _clamp_minutes(data.get("scan_frequency_minutes", FETCH_INTERVAL_MINUTES))
        nse = _clamp_minutes(data.get("scan_frequency_nse", minutes))
        mcx = _clamp_minutes(data.get("scan_frequency_mcx", minutes))
        return {
            "scan_frequency_minutes": minutes,
            "scan_frequency_nse": nse,
            "scan_frequency_mcx": mcx,
        }
    except Exception:
        return {
            "scan_frequency_minutes": default_freq,
            "scan_frequency_nse": default_freq,
            "scan_frequency_mcx": default_freq,
        }


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
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return val


def set_scan_frequency_nse(minutes: int) -> int:
    val = _clamp_minutes(minutes)
    config = load_runtime_config()
    config["scan_frequency_nse"] = val
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return val


def set_scan_frequency_mcx(minutes: int) -> int:
    val = _clamp_minutes(minutes)
    config = load_runtime_config()
    config["scan_frequency_mcx"] = val
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return val
