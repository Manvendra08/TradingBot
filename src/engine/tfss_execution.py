"""
Trend Following Short Strangle (TFSS) Standalone Execution System.

Decoupled, multi-tranche volatility harvesting execution engine supporting both
paper trading (run_tfss_strategy) and live/shadow execution (run_live_tfss_strategy)
with unified, selective delta-stop exit monitoring (monitor_tfss_exits).

Core Invariants:
1. Multi-Tranche Scaling: Tranches scale by TRANCHE_SEQUENCE [0.50, 0.30, 0.20].
2. Trend Persistence: Requires at least 3 of 5 scans agreeing on direction.
3. Dynamic Capital & Margin Capacity: Scales dynamically to 40% of total portfolio capital.
4. Selective Delta-Stop Exits: Delta stop (0.50) closes ONLY the tested leg;
   the opposite profitable decaying leg remains active to harvest full premium.
5. Disaster Tail-Risk Wings: Far-OTM 0.03-delta wing caps catastrophe gap risk and unlocks margin benefits.
6. Fail-Closed Broker Gate: Real Kite execution gated via authorize_broker_execution.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from config.settings import IST, LOT_SIZES
from config.trend_following_short_strangle import (
    STRATEGY_MODE,
    PERSISTENCE_WINDOW,
    PERSISTENCE_MIN_MATCH,
    ENABLE_TFSS_TRADE_BLOCKED_RULES,
    ENABLE_TFSS_TRANCHE_SCALING,
    DTE_DELTA_BANDS,
    TRANCHE_SEQUENCE,
    HARD_STOP_DELTA,
    REBALANCE_DELTA_THRESHOLD,
    TIME_DECAY_PROFIT_TARGET_PCT,
    TFSS_ENABLE_DISASTER_WINGS,
    TFSS_DISASTER_WING_DELTA,
    get_tfss_max_book_margin,
)
from src.engine.trend_following_short_strangle import (
    compute_persisted_trend,
    classify_direction,
    is_confirmed_reversal,
)
from src.engine.trade_plan import select_candidate, get_option_premium
from src.engine.capital_allocator import calculate_trade_lots
from src.engine.risk_engine import check_risk_limits, check_live_risk_limits, compute_combined_book
from src.models.schema import (
    get_open_tfss_legs,
    insert_paper_trade,
    insert_live_trade,
    close_paper_trade,
    close_live_trade,
    get_conn,
    get_read_conn,
)

log = logging.getLogger(__name__)


def _get_ist_now() -> datetime:
    return datetime.now(IST)


def _get_today_date_str() -> str:
    return datetime.now(IST).strftime("%Y%m%d")


def _is_tfss_trading_allowed(now_ist: datetime | None = None) -> tuple[bool, str]:
    """Check market trading hours window for TFSS entries.
    Allowed 09:18 - 15:15 IST on trading days.
    On Fridays, block new entries post 14:00 IST to avoid weekend close risk.
    """
    if now_ist is None:
        now_ist = _get_ist_now()

    # Weekend check
    if now_ist.weekday() >= 5:
        return False, "Market closed (Weekend)"

    time_val = now_ist.hour * 100 + now_ist.minute

    # Pre-open / opening volatility buffer (09:00 - 09:18)
    if time_val < 918:
        return False, f"TFSS pre-open buffer active ({time_val:04d} < 0918 IST)"

    # Closing Call Auction Session (CAS) begins at 15:15
    if time_val >= 1515:
        return False, f"TFSS CAS / market closing active ({time_val:04d} >= 1515 IST)"

    # Friday entry cutoff at 14:00 IST
    if now_ist.weekday() == 4 and time_val >= 1400:
        return False, f"TFSS Friday entry cutoff reached ({time_val:04d} >= 1400 IST)"

    return True, "Trading allowed"


def _extract_option_chain(scan_context: dict) -> list[dict]:
    """Extract list of option chain rows from scan_context or intel."""
    if not isinstance(scan_context, dict):
        return []
    chain = scan_context.get("option_chain") or scan_context.get("current_expiry_option_rows")
    if chain and isinstance(chain, list):
        return chain
    intel = scan_context.get("intel", {})
    if isinstance(intel, dict):
        chain = intel.get("option_rows") or intel.get("option_chain")
        if chain and isinstance(chain, list):
            return chain
    return []


def _get_option_rows_for_expiry(scan_context: dict, expiry: str | None, symbol: str = "") -> list[dict]:
    """Retrieve option chain rows strictly for the target expiry."""
    if not expiry:
        return _extract_option_chain(scan_context)
    exp_str = str(expiry).strip()
    curr_exp = str(scan_context.get("current_expiry") or "").strip()
    if curr_exp and exp_str == curr_exp:
        rows = scan_context.get("current_expiry_option_rows") or _extract_option_chain(scan_context)
        if rows:
            return rows

    target_exp = str(scan_context.get("expiry") or "").strip()
    if target_exp and exp_str == target_exp:
        rows = _extract_option_chain(scan_context)
        if rows:
            return rows

    sym = symbol or scan_context.get("symbol") or ""
    if sym and exp_str:
        try:
            from src.models.schema import get_latest_snapshots_for_symbol
            db_rows = get_latest_snapshots_for_symbol(sym, exp_str)
            if db_rows:
                return db_rows
        except Exception:
            pass

    return _extract_option_chain(scan_context)


def _find_disaster_wing(
    option_chain: list[dict],
    option_type: str,
    short_strike: float,
    underlying: float,
    target_delta: float = 0.03,
) -> dict | None:
    """Find a far-OTM tail-risk protection wing (~0.03 delta).
    PE wing must be below short_strike.
    CE wing must be above short_strike.
    """
    if not option_chain or short_strike <= 0:
        return None

    opt_type_upper = option_type.upper()
    candidates = []

    for row in option_chain:
        if str(row.get("option_type", "")).upper() != opt_type_upper:
            continue
        try:
            strike = float(row.get("strike", 0))
            if opt_type_upper == "PE" and strike >= short_strike:
                continue
            if opt_type_upper == "CE" and strike <= short_strike:
                continue

            raw_d = row.get("delta")
            if raw_d is not None and abs(float(raw_d)) > 0:
                delta = abs(float(raw_d))
            else:
                # Estimate implied delta from distance
                if underlying > 0:
                    dist_pct = (underlying - strike) / underlying * 100 if opt_type_upper == "PE" else (strike - underlying) / underlying * 100
                    if dist_pct >= 4.0:
                        delta = 0.02
                    elif dist_pct >= 2.5:
                        delta = 0.04
                    elif dist_pct >= 1.5:
                        delta = 0.08
                    else:
                        delta = 0.15
                else:
                    delta = 0.0

            prem_raw = row.get("ltp") or row.get("premium") or row.get("close")
            if prem_raw is None:
                continue
            premium = float(prem_raw)
            if premium <= 0.05:
                continue

            # Accept delta in 0.01 to 0.06 range
            if 0.01 <= delta <= 0.06:
                candidates.append({
                    "strike": strike,
                    "delta": delta,
                    "premium": premium,
                    "option_type": opt_type_upper,
                    "side": "BUY",
                })
        except (ValueError, TypeError):
            continue

    if not candidates:
        return None

    # Pick candidate closest to target delta (0.03)
    candidates.sort(key=lambda c: abs(c["delta"] - target_delta))
    return candidates[0]


def monitor_tfss_exits(symbol: str, scan_context: dict, table: str = "paper_trades") -> list[dict]:
    """Unified exit monitor for TFSS positions across both paper_trades and live_trades.

    Exit Precedents (Evaluated strictly):
    1. 3.0% Portfolio Capital Cap: Total cumulative loss across open TFSS legs >= 3% of capital -> Square off ALL.
    2. Friday Weekend Risk: Friday >= 15:35 IST -> Mandatory square off before CAS derivatives halt.
    3. Selective Delta Stop (HARD_STOP_DELTA = 0.50): Closes ONLY the tested side; decaying opposite side stays open.
    4. Profit Target (35% Time Decay): Captured 35%+ decay on short premium -> Lock profit.
    5. Confirmed Trend Reversal: Persisted 3/5 trend flips against the holding side -> Exit opposing leg.
    """
    if table not in ("paper_trades", "live_trades"):
        table = "paper_trades"

    open_legs = get_open_tfss_legs(symbol, table=table)
    if not open_legs:
        return []

    actions: list[dict] = []
    now_ist = _get_ist_now()
    now_iso = datetime.now(timezone.utc).isoformat()
    underlying = float(scan_context.get("underlying") or scan_context.get("spot") or 0.0)

    # 1. 3.0% Portfolio Capital Risk Check
    try:
        from config.runtime_config import get_total_capital
        tot_cap = float(get_total_capital() or 5000000.0)
    except Exception:
        tot_cap = 5000000.0
    max_allowed_book_loss = tot_cap * 0.03

    # Calculate cumulative open P&L for TFSS legs of this symbol
    total_unrealized_pnl = 0.0
    sample_expiry = open_legs[0].get("expiry") if open_legs else None
    opt_rows = _get_option_rows_for_expiry(scan_context, sample_expiry, symbol=symbol)

    leg_live_data: dict[int, dict] = {}
    for leg in open_legs:
        lid = leg["id"]
        exp = leg.get("expiry")
        strike = float(leg.get("strike") or 0)
        otype = str(leg.get("option_type") or "").upper()
        side = str(leg.get("side") or "").upper()
        entry_p = float(leg.get("entry_premium") or 0.0)
        lots = int(leg.get("lots") or 1)
        base_sym = symbol.upper().split()[0]
        lot_size = int(leg.get("lot_size") or LOT_SIZES.get(base_sym, 1))

        current_p = get_option_premium(symbol, exp, strike, otype, opt_rows) or entry_p
        # For SELL leg: profit = entry - current
        # For BUY leg: profit = current - entry
        if side == "SELL":
            pnl_pts = entry_p - current_p
        else:
            pnl_pts = current_p - entry_p
        leg_pnl_rupees = pnl_pts * lots * lot_size
        total_unrealized_pnl += leg_pnl_rupees

        # Find current delta
        current_delta = 0.0
        for r in opt_rows:
            if str(r.get("option_type", "")).upper() == otype and abs(float(r.get("strike", 0)) - strike) < 0.5:
                raw_d = r.get("delta")
                if raw_d is not None:
                    current_delta = abs(float(raw_d))
                break

        leg_live_data[lid] = {
            "current_premium": current_p,
            "current_delta": current_delta,
            "pnl_rupees": leg_pnl_rupees,
        }

    # Precedent 1: Global 3.0% Capital Stop
    if total_unrealized_pnl <= -max_allowed_book_loss:
        log.warning(
            "%s: TFSS EMERGENCY 3%% RISK CAP HIT — total P&L ₹%.2f breached limit -₹%.2f. Closing all %d open legs.",
            symbol, total_unrealized_pnl, max_allowed_book_loss, len(open_legs)
        )
        for leg in open_legs:
            lid = leg["id"]
            live_info = leg_live_data.get(lid, {})
            exit_p = live_info.get("current_premium")
            _close_single_tfss_leg(table, lid, now_iso, underlying, exit_p, "CLOSED_PORTFOLIO_3PCT_RISK_CAP", "3% Portfolio Risk Cap Exceeded")
            actions.append({"action": "CLOSED_PORTFOLIO_3PCT_RISK_CAP", "trade_id": lid, "symbol": symbol})
        return actions

    # Precedent 2: Friday Weekend Risk CAS close (>= 15:35 IST)
    time_val = now_ist.hour * 100 + now_ist.minute
    if now_ist.weekday() == 4 and time_val >= 1535:
        log.info("%s: TFSS Friday mandatory square-off (%04d IST). Closing all %d open legs.", symbol, time_val, len(open_legs))
        for leg in open_legs:
            lid = leg["id"]
            live_info = leg_live_data.get(lid, {})
            exit_p = live_info.get("current_premium")
            _close_single_tfss_leg(table, lid, now_iso, underlying, exit_p, "CLOSED_FRIDAY_WEEKEND_RISK", "Friday CAS Mandatory Square-off")
            actions.append({"action": "CLOSED_FRIDAY_WEEKEND_RISK", "trade_id": lid, "symbol": symbol})
        return actions

    # Precedents 3, 4, 5: Selective Delta-Stop, Profit Target, Reversal
    persisted = compute_persisted_trend(symbol, scan_context)

    for leg in open_legs:
        lid = leg["id"]
        side = str(leg.get("side") or "").upper()
        otype = str(leg.get("option_type") or "").upper()
        entry_p = float(leg.get("entry_premium") or 0.0)
        live_info = leg_live_data.get(lid, {})
        current_p = live_info.get("current_premium", entry_p)
        cur_delta = live_info.get("current_delta", 0.0)

        should_close = False
        exit_status = ""
        exit_log_reason = ""

        # Only evaluate delta stops and targets on short sell legs
        if side == "SELL":
            # 3. Selective Delta Stop (Tested Side Breach)
            if cur_delta >= HARD_STOP_DELTA:
                should_close = True
                exit_status = "CLOSED_DELTA_STOP"
                exit_log_reason = f"Selective Delta-Stop breached (cur_delta={cur_delta:.2f} >= {HARD_STOP_DELTA:.2f})"
                log.info(
                    "%s: TFSS leg #%s (%s %s %s) TESTED SIDE BREACH (delta %.2f >= %.2f) — selective exit (decaying opposite side stays active).",
                    symbol, lid, side, otype, leg.get("strike"), cur_delta, HARD_STOP_DELTA
                )

            # 4. Profit Target (35% Time Decay)
            elif entry_p > 0 and current_p <= entry_p * (1.0 - TIME_DECAY_PROFIT_TARGET_PCT):
                should_close = True
                exit_status = "CLOSED_PROFIT_TARGET"
                decay_pct = (entry_p - current_p) / entry_p * 100.0
                exit_log_reason = f"35% Time Decay Target Hit (decay={decay_pct:.1f}%)"
                log.info(
                    "%s: TFSS leg #%s (%s %s %s) PROFIT TARGET HIT — premium decayed from %.2f to %.2f (%.1f%%).",
                    symbol, lid, side, otype, leg.get("strike"), entry_p, current_p, decay_pct
                )

            # 5. Trend Reversal Exit
            elif persisted.is_valid:
                exec_side = "SELL_PE" if otype == "PE" else "SELL_CE"
                if is_confirmed_reversal(persisted.label, exec_side):
                    should_close = True
                    exit_status = "CLOSED_TREND_REVERSAL"
                    exit_log_reason = f"Trend Reversal to {persisted.label} confirmed ({persisted.agreeing_count}/5 scans)"
                    log.info(
                        "%s: TFSS leg #%s (%s %s) REVERSAL EXIT — trend flipped to %s.",
                        symbol, lid, exec_side, persisted.label
                    )

        if should_close:
            _close_single_tfss_leg(table, lid, now_iso, underlying, current_p, exit_status, exit_log_reason)
            actions.append({"action": exit_status, "trade_id": lid, "symbol": symbol, "reason": exit_log_reason})

            # If this short leg had a disaster wing paired with it, close the disaster wing as well
            wing_leg = _find_paired_wing(open_legs, leg)
            if wing_leg:
                w_lid = wing_leg["id"]
                w_live = leg_live_data.get(w_lid, {})
                w_exit_p = w_live.get("current_premium", wing_leg.get("entry_premium"))
                _close_single_tfss_leg(table, w_lid, now_iso, underlying, w_exit_p, exit_status, f"Paired disaster wing closed with leg #{lid}")
                actions.append({"action": f"{exit_status}_WING", "trade_id": w_lid, "symbol": symbol})

    return actions


def _find_paired_wing(all_open_legs: list[dict], short_leg: dict) -> dict | None:
    """Find the disaster wing paired with a short leg (same group, same tranche, BUY side)."""
    gid = short_leg.get("leg_group_id")
    t_idx = short_leg.get("tranche_index")
    otype = short_leg.get("option_type")
    for leg in all_open_legs:
        if (
            leg.get("side") == "BUY"
            and leg.get("option_type") == otype
            and leg.get("leg_group_id") == gid
            and leg.get("tranche_index") == t_idx
        ):
            return leg
    return None


def _close_single_tfss_leg(
    table: str,
    trade_id: int,
    closed_at_iso: str,
    exit_underlying: float,
    exit_premium: float | None,
    status: str,
    reason: str,
) -> None:
    """Close a TFSS leg in either paper_trades or live_trades."""
    try:
        if table == "live_trades":
            close_live_trade(
                trade_id=trade_id,
                closed_at=closed_at_iso,
                exit_underlying=exit_underlying,
                exit_premium=exit_premium,
                status=status,
                reason=reason,
            )
        else:
            close_paper_trade(
                trade_id=trade_id,
                closed_at=closed_at_iso,
                exit_underlying=exit_underlying,
                exit_premium=exit_premium,
                status=status,
                reason=reason,
            )
    except Exception as exc:
        log.exception("Failed to close TFSS leg #%s in %s: %s", trade_id, table, exc)


def run_tfss_strategy(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict: Any = None,
) -> dict | None:
    """Dedicated Paper Trading Runner for Trend Following Short Strangle (TFSS).

    Executes multi-tranche volatility harvesting:
    1. Monitors exits on existing open legs.
    2. Validates market hours & trading window.
    3. Confirms trend persistence (3 of 5 scans).
    4. Evaluates tranche capacity (max 3 tranches per side, 6 legs total).
    5. Scales lots per TRANCHE_SEQUENCE (50%, 30%, 20%).
    6. Selects candidate delta strike (0.15 - 0.25 delta).
    7. Validates combined delta cap (0.60) & dynamic margin capacity (40% capital).
    8. Pairs with far-OTM disaster wing (0.03 delta).
    9. Atomically logs trade to paper_trades with leg_group_id.
    """
    # Step 1: Monitor exits on existing open paper trades
    exit_actions = monitor_tfss_exits(symbol, scan_context, table="paper_trades")
    if exit_actions:
        log.info("%s: TFSS paper exit monitor executed %d actions", symbol, len(exit_actions))

    # Step 2: Market hours window check
    allowed, hours_reason = _is_tfss_trading_allowed()
    if not allowed:
        return {"action": "BLOCKED_MARKET_HOURS", "reason": hours_reason}

    # Step 3: Trend persistence evaluation (3 of 5 scans)
    persisted = compute_persisted_trend(symbol, scan_context)
    if not persisted.is_valid:
        log.info("%s: TFSS entry blocked — persistence unconfirmed (%s)", symbol, persisted.reason)
        return {"action": "BLOCKED_PERSISTENCE", "reason": persisted.reason}

    if persisted.label == "BULLISH":
        side = "SELL_PE"
        option_type = "PE"
    elif persisted.label == "BEARISH":
        side = "SELL_CE"
        option_type = "CE"
    else:
        return {"action": "BLOCKED_NEUTRAL", "reason": "Trend is neutral"}

    # Step 4: Tranche capacity check
    open_legs = get_open_tfss_legs(symbol, table="paper_trades")
    side_short_legs = [
        l for l in open_legs
        if l.get("side") == "SELL" and str(l.get("option_type", "")).upper() == option_type
    ]
    if len(side_short_legs) >= len(TRANCHE_SEQUENCE):
        return {
            "action": "BLOCKED_MAX_TRANCHES",
            "reason": f"Already {len(side_short_legs)} {option_type} tranches open (cap={len(TRANCHE_SEQUENCE)})",
        }
    if len(open_legs) >= len(TRANCHE_SEQUENCE) * 2:
        return {"action": "BLOCKED_MAX_BOOK_LEGS", "reason": "Max 6 TFSS legs reached"}

    tranche_idx = len(side_short_legs)

    # Step 5: Option chain candidate selection
    option_chain = _extract_option_chain(scan_context)
    if not option_chain:
        return {"action": "BLOCKED_NO_CHAIN", "reason": "Option chain unavailable"}

    underlying = float(scan_context.get("underlying") or scan_context.get("spot") or 0.0)
    dte = int(scan_context.get("dte") or scan_context.get("days_to_expiry") or 5)
    atr_state = {
        "underlying": underlying,
        "is_tightened": False,
    }

    candidate = select_candidate(side, persisted.label, dte, atr_state, option_chain)
    if not candidate:
        return {"action": "BLOCKED_NO_CANDIDATE", "reason": f"No {option_type} candidate in delta band"}

    short_strike = float(candidate["strike"])
    short_premium = float(candidate["premium"])
    short_delta = float(candidate["delta"])

    # Step 6: Lot sizing with tranche scaling
    lots = calculate_trade_lots(
        symbol=symbol,
        entry_premium=short_premium,
        side="SELL",
        is_paper=True,
        setup_type="TFSS",
        tranche_index=tranche_idx,
        option_type=option_type,
        strike=short_strike,
    )
    if lots <= 0:
        return {"action": "BLOCKED_LOT_SIZE", "reason": "Calculated 0 lots"}

    # Step 7: Risk Engine Gates (combined delta <= 0.60, book margin <= 40% capital)
    candidate_leg = {
        "symbol": symbol,
        "option_type": option_type,
        "side": "SELL",
        "strike": short_strike,
        "lots": lots,
        "premium": short_premium,
        "entry_premium": short_premium,
        "delta": short_delta,
        "option_rows": option_chain,
    }
    is_safe, risk_reason = check_risk_limits(
        symbol=symbol,
        setup_type="TFSS",
        candidate_leg=candidate_leg,
        scan_context=scan_context,
    )
    if not is_safe:
        log.warning("%s: TFSS risk check failed — %s", symbol, risk_reason)
        return {"action": "BLOCKED_RISK", "reason": risk_reason}

    # Step 8: Tail-risk Disaster Wing (0.03-delta crash hedge)
    wing_candidate = None
    if TFSS_ENABLE_DISASTER_WINGS:
        wing_candidate = _find_disaster_wing(
            option_chain=option_chain,
            option_type=option_type,
            short_strike=short_strike,
            underlying=underlying,
            target_delta=TFSS_DISASTER_WING_DELTA,
        )

    # Step 9: Atomically log trades into paper_trades
    now_iso = datetime.now(timezone.utc).isoformat()
    today_date = _get_today_date_str()
    leg_group_id = f"{symbol}:{today_date}:TFSS"
    expiry = scan_context.get("expiry") or scan_context.get("current_expiry")

    # Short Leg Trade
    short_trade_data = {
        "opened_at": now_iso,
        "symbol": symbol,
        "expiry": expiry,
        "verdict_label": persisted.label,
        "side": "SELL",
        "option_type": option_type,
        "strike": short_strike,
        "entry_underlying": underlying,
        "entry_premium": short_premium,
        "sl_underlying": 0.0,
        "target_underlying": 0.0,
        "sl_premium": round(short_premium * 2.0, 2),  # Fallback premium stop
        "target_premium": round(short_premium * (1.0 - TIME_DECAY_PROFIT_TARGET_PCT), 2),
        "lots": lots,
        "status": "OPEN",
        "reason": f"TFSS Tranche {tranche_idx+1}/3 | {persisted.label} ({persisted.agreeing_count}/5 scans)",
        "digest_id": digest_id,
        "trade_status": "TRIGGERED_CORE",
        "setup_type": "TFSS",
        "leg_group_id": leg_group_id,
        "tranche_index": tranche_idx,
        "decision_reason": f"TFSS {side} Tranche {tranche_idx} entered",
        "confidence_score": 85,
        "signal_key": f"{symbol}:{option_type}:{int(short_strike)}:{today_date}:TFSS:{tranche_idx}:short:paper",
    }
    short_id = insert_paper_trade(short_trade_data)

    wing_id = None
    if wing_candidate and short_id:
        wing_strike = float(wing_candidate["strike"])
        wing_premium = float(wing_candidate["premium"])
        wing_trade_data = {
            "opened_at": now_iso,
            "symbol": symbol,
            "expiry": expiry,
            "verdict_label": persisted.label,
            "side": "BUY",
            "option_type": option_type,
            "strike": wing_strike,
            "entry_underlying": underlying,
            "entry_premium": wing_premium,
            "sl_underlying": 0.0,
            "target_underlying": 0.0,
            "sl_premium": 0.0,
            "target_premium": round(wing_premium * 3.0, 2),
            "lots": lots,
            "status": "OPEN",
            "reason": f"TFSS Disaster Wing | paired with short leg #{short_id}",
            "digest_id": digest_id,
            "trade_status": "TRIGGERED_CORE",
            "setup_type": "TFSS",
            "leg_group_id": leg_group_id,
            "tranche_index": tranche_idx,
            "decision_reason": f"TFSS Disaster Wing 0.03d hedge",
            "confidence_score": 85,
            "signal_key": f"{symbol}:{option_type}:{int(wing_strike)}:{today_date}:TFSS:{tranche_idx}:wing:paper",
        }
        wing_id = insert_paper_trade(wing_trade_data)

    log.info(
        "%s: TFSS paper entry complete — Tranche %d/3: SELL %s %g @ ₹%.2f (x%d lots). Wing: %s",
        symbol, tranche_idx + 1, option_type, short_strike, short_premium, lots,
        f"BUY {wing_candidate['strike']} @ ₹{wing_candidate['premium']:.2f}" if wing_candidate else "NONE"
    )

    return {
        "action": "ENTERED_TFSS",
        "symbol": symbol,
        "tranche_index": tranche_idx,
        "side": side,
        "strike": short_strike,
        "premium": short_premium,
        "lots": lots,
        "short_trade_id": short_id,
        "wing_trade_id": wing_id,
        "leg_group_id": leg_group_id,
    }


def run_live_tfss_strategy(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict: Any = None,
    exit_check: bool = False,
) -> dict | None:
    """Dedicated Live & Shadow Trading Runner for Trend Following Short Strangle (TFSS).

    Gated strictly by authorize_broker_execution:
    - Shadow mode: executes simulated fills with 'sh-' broker IDs into live_trades.
    - Live broker mode: places real Kite orders sequentially (short leg first, then disaster wing)
      with fill verification and rollback.
    """
    # Step 1: Monitor exits on existing open live trades
    exit_actions = monitor_tfss_exits(symbol, scan_context, table="live_trades")
    if exit_check:
        return {"action": "EXIT_CHECK_COMPLETE", "actions": exit_actions}

    # Step 2: Broker Gate Authorization
    try:
        from src.engine.broker_gate import authorize_broker_execution
        auth = authorize_broker_execution(symbol, operation="ENTRY", scan_context=scan_context)
    except Exception as exc:
        log.error("%s: TFSS broker gate authorization failed: %s", symbol, exc)
        return {"action": "BLOCKED_BROKER_GATE", "reason": str(exc)}

    if not auth.is_authorized and not auth.is_shadow:
        log.debug("%s: TFSS live entry blocked by broker gate: %s", symbol, auth.reason)
        return {"action": "BLOCKED_BROKER_GATE", "reason": auth.reason}

    is_shadow = auth.is_shadow

    # Step 3: Market hours window check
    allowed, hours_reason = _is_tfss_trading_allowed()
    if not allowed:
        return {"action": "BLOCKED_MARKET_HOURS", "reason": hours_reason}

    # Step 4: Trend persistence evaluation (3 of 5 scans)
    persisted = compute_persisted_trend(symbol, scan_context)
    if not persisted.is_valid:
        return {"action": "BLOCKED_PERSISTENCE", "reason": persisted.reason}

    if persisted.label == "BULLISH":
        side = "SELL_PE"
        option_type = "PE"
    elif persisted.label == "BEARISH":
        side = "SELL_CE"
        option_type = "CE"
    else:
        return {"action": "BLOCKED_NEUTRAL", "reason": "Trend is neutral"}

    # Step 5: Tranche capacity check on live_trades
    open_legs = get_open_tfss_legs(symbol, table="live_trades")
    side_short_legs = [
        l for l in open_legs
        if l.get("side") == "SELL" and str(l.get("option_type", "")).upper() == option_type
    ]
    if len(side_short_legs) >= len(TRANCHE_SEQUENCE):
        return {
            "action": "BLOCKED_MAX_TRANCHES",
            "reason": f"Already {len(side_short_legs)} {option_type} tranches open in live/shadow",
        }
    if len(open_legs) >= len(TRANCHE_SEQUENCE) * 2:
        return {"action": "BLOCKED_MAX_BOOK_LEGS", "reason": "Max 6 TFSS legs reached in live/shadow"}

    tranche_idx = len(side_short_legs)

    # Step 6: Option chain candidate selection
    option_chain = _extract_option_chain(scan_context)
    if not option_chain:
        return {"action": "BLOCKED_NO_CHAIN", "reason": "Option chain unavailable"}

    underlying = float(scan_context.get("underlying") or scan_context.get("spot") or 0.0)
    dte = int(scan_context.get("dte") or scan_context.get("days_to_expiry") or 5)
    atr_state = {"underlying": underlying, "is_tightened": False}

    candidate = select_candidate(side, persisted.label, dte, atr_state, option_chain)
    if not candidate:
        return {"action": "BLOCKED_NO_CANDIDATE", "reason": f"No {option_type} candidate in delta band"}

    short_strike = float(candidate["strike"])
    short_premium = float(candidate["premium"])
    short_delta = float(candidate["delta"])

    # Step 7: Lot sizing
    lots = calculate_trade_lots(
        symbol=symbol,
        entry_premium=short_premium,
        side="SELL",
        is_paper=False,
        setup_type="TFSS",
        tranche_index=tranche_idx,
        option_type=option_type,
        strike=short_strike,
    )
    if lots <= 0:
        return {"action": "BLOCKED_LOT_SIZE", "reason": "Calculated 0 lots"}

    # Step 8: Risk Engine Gates on live_trades
    candidate_leg = {
        "symbol": symbol,
        "option_type": option_type,
        "side": "SELL",
        "strike": short_strike,
        "lots": lots,
        "premium": short_premium,
        "entry_premium": short_premium,
        "delta": short_delta,
        "option_rows": option_chain,
    }
    is_safe, risk_reason = check_live_risk_limits(
        symbol=symbol,
        setup_type="TFSS",
        candidate_leg=candidate_leg,
        scan_context=scan_context,
    )
    if not is_safe:
        log.warning("%s: TFSS live risk check failed — %s", symbol, risk_reason)
        return {"action": "BLOCKED_RISK", "reason": risk_reason}

    # Step 9: Tail-risk Disaster Wing
    wing_candidate = None
    if TFSS_ENABLE_DISASTER_WINGS:
        wing_candidate = _find_disaster_wing(
            option_chain=option_chain,
            option_type=option_type,
            short_strike=short_strike,
            underlying=underlying,
            target_delta=TFSS_DISASTER_WING_DELTA,
        )

    # Step 10: Broker Order Execution
    now_iso = datetime.now(timezone.utc).isoformat()
    today_date = _get_today_date_str()
    leg_group_id = f"{symbol}:{today_date}:TFSS"
    expiry = scan_context.get("expiry") or scan_context.get("current_expiry")

    short_order_id = f"sh-tfss-{uuid.uuid4().hex[:8]}" if is_shadow else None
    wing_order_id = f"sh-tfss-w-{uuid.uuid4().hex[:8]}" if is_shadow and wing_candidate else None

    if not is_shadow:
        # Real Kite Order Placement
        try:
            from src.engine.live_trading import get_kite_client, place_kite_order, confirm_order_fill
            from src.engine.symbol_resolver import resolve_instrument
            kite = get_kite_client()
            if not kite:
                return {"action": "BLOCKED_NO_KITE_CLIENT", "reason": "Kite client unavailable"}

            # Resolve short leg instrument
            res_short = resolve_instrument(symbol, expiry, short_strike, option_type)
            if not res_short or not res_short.get("tradingsymbol"):
                return {"action": "BLOCKED_INSTRUMENT_NOT_RESOLVED", "reason": "Could not resolve short instrument"}

            base_sym = symbol.upper().split()[0]
            lot_size = LOT_SIZES.get(base_sym, 1)
            quantity = lots * lot_size

            # Place short order
            short_order_id = place_kite_order(
                kite=kite,
                tradingsymbol=res_short["tradingsymbol"],
                exchange=res_short.get("exchange", "NFO"),
                transaction_type="SELL",
                quantity=quantity,
                order_type="MARKET",
                price=None,
            )
            status, msg = confirm_order_fill(kite, short_order_id, shadow_mode=False)
            if status != "COMPLETE":
                log.error("%s: TFSS short leg fill failed (%s: %s) — aborting", symbol, status, msg)
                return {"action": "BLOCKED_ORDER_FAILED", "reason": f"Kite fill failed: {msg}"}

            # If disaster wing enabled, place wing order
            if wing_candidate:
                res_wing = resolve_instrument(symbol, expiry, float(wing_candidate["strike"]), option_type)
                if res_wing and res_wing.get("tradingsymbol"):
                    wing_order_id = place_kite_order(
                        kite=kite,
                        tradingsymbol=res_wing["tradingsymbol"],
                        exchange=res_wing.get("exchange", "NFO"),
                        transaction_type="BUY",
                        quantity=quantity,
                        order_type="MARKET",
                        price=None,
                    )
                    confirm_order_fill(kite, wing_order_id, shadow_mode=False)
        except Exception as exc:
            log.exception("%s: TFSS live broker order placement failed: %s", symbol, exc)
            return {"action": "BLOCKED_BROKER_EXCEPTION", "reason": str(exc)}

    # Step 11: Record to live_trades
    short_trade_data = {
        "opened_at": now_iso,
        "symbol": symbol,
        "expiry": expiry,
        "verdict_label": persisted.label,
        "side": "SELL",
        "option_type": option_type,
        "strike": short_strike,
        "entry_underlying": underlying,
        "entry_premium": short_premium,
        "sl_underlying": 0.0,
        "target_underlying": 0.0,
        "sl_premium": round(short_premium * 2.0, 2),
        "target_premium": round(short_premium * (1.0 - TIME_DECAY_PROFIT_TARGET_PCT), 2),
        "lots": lots,
        "status": "OPEN",
        "reason": f"TFSS Tranche {tranche_idx+1}/3 | {persisted.label} ({persisted.agreeing_count}/5 scans)",
        "digest_id": digest_id,
        "trade_status": "TRIGGERED_CORE",
        "setup_type": "TFSS",
        "leg_group_id": leg_group_id,
        "tranche_index": tranche_idx,
        "decision_reason": f"TFSS live/shadow {side} Tranche {tranche_idx} entered",
        "confidence_score": 85,
        "broker_order_id": short_order_id,
        "broker_status": "COMPLETE",
        "signal_key": f"{symbol}:{option_type}:{int(short_strike)}:{today_date}:TFSS:{tranche_idx}:short:live",
    }
    short_id = insert_live_trade(short_trade_data)

    wing_id = None
    if wing_candidate and short_id:
        wing_strike = float(wing_candidate["strike"])
        wing_premium = float(wing_candidate["premium"])
        wing_trade_data = {
            "opened_at": now_iso,
            "symbol": symbol,
            "expiry": expiry,
            "verdict_label": persisted.label,
            "side": "BUY",
            "option_type": option_type,
            "strike": wing_strike,
            "entry_underlying": underlying,
            "entry_premium": wing_premium,
            "sl_underlying": 0.0,
            "target_underlying": 0.0,
            "sl_premium": 0.0,
            "target_premium": round(wing_premium * 3.0, 2),
            "lots": lots,
            "status": "OPEN",
            "reason": f"TFSS Disaster Wing | paired with live short leg #{short_id}",
            "digest_id": digest_id,
            "trade_status": "TRIGGERED_CORE",
            "setup_type": "TFSS",
            "leg_group_id": leg_group_id,
            "tranche_index": tranche_idx,
            "decision_reason": "TFSS Disaster Wing 0.03d hedge",
            "confidence_score": 85,
            "broker_order_id": wing_order_id,
            "broker_status": "COMPLETE",
            "signal_key": f"{symbol}:{option_type}:{int(wing_strike)}:{today_date}:TFSS:{tranche_idx}:wing:live",
        }
        wing_id = insert_live_trade(wing_trade_data)

    log.info(
        "%s: TFSS live/shadow entry complete (%s) — Tranche %d/3: SELL %s %g @ ₹%.2f (x%d lots). Order: %s",
        symbol, "SHADOW" if is_shadow else "LIVE", tranche_idx + 1, option_type, short_strike, short_premium, lots, short_order_id
    )

    return {
        "action": "ENTERED_TFSS_LIVE",
        "symbol": symbol,
        "mode": "SHADOW" if is_shadow else "LIVE",
        "tranche_index": tranche_idx,
        "side": side,
        "strike": short_strike,
        "premium": short_premium,
        "lots": lots,
        "short_trade_id": short_id,
        "wing_trade_id": wing_id,
        "short_order_id": short_order_id,
        "leg_group_id": leg_group_id,
    }
