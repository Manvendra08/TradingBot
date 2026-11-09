"""
Configuration for the Trend Following Short Strangle (TFSS) strategy.
"""
from typing import TypedDict, List, Dict

STRATEGY_MODE = "TREND_FOLLOWING_SHORT_STRANGLE"
PERSISTENCE_WINDOW = 5
PERSISTENCE_MIN_MATCH = 3
REQUIRE_BROAD_CORROBORATION = False

# Master switch for TFSS Trade Blocked Rules (persistence history, min match, tranches, delta caps).
ENABLE_TFSS_TRADE_BLOCKED_RULES = True

# Master switch for TFSS Multi-Leg Tranche Sizing Logic (scaling down per tranche).
ENABLE_TFSS_TRANCHE_SCALING = True

# Disaster wing pairing (far-OTM 0.03-delta wing to cap black swan gap risk)
TFSS_ENABLE_DISASTER_WINGS = True
TFSS_DISASTER_WING_DELTA = 0.03

class DteDeltaBand(TypedDict):
    min_dte: int
    max_dte: int
    base_delta_min: float
    base_delta_max: float
    tight_delta_min: float
    tight_delta_max: float

# Default delta bands by Days to Expiry (DTE)
DTE_DELTA_BANDS: List[DteDeltaBand] = [
    {
        "min_dte": 0, "max_dte": 2,
        "base_delta_min": 0.10, "base_delta_max": 0.20,
        "tight_delta_min": 0.05, "tight_delta_max": 0.15,
    },
    {
        "min_dte": 3, "max_dte": 7,
        "base_delta_min": 0.15, "base_delta_max": 0.25,
        "tight_delta_min": 0.10, "tight_delta_max": 0.20,
    },
    {
        "min_dte": 8, "max_dte": 30,
        "base_delta_min": 0.20, "base_delta_max": 0.30,
        "tight_delta_min": 0.15, "tight_delta_max": 0.25,
    }
]

# Tranche sequence for multi-tranche scaling: 50% on Tranche 0, 30% on Tranche 1, 20% on Tranche 2
TRANCHE_SEQUENCE = [0.50, 0.30, 0.20]

# High priority overrides lower priority. 1 is highest priority.
EXIT_PRIORITY_MAP = {
    "RISK_CAP_EXCEEDED": 1,
    "DELTA_STOP": 2,
    "TREND_REVERSAL": 3,
    "PROFIT_TARGET": 4,
    "TIME_DECAY_EXIT": 5,
}

ATR_TIGHTENING_MULTIPLIER_THRESHOLD = 1.5

# Multi-leg book caps and thresholds
TFSS_COMBINED_DELTA_CAP = 0.60
HARD_STOP_DELTA = 0.50
REBALANCE_DELTA_THRESHOLD = 0.45  # Dynamic delta rebalancing triggered when leg delta reaches 0.45
TIME_DECAY_PROFIT_TARGET_PCT = 0.35  # 35% time decay profit target for option sell legs

# Legacy fallback cap
TFSS_MAX_BOOK_MARGIN = 500000.0


def get_tfss_max_book_margin() -> float:
    """Dynamically scale TFSS book margin capacity to 40% of total portfolio capital."""
    try:
        from config.runtime_config import get_total_capital
        tot_cap = get_total_capital()
        return max(500000.0, float(tot_cap) * 0.40)
    except Exception:
        return 500000.0
