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
    from config.settings import (
        NG_STRATEGY_ENABLED,
        PARITY_DEV_ENTRY_PCT,
        PARITY_DEV_MAX_ENTRY_PCT,
        PARITY_DEV_STOP_MULT,
        MAX_NG_PARITY_SL_POINTS,
        MIN_NG_PARITY_SL_POINTS,
        NG_MIN_DTE_ENTRY,
    )
    
    if not NG_STRATEGY_ENABLED:
        return None

    now_ist = datetime.now(IST)
    from src.engine.ng_session_router import get_ng_regime
    regime, reason = get_ng_regime(now_ist)

    if regime != "PARITY":
        return None

    # DTE Guard: Never enter parity mean-reversion trades on expiring contracts (DTE <= 3)
    expiry_str = scan_context.get("futures_expiry") or scan_context.get("expiry")
    if expiry_str:
        try:
            from datetime import datetime as _dt
            cleaned_exp = str(expiry_str).strip().split("T")[0].split()[0]
            exp_date = None
            for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
                try:
                    exp_date = _dt.strptime(cleaned_exp, fmt).date()
                    break
                except Exception:
                    continue
            if exp_date:
                dte = (exp_date - now_ist.date()).days
                if dte <= NG_MIN_DTE_ENTRY:
                    log.info("NG Parity Entry blocked: Expiring contract (DTE=%d <= %d). Rollover basis uncoupled.", dte, NG_MIN_DTE_ENTRY)
                    return {"action": "BLOCKED_EXPIRY", "reason": f"Expiring contract (DTE={dte} <= {NG_MIN_DTE_ENTRY})"}
        except Exception as e:
            log.debug("NG Parity DTE check error: %s", e)

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

    # Entry Check 1: Deviation threshold (must be within tradeable mean-reversion band)
    if abs_dev < PARITY_DEV_ENTRY_PCT:
        return {"action": "HOLD", "reason": f"Deviation {dev_pct:+.2f}% < threshold {PARITY_DEV_ENTRY_PCT}%"}

    if abs_dev > PARITY_DEV_MAX_ENTRY_PCT:
        log.warning(
            "NG Parity Entry blocked: Deviation %+.2f%% exceeds max safe threshold %.2f%% (event shock / rollover divergence).",
            dev_pct,
            PARITY_DEV_MAX_ENTRY_PCT,
        )
        return {
            "action": "BLOCKED_RISK",
            "reason": f"Deviation {dev_pct:+.2f}% exceeds max limit ({PARITY_DEV_MAX_ENTRY_PCT}%)",
        }

    # Entry Check 2: Position limit (enforced at NG_MAX_POSITIONS = 1)
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

    # Check Macro Context: Block selling rich MCX into an active BULLISH_TIGHTENING stance during rollover squeeze weeks
    try:
        from src.engine.ng_macro_context import get_active_ng_macro_context
        macro_ctx = get_active_ng_macro_context()
        macro_stance = macro_ctx.get("macro_stance", "NEUTRAL_BALANCED")
        squeeze_risk = macro_ctx.get("is_rollover_squeeze_risk", False)
        if side == "SELL" and macro_stance == "BULLISH_TIGHTENING" and squeeze_risk:
            log.warning("NG Parity Entry blocked: Selling rich MCX into front-month rollover short squeeze & BULLISH_TIGHTENING stance.")
            return {"action": "BLOCKED_RISK", "reason": "Rollover squeeze & BULLISH_TIGHTENING stance"}
    except Exception as e:
        log.debug("NG Parity Macro Context check error: %s", e)

    # Sizing calculations
    config = load_runtime_config()
    capital = float(config.get("live_capital_per_trade_inr") or 50000.0)
    
    # Stop distance in rupees/points: dev_pct * multiplier, capped strictly to realistic bounds
    raw_stop_points = abs(dev_pct * PARITY_DEV_STOP_MULT / 100.0 * underlying)
    stop_distance_points = min(MAX_NG_PARITY_SL_POINTS, max(MIN_NG_PARITY_SL_POINTS, raw_stop_points))

    sl_underlying = round(underlying + stop_distance_points if side == "SELL" else underlying - stop_distance_points, 2)
    # Target is parity (deviation = 0)
    target_underlying = round(parity_state.fair_value, 2)
    target_distance_points = abs(target_underlying - underlying)

    # R:R sanity check: Target must be at least equal to SL distance (1:1 minimum)
    if target_distance_points < stop_distance_points:
        log.info(
            "NG Parity Entry blocked: Unfavorable R:R (target=%.2f pts < SL=%.2f pts)",
            target_distance_points,
            stop_distance_points,
        )
        return {
            "action": "BLOCKED_RISK",
            "reason": f"Unfavorable R:R (target={target_distance_points:.2f} pts < SL={stop_distance_points:.2f} pts)",
        }
    
    # Link lots to number defined in Setting cockpit (runtime_config.json)
    from src.engine.capital_allocator import calculate_trade_lots

    is_broker_mode = not bool(config.get("live_broker_disabled", False))
    raw_lots = calculate_trade_lots(
        "NATURALGAS",
        underlying,
        side=side,
        is_paper=not is_broker_mode,
        setup_type="NG_PARITY",
        option_type="FUT",
    )
    # Natural Gas FUT parity is high-notional (1 lot = 1250 units ≈ ₹3.4 Lakh); clamp to 1 lot
    lots = max(1, min(raw_lots, 1))

    opened_at = datetime.now(timezone.utc).isoformat()
    signal_key = f"NG_PARITY_{opened_at}_{side}"

    trade_data = {
        "opened_at": opened_at,
        "symbol": "NATURALGAS",
        "expiry": scan_context.get("futures_expiry") or scan_context.get("expiry"),
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
    Evaluates open Natural Gas parity trades. Runs every 15 minutes in the background.
    """
    from config.symbol_classes import is_market_open
    from config.holidays import is_market_holiday
    from datetime import datetime, timezone, timedelta
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if not is_market_open("NATURALGAS", now_ist) or is_market_holiday("NATURALGAS", now_ist):
        return

    with get_conn(read_only=True) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NATURALGAS' AND status='OPEN' AND setup_type='NG_PARITY'"
        ).fetchall()
        open_trades = [dict(r) for r in rows] if rows else []

    if not open_trades:
        return

    # Fetch real-time underlying price
    from src.fetchers.router import fetch_option_chain
    trade_expiry = open_trades[0].get("expiry")
    oc = fetch_option_chain("NATURALGAS", expiry=trade_expiry)
    if not oc:
        log.warning("NG Parity exits check: Failed to fetch underlying price for NATURALGAS")
        return

    underlying = oc.get("underlying_price")
    if not underlying or underlying <= 0:
        return

    # Calculate current deviation
    parity_state = get_parity_state(underlying)
    
    # Load exit constants
    from config.settings import PARITY_DEV_STOP_MULT, MAX_NG_PARITY_SL_POINTS
    
    dev_pct = parity_state.dev_pct
    feed_lost = not parity_state.valid

    # Hard time-stop flat at 17:30 IST
    now_ist = datetime.now(IST)
    is_time_stop = now_ist.time() >= datetime.strptime("17:30", "%H:%M").time()

    for open_trade in open_trades:
        entry_underlying = float(open_trade.get("entry_underlying") or underlying)
        entry_dev_pct = float(open_trade.get("entry_dev_pct") or 0.0)
        side = open_trade["side"]
        sl_pct = abs(entry_dev_pct) * PARITY_DEV_STOP_MULT

        # Expiry guard: if contract DTE <= 1, force close before delivery / cash settlement
        t_expiry = open_trade.get("expiry")
        is_expiry_close = False
        if t_expiry:
            try:
                from datetime import datetime as _dt
                cleaned = str(t_expiry).strip().split("T")[0].split()[0]
                for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
                    try:
                        exp_d = _dt.strptime(cleaned, fmt).date()
                        break
                    except Exception:
                        continue
                else:
                    exp_d = None
                if exp_d and (exp_d - now_ist.date()).days <= 1:
                    is_expiry_close = True
            except Exception:
                pass

        # Check Exits (Directional):
        if side == "SELL":
            hit_sl = dev_pct >= sl_pct or (underlying - entry_underlying) >= MAX_NG_PARITY_SL_POINTS
            hit_target = dev_pct <= 0.10
        else:
            hit_sl = dev_pct <= -sl_pct or (entry_underlying - underlying) >= MAX_NG_PARITY_SL_POINTS
            hit_target = dev_pct >= -0.10

        exit_hit = hit_target or hit_sl or feed_lost or is_time_stop or is_expiry_close
        if exit_hit:
            status = "CLOSED"
            reason = ""
            if hit_target:
                status = "CLOSED_TARGET"
                reason = f"Parity reached | dev_pct = {dev_pct:.2f}%"
            elif hit_sl:
                status = "CLOSED_SL"
                reason = f"Deviation stop hit | dev_pct = {dev_pct:.2f}% (SL threshold = {sl_pct:.2f}%)"
            elif is_expiry_close:
                status = "CLOSED_MANUAL"
                reason = "Expiry rollover guard (DTE <= 1) — closed before expiry"
            elif feed_lost:
                reason = "Feed invalid / stale legs"
            elif is_time_stop:
                reason = "Force-flat handoff 17:30 IST"

            exit_premium = underlying

            close_paper_trade(
                open_trade["id"],
                datetime.now(timezone.utc).isoformat(),
                underlying,
                exit_premium,
                status,
                reason
            )
            log.info("Closed NG Parity paper trade #%d | reason: %s at price %g",
                     open_trade["id"], reason, underlying)
            try:
                from src.alerts.telegram_dispatcher import send_text
                send_text(f"🛑 **NG PARITY Paper Trade CLOSED**\n"
                          f"• Trade ID: #{open_trade['id']}\n"
                          f"• Reason: {reason}\n"
                          f"• Price: ₹{underlying:.2f}\n"
                          f"• Current Deviation: {dev_pct:.2f}%")
            except Exception:
                pass
