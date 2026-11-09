"""
Deterministic exit priority evaluator and AI authority enforcement engine.

Implements Phase 4 from the trading decision matrix specification:
1. Strict ordered exit priority list:
   HARD_RISK -> TESTED_SIDE -> EVENT -> EXPIRY -> REGIME_FLIP -> TARGET -> DEAD_TRADE -> AI_TIGHTEN
2. SELL legs closed first execution ordering.
3. AI authority enforcement: veto/size-cut/tighten-only. Widen stop or loosen target forbidden.
4. Early AI close permitted only when profit >= 30% of credit or loss >= 50% of stop loss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from config.decision_matrix_loader import load_decision_matrix
from config.settings import IST

log = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """Represents the verdict of the exit priority evaluation."""
    should_exit: bool = False
    exit_reason: str = ""          # One of: HARD_RISK, TESTED_SIDE, EVENT, EXPIRY, REGIME_FLIP, TARGET, DEAD_TRADE, AI_TIGHTEN
    details: str = ""              # Human-readable justification (e.g. "CE 25000 Δ0.49 >= 0.48 trigger")
    priority: int = 99             # Priority index from configured list (1 is highest)
    ordered_legs: list[dict] = field(default_factory=list) # SELL legs ordered before BUY legs
    action: str = "HOLD"           # "CLOSED", "ADJUST", "HOLD"


def order_legs_for_exit(legs: list[dict]) -> list[dict]:
    """
    Order legs for exit execution so that SELL (short) legs are closed FIRST.

    In multi-leg option positions, closing the short legs first eliminates broker margin
    requirements and avoids leaving the account temporarily with naked short exposure
    during sequential broker execution.
    """
    if not legs:
        return []

    def _is_short(leg: dict) -> bool:
        side = str(leg.get("side") or leg.get("action") or "").upper().strip()
        return side == "SELL"

    short_legs = [leg for leg in legs if _is_short(leg)]
    long_legs = [leg for leg in legs if not _is_short(leg)]
    return short_legs + long_legs


def evaluate_exit_priority(
    book: dict,
    legs: list[dict],
    market_state: dict,
    cfg: dict | None = None,
    now_ist: datetime | None = None,
    ai_verdict: dict | None = None,
) -> ExitDecision:
    """
    Evaluate all configured exit conditions for an open multi-leg book or trade.

    Iterates through configured exit conditions in strict priority order.
    The first matching condition determines the exit reason and decision.

    Args:
        book: Multi-leg trade dictionary or DB row
        legs: List of option legs in the book
        market_state: Current market data (underlying, pnl, current_iv, new_verdict, etc.)
        cfg: Decision matrix configuration dict (loaded automatically if None)
        now_ist: Current time in IST (defaults to now)
        ai_verdict: Optional AI advice dict (e.g. {"action": "CLOSE", "reason": ...})

    Returns:
        ExitDecision object
    """
    symbol = str(book.get("symbol") or market_state.get("symbol") or "").upper().strip()
    if cfg is None:
        cfg = load_decision_matrix(symbol)

    if now_ist is None:
        now_ist = datetime.now(IST)

    exits_cfg = cfg.get("exits", {})
    priority_order = exits_cfg.get("priority_order", [
        "HARD_RISK",
        "TESTED_SIDE",
        "EVENT",
        "EXPIRY",
        "REGIME_FLIP",
        "TARGET",
        "DEAD_TRADE",
        "AI_TIGHTEN",
    ])

    strategy_type = str(book.get("strategy_type") or book.get("structure") or "").upper()
    is_defined_risk = strategy_type in ("IRON_CONDOR", "BEAR_CALL_SPREAD", "BULL_PUT_SPREAD", "SPREAD")

    underlying = float(market_state.get("underlying") or book.get("entry_underlying") or 0.0)
    current_pnl = float(market_state.get("total_pnl") or market_state.get("pnl_rupees") or 0.0)
    net_credit = float(book.get("net_premium") or 0.0)

    # Order legs so short legs close first
    ordered_legs = order_legs_for_exit(legs)

    for rank, reason in enumerate(priority_order, start=1):

        # -------------------------------------------------------------------
        # 1. HARD_RISK
        # -------------------------------------------------------------------
        if reason == "HARD_RISK":
            # Book stop loss in rupees
            from src.engine.multileg_paper_trading import _get_stop_loss_threshold_rupees
            try:
                sl_threshold = _get_stop_loss_threshold_rupees(book, legs, symbol)
            except Exception:
                sl_threshold = 50000.0

            if current_pnl <= -sl_threshold:
                return ExitDecision(
                    should_exit=True,
                    exit_reason="HARD_RISK",
                    details=f"Total loss ₹{abs(current_pnl):.0f} breached SL cap ₹{sl_threshold:.0f}",
                    priority=rank,
                    ordered_legs=ordered_legs,
                    action="CLOSED",
                )

            # Capital cap check (e.g. 3% max capital loss)
            from config.runtime_config import get_capital_cap_pct, get_total_capital

            cfg_cap_pct = cfg.get("stops_targets", {}).get("options", {}).get("capital_cap_pct") if cfg else None
            if cfg_cap_pct is not None:
                try:
                    capital_cap_pct = float(cfg_cap_pct)
                except (ValueError, TypeError):
                    capital_cap_pct = get_capital_cap_pct()
            else:
                capital_cap_pct = get_capital_cap_pct()

            if capital_cap_pct > 1.0:
                capital_cap_pct /= 100.0

            total_capital = float(market_state.get("total_capital") or get_total_capital())
            capital_loss_ceiling = total_capital * capital_cap_pct
            if current_pnl <= -capital_loss_ceiling:
                return ExitDecision(
                    should_exit=True,
                    exit_reason="HARD_RISK",
                    details=f"Loss ₹{abs(current_pnl):.0f} breached capital cap ₹{capital_loss_ceiling:.0f} ({capital_cap_pct*100:.1f}%)",
                    priority=rank,
                    ordered_legs=ordered_legs,
                    action="CLOSED",
                )

        # -------------------------------------------------------------------
        # 2. TESTED_SIDE
        # -------------------------------------------------------------------
        elif reason == "TESTED_SIDE":
            tested_cfg = exits_cfg.get("tested_side", {})
            def_delta_trig = float(tested_cfg.get("defined_risk_delta_trigger", 0.48))
            naked_delta_trig = float(tested_cfg.get("naked_delta_trigger", 0.35))
            delta_trig = def_delta_trig if is_defined_risk else naked_delta_trig
            check_candle_close = bool(tested_cfg.get("candle_close_15m_breach", True))

            for leg in legs:
                side = str(leg.get("side") or leg.get("action") or "").upper().strip()
                if side != "SELL":
                    continue  # Only short legs get tested

                strike = float(leg.get("strike") or 0.0)
                opt_type = str(leg.get("option_type") or "").upper().strip()
                delta = abs(float(leg.get("delta") or 0.0))

                # Delta trigger breach
                if delta >= delta_trig:
                    return ExitDecision(
                        should_exit=True,
                        exit_reason="TESTED_SIDE",
                        details=f"Short {opt_type} {strike:.1f} delta {delta:.2f} reached tested trigger {delta_trig:.2f}",
                        priority=rank,
                        ordered_legs=ordered_legs,
                        action="CLOSED",
                    )

                # 15-min candle close / spot breach past short strike
                if check_candle_close and strike > 0 and underlying > 0:
                    if opt_type == "CE" and underlying >= strike:
                        return ExitDecision(
                            should_exit=True,
                            exit_reason="TESTED_SIDE",
                            details=f"Underlying spot {underlying:.2f} breached short CE strike {strike:.1f}",
                            priority=rank,
                            ordered_legs=ordered_legs,
                            action="CLOSED",
                        )
                    elif opt_type == "PE" and underlying <= strike:
                        return ExitDecision(
                            should_exit=True,
                            exit_reason="TESTED_SIDE",
                            details=f"Underlying spot {underlying:.2f} breached short PE strike {strike:.1f}",
                            priority=rank,
                            ordered_legs=ordered_legs,
                            action="CLOSED",
                        )

        # -------------------------------------------------------------------
        # 3. EVENT
        # -------------------------------------------------------------------
        elif reason == "EVENT":
            close_min_prior = int(exits_cfg.get("event", {}).get("close_minutes_prior", 30))
            events = cfg.get("events", [])
            for ev in events:
                ev_name = ev.get("name", "EVENT")
                ev_syms = [s.upper() for s in ev.get("symbols", [])]
                if not ev_syms or any(s in symbol for s in ev_syms):
                    ev_dt_str = ev.get("datetime_ist", "")
                    if ev_dt_str:
                        try:
                            ev_dt = datetime.fromisoformat(ev_dt_str)
                            if ev_dt.tzinfo is None:
                                ev_dt = IST.localize(ev_dt)
                            diff_sec = (ev_dt - now_ist).total_seconds()
                            if 0 <= diff_sec <= (close_min_prior * 60):
                                return ExitDecision(
                                    should_exit=True,
                                    exit_reason="EVENT",
                                    details=f"Event {ev_name} scheduled in {diff_sec/60:.0f}m (threshold {close_min_prior}m)",
                                    priority=rank,
                                    ordered_legs=ordered_legs,
                                    action="CLOSED",
                                )
                        except Exception:
                            continue

        # -------------------------------------------------------------------
        # 4. EXPIRY
        # -------------------------------------------------------------------
        elif reason == "EXPIRY":
            exp_cfg = exits_cfg.get("expiry", {})
            from config.settings import MCX_SYMBOLS
            NSE_INDICES = frozenset({'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX'})

            is_mcx = any(s in symbol for s in MCX_SYMBOLS)
            is_index = any(s in symbol for s in NSE_INDICES)

            # Expiry day check
            expiry_str = str(book.get("expiry") or market_state.get("expiry") or "")
            is_expiry_today = False
            dte = market_state.get("days_to_expiry")
            if dte is not None:
                dte = int(dte)
            elif expiry_str:
                try:
                    exp_date = datetime.strptime(expiry_str[:10], "%Y-%m-%d").date()
                    dte = (exp_date - now_ist.date()).days
                except Exception:
                    dte = None

            if dte == 0:
                is_expiry_today = True

            # NSE Indices exit at configured time on expiry day
            if is_index and is_expiry_today:
                exit_time_str = exp_cfg.get("indices_exit_time_ist", "15:15")
                ex_h, ex_m = map(int, exit_time_str.split(":"))
                target_exit_time = now_ist.replace(hour=ex_h, minute=ex_m, second=0, microsecond=0)
                if now_ist >= target_exit_time:
                    return ExitDecision(
                        should_exit=True,
                        exit_reason="EXPIRY",
                        details=f"NSE Index expiry time reached ({now_ist.strftime('%H:%M')} >= {exit_time_str} IST)",
                        priority=rank,
                        ordered_legs=ordered_legs,
                        action="CLOSED",
                    )

            # MCX Options pre-tender / pre-expiry exit
            if is_mcx and dte is not None:
                mcx_days_floor = int(exp_cfg.get("mcx_days_before_expiry", 1))
                if dte <= mcx_days_floor:
                    return ExitDecision(
                        should_exit=True,
                        exit_reason="EXPIRY",
                        details=f"MCX contract DTE {dte} <= required buffer {mcx_days_floor} day(s)",
                        priority=rank,
                        ordered_legs=ordered_legs,
                        action="CLOSED",
                    )

            # Session close buffer (10 min before exchange close)
            buffer_min = int(exp_cfg.get("session_close_buffer_minutes", 10))
            if is_mcx:
                from src.engine.time_guards import get_mcx_close_time
                close_h, close_m = get_mcx_close_time(now_ist)
            else:
                close_h, close_m = (15, 30)

            session_close_dt = now_ist.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
            diff_to_close_sec = (session_close_dt - now_ist).total_seconds()
            if 0 <= diff_to_close_sec <= (buffer_min * 60) and is_expiry_today:
                return ExitDecision(
                    should_exit=True,
                    exit_reason="EXPIRY",
                    details=f"Session close within {diff_to_close_sec/60:.0f}m on expiry day",
                    priority=rank,
                    ordered_legs=ordered_legs,
                    action="CLOSED",
                )

        # -------------------------------------------------------------------
        # 5. REGIME_FLIP
        # -------------------------------------------------------------------
        elif reason == "REGIME_FLIP":
            rf_cfg = exits_cfg.get("regime_flip", {})
            conf_threshold = int(rf_cfg.get("confidence_threshold", 75))
            new_verdict = str(market_state.get("verdict") or market_state.get("new_verdict") or "").upper()
            new_conf = int(market_state.get("confidence") or market_state.get("new_confidence") or 0)

            if new_verdict and new_conf >= conf_threshold:
                # Determine opposing verdict
                is_trade_bullish = "BULL" in strategy_type or "PUT_SPREAD" in strategy_type
                is_trade_bearish = "BEAR" in strategy_type or "CALL_SPREAD" in strategy_type

                is_verdict_bullish = any(w in new_verdict for w in ("BUY", "BULLISH", "LONG"))
                is_verdict_bearish = any(w in new_verdict for w in ("SELL", "BEARISH", "SHORT"))

                flipped = (is_trade_bullish and is_verdict_bearish) or (is_trade_bearish and is_verdict_bullish)

                if flipped:
                    # On MCX, verify NYMEX agreement if configured
                    require_nymex = bool(rf_cfg.get("require_nymex_agree_on_mcx", True))
                    from config.settings import MCX_SYMBOLS
                    if any(s in symbol for s in MCX_SYMBOLS) and require_nymex:
                        nymex_direction = str(market_state.get("nymex_direction") or "").upper()
                        # Only flip if NYMEX agrees with the new opposing verdict
                        if (is_verdict_bearish and nymex_direction == "BEARISH") or (is_verdict_bullish and nymex_direction == "BULLISH"):
                            return ExitDecision(
                                should_exit=True,
                                exit_reason="REGIME_FLIP",
                                details=f"Regime flipped to {new_verdict} (conf {new_conf}%) with NYMEX agreement",
                                priority=rank,
                                ordered_legs=ordered_legs,
                                action="CLOSED",
                            )
                    else:
                        return ExitDecision(
                            should_exit=True,
                            exit_reason="REGIME_FLIP",
                            details=f"Regime flipped to {new_verdict} (conf {new_conf}% >= {conf_threshold}%)",
                            priority=rank,
                            ordered_legs=ordered_legs,
                            action="CLOSED",
                        )

        # -------------------------------------------------------------------
        # 6. TARGET
        # -------------------------------------------------------------------
        elif reason == "TARGET":
            from src.engine.trade_plan import calculate_option_book_stop_target
            from config.settings import LOT_SIZES

            lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(symbol.split()[0], 1))
            total_lots = max((int(leg.get("lots") or 1) for leg in legs), default=1)
            expiry_str = str(book.get("expiry") or market_state.get("expiry") or "")
            is_expiry_today = False
            if expiry_str:
                try:
                    is_expiry_today = (datetime.strptime(expiry_str[:10], "%Y-%m-%d").date() == now_ist.date())
                except Exception:
                    pass

            target_res = calculate_option_book_stop_target(
                strategy_type=strategy_type,
                net_credit=net_credit,
                wing_width=float(book.get("max_loss") or 0.0) + net_credit if is_defined_risk else 0.0,
                is_expiry_day=is_expiry_today,
                symbol=symbol,
                lot_size=lot_size,
                total_lots=total_lots,
            )
            target_rupees = target_res["target_profit_rupees"]
            target_pct = target_res["target_profit_pct"]

            if target_rupees > 0 and current_pnl >= target_rupees:
                return ExitDecision(
                    should_exit=True,
                    exit_reason="TARGET",
                    details=f"Profit target hit: ₹{current_pnl:.0f} >= ₹{target_rupees:.0f} ({target_pct*100:.0f}% of credit)",
                    priority=rank,
                    ordered_legs=ordered_legs,
                    action="CLOSED",
                )

        # -------------------------------------------------------------------
        # 7. DEAD_TRADE
        # -------------------------------------------------------------------
        elif reason == "DEAD_TRADE":
            dt_cfg = exits_cfg.get("dead_trade", {})
            days_threshold = float(dt_cfg.get("days_threshold", 3))
            max_pnl_pct = float(dt_cfg.get("max_pnl_pct", 0.05))

            opened_at_str = str(book.get("opened_at") or "")
            if opened_at_str:
                try:
                    opened_at = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - opened_at).total_seconds() / 86400.0
                    max_profit = float(book.get("max_profit") or 1.0)
                    if age_days >= days_threshold and (current_pnl / max_profit) < max_pnl_pct:
                        return ExitDecision(
                            should_exit=True,
                            exit_reason="DEAD_TRADE",
                            details=f"Trade age {age_days:.1f}d >= {days_threshold}d with stagnant PnL {current_pnl/max_profit*100:.1f}% < {max_pnl_pct*100:.1f}%",
                            priority=rank,
                            ordered_legs=ordered_legs,
                            action="CLOSED",
                        )
                except Exception:
                    pass

        # -------------------------------------------------------------------
        # 8. AI_TIGHTEN
        # -------------------------------------------------------------------
        elif reason == "AI_TIGHTEN":
            if ai_verdict and str(ai_verdict.get("action", "")).upper() in ("CLOSE", "ADJUST"):
                ai_auth = cfg.get("ai_authority", {})
                min_prof_pct = float(ai_auth.get("close_allowed_min_profit_credit_pct", 0.30))
                min_loss_ratio = float(ai_auth.get("close_allowed_min_loss_stop_ratio", 0.50))

                from src.engine.multileg_paper_trading import _get_stop_loss_threshold_rupees
                try:
                    sl_rupees = _get_stop_loss_threshold_rupees(book, legs, symbol)
                except Exception:
                    sl_rupees = 50000.0

                from config.settings import LOT_SIZES
                lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(symbol.split()[0], 1))
                total_lots = max((int(leg.get("lots") or 1) for leg in legs), default=1)
                credit_rupees = net_credit * lot_size * total_lots

                has_qualifying_profit = (credit_rupees > 0 and current_pnl >= min_prof_pct * credit_rupees)
                has_qualifying_loss = (sl_rupees > 0 and current_pnl <= -min_loss_ratio * sl_rupees)

                if has_qualifying_profit or has_qualifying_loss:
                    action_type = str(ai_verdict.get("action")).upper()
                    return ExitDecision(
                        should_exit=True,
                        exit_reason="AI_TIGHTEN",
                        details=f"AI {action_type} authorized: PnL ₹{current_pnl:.0f} qualified under AI authority gates",
                        priority=rank,
                        ordered_legs=ordered_legs,
                        action=action_type,
                    )
                else:
                    log.info(
                        "[exit_evaluator] %s: AI exit advice REJECTED by AI authority (PnL ₹%.0f below 30%% profit ₹%.0f and 50%% loss ₹%.0f)",
                        symbol, current_pnl, min_prof_pct * credit_rupees, min_loss_ratio * sl_rupees
                    )

    return ExitDecision(
        should_exit=False,
        exit_reason="",
        details="All exit conditions HOLD",
        priority=99,
        ordered_legs=ordered_legs,
        action="HOLD",
    )


def resolve_effective_stop_and_target(
    rule_sl_pts: float,
    rule_tgt_pts: float,
    ai_sl_pts: float | None = None,
    ai_tgt_pts: float | None = None,
    cfg: dict | None = None,
) -> tuple[float, float]:
    """
    Apply AI Authority rules to stop loss and profit target adjustments.

    Invariants:
    - AI can only tighten stops (smaller loss distance), never widen.
    - AI can only tighten targets (closer profit distance), never loosen.
    - Final stop = tighter of rule stop and AI stop.
    """
    ai_auth = (cfg or {}).get("ai_authority", {})
    tighten_stop_allowed = bool(ai_auth.get("tighten_stop_allowed", True))
    widen_stop_allowed = bool(ai_auth.get("widen_stop_allowed", False))
    loosen_target_allowed = bool(ai_auth.get("loosen_target_allowed", False))

    effective_sl = float(rule_sl_pts)
    if ai_sl_pts is not None and ai_sl_pts > 0:
        if ai_sl_pts < effective_sl and tighten_stop_allowed:
            effective_sl = ai_sl_pts  # Tighten stop
        elif ai_sl_pts > effective_sl and not widen_stop_allowed:
            log.warning(
                "resolve_effective_stop_and_target: AI proposed wider stop (%.2f > %.2f) — rejected",
                ai_sl_pts, effective_sl
            )

    effective_tgt = float(rule_tgt_pts)
    if ai_tgt_pts is not None and ai_tgt_pts > 0:
        if ai_tgt_pts < effective_tgt:
            effective_tgt = ai_tgt_pts  # Tighter target (take profit earlier)
        elif ai_tgt_pts > effective_tgt and not loosen_target_allowed:
            log.warning(
                "resolve_effective_stop_and_target: AI proposed looser target (%.2f > %.2f) — rejected",
                ai_tgt_pts, effective_tgt
            )

    return round(effective_sl, 2), round(effective_tgt, 2)
