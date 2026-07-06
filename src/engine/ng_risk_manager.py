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

def check_ng_position_limit() -> bool:
    """Returns True if open positions are below limit (NG_MAX_POSITIONS = 1)."""
    from config.settings import NG_MAX_POSITIONS
    
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE symbol = 'NATURALGAS' AND status = 'OPEN'"
        ).fetchone()
        open_count = int(row[0]) if row else 0
        
    return open_count < NG_MAX_POSITIONS

def check_ng_daily_loss_cap() -> bool:
    """
    Returns True if the daily loss cap of 2 consecutive stops has been hit today.
    """
    # Get today's date in IST
    now_ist = datetime.now(IST)
    today_ist_start = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert to UTC ISO format string for comparing stored opened_at timestamps
    today_utc_iso = today_ist_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    
    with get_conn() as conn:
        # Get last 2 closed trades for NATURALGAS today
        rows = conn.execute(
            """
            SELECT status FROM paper_trades 
            WHERE symbol = 'NATURALGAS' 
              AND status != 'OPEN'
              AND opened_at >= ?
            ORDER BY closed_at DESC 
            LIMIT 2
            """,
            (today_utc_iso,)
        ).fetchall()
        
    statuses = [r["status"] for r in rows]
    # Check if we have 2 closed trades today, and both are SL
    if len(statuses) >= 2 and all(s == "CLOSED_SL" for s in statuses):
        log.warning("NG Daily Loss Cap hit! 2 consecutive stops hit today.")
        return True
        
    return False

def calculate_ng_lot_size(capital: float, stop_distance: float) -> int:
    """
    Calculate contract lot size based on capital risk percent and stop distance.
    Sizing = floor(capital * NG_RISK_PCT_PER_TRADE% / (stop_distance * lot_size))
    """
    from config.settings import NG_RISK_PCT_PER_TRADE, LOT_SIZES
    
    lot_size = LOT_SIZES.get("NATURALGAS", 1250)
    if stop_distance <= 0:
        return 1
        
    risk_cap = capital * (NG_RISK_PCT_PER_TRADE / 100.0)
    lots = int(risk_cap // (stop_distance * lot_size))
    
    # Return at least 1 lot if capital allows
    return max(1, lots)
