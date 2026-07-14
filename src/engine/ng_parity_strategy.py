"""
Natural Gas Parity Strategy (Session A).
Mean-reversion trading strategy running between 09:00 and 17:30 IST.
"""

import logging
from datetime import datetime, timezone
import pytz
from src.models.schema import get_conn, get_open_paper_trade, insert_paper_trade, close_paper_trade
from src.engine.parity_engine import get_parity_state
from src.engine.ng_risk_manager import check_ng_position_limit, check_ng_daily_loss_cap, calculate_ng_lot_size
from config.runtime_config import load_runtime_config
from config.settings import LOT_SIZES

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

def check_deviation_stable_or_shrinking(current_dev: float) -> bool:
    """
    Returns True if deviation is stable or shrinking vs the last logged parity record.
    If no previous record is found, returns True.
    """
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT dev_pct FROM ng_parity_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return True
            prev_dev = float(row["dev_pct"])
            # Shrinking or stable means absolute value is not expanding further away from 0.
            # E.g. last dev was 0.6%, current is 0.5% (shrinking).
            # If signs are opposite, deviation has already crossed parity, which is fine to trade.
            if (prev_dev >= 0 and current_dev >= 0) or (prev_dev <= 0 and current_dev <= 0):
                return abs(current_dev) <= abs(prev_dev)
    except Exception as e:
        log.warning("Failed to check shrinking deviation: %s", e)
    return True

def run_ng_parity_strategy(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict=None,
) -> dict | None:
    """
    Natural Gas Parity Strategy Runner (Session A).
    """
    from config.settings import NG_STRATEGY_ENABLED, PARITY_DEV_ENTRY_PCT, PARITY_DEV_STOP_MULT
    
    if not NG_STRATEGY_ENABLED:
        return None

    now_ist = datetime.now(IST)
    from src.engine.ng_session_router import get_ng_regime
    regime, reason = get_ng_regime(now_ist)

    if regime != "PARITY":
        return None

    underlying = float((scan_context or {}).get("underlying") or 0.0)
    if underlying <= 0:
        return None

    # Calculate fair value and deviation
    parity_state = get_parity_state(underlying)
    if not parity_state.valid:
        log.warning("NG Parity Strategy: Parity calculations are invalid/stale. Blocking entry.")
        return {"action": "BLOCKED_PLAN", "reason": "Parity calculations invalid/stale"}

    dev_pct = parity_state.dev_pct
    abs_dev = abs(dev_pct)

    # Entry Check 1: Deviation threshold
    if abs_dev < PARITY_DEV_ENTRY_PCT:
        return {"action": "HOLD", "reason": f"Deviation {dev_pct:+.2f}% < threshold {PARITY_DEV_ENTRY_PCT}%"}

    # Entry Check 2: Position limit
    if not check_ng_position_limit():
        return {"action": "BLOCKED_RISK", "reason": "NG position limit hit"}

    # Entry Check 3: Daily loss cap
    if check_ng_daily_loss_cap():
        log.warning("NG Parity Entry blocked: Daily loss cap hit.")
        return {"action": "BLOCKED_RISK", "reason": "NG daily loss cap hit"}

    # Entry Check 4: Shrinking deviation
    if not check_deviation_stable_or_shrinking(dev_pct):
        log.info("NG Parity Entry blocked: Deviation is expanding (catching the tail).")
        return {"action": "HOLD", "reason": f"Deviation expanding ({dev_pct:+.2f}%)"}

    # Determine Side: dev > 0 (MCX rich) -> SELL FUT; dev < 0 (MCX cheap) -> BUY FUT
    side = "SELL" if dev_pct > 0 else "BUY"
    verdict = "NG Parity - Short" if side == "SELL" else "NG Parity - Long"

    # Sizing calculations
    config = load_runtime_config()
    capital = float(config.get("live_capital_per_trade_inr") or 50000.0)
    
    # Stop distance in rupees/points: dev_pct * multiplier
    stop_distance_points = abs(dev_pct * PARITY_DEV_STOP_MULT / 100.0 * underlying)
    
    # Link lots to number defined in Setting cockpit (runtime_config.json)
    try:
        saved_lots = config.get("paper_symbol_lots") or {}
        if "NATURALGAS" in saved_lots:
            lots = max(1, int(saved_lots["NATURALGAS"]))
        else:
            lots = calculate_ng_lot_size(capital, stop_distance_points)
    except Exception as e:
        log.warning("Failed to load cockpit lots for NATURALGAS parity trade: %s", e)
        lots = calculate_ng_lot_size(capital, stop_distance_points)

    sl_underlying = underlying + stop_distance_points if side == "SELL" else underlying - stop_distance_points
    # Target is parity (deviation = 0)
    target_underlying = parity_state.fair_value

    opened_at = datetime.now(timezone.utc).isoformat()
    signal_key = f"NG_PARITY_{opened_at}_{side}"

    trade_data = {
        "opened_at": opened_at,
        "symbol": "NATURALGAS",
        "expiry": scan_context.get("futures_expiry"),
        "verdict_label": verdict,
        "side": side,
        "option_type": "FUT",
        "strike": None,
        "entry_underlying": underlying,
        "entry_premium": underlying,
        "sl_underlying": sl_underlying,
        "target_underlying": target_underlying,
        "lots": lots,
        "status": "OPEN",
        "reason": f"NG Parity entry | dev={dev_pct:.2f}% (threshold={PARITY_DEV_ENTRY_PCT}%)",
        "digest_id": digest_id,
        "trade_status": "TRIGGERED_CORE",
        "setup_type": "NG_PARITY",
        "decision_reason": f"Parity deviation {dev_pct:.2f}% triggers {side}",
        "confidence_score": 100,
        "entry_quality_score": 100,
        "trend_alignment_score": 100,
        "regime_score": 100,
        "signal_key": signal_key,
        "regime": "PARITY",
        "underlying": underlying,
        "entry_dev_pct": dev_pct
    }

    trade_id = insert_paper_trade(trade_data)
    if trade_id:
        log.info("Opened NG Parity paper trade #%d | %s FUT %d lots at %g | SL %g, Tgt %g",
                 trade_id, side, lots, underlying, sl_underlying, target_underlying)
        try:
            from src.alerts.telegram_dispatcher import send_text
            send_text(f"🚀 **NG PARITY Paper Trade OPENED**\n"
                      f"• Side: {side} FUT\n"
                      f"• Price: ₹{underlying:.2f}\n"
                      f"• Deviation: {dev_pct:.2f}%\n"
                      f"• Lots: {lots} (lot size: {LOT_SIZES.get('NATURALGAS', 1250)})\n"
                      f"• SL: ₹{sl_underlying:.2f} | Tgt: ₹{target_underlying:.2f}")
        except Exception:
            pass
        return {"action": "EXECUTED", "trade_id": trade_id, "reason": "Parity trade opened"}

    return None

