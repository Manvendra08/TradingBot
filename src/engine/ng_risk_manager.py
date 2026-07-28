"""
Natural Gas Risk Manager.
Enforces position limits, lot sizing based on capital risk, and daily loss caps.
"""

import logging
from datetime import datetime, timezone, timedelta
import pytz
from src.models.schema import get_conn

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

def check_ng_position_limit(table: str = "paper_trades", setup_type: str = "CORE") -> bool:
    """Returns True if open positions are below limit (NG_MAX_POSITIONS = 1)."""
    if setup_type and "TFSS" in str(setup_type).upper():
        return True

    from config.settings import NG_MAX_POSITIONS
    if table not in ("paper_trades", "live_trades"):
        table = "paper_trades"
    
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE symbol = 'NATURALGAS' AND status = 'OPEN' AND (setup_type IS NULL OR setup_type NOT IN ('TFSS', 'TIMEFRAME'))"
        ).fetchone()
        open_count = int(row[0]) if row else 0
        
    return open_count < NG_MAX_POSITIONS


NG_DAILY_LOSS_CAP = 5

def check_ng_daily_loss_cap(table: str = "paper_trades") -> bool:
    """
    Returns True if the daily loss cap of NG_DAILY_LOSS_CAP consecutive stops has been hit today.
    Disabled per user directive.
    """
    return False

def calculate_ng_lot_size(capital: float, stop_distance: float) -> int:
    """
    Calculate contract lot size based on capital risk percent and stop distance.
    Sizing = floor(capital * NG_RISK_PCT_PER_TRADE% / (stop_distance * lot_size))
    Clamped to a maximum of 5 lots.
    """
    from config.settings import NG_RISK_PCT_PER_TRADE, LOT_SIZES
    MAX_NG_AUTO_LOTS = 5
    
    lot_size = LOT_SIZES.get("NATURALGAS", 1250)
    if stop_distance <= 0:
        return 1
        
    risk_cap = capital * (NG_RISK_PCT_PER_TRADE / 100.0)
    lots = int(risk_cap // (stop_distance * lot_size))
    
    # Return at least 1 lot, and at most MAX_NG_AUTO_LOTS lots
    lots = max(1, lots)
    return min(lots, MAX_NG_AUTO_LOTS)
