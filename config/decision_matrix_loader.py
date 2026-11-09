"""
Decision Matrix Loader & Hot-Reload Engine for Indian Markets.
Maintains fail-closed defaults, schema validation, mtime-based hot-reloading,
and atomic file writes.
"""

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DECISION_MATRIX_PATH = Path(__file__).parent / "decision_matrix.json"

_CACHED_CONFIG: dict[str, Any] | None = None
_CACHED_MTIME: float = 0.0
_CONFIG_LOCK = threading.RLock()

# Deep copyable compile-time fallback defaults (fail-closed)
DEFAULTS: dict[str, Any] = {
    "version": "1.0.0",
    "mode": "shadow",
    "vol_buckets": {
        "low_max": 25.0,
        "mid_min": 25.0,
        "mid_max": 70.0,
        "high_min": 70.0,
        "lookback_days": 30,
        "min_samples": 10,
        "fallback_bucket": "HIGH",
    },
    "regime_table": {
        "NEUTRAL": {
            "LOW": ["SHORT_STRANGLE"],
            "MID": ["IRON_CONDOR", "SHORT_STRANGLE"],
            "HIGH": ["IRON_CONDOR"],
        },
        "BULLISH": {
            "LOW": ["NO_TRADE"],
            "MID": ["BULL_PUT_SPREAD"],
            "HIGH": ["BULL_PUT_SPREAD", "JADE_LIZARD"],
        },
        "BEARISH": {
            "LOW": ["NO_TRADE"],
            "MID": ["BEAR_CALL_SPREAD"],
            "HIGH": ["BEAR_CALL_SPREAD"],
        },
    },
    "overrides": {
        "mcx_no_naked": {
            "enabled": True,
            "replacement": {"SHORT_STRANGLE": "IRON_CONDOR"},
        },
        "event_window": {
            "enabled": True,
            "hours_before": 1.0,
            "hours_after": 0.5,
            "action": "defined_risk_half_size",
        },
        "zero_dte": {
            "enabled": True,
            "no_entry_after_ist": "13:00",
        },
        "monthly_dte_window": {
            "enabled": True,
            "min_days": 3,
            "max_days": 35,
        },
        "min_credit": {
            "SHORT_STRANGLE": 15.0,
            "IRON_CONDOR": 8.0,
            "BULL_PUT_SPREAD": 5.0,
            "BEAR_CALL_SPREAD": 5.0,
        },
        "blocked_structures": [],
    },
    "delta_profile": "aggressive",
    "delta_bands": {
        "aggressive": {
            "indices": {
                "CREDIT_SPREAD_SHORT": {"min": 0.30, "max": 0.35, "danger_ceiling": 0.40},
                "CREDIT_SPREAD_LONG": {"min": 0.14, "max": 0.18, "danger_ceiling": 0.25},
                "IRON_CONDOR_SHORT": {"min": 0.24, "max": 0.28, "danger_ceiling": 0.32},
                "IRON_CONDOR_LONG": {"min": 0.08, "max": 0.12, "danger_ceiling": 0.15},
                "SHORT_STRANGLE_SHORT": {"min": 0.15, "max": 0.20, "danger_ceiling": 0.25},
            },
            "mcx": {
                "CREDIT_SPREAD_SHORT": {"min": 0.24, "max": 0.28, "danger_ceiling": 0.32},
                "CREDIT_SPREAD_LONG": {"min": 0.10, "max": 0.14, "danger_ceiling": 0.20},
                "IRON_CONDOR_SHORT": {"min": 0.18, "max": 0.22, "danger_ceiling": 0.25},
                "IRON_CONDOR_LONG": {"min": 0.08, "max": 0.12, "danger_ceiling": 0.15},
                "SHORT_STRANGLE_SHORT": {"min": 0.15, "max": 0.20, "danger_ceiling": 0.25},
            },
        },
        "defensive": {
            "indices": {
                "CREDIT_SPREAD_SHORT": {"min": 0.25, "max": 0.30, "danger_ceiling": 0.40},
                "CREDIT_SPREAD_LONG": {"min": 0.08, "max": 0.12, "danger_ceiling": 0.25},
                "IRON_CONDOR_SHORT": {"min": 0.18, "max": 0.22, "danger_ceiling": 0.32},
                "IRON_CONDOR_LONG": {"min": 0.05, "max": 0.08, "danger_ceiling": 0.15},
                "SHORT_STRANGLE_SHORT": {"min": 0.15, "max": 0.20, "danger_ceiling": 0.25},
            },
            "mcx": {
                "CREDIT_SPREAD_SHORT": {"min": 0.20, "max": 0.25, "danger_ceiling": 0.32},
                "CREDIT_SPREAD_LONG": {"min": 0.08, "max": 0.12, "danger_ceiling": 0.20},
                "IRON_CONDOR_SHORT": {"min": 0.12, "max": 0.18, "danger_ceiling": 0.25},
                "IRON_CONDOR_LONG": {"min": 0.05, "max": 0.08, "danger_ceiling": 0.15},
                "SHORT_STRANGLE_SHORT": {"min": 0.15, "max": 0.20, "danger_ceiling": 0.25},
            },
        },
    },
    "wings": {
        "NIFTY": 200,
        "BANKNIFTY": 500,
        "SENSEX": 600,
        "FINNIFTY": 200,
        "MIDCPNIFTY": 150,
        "NATURALGAS": 10.0,
        "CRUDEOIL": 100.0,
        "GOLD": 500.0,
        "SILVER": 1000.0,
    },
    "credit_rules": {
        "min_credit_ratio_full_size": 0.30,
        "min_credit_ratio_half_size": 0.25,
        "reject_below_ratio": 0.25,
    },
    "strike_rules": {
        "beyond_oi_wall": True,
    },
    "stops_targets": {
        "futures": {
            "sl_atr_mult": 1.5,
            "target_atr_mult": 2.0,
            "breakeven_atr_mult": 1.0,
            "trail_atr_mult": 1.0,
        },
        "options": {
            "spread_max_loss_ratio": 0.50,
            "strangle_credit_mult": 1.0,
            "monthly_strangle_credit_mult": 1.5,
            "target_credit_pct": 0.40,
            "target_credit_pct_expiry": 0.60,
            "capital_cap_pct": 0.03,
            "gap_stress_atr_mult": 2.0,
            "gap_stress_cap_pct": 0.03,
        },
    },
    "exits": {
        "priority_order": [
            "HARD_RISK",
            "TESTED_SIDE",
            "EVENT",
            "EXPIRY",
            "REGIME_FLIP",
            "TARGET",
            "DEAD_TRADE",
            "AI_TIGHTEN",
        ],
        "tested_side": {
            "defined_risk_delta_trigger": 0.48,
            "naked_delta_trigger": 0.35,
            "candle_close_15m_breach": True,
        },
        "event": {
            "close_minutes_prior": 30,
        },
        "expiry": {
            "indices_exit_time_ist": "15:15",
            "mcx_days_before_expiry": 1,
            "session_close_buffer_minutes": 10,
        },
        "regime_flip": {
            "confidence_threshold": 75,
            "require_nymex_agree_on_mcx": True,
        },
        "dead_trade": {
            "days_threshold": 3,
            "max_pnl_pct": 0.05,
        },
    },
    "ai_authority": {
        "veto_allowed": True,
        "size_cut_allowed": True,
        "tighten_stop_allowed": True,
        "widen_stop_allowed": False,
        "loosen_target_allowed": False,
        "close_allowed_min_profit_credit_pct": 0.30,
        "close_allowed_min_loss_stop_ratio": 0.50,
    },
    "nymex": {
        "mode": "shadow",
        "symbol_map": {
            "NATURALGAS": "NG=F",
            "CRUDEOIL": "CL=F",
            "GOLD": "GC=F",
            "SILVER": "SI=F",
        },
        "ema_span": 20,
        "cache_secs": 300,
    },
    "liquidity": {
        "mcx": {
            "min_oi": 1500,
            "max_abs_spread": 0.30,
            "max_spread_pct": 2.0,
            "require_quote": True,
            "spread_mode": "either",
            "hard_max_spread_pct": 15.0,
            "time_tiers": [
                {"before_hour": 12, "min_vol": 50},
                {"before_hour": 17, "min_vol": 150},
                {"default": 400},
            ],
        },
    },
    "events": [
        {"name": "RBI_POLICY", "datetime_ist": "2026-12-06T10:00:00", "symbols": ["NIFTY", "BANKNIFTY"]},
        {"name": "UNION_BUDGET", "datetime_ist": "2027-02-01T11:00:00", "symbols": ["NIFTY", "BANKNIFTY", "SENSEX"]},
    ],
    "churn_guards": {
        "cooldown_minutes": 30,
        "max_entries_per_day": 3,
        "max_open_books_per_symbol": 2,
    },
    "telegram": {
        "verbosity": "compact",
        "events": {
            "digest_line": True,
            "entry": True,
            "exit": True,
            "skip": True,
            "config": True,
        },
        "skip_cooldown_min": 30,
    },
    "overrides_by_symbol": {},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base dictionary."""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def validate_decision_matrix(cfg: dict[str, Any]) -> list[str]:
    """Validate decision matrix configuration. Returns list of error messages (empty if valid)."""
    errors: list[str] = []

    if not isinstance(cfg, dict):
        return ["Configuration root must be a dictionary"]

    mode = cfg.get("mode")
    if mode not in ("off", "shadow", "enforce"):
        errors.append(f"Invalid mode '{mode}': must be 'off', 'shadow', or 'enforce'")

    delta_profile = cfg.get("delta_profile")
    if delta_profile not in ("aggressive", "defensive"):
        errors.append(f"Invalid delta_profile '{delta_profile}': must be 'aggressive' or 'defensive'")

    delta_bands = cfg.get("delta_bands", {})
    if not isinstance(delta_bands, dict):
        errors.append("delta_bands must be a dictionary")
    else:
        for prof in ("aggressive", "defensive"):
            p_data = delta_bands.get(prof)
            if not isinstance(p_data, dict):
                errors.append(f"delta_bands missing profile '{prof}'")
                continue
            for asset_class in ("indices", "mcx"):
                a_data = p_data.get(asset_class)
                if not isinstance(a_data, dict):
                    errors.append(f"delta_bands.{prof} missing '{asset_class}'")
                    continue
                for structure, band in a_data.items():
                    if not isinstance(band, dict):
                        errors.append(f"Invalid band for {prof}.{asset_class}.{structure}")
                        continue
                    mn = band.get("min")
                    mx = band.get("max")
                    dc = band.get("danger_ceiling")
                    if not (isinstance(mn, (int, float)) and isinstance(mx, (int, float)) and isinstance(dc, (int, float))):
                        errors.append(f"Non-numeric delta values in {prof}.{asset_class}.{structure}")
                        continue
                    if not (0.0 < mn < mx <= 1.0):
                        errors.append(f"Invalid delta range ({mn}, {mx}) in {prof}.{asset_class}.{structure}")
                    if not (mx <= dc <= 1.0):
                        errors.append(f"Invalid danger_ceiling ({dc}) < max ({mx}) in {prof}.{asset_class}.{structure}")

    credit_rules = cfg.get("credit_rules", {})
    if isinstance(credit_rules, dict):
        full = credit_rules.get("min_credit_ratio_full_size", 0.0)
        half = credit_rules.get("min_credit_ratio_half_size", 0.0)
        reject = credit_rules.get("reject_below_ratio", 0.0)
        if not (0.0 < reject <= half <= full <= 1.0):
            errors.append(f"Invalid credit_rules hierarchy: reject({reject}) <= half({half}) <= full({full})")

    vol_buckets = cfg.get("vol_buckets", {})
    if isinstance(vol_buckets, dict):
        low_max = vol_buckets.get("low_max", 0.0)
        mid_min = vol_buckets.get("mid_min", 0.0)
        mid_max = vol_buckets.get("mid_max", 0.0)
        high_min = vol_buckets.get("high_min", 0.0)
        if not (0.0 < low_max <= mid_min <= mid_max <= high_min <= 100.0):
            errors.append(f"Invalid vol_buckets bounds: {low_max}, {mid_min}, {mid_max}, {high_min}")

    exits = cfg.get("exits", {})
    if isinstance(exits, dict):
        order = exits.get("priority_order", [])
        if not isinstance(order, list) or len(order) == 0:
            errors.append("exits.priority_order must be a non-empty list")

    return errors


def load_decision_matrix(
    path: Path | str | None = None,
    force_reload: bool = False,
) -> dict[str, Any]:
    """
    Load decision matrix config with hot-reloading and fail-closed fallback to DEFAULTS.
    Stamps 'config_version' on return.
    """
    global _CACHED_CONFIG, _CACHED_MTIME

    target_path = Path(path) if path else DECISION_MATRIX_PATH

    with _CONFIG_LOCK:
        if not force_reload and _CACHED_CONFIG is not None:
            try:
                current_mtime = os.path.getmtime(target_path)
                if current_mtime == _CACHED_MTIME:
                    return copy.deepcopy(_CACHED_CONFIG)
            except OSError:
                # File temporarily missing or unreadable -> keep existing cached config
                return copy.deepcopy(_CACHED_CONFIG)

        if not target_path.exists():
            log.warning("[decision_matrix] %s missing -> using fail-closed DEFAULTS", target_path)
            fallback = copy.deepcopy(DEFAULTS)
            fallback["config_version"] = f"{fallback.get('version', '1.0.0')}-fallback-missing"
            _CACHED_CONFIG = fallback
            _CACHED_MTIME = 0.0
            return copy.deepcopy(fallback)

        try:
            mtime = os.path.getmtime(target_path)
            content = target_path.read_text(encoding="utf-8")
            raw_cfg = json.loads(content)
            merged = _deep_merge(DEFAULTS, raw_cfg)

            errors = validate_decision_matrix(merged)
            if errors:
                log.warning(
                    "[decision_matrix] Config validation failed with %d errors: %s -> falling back to DEFAULTS",
                    len(errors),
                    "; ".join(errors),
                )
                fallback = copy.deepcopy(DEFAULTS)
                fallback["config_version"] = f"{fallback.get('version', '1.0.0')}-fallback-invalid"
                _CACHED_CONFIG = fallback
                _CACHED_MTIME = mtime
                return copy.deepcopy(fallback)

            merged["config_version"] = str(merged.get("version", "1.0.0"))
            _CACHED_CONFIG = merged
            _CACHED_MTIME = mtime
            log.debug("[decision_matrix] Loaded config version %s from %s", merged["config_version"], target_path)
            return copy.deepcopy(merged)

        except Exception as exc:
            log.exception("[decision_matrix] Error reading %s: %s -> falling back to DEFAULTS", target_path, exc)
            fallback = copy.deepcopy(DEFAULTS)
            fallback["config_version"] = f"{fallback.get('version', '1.0.0')}-fallback-exception"
            _CACHED_CONFIG = fallback
            _CACHED_MTIME = 0.0
            return copy.deepcopy(fallback)


def save_decision_matrix(
    cfg: dict[str, Any],
    path: Path | str | None = None,
) -> bool:
    """
    Atomically validate and save decision matrix config to disk.
    """
    global _CACHED_CONFIG, _CACHED_MTIME

    target_path = Path(path) if path else DECISION_MATRIX_PATH

    errors = validate_decision_matrix(cfg)
    if errors:
        log.error("[decision_matrix] Cannot save invalid config: %s", "; ".join(errors))
        return False

    with _CONFIG_LOCK:
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(cfg, indent=2)
            tmp_path = target_path.with_suffix(f".{os.getpid()}_{threading.get_ident()}.tmp")
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, target_path)

            _CACHED_CONFIG = copy.deepcopy(cfg)
            _CACHED_CONFIG["config_version"] = str(cfg.get("version", "1.0.0"))
            _CACHED_MTIME = os.path.getmtime(target_path)
            log.info("[decision_matrix] Saved config v%s to %s", _CACHED_CONFIG["config_version"], target_path)
            return True
        except Exception as exc:
            log.exception("[decision_matrix] Failed to save config to %s: %s", target_path, exc)
            return False


def get_decision_matrix_mode(
    target: str | dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
    symbol: str | None = None,
) -> str:
    """
    Return active decision matrix mode: 'off', 'shadow', or 'enforce'.
    Supports either get_decision_matrix_mode(symbol="NATURALGAS") or get_decision_matrix_mode(cfg).
    """
    if isinstance(target, dict):
        cfg = target
    elif isinstance(target, str) and not symbol:
        symbol = target

    if cfg is None:
        cfg = load_decision_matrix()

    if symbol and "overrides_by_symbol" in cfg and symbol in cfg["overrides_by_symbol"]:
        sym_ov = cfg["overrides_by_symbol"][symbol]
        if isinstance(sym_ov, dict) and "mode" in sym_ov:
            return str(sym_ov["mode"]).lower()

    return str(cfg.get("mode", "shadow")).lower()


# Backwards-compatible alias
get_matrix_mode = get_decision_matrix_mode
