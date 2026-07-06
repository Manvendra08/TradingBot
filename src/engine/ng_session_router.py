"""
Session router for NATURALGAS.
Determines the current market regime based on time, holidays, and events.
Regimes:
- PARITY: NYMEX is closed/thin; MCX trades around fair value.
- MOMENTUM: NYMEX is live.
- EVENT: EIA storage report window (Thursday).
- BLOCKED: Dead zones, weekends, CME holidays, expiry week.
"""

from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
NY_TZ = pytz.timezone("America/New_York")

def get_ng_regime(now_ist: datetime) -> tuple[str, str]:
    """
    Returns (regime, reason).
    Precedence:
    1. CME holiday / weekend -> BLOCKED
    2. EVENT (Thu around 10:30 AM NY Time) -> EVENT
    3. PARITY (09:00 - 17:30 IST) -> PARITY
    4. MOMENTUM (18:00 - 23:00 IST) -> MOMENTUM
    5. Else -> BLOCKED
    """
    if now_ist.weekday() >= 5:  # Saturday/Sunday
        return "BLOCKED", "Weekend"
        
    from config.cme_holidays import is_cme_closed
    if is_cme_closed(now_ist.date()):
        return "BLOCKED", "CME Holiday"

    # Thursday EIA Storage Report Event
    if now_ist.weekday() == 3:
        # Calculate 10:30 AM in New York for today
        ny_now = now_ist.astimezone(NY_TZ)
        ny_eia_time = ny_now.replace(hour=10, minute=30, second=0, microsecond=0)
        eia_ist = ny_eia_time.astimezone(IST)
        
        # Window: T-15 mins to T+90 mins
        delta_mins = (now_ist - eia_ist).total_seconds() / 60.0
        if -15 <= delta_mins <= 90:
            return "EVENT", "EIA Storage Report Window"

    h, m = now_ist.hour, now_ist.minute
    time_val = h * 60 + m
    
    # PARITY: 09:00 (540) to 17:30 (1050)
    if 540 <= time_val <= 1050:
        return "PARITY", "09:00-17:30 IST Parity Regime"
        
    # MOMENTUM: 18:00 (1080) to 23:00 (1380)
    if 1080 <= time_val <= 1380:
        return "MOMENTUM", "18:00-23:00 IST NYMEX Live"
        
    return "BLOCKED", "Outside Active NG Regimes (Handoff or Late)"