def check_ng_parity_exits_every_2_min() -> None:
    """
    Evaluates open Natural Gas parity trades. Runs every 2 minutes in the background.
    """
    open_trade = get_open_paper_trade("NATURALGAS")
    if not open_trade or open_trade.get("setup_type") != "NG_PARITY":
        return

    # Fetch real-time underlying price
    from src.fetchers.router import fetch_option_chain
    oc = fetch_option_chain("NATURALGAS")
    if not oc:
        log.warning("NG Parity exits check: Failed to fetch underlying price for NATURALGAS")
        return

    underlying = oc.get("underlying_price")
    if not underlying or underlying <= 0:
        return

    # Calculate current deviation
    parity_state = get_parity_state(underlying)
    
    # Load exit constants
    from config.settings import PARITY_DEV_STOP_MULT
    
    dev_pct = parity_state.dev_pct
    entry_dev = float(open_trade.get("entry_underlying") or underlying)  # deviation is proportional to fair value
    entry_dev_pct = float(open_trade.get("entry_dev_pct") or 0.0)

    side = open_trade["side"]
    sl_pct = abs(entry_dev_pct) * PARITY_DEV_STOP_MULT

    # Check Exits:
    hit_target = abs(dev_pct) <= 0.10
    hit_sl = abs(dev_pct) >= sl_pct
    feed_lost = not parity_state.valid

    # Hard time-stop flat at 17:30 IST
    now_ist = datetime.now(IST)
    is_time_stop = now_ist.time() >= datetime.strptime("17:30", "%H:%M").time()

    exit_hit = hit_target or hit_sl or feed_lost or is_time_stop
    if exit_hit:
        status = "CLOSED"
        reason = ""
        if hit_target:
            status = "CLOSED_TARGET"
            reason = f"Parity reached | dev_pct = {dev_pct:.2f}%"
        elif hit_sl:
            status = "CLOSED_SL"
            reason = f"Deviation stop hit | dev_pct = {dev_pct:.2f}% (SL threshold = {sl_pct:.2f}%)"
        elif feed_lost:
            reason = "Feed invalid / stale legs"
        elif is_time_stop:
            reason = "Force-flat handoff 17:30 IST"

        close_paper_trade(
            open_trade["id"],
            datetime.now(timezone.utc).isoformat(),
            underlying,
            underlying,
            status,
            reason
        )
        log.info("Closed NG Parity paper trade #%d | reason: %s at price %g",
                 open_trade["id"], reason, underlying)
        try:
            from src.alerts.telegram_dispatcher import send_text
            pnl_text = "PnL Net: (calculated in db)"
            send_text(f"🛑 **NG PARITY Paper Trade CLOSED**\n"
                      f"• Trade ID: #{open_trade['id']}\n"
                      f"• Reason: {reason}\n"
                      f"• Price: ₹{underlying:.2f}\n"
                      f"• Current Deviation: {dev_pct:.2f}%")
        except Exception:
            pass
