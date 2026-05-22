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
    if not RUNTIME_CONFIG_PATH.exists():
        return {"scan_frequency_minutes": _clamp_minutes(int(FETCH_INTERVAL_MINUTES))}
    try:
        data = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
        minutes = _clamp_minutes(data.get("scan_frequency_minutes", FETCH_INTERVAL_MINUTES))
        return {"scan_frequency_minutes": minutes}
    except Exception:
        return {"scan_frequency_minutes": _clamp_minutes(int(FETCH_INTERVAL_MINUTES))}


def get_scan_frequency_minutes() -> int:
    return load_runtime_config()["scan_frequency_minutes"]


def set_scan_frequency_minutes(minutes: int) -> int:
    val = _clamp_minutes(minutes)
    payload = {"scan_frequency_minutes": val}
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return val
