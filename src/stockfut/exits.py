"""Stock Futures Exit Matrix Engine.
Implements Section 4.4:
Priority order:
1. HARD_STOP: Price through SL (Exit all)
2. GAP_THROUGH: Opens beyond SL (Cancel resting GTT -> ladder)
3. THESIS_BREAK: Daily close through support zone or 2 straight days opposing OI (Exit all)
4. EVENT: Results/AGM/ex-date within 1 session; ban entry (Exit all)
5. T1_HIT: Reaches T1 (Book floor(n/2), move runner SL to entry - 0.25 * ATR)
6. T2_HIT: Reaches T2 (Exit remainder)
7. TRAIL: After T1, daily close below last swing low or support zone (Exit remainder)
8. REGIME_TURN: Market regime flips against trade (Tighten SL)
9. TIME_STOP: T1 not hit in 5 sessions, or 10 sessions total (Exit all)
10. EXPIRY: Flat 5 sessions before expiry (delivery-margin window)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from src.stockfut.universe import count_trading_sessions, parse_date

log = logging.getLogger("stockfut.exits")
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class ExitDecision:
    rule: str
    action: str  # "EXIT_ALL" | "PARTIAL_T1" | "TIGHTEN_SL" | "HOLD"
    lots: int
    new_sl: Optional[float] = None
    reason: str = ""


def evaluate_future_exit(
    trade: Dict[str, Any],
    market: Dict[str, Any],
    now: Optional[datetime] = None,
    ban_list: Optional[Set[str]] = None,
    events_dict: Optional[Dict[str, List[str]]] = None,
    regime: Optional[str] = None,
    last_daily_bar: Optional[Dict[str, Any]] = None,
    daily_support_level: Optional[float] = None,
) -> Optional[ExitDecision]:
    """
    Evaluate exit rules in strict priority order.
    Returns the first matching ExitDecision or None.
    """
    now_dt = now or datetime.now(IST)
    today = now_dt.date()

    symbol = str(trade.get("symbol", "")).upper()
    side = str(trade.get("side", "LONG")).upper()
    is_long = side in ("LONG", "BUY")

    lots = int(trade.get("lots", 2))
    t1_lots = int(trade.get("t1_lots", lots // 2))
    t2_lots = int(trade.get("t2_lots", lots - t1_lots))
    remaining_qty = int(trade.get("remaining_qty", lots * int(trade.get("lot_size", 1))))
    lot_size = int(trade.get("lot_size", 1))
    remaining_lots = max(1, remaining_qty // lot_size) if lot_size > 0 else 1

    entry_price = float(trade.get("entry_price", 0.0))
    current_sl = float(trade.get("sl", 0.0))
    t1_price = float(trade.get("t1", 0.0))
    t2_price = float(trade.get("t2", 0.0))
    atr = float(trade.get("atr", market.get("atr", 10.0)))
    status = str(trade.get("status", "OPEN")).upper()

    ltp = float(market.get("price", 0.0))
    low = float(market.get("low", ltp))
    high = float(market.get("high", ltp))
    open_p = float(market.get("open", ltp))

    # 1. HARD_STOP
    if is_long and (ltp <= current_sl or low <= current_sl):
        return ExitDecision(
            rule="HARD_STOP",
            action="EXIT_ALL",
            lots=remaining_lots,
            reason=f"Price traded through SL ({ltp:.2f} <= {current_sl:.2f})",
        )
    elif not is_long and (ltp >= current_sl or high >= current_sl):
        return ExitDecision(
            rule="HARD_STOP",
            action="EXIT_ALL",
            lots=remaining_lots,
            reason=f"Price traded through SL ({ltp:.2f} >= {current_sl:.2f})",
        )

    # 2. GAP_THROUGH (Open price jumped beyond SL)
    if is_long and open_p <= current_sl:
        return ExitDecision(
            rule="GAP_THROUGH",
            action="EXIT_ALL",
            lots=remaining_lots,
            reason=f"Opened beyond SL ({open_p:.2f} <= {current_sl:.2f})",
        )
    elif not is_long and open_p >= current_sl:
        return ExitDecision(
            rule="GAP_THROUGH",
            action="EXIT_ALL",
            lots=remaining_lots,
            reason=f"Opened beyond SL ({open_p:.2f} >= {current_sl:.2f})",
        )

    # 3. THESIS_BREAK (Evaluated on last completed daily bar at 09:30)
    if last_daily_bar:
        last_close = float(last_daily_bar.get("close", 0.0))
        if daily_support_level is not None:
            if is_long and last_close < daily_support_level:
                return ExitDecision(
                    rule="THESIS_BREAK",
                    action="EXIT_ALL",
                    lots=remaining_lots,
                    reason=f"Daily close broke support zone ({last_close:.2f} < {daily_support_level:.2f})",
                )
            elif not is_long and last_close > daily_support_level:
                return ExitDecision(
                    rule="THESIS_BREAK",
                    action="EXIT_ALL",
                    lots=remaining_lots,
                    reason=f"Daily close broke resistance zone ({last_close:.2f} > {daily_support_level:.2f})",
                )

    # 4. EVENT (Results/AGM within 1 session, or entered ban list)
    if ban_list and symbol in ban_list:
        return ExitDecision(
            rule="EVENT",
            action="EXIT_ALL",
            lots=remaining_lots,
            reason="Held security entered F&O Ban list",
        )
    if events_dict and symbol in events_dict and events_dict[symbol]:
        return ExitDecision(
            rule="EVENT",
            action="EXIT_ALL",
            lots=remaining_lots,
            reason=f"Corporate event within 1 session: {events_dict[symbol]}",
        )

    # 5. T1_HIT (Reaches T1, only if status is still OPEN)
    if status == "OPEN":
        t1_touched = (ltp >= t1_price or high >= t1_price) if is_long else (ltp <= t1_price or low <= t1_price)
        if t1_touched:
            # Move runner stop to entry - 0.25 * atr (for long) or entry + 0.25 * atr (for short)
            noise_buffer = 0.25 * atr
            runner_sl = round(entry_price - noise_buffer if is_long else entry_price + noise_buffer, 2)
            return ExitDecision(
                rule="T1_HIT",
                action="PARTIAL_T1",
                lots=t1_lots,
                new_sl=runner_sl,
                reason=f"Target 1 reached at {t1_price:.2f}; book {t1_lots} lots, move runner SL to {runner_sl:.2f}",
            )

    # 6. T2_HIT (Reaches T2, applies to remaining position)
    t2_touched = (ltp >= t2_price or high >= t2_price) if is_long else (ltp <= t2_price or low <= t2_price)
    if t2_touched:
        return ExitDecision(
            rule="T2_HIT",
            action="EXIT_ALL",
            lots=remaining_lots,
            reason=f"Target 2 reached at {t2_price:.2f}; exit remaining {remaining_lots} lots",
        )

    # 7. TRAIL (After T1 hit, daily close below last swing low or support zone)
    if status == "PARTIAL" and last_daily_bar:
        last_close = float(last_daily_bar.get("close", 0.0))
        trail_level = daily_support_level or current_sl
        if is_long and last_close < trail_level:
            return ExitDecision(
                rule="TRAIL",
                action="EXIT_ALL",
                lots=remaining_lots,
                reason=f"Daily close below trailing support ({last_close:.2f} < {trail_level:.2f})",
            )
        elif not is_long and last_close > trail_level:
            return ExitDecision(
                rule="TRAIL",
                action="EXIT_ALL",
                lots=remaining_lots,
                reason=f"Daily close above trailing resistance ({last_close:.2f} > {trail_level:.2f})",
            )

    # 8. REGIME_TURN (Market regime flips against trade)
    if regime:
        regime_flipped = (is_long and regime == "RISK_OFF") or (not is_long and regime == "RISK_ON")
        if regime_flipped and last_daily_bar:
            swing_low = float(last_daily_bar.get("low", current_sl))
            swing_high = float(last_daily_bar.get("high", current_sl))
            tightened_sl = max(current_sl, swing_low) if is_long else min(current_sl, swing_high)
            if (is_long and tightened_sl > current_sl) or (not is_long and tightened_sl < current_sl):
                return ExitDecision(
                    rule="REGIME_TURN",
                    action="TIGHTEN_SL",
                    lots=remaining_lots,
                    new_sl=round(tightened_sl, 2),
                    reason=f"Regime flipped to {regime}; tighten SL to {tightened_sl:.2f}",
                )

    # 9. TIME_STOP (T1 not hit in 5 sessions, or 10 sessions total)
    entry_time_str = str(trade.get("entry_time", ""))
    if entry_time_str:
        try:
            entry_date = parse_date(entry_time_str)
            sessions_held = count_trading_sessions(entry_date, today)
            if status == "OPEN" and sessions_held >= 5:
                return ExitDecision(
                    rule="TIME_STOP",
                    action="EXIT_ALL",
                    lots=remaining_lots,
                    reason=f"T1 not hit within 5 sessions (held {sessions_held} sessions)",
                )
            if sessions_held >= 10:
                return ExitDecision(
                    rule="TIME_STOP",
                    action="EXIT_ALL",
                    lots=remaining_lots,
                    reason=f"Maximum trade duration of 10 sessions reached (held {sessions_held} sessions)",
                )
        except Exception:
            pass

    # 10. EXPIRY (Exit 5 sessions before expiry to avoid physical settlement margin spikes)
    expiry_str = str(trade.get("expiry", ""))
    if expiry_str:
        try:
            expiry_date = parse_date(expiry_str)
            sessions_to_exp = count_trading_sessions(today, expiry_date)
            if sessions_to_exp <= 5:
                return ExitDecision(
                    rule="EXPIRY",
                    action="EXIT_ALL",
                    lots=remaining_lots,
                    reason=f"Only {sessions_to_exp} sessions to expiry (<= 5); exit to prevent delivery margin",
                )
        except Exception:
            pass

    return None
