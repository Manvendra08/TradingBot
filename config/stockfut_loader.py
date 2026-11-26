"""Loader for stock futures configuration (config/stockfut.json).
Fails closed with safe defaults if file is missing, corrupt, or invalid.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("stockfut.config")

CONFIG_PATH = Path(__file__).resolve().parent / "stockfut.json"

DEFAULT_STOCKFUT_CONFIG: Dict[str, Any] = {
    "stockfut_live_enabled": False,
    "capital_reference": 10000000.0,
    "risk_budget_pct": 0.75,
    "min_lots": 2,
    "max_risk_multiplier": 1.25,
    "max_open_positions": 6,
    "max_positions_per_sector": 2,
    "max_net_directional_pct": 0.60,
    "max_gross_notional_mult": 2.0,
    "gap_stress_loss_cap_pct": 0.06,
    "margin_headroom_min_pct": 0.40,
    "daily_loss_limit_pct": 0.02,
    "weekly_loss_limit_pct": 0.04,
    "consecutive_loss_limit": 3,
    "drawdown_kill_switch_pct": 0.08,
    "min_sessions_to_expiry_near": 1,
    "expiry_squareoff_sessions": 0,
    "events_blackout_sessions": 7,
    "mwpl_max_pct": 80.0,
    "min_oi_value_cr": 50.0,
    "max_spread_pct": 0.10,
    "sl_atr_min": 0.75,
    "sl_atr_max": 2.0,
    "target_r_ratio": 1.6,
    "opposing_zone_atr_buffer": 0.5,
    "entry_max_drift_atr": 0.6,
    "gtt_sl_limit_buffer_atr": 0.3,
    "runner_sl_entry_buffer_atr": 0.25,
    "scoring_weights": {
        "trend": 25,
        "relative_strength": 25,
        "oi_flow": 30,
        "regime_fit": 20,
    },
    "matrix_permissions": {
        "STRONG_BULL": {
            "RISK_ON": {"side": "LONG", "min_score": 65},
            "NEUTRAL": {"side": "LONG", "min_score": 70},
            "RISK_OFF": {"side": "LONG", "min_score": 80},
        },
        "BULL": {
            "RISK_ON": {"side": "LONG", "min_score": 70},
            "NEUTRAL": {"side": "LONG", "min_score": 75},
            "RISK_OFF": None,
        },
        "NEUTRAL": {
            "RISK_ON": None,
            "NEUTRAL": None,
            "RISK_OFF": None,
        },
        "BEAR": {
            "RISK_ON": None,
            "NEUTRAL": {"side": "SHORT", "min_score": 75},
            "RISK_OFF": {"side": "SHORT", "min_score": 70},
        },
        "STRONG_BEAR": {
            "RISK_ON": {"side": "SHORT", "min_score": 80},
            "NEUTRAL": {"side": "SHORT", "min_score": 70},
            "RISK_OFF": {"side": "SHORT", "min_score": 65},
        },
    },
    "whitelist": [],
}


def load_stockfut_config(config_path: Path | str | None = None) -> Dict[str, Any]:
    """Load stock futures configuration with fail-closed fallback."""
    path = Path(config_path) if config_path else CONFIG_PATH
    if not path.is_file():
        logger.warning("Stockfut config file not found at %s; using safe defaults", path)
        return copy.deepcopy(DEFAULT_STOCKFUT_CONFIG)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.error("Stockfut config is not a JSON object; failing closed")
            return copy.deepcopy(DEFAULT_STOCKFUT_CONFIG)

        # Merge with defaults to ensure all keys exist
        merged = copy.deepcopy(DEFAULT_STOCKFUT_CONFIG)
        merged.update(data)
        return merged
    except Exception as exc:
        logger.error("Failed to parse stockfut config at %s: %s; failing closed", path, exc)
        return copy.deepcopy(DEFAULT_STOCKFUT_CONFIG)
