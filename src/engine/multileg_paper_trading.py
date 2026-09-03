"""Multi-leg paper trading runner.

Implements the paper-trading entry point for multi-leg strategies
(iron condors, strangles, straddles, spreads, jade lizards, custom).

Flow:
    1. Market hours guard
    2. AI mode resolution from strategy_registry
    3. Open book monitoring (profit target, stop loss, time decay)
    4. New book entry via LLM verdict + validation + risk checks
    5. Advisory-only vs full execution based on ai_mode
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from config.settings import IST
from src.utils.text_sanitizer import sanitize_mojibake

log = logging.getLogger(__name__)


def _dte_from_expiry(expiry: str) -> int:
    """Calculate days to expiry from YYYY-MM-DD string using IST timezone date."""
    try:
        from config.settings import IST

        exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        today = datetime.now(IST).date()
        return (exp_date - today).days
    except Exception:
        return 999


def _get_stop_loss_threshold_rupees(book: dict, legs: list[dict], symbol: str) -> float:
    """Calculate the exact Stop Loss threshold in Rupees for a multi-leg book.

    Prevents comparing total_pnl (in Rupees) against max_loss (in points/unscaled units),
    which previously caused premature false liquidations on tiny tick movements.
    """
    from config.settings import LOT_SIZES

    base_symbol = symbol.upper().split()[0] if symbol else symbol.upper()
    lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_symbol, 1))
    total_lots = max((int(leg.get("lots") or 1) for leg in legs), default=1)

    strategy_type = (book.get("strategy_type") or book.get("structure") or "").upper()
    net_premium = float(book.get("net_premium") or 0.0)
    max_loss_val = float(book.get("max_loss") or 0.0)
    stop_loss_pct = float(book.get("stop_loss_pct") or 1.5)

    underlying = float(book.get("entry_underlying") or 0.0)
    is_defined_risk = strategy_type in ("IRON_CONDOR", "BEAR_CALL_SPREAD", "BULL_PUT_SPREAD")

    if is_defined_risk and max_loss_val > 0 and (underlying <= 0 or max_loss_val < underlying * 0.4):
        total_max_loss_rupees = max_loss_val * lot_size * total_lots * stop_loss_pct
    else:
        # Undefined risk / short strangle / straddle / fallback:
        # Cap loss at stop_loss_pct (default 1.5x = 150%) of net premium collected
        total_max_loss_rupees = max(net_premium, 1.0) * lot_size * total_lots * stop_loss_pct

    return max(total_max_loss_rupees, 1.0)


def run_multileg_paper_strategy(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict=None,
) -> dict | None:
    """Multi-leg paper trading entry point.

    Monitors open books for exits and triggers new multi-leg entries.

    Args:
        symbol: Trading symbol (e.g. "NIFTY", "BANKNIFTY")
        scan_context: Current scan snapshot (underlying, expiry, option_rows, etc.)
        digest_id: Digest identifier for traceability
        intel: Intelligence dict with verdict_label, confidence, news, etc.
        ai_verdict: Optional AI verdict from the LLM pipeline

    Returns:
        dict with action key, or None if nothing to do.
    """
    try:
        return _run_multileg_paper_strategy_inner(
            symbol, scan_context, digest_id, intel, ai_verdict
        )
    except Exception as e:
        log.error(
            "[multileg-paper] %s: Unhandled exception in paper strategy: %s",
            symbol,
            e,
            exc_info=True,
        )
        return None


def _run_multileg_paper_strategy_inner(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict=None,
) -> dict | None:
    """Inner implementation — isolated so the outer wrapper can catch all errors."""
    # ── 1. Market hours guard ──────────────────────────────────────────
    from src.engine.paper_trading import _is_market_open

    if not _is_market_open(symbol):
        log.debug(
            "[multileg-paper] %s: skipped — outside market hours", symbol
        )
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    # ── 2. Basic validation ────────────────────────────────────────────
    from config.multileg_strategies import ALLOWED_SYMBOLS

    if symbol not in ALLOWED_SYMBOLS:
        log.debug(
            "[multileg-paper] %s: not in ALLOWED_SYMBOLS %s",
            symbol,
            ALLOWED_SYMBOLS,
        )
        return None

    underlying = float((scan_context or {}).get("underlying") or 0.0)
    if underlying <= 0:
        log.debug("[multileg-paper] %s: no underlying price", symbol)
        return None

    # ── 3. AI mode from strategy_registry ───────────────────────────────
    from src.engine.strategy_registry import get_ai_mode

    ai_mode = get_ai_mode("MULTILEG")
    log.debug("[multileg-paper] %s: ai_mode=%s", symbol, ai_mode)

    now_iso = datetime.now(timezone.utc).isoformat()

    # ── 4. Check open books ────────────────────────────────────────────
    from src.models.schema import (
        close_book,
        close_leg,
        get_open_book_legs,
        get_open_books_for_symbol,
        increment_adjustment_count,
        insert_multileg_trade_atomically,
    )

    open_books = get_open_books_for_symbol(symbol)
    mon_res = None
    if open_books:
        mon_res = _monitor_open_books(
            symbol,
            scan_context,
            digest_id,
            intel,
            open_books,
            ai_mode,
            now_iso,
        )
        # Re-fetch open books in case monitoring closed a book
        open_books = get_open_books_for_symbol(symbol)

    # ── 5. Attempt new entry ─────────────────────────────────────────────
    new_res = _attempt_new_entry(
        symbol,
        scan_context,
        digest_id,
        intel,
        ai_verdict,
        ai_mode,
        now_iso,
        open_books=open_books,
    )
    if new_res and isinstance(mon_res, dict):
        # Preserve open-book state so the digest can show both the live book
        # and this scan's new-entry decision.
        new_res.setdefault("live_books", mon_res.get("live_books") or [])
        new_res.setdefault("closed", mon_res.get("closed") or [])
        new_res.setdefault("ai_exit_advice", mon_res.get("ai_exit_advice"))
    return new_res if new_res else mon_res


def _monitor_open_books(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    open_books: list[dict],
    ai_mode: str,
    now_iso: str,
) -> dict | None:
    """Monitor existing open books for exit conditions.

    Checks profit target, stop loss, and time decay exit for each open book.
    If ai_mode is "full", also calls LLM for exit/adjustment advice.
    """
    from config.multileg_strategies import (
        DEFAULT_PROFIT_TARGET_PCT,
        DEFAULT_STOP_LOSS_PCT,
        DEFAULT_TIME_DECAY_EXIT_DTE,
    )
    from src.models.schema import (
        close_book,
        close_leg,
        get_open_book_legs,
        increment_adjustment_count,
    )

    closed_actions = []
    live_books: list[dict] = []
    ai_advice_seen: dict | None = None

    for book in open_books:
        book_id = book.get("book_id", "")
        trade_id = book.get("id", 0)
        strategy_type = book.get("strategy_type", "")
        net_premium = float(book.get("net_premium") or 0.0)
        legs = book.get("legs") or get_open_book_legs(trade_id)
        total_pnl = _calc_multileg_pnl(book, legs)
        profit_target_pct = float(
            book.get("profit_target_pct") or DEFAULT_PROFIT_TARGET_PCT
        )
        stop_loss_pct = float(
            book.get("stop_loss_pct") or DEFAULT_STOP_LOSS_PCT
        )
        time_decay_exit_dte = int(
            book.get("time_decay_exit_dte") or DEFAULT_TIME_DECAY_EXIT_DTE
        )
        expiry = book.get("expiry", "")
        entry_underlying = float(book.get("entry_underlying") or 0.0)
        adjustment_count = int(book.get("adjustment_count") or 0)

        if not legs:
            log.warning(
                "[multileg-paper] %s: book %s has no open legs — closing",
                symbol,
                book_id,
            )
            close_book(
                book_id, now_iso, "CLOSED", "NO_OPEN_LEGS", total_pnl
            )
            closed_actions.append({
                "action": "CLOSED",
                "book_id": book_id,
                "reason": "No open legs — auto-closed",
                "total_pnl": total_pnl,
            })
            continue

        # ── Check exit conditions ──────────────────────────────────────
        # 4a. Profit target
        if net_premium > 0:
            # BUG FIX: net_premium is per-lot; scale it by lot_size to match total_pnl units
            from config.settings import LOT_SIZES
            base_symbol = symbol.upper().split()[0] if symbol else symbol.upper()
            lot_size = LOT_SIZES.get(base_symbol, 1)
            # Max lots across legs
            total_lots = max((int(leg.get("lots") or 1) for leg in legs), default=1)
            max_profit = net_premium * lot_size * total_lots  # Total premium collected for strategy
            profit_pct = total_pnl / max_profit if max_profit > 0 else 0.0
            if profit_pct >= profit_target_pct:
                log.info(
                    "[multileg-paper] %s: book %s hit profit target %.0f%% >= %.0f%%",
                    symbol,
                    book_id,
                    profit_pct * 100,
                    profit_target_pct * 100,
                )
                curr_und = float((scan_context or {}).get("underlying") or entry_underlying)
                close_book(
                    book_id, now_iso, "CLOSED",
                    f"PROFIT_TARGET ({profit_pct*100:.0f}% of max)",
                    total_pnl,
                    curr_und,
                )
                for leg in legs:
                    leg_id = leg.get("id")
                    if leg_id:
                        leg_exit_prem = float(leg.get("current_premium") or leg.get("entry_premium") or 0.0)
                        close_leg(leg_id, now_iso, leg_exit_prem, "BOOK_CLOSED_PROFIT_TARGET")
                closed_actions.append({
                "action": "CLOSED",
                "book_id": book_id,
                "strategy_type": book.get("strategy_type") or book.get("structure"),
                "reason": f"Profit target hit: {profit_pct*100:.0f}%",
                "total_pnl": total_pnl,
                "legs": legs,
            })
                continue

        # 4b. Stop loss
        stop_loss_threshold_rupees = _get_stop_loss_threshold_rupees(book, legs, symbol)
        if total_pnl <= -stop_loss_threshold_rupees:
            log.info(
                "[multileg-paper] %s: book %s hit stop loss — loss ₹%.1f exceeds cap ₹%.1f",
                symbol,
                book_id,
                abs(total_pnl),
                stop_loss_threshold_rupees,
            )
            curr_und = float((scan_context or {}).get("underlying") or entry_underlying)
            close_book(
                book_id, now_iso, "CLOSED",
                f"STOP_LOSS (loss ₹{abs(total_pnl):.0f} > cap ₹{stop_loss_threshold_rupees:.0f})",
                total_pnl,
                curr_und,
            )
            for leg in legs:
                leg_id = leg.get("id")
                if leg_id:
                    leg_exit_prem = float(leg.get("current_premium") or leg.get("entry_premium") or 0.0)
                    close_leg(leg_id, now_iso, leg_exit_prem, "BOOK_CLOSED_STOP_LOSS")
            closed_actions.append({
                "action": "CLOSED",
                "book_id": book_id,
                "strategy_type": book.get("strategy_type") or book.get("structure"),
                "reason": f"Stop loss hit: ₹{abs(total_pnl):.0f} loss",
                "total_pnl": total_pnl,
                "legs": legs,
            })
            continue

        # 4c. Time decay exit
        dte = _dte_from_expiry(expiry)
        now_ist = datetime.now(IST)
        is_mcx = symbol in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER")
        is_weekly_index = symbol in ("NIFTY", "BANKNIFTY", "SENSEX")
        is_expiry_close = False
        is_expiry_after_1pm = False
        if dte == 0:
            if is_mcx:
                is_expiry_close = (now_ist.hour > 23 or (now_ist.hour == 23 and now_ist.minute >= 25))
            else:
                is_expiry_close = (now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 25))
                is_expiry_after_1pm = (now_ist.hour >= 13)

        if is_weekly_index:
            # Weekly index options (NIFTY, BANKNIFTY, SENSEX): DTE 1 or 2 is standard holding territory.
            # Time decay exit occurs ONLY on expiry day after 1:00 PM IST (13:00) or at 15:25 close squareoff.
            should_exit_time_decay = (
                (dte == 0 and is_expiry_after_1pm) or
                (dte < 0)
            )
            exit_reason_str = f"EXPIRY_SQUAREOFF (15:25 IST)" if (dte == 0 and is_expiry_close) else (
                "EXPIRY_TIME_DECAY (DTE 0 after 13:00 IST)" if (dte == 0 and is_expiry_after_1pm) else f"EXPIRED (DTE {dte})"
            )
        else:
            should_exit_time_decay = (
                (time_decay_exit_dte > 0 and dte < time_decay_exit_dte and dte > 0) or
                (dte == 0 and is_expiry_close) or
                (dte < 0)
            )
            exit_reason_str = f"EXPIRY_SQUAREOFF ({'23:25' if is_mcx else '15:25'} IST)" if (dte == 0 and is_expiry_close) else f"TIME_DECAY (DTE {dte} < {time_decay_exit_dte})"

        if should_exit_time_decay:
            log.info(
                "[multileg-paper] %s: book %s time decay exit — DTE %d (time_decay_exit_dte=%d)",
                symbol,
                book_id,
                dte,
                time_decay_exit_dte,
            )
            # BUG FIX: Fetch live exit premiums from DB instead of hardcoding 0.0
            from src.models.schema import get_latest_option_snapshot
            for leg in legs:
                leg_id = leg.get("id")
                if leg_id:
                    leg_strike = float(leg.get("strike") or 0.0)
                    leg_opt = leg.get("option_type", "CE")
                    # Fetch latest premium snapshot
                    snap = get_latest_option_snapshot(symbol, expiry, leg_strike, leg_opt)
                    exit_premium = float(snap.get("ltp") or 0.0) if snap else 0.0
                    if exit_premium <= 0:
                        # Fallback to intrinsic value if no snapshot
                        current_underlying = float((scan_context or {}).get("underlying") or entry_underlying)
                        if leg_opt == "CE":
                            exit_premium = max(0.0, current_underlying - leg_strike)
                        else:
                            exit_premium = max(0.0, leg_strike - current_underlying)
                    close_leg(leg_id, now_iso, exit_premium, "BOOK_CLOSED_TIME_DECAY")

            curr_und = float((scan_context or {}).get("underlying") or entry_underlying)
            close_book(
                book_id, now_iso, "CLOSED",
                exit_reason_str,
                total_pnl,
                curr_und,
            )
            closed_actions.append({
                "action": "CLOSED",
                "book_id": book_id,
                "strategy_type": book.get("strategy_type") or book.get("structure"),
                "reason": exit_reason_str,
                "total_pnl": total_pnl,
                "legs": legs,
            })
            continue

        # 4d. AI exit advice (full mode only)
        if ai_mode == "full":
            try:
                from src.engine.llm_enrichment import get_multileg_exit_advice

                advice = get_multileg_exit_advice(
                    symbol, book, legs, scan_context, intel
                )
                if advice and isinstance(advice, dict):
                    action = (advice.get("action") or "HOLD").upper()
                    reasoning = sanitize_mojibake(advice.get("reasoning", ""))
                    ai_advice_seen = {
                        "book_id": book_id,
                        "action": action,
                        "urgency": advice.get("urgency", "LOW"),
                        "reasoning": reasoning,
                    }

                    if action == "CLOSE":
                        from config.runtime_config import load_runtime_config
                        exit_advisor_enabled = load_runtime_config().get("live_ai_exit_advisor_enabled", True)
                        if exit_advisor_enabled:
                            log.info(
                                "[multileg-paper] %s: book %s — AI executing autonomous CLOSE: %s",
                                symbol,
                                book_id,
                                reasoning,
                            )
                            exit_reason_str = f"CLOSED_AI_EXIT ({reasoning[:60]})"
                            from src.models.schema import get_latest_option_snapshot
                            for leg in legs:
                                leg_id = leg.get("id")
                                if leg_id:
                                    leg_strike = float(leg.get("strike") or 0.0)
                                    leg_opt = leg.get("option_type", "CE")
                                    snap = get_latest_option_snapshot(symbol, expiry, leg_strike, leg_opt)
                                    exit_premium = float(snap.get("ltp") or 0.0) if snap else 0.0
                                    if exit_premium <= 0:
                                        current_underlying = float((scan_context or {}).get("underlying") or entry_underlying)
                                        if leg_opt == "CE":
                                            exit_premium = max(0.0, current_underlying - leg_strike)
                                        else:
                                            exit_premium = max(0.0, leg_strike - current_underlying)
                                    close_leg(leg_id, now_iso, exit_premium, "BOOK_CLOSED_AI_EXIT")

                            curr_und = float((scan_context or {}).get("underlying") or entry_underlying)
                            close_book(
                                book_id, now_iso, "CLOSED",
                                exit_reason_str,
                                total_pnl,
                                curr_und,
                            )
                            closed_actions.append({
                                "action": "CLOSED",
                                "book_id": book_id,
                                "strategy_type": book.get("strategy_type") or book.get("structure"),
                                "reason": exit_reason_str,
                                "total_pnl": total_pnl,
                                "legs": legs,
                            })
                            continue
                        else:
                            log.info(
                                "[multileg-paper] %s: book %s — AI recommends CLOSE (advisory only; AI Exit Advisor disabled): %s",
                                symbol,
                                book_id,
                                reasoning,
                            )
                    elif action == "ADJUST":
                        adjustment_details = advice.get("adjustment")
                        from config.runtime_config import load_runtime_config
                        exit_advisor_enabled = load_runtime_config().get("live_ai_exit_advisor_enabled", True)
                        if exit_advisor_enabled and adjustment_details and adjustment_count < 3:
                            log.info(
                                "[multileg-paper] %s: book %s — AI executing autonomous ADJUST: %s",
                                symbol,
                                book_id,
                                reasoning,
                            )
                            increment_adjustment_count(book_id)
                            ai_advice_seen["action"] = "ADJUSTED"
                        elif not exit_advisor_enabled and adjustment_details and adjustment_count < 3:
                            log.info(
                                "[multileg-paper] %s: book %s — AI recommends ADJUST (advisory only; AI Exit Advisor disabled): %s",
                                symbol,
                                book_id,
                                reasoning,
                            )
                        else:
                            log.info(
                                "[multileg-paper] %s: book %s — ADJUST advisory ignored; max adjustments (%d) reached or no details",
                                symbol,
                                book_id,
                                adjustment_count,
                            )
            except Exception as e:
                log.warning(
                    "[multileg-paper] %s: AI exit advice failed: %s",
                    symbol,
                    e,
                )

        max_profit_rupees = 0.0
        try:
            from config.settings import LOT_SIZES
            _base = symbol.upper().split()[0] if symbol else ""
            _lot = LOT_SIZES.get(symbol, LOT_SIZES.get(_base, 1))
            _lots = max((int(l.get("lots") or 1) for l in legs), default=1)
            max_profit_rupees = net_premium * _lot * _lots
        except Exception:
            pass

        live_books.append({
            "book_id": book_id,
            "strategy_type": strategy_type,
            "legs": legs,
            "net_premium": net_premium,
            "total_pnl": total_pnl,
            "max_profit_rupees": max_profit_rupees,
            "pnl_pct_of_max": (total_pnl / max_profit_rupees) if max_profit_rupees > 0 else 0.0,
            "net_delta": float(book.get("net_delta") or 0.0),
            "net_theta": float(book.get("net_theta") or 0.0),
            "breakeven_lower": float(book.get("breakeven_lower") or 0.0),
            "breakeven_upper": float(book.get("breakeven_upper") or 0.0),
            "profit_target_pct": profit_target_pct,
            "stop_loss_pct": stop_loss_pct,
            "stop_loss_rupees": _get_stop_loss_threshold_rupees(book, legs, symbol),
            "time_decay_exit_dte": time_decay_exit_dte,
            "dte": _dte_from_expiry(expiry),
            "adjustment_count": adjustment_count,
            "opened_at": book.get("opened_at"),
        })

    if closed_actions:
        return {
            "action": "MONITORED",
            "decision_stage": "BOOK_MONITOR",
            "closed": closed_actions,
            "live_books": live_books,
            "ai_exit_advice": ai_advice_seen,
            "open_books_remaining": len(open_books) - len(
                [a for a in closed_actions if a.get("action") == "CLOSED"]
            ),
        }

    # No exits triggered — report status
    log.debug(
        "[multileg-paper] %s: %d open book(s) — all within thresholds",
        symbol,
        len(open_books),
    )
    return {
        "action": "HOLD",
        "decision_stage": "BOOK_MONITOR",
        "open_books": len(open_books),
        "live_books": live_books,
        "ai_exit_advice": ai_advice_seen,
        "reason": "No exit conditions met",
    }


def _attempt_new_entry(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict,
    ai_mode: str,
    now_iso: str,
    open_books: list[dict],
) -> dict | None:
    """Attempt to enter a new multi-leg book."""
    legit = (scan_context or {}).get("data_legitimacy") or {}
    if (isinstance(legit, dict) and legit.get("is_0dte_cutoff")) or (scan_context or {}).get("is_0dte_cutoff"):
        log.info("[multileg-paper] %s: 0DTE entry cutoff reached — new entries prohibited", symbol)
        return {"action": "BLOCKED_0DTE_CUTOFF", "reason": "0DTE entry cutoff reached — new entries prohibited"}

    from config.multileg_strategies import (
        ALLOWED_SYMBOLS,
        CONFLICTING_STRATEGIES,
        MAX_BOOK_MARGIN,
        MAX_LEGS_PER_BOOK,
        MAX_NET_DELTA,
    )
    from src.models.schema import get_open_books_for_symbol, insert_multileg_trade_atomically

    # ── 5a. LLM verdict ───────────────────────────────────────────────
    from src.engine.llm_enrichment import get_multileg_verdict

    try:
        verdict = get_multileg_verdict(
            symbol=symbol,
            intel=intel,
            scan_context=scan_context,
            alerts=intel.get("alerts") if isinstance(intel, dict) else None,
            news_data=intel.get("news_data") if isinstance(intel, dict) else None,
            open_books=open_books or get_open_books_for_symbol(symbol),
        )
    except Exception as e:
        log.error("[multileg-paper] %s: LLM verdict call failed: %s", symbol, e)
        return None

    if verdict is None:
        log.debug("[multileg-paper] %s: LLM returned no verdict", symbol)
        return None

    st_upper = str(getattr(verdict, "strategy_type", "")).upper().strip()
    if st_upper in ("NONE", "NO_TRADE", "NO_SIGNAL", "SKIP", "N/A", "") or not getattr(verdict, "legs", None):
        log.info("[multileg-paper] %s: LLM returned no-trade verdict (%s)", symbol, st_upper or "NO_LEGS")
        return {
            "action": "NO_TRADE",
            "decision_stage": "LLM_STRUCTURE_SELECTION",
            "strategy_type": getattr(verdict, "strategy_type", "NO_TRADE"),
            "reason": f"LLM multileg verdict: {getattr(verdict, 'strategy_type', 'NO_TRADE')}",
            "entry_rationale": getattr(verdict, "entry_rationale", ""),
            "thesis": getattr(verdict, "thesis", ""),
            "confidence": getattr(verdict, "confidence", 0),
            "ai_model_name": getattr(verdict, "model_name", None),
        }

    # ── 5b. Validate legs ─────────────────────────────────────────────
    try:
        from src.engine.multileg_strategy import (
            validate_legs,
            compute_book_greeks,
            compute_book_risk_profile,
            calculate_combined_margin,
            score_entry_quality,
            check_book_conflicts,
        )
    except ImportError:
        log.warning(
            "[multileg-paper] %s: multileg_strategy module not available — "
            "inserting without engine validation",
            symbol,
        )
        # Fall back to basic validation only
        validate_legs = None
        compute_book_greeks = None
        compute_book_risk_profile = None
        calculate_combined_margin = None
        score_entry_quality = None
        check_book_conflicts = None

    strategy_type = verdict.strategy_type
    # Convert Pydantic legs to mutable dicts and apply dashboard paper lot sizing.
    legs = [
        leg.model_dump() if hasattr(leg, "model_dump") else (leg.dict() if hasattr(leg, "dict") else dict(leg))
        for leg in (verdict.legs or [])
    ]

    # Resolve REAL live market premiums from option chain snapshots (never trust LLM estimated premiums)
    from src.engine.trade_plan import get_option_premium
    expiry = (scan_context or {}).get("expiry", "")
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    option_rows = list((scan_context or {}).get("option_rows") or [])

    for leg in legs:
        leg_strike = float(leg.get("strike") or 0.0)
        leg_opt = str(leg.get("option_type") or "").upper()
        live_prem = get_option_premium(
            symbol=symbol,
            expiry=expiry,
            strike=leg_strike,
            option_type=leg_opt,
            option_rows=option_rows,
            underlying_price=underlying,
        )
        if live_prem is not None and live_prem > 0:
            leg["premium"] = float(live_prem)
            leg["entry_premium"] = float(live_prem)
        else:
            # Fallback to search directly in option_rows
            for row in option_rows:
                if abs(float(row.get("strike") or 0.0) - leg_strike) < 0.01 and str(row.get("option_type") or "").upper() == leg_opt:
                    r_ltp = float(row.get("ltp") or 0.0)
                    if r_ltp > 0:
                        leg["premium"] = r_ltp
                        leg["entry_premium"] = r_ltp
                        break

    try:
        from src.engine.capital_allocator import calculate_trade_lots
        sizing_premium = max((float(l.get("premium") or 0) for l in legs), default=0.0)
        dashboard_lots = calculate_trade_lots(
            symbol,
            sizing_premium,
            side="SELL",
            is_paper=True,
            setup_type="MULTILEG",
        )
        for leg in legs:
            leg["lots"] = dashboard_lots
    except Exception as e:
        log.warning("[multileg-paper] %s: lot sizing failed, defaulting to 1 lot: %s", symbol, e)
        for leg in legs:
            leg.setdefault("lots", 1)

    if validate_legs is not None:
        underlying = float((scan_context or {}).get("underlying") or 0.0)
        option_rows = list((scan_context or {}).get("option_rows") or [])

        is_valid, validation_msg = validate_legs(
            strategy_type, legs, option_rows, underlying
        )
        if not is_valid:
            log.info(
                "[multileg-paper] %s: legs validation failed — %s",
                symbol,
                validation_msg,
            )
            return {
                "action": "REJECTED",
                "decision_stage": "LEG_VALIDATION",
                "strategy_type": strategy_type,
                "confidence": getattr(verdict, "confidence", 0),
                "thesis": getattr(verdict, "thesis", ""),
                "ai_model_name": getattr(verdict, "model_name", None),
                "reason": f"Legs validation: {validation_msg}",
            }

    # ── Strict Binary Pre-Flight Validation for All Entry Legs (Flaw #5) ──
    from src.engine.data_validator import validate_trade_leg_data
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    oc_data_payload = {"strikes": list((scan_context or {}).get("option_rows") or [])}
    is_binary_valid, binary_issues = validate_trade_leg_data(legs, oc_data_payload, underlying)
    if not is_binary_valid:
        log.warning(
            "[multileg-paper] %s: atomic leg data validation failed — %s",
            symbol,
            binary_issues,
        )
        return {
            "action": "REJECTED",
            "decision_stage": "LEG_DATA_INTEGRITY",
            "strategy_type": strategy_type,
            "confidence": getattr(verdict, "confidence", 0),
            "thesis": getattr(verdict, "thesis", ""),
            "ai_model_name": getattr(verdict, "model_name", None),
            "reason": f"Leg data integrity rejected: {', '.join(binary_issues)}",
        }

    # ── 5c. Compute Greeks and risk profile ────────────────────────────
    expiry = (scan_context or {}).get("expiry", "")
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    option_rows = list((scan_context or {}).get("option_rows") or [])
    
    # Calculate net_premium from legs to protect against LLM returning 0.0
    sell_prem_sum = sum(float(l.get("entry_premium") or l.get("premium") or 0.0) for l in legs if (l.get("side") or "SELL").upper() == "SELL")
    buy_prem_sum = sum(float(l.get("entry_premium") or l.get("premium") or 0.0) for l in legs if (l.get("side") or "SELL").upper() == "BUY")
    calc_net_prem = round(sell_prem_sum - buy_prem_sum, 2)
    net_premium = calc_net_prem if calc_net_prem > 0 else (verdict.net_premium or 0.0)

    book_greeks = {}
    risk_profile = {}
    combined_margin = 0.0

    if compute_book_greeks is not None:
        try:
            book_greeks = compute_book_greeks(
                legs, option_rows, underlying, expiry
            ) or {}
        except Exception as e:
            log.warning("[multileg-paper] %s: Greeks computation failed: %s", symbol, e)

    if compute_book_risk_profile is not None:
        try:
            risk_profile = compute_book_risk_profile(
                strategy_type, legs, net_premium, underlying
            ) or {}
        except Exception as e:
            log.warning(
                "[multileg-paper] %s: Risk profile computation failed: %s",
                symbol,
                e,
            )

    if calculate_combined_margin is not None:
        try:
            combined_margin = calculate_combined_margin(
                legs, symbol, risk_profile=risk_profile, underlying=underlying
            )
        except Exception as e:
            log.warning(
                "[multileg-paper] %s: Margin computation failed: %s", symbol, e
            )

    # ── 5d. Book conflict check ────────────────────────────────────────
    if check_book_conflicts is not None:
        try:
            has_conflict, conflict_msg = check_book_conflicts(
                symbol, strategy_type, open_books or get_open_books_for_symbol(symbol)
            )
            if has_conflict:
                log.info(
                    "[multileg-paper] %s: book conflict — %s",
                    symbol,
                    conflict_msg,
                )
                return {
                    "action": "CONFLICT",
                    "decision_stage": "BOOK_CONFLICT",
                    "strategy_type": strategy_type,
                    "confidence": getattr(verdict, "confidence", 0),
                    "thesis": getattr(verdict, "thesis", ""),
                    "ai_model_name": getattr(verdict, "model_name", None),
                    "reason": conflict_msg,
                }
        except Exception as e:
            log.warning(
                "[multileg-paper] %s: Conflict check failed: %s", symbol, e
            )

    # ── 5e. Entry quality score ────────────────────────────────────────
    entry_quality = 0
    quality_reasons: list[str] = []
    if score_entry_quality is not None:
        try:
            entry_quality, quality_reasons = score_entry_quality(
                symbol=symbol,
                strategy_type=strategy_type,
                legs=legs,
                net_premium=net_premium,
                underlying=underlying,
                scan_context=scan_context,
                intel=intel,
                book_greeks=book_greeks,
                risk_profile=risk_profile,
                combined_margin=combined_margin,
            )
        except Exception as e:
            log.warning(
                "[multileg-paper] %s: Entry quality scoring failed: %s",
                symbol,
                e,
            )

    # ── 5f. Margin cap check ──────────────────────────────────────────
    if combined_margin > MAX_BOOK_MARGIN:
        log.info(
            "[multileg-paper] %s: combined margin ₹%.0f exceeds cap ₹%.0f",
            symbol,
            combined_margin,
            MAX_BOOK_MARGIN,
        )
        return {
            "action": "REJECTED",
            "decision_stage": "RISK_GATE_MARGIN",
            "strategy_type": strategy_type,
            "legs": legs,
            "net_premium": net_premium,
            "confidence": verdict.confidence,
            "entry_quality": entry_quality,
            "quality_reasons": quality_reasons,
            "book_greeks": book_greeks,
            "risk_profile": risk_profile,
            "margin": combined_margin,
            "margin_cap": MAX_BOOK_MARGIN,
            "thesis": getattr(verdict, "thesis", ""),
            "ai_model_name": getattr(verdict, "model_name", None),
            "reason": f"Margin ₹{combined_margin:,.0f} exceeds cap ₹{MAX_BOOK_MARGIN:,.0f}",
        }

    # ── 5g. Net delta cap check ───────────────────────────────────────
    net_delta = book_greeks.get("net_delta", verdict.net_delta)
    if abs(net_delta) > MAX_NET_DELTA:
        log.info(
            "[multileg-paper] %s: net delta %.2f exceeds cap %.2f",
            symbol,
            net_delta,
            MAX_NET_DELTA,
        )
        return {
            "action": "REJECTED",
            "decision_stage": "RISK_GATE_DELTA",
            "strategy_type": strategy_type,
            "legs": legs,
            "net_premium": net_premium,
            "confidence": verdict.confidence,
            "entry_quality": entry_quality,
            "quality_reasons": quality_reasons,
            "book_greeks": book_greeks,
            "risk_profile": risk_profile,
            "margin": combined_margin,
            "net_delta": net_delta,
            "delta_cap": MAX_NET_DELTA,
            "thesis": getattr(verdict, "thesis", ""),
            "ai_model_name": getattr(verdict, "model_name", None),
            "reason": f"Net delta {net_delta:.2f} exceeds cap {MAX_NET_DELTA}",
        }

    # ── 5h. Log verdict summary ────────────────────────────────────────
    log.info(
        "[multileg-paper] %s: %s with %d legs, net premium ₹%.1f, "
        "margin ₹%.0f, entry_quality=%d, confidence=%d%% (mode=%s)",
        symbol,
        strategy_type,
        len(legs),
        net_premium,
        combined_margin,
        entry_quality,
        verdict.confidence,
        ai_mode,
    )
    for i, leg in enumerate(legs):
        log.debug(
            "[multileg-paper]   leg %d: SELL %s %s @ ₹%.1f (delta=%.2f, lots=%s)",
            i + 1,
            leg.get("option_type", "?"),
            leg.get("strike", "?"),
            float(leg.get("premium") or 0.0),
            float(leg.get("delta") or 0.0),
            leg.get("lots", 1),
        )

    # ── 5i. Advisory mode — log only, no trade insertion ───────────────
    if ai_mode == "advisory":
        log.info(
            "[multileg-paper] %s: advisory mode — logging verdict only, no trade",
            symbol,
        )
        return {
            "action": "ADVISORY",
            "decision_stage": "AI_MODE_ADVISORY",
            "strategy_type": strategy_type,
            "legs_count": len(legs),
            "legs": legs,
            "net_premium": net_premium,
            "confidence": verdict.confidence,
            "entry_quality": entry_quality,
            "quality_reasons": quality_reasons,
            "book_greeks": book_greeks,
            "risk_profile": risk_profile,
            "margin": combined_margin,
            "entry_rationale": getattr(verdict, "entry_rationale", ""),
            "thesis": getattr(verdict, "thesis", ""),
            "exit_plan": {
                "profit_target_pct": verdict.profit_target_pct,
                "stop_loss_pct": verdict.stop_loss_pct,
                "time_decay_exit_dte": verdict.time_decay_exit_dte,
            },
            "adjustment_plan": getattr(verdict, "adjustment_plan", ""),
            "ai_model_name": getattr(verdict, "model_name", None),
            "reason": "Advisory mode — no trade inserted",
        }

    # ── 5j. Full mode — insert trade atomically ───────────────────────
    book_id = f"ML-{symbol[:3]}-{uuid.uuid4().hex[:8]}"
    trade_id = int(uuid.uuid4().int % 999999999)  # Fallback; DB generates real ID

    entry_reason = (
        getattr(verdict, "entry_rationale", None)
        or getattr(verdict, "thesis", None)
        or f"{strategy_type} setup for {symbol} ({len(legs)} legs, net prem ₹{net_premium:.2f})"
    )
    if not entry_reason:
        leg_rats = [l.get("rationale") for l in legs if l.get("rationale")]
        entry_reason = "; ".join(leg_rats) if leg_rats else f"{strategy_type} entry"

    trade_dict = {
        "trade_ref": 0,  # Will be set by DB if needed
        "symbol": symbol,
        "structure": strategy_type,
        "net_premium": net_premium,
        "margin_req": combined_margin,
        "total_pnl": 0.0,
        "opened_at": now_iso,
        "closed_at": None,
        "status": "OPEN",
        "reason": entry_reason,
        "entry_reason": entry_reason,
        "exit_reason": None,
        "profit_factor": 0.0,
        "book_id": book_id,
        "strategy_type": strategy_type,
        "expiry": expiry,
        "entry_underlying": underlying,
        "exit_underlying": None,
        "net_delta": book_greeks.get("net_delta", verdict.net_delta),
        "net_theta": book_greeks.get("net_theta", verdict.net_theta),
        "net_vega": book_greeks.get("net_vega", verdict.net_vega),
        "max_profit": risk_profile.get("max_profit") if risk_profile.get("max_profit") is not None else getattr(verdict, "max_profit", 0.0),
        "max_loss": risk_profile.get("max_loss") if risk_profile.get("max_loss") is not None else getattr(verdict, "max_loss", 0.0),
        "breakeven_upper": risk_profile.get("breakeven_upper") if risk_profile.get("breakeven_upper") is not None else getattr(verdict, "breakeven_upper", 0.0),
        "breakeven_lower": risk_profile.get("breakeven_lower") if risk_profile.get("breakeven_lower") is not None else getattr(verdict, "breakeven_lower", 0.0),
        "profit_target_pct": verdict.profit_target_pct,
        "stop_loss_pct": verdict.stop_loss_pct,
        "time_decay_exit_dte": verdict.time_decay_exit_dte,
        "adjustment_count": 0,
        "confidence_score": verdict.confidence,
        "entry_quality_score": entry_quality,
        "digest_id": digest_id,
        "ai_model_name": verdict.model_name,
    }

    leg_dicts = []
    for leg in legs:
        leg_dicts.append({
            "trade_id": 0,  # Set by insert_multileg_trade_atomically
            "side": leg.get("side", "SELL"),
            "lots": int(leg.get("lots") or 1),
            "strike": float(leg.get("strike") or 0.0),
            "option_type": leg.get("option_type", ""),
            "entry_premium": float(leg.get("premium") or 0.0),
            "exit_premium": 0.0,
            "delta": float(leg.get("delta") or 0.0),
            "theta": 0.0,
            "vega": 0.0,
            "iv": 0.0,
            "rationale": leg.get("rationale", ""),
            "status": "OPEN",
            "closed_at": None,
            "exit_reason": None,
            "broker_order_id": None,
        })

    try:
        inserted_id = insert_multileg_trade_atomically(trade_dict, leg_dicts)
        if inserted_id:
            log.info(
                "[multileg-paper] %s: inserted book %s (trade_id=%d) — %s, %d legs, net premium ₹%.1f",
                symbol,
                book_id,
                inserted_id,
                strategy_type,
                len(leg_dicts),
                net_premium,
            )
            return {
                "action": "ENTERED",
                "decision_stage": "EXECUTED",
                "trade_id": inserted_id,
                "book_id": book_id,
                "strategy_type": strategy_type,
                "legs_count": len(leg_dicts),
                "legs": leg_dicts,
                "net_premium": net_premium,
                "confidence": verdict.confidence,
                "entry_quality": entry_quality,
                "quality_reasons": quality_reasons,
                "book_greeks": book_greeks,
                "risk_profile": risk_profile,
                "margin": combined_margin,
                "entry_rationale": entry_reason,
                "thesis": verdict.thesis,
                "exit_plan": {
                    "profit_target_pct": verdict.profit_target_pct,
                    "stop_loss_pct": verdict.stop_loss_pct,
                    "time_decay_exit_dte": verdict.time_decay_exit_dte,
                },
                "adjustment_plan": getattr(verdict, "adjustment_plan", ""),
                "ai_model_name": verdict.model_name,
            }
        else:
            log.error(
                "[multileg-paper] %s: insert_multileg_trade_atomically returned 0",
                symbol,
            )
            return None
    except Exception as e:
        log.error(
            "[multileg-paper] %s: Failed to insert trade: %s", symbol, e, exc_info=True
        )
        return None


def _calc_multileg_pnl(book: dict, legs: list[dict]) -> float:
    """Calculate total PnL for a multi-leg options book.

    For short options (SELL): PnL = (entry_premium - current_premium) * lots * lot_size
    summed across all legs.
    """
    from config.settings import LOT_SIZES
    from src.models.schema import get_read_conn
    from src.engine.trade_plan import is_valid_option_premium

    scan_context = book.get("scan_context") or {}
    option_rows = list((scan_context or {}).get("option_rows") or [])
    symbol = book.get("symbol", "")
    base_sym = symbol.upper().split()[0] if symbol else ""
    lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 1))
    underlying = float((scan_context or {}).get("underlying") or book.get("entry_underlying") or 0.0)

    total_pnl = 0.0
    for leg in legs:
        strike = float(leg.get("strike") or 0.0)
        option_type = (leg.get("option_type") or "").upper()
        entry_premium = float(leg.get("entry_premium") or leg.get("premium") or 0.0)
        lots = int(leg.get("lots") or 1)
        side = (leg.get("side") or "SELL").upper()

        current_premium = None
        for row in option_rows:
            row_strike = float(row.get("strike") or 0.0)
            row_type = (row.get("option_type") or "").upper()
            if abs(row_strike - strike) < 0.01 and row_type == option_type:
                ltp = float(row.get("ltp") or row.get("premium") or 0.0)
                if ltp > 0:
                    if underlying > 0 and not is_valid_option_premium(strike, option_type, ltp, underlying):
                        log.warning(
                            "[multileg-paper] %s: _calc_multileg_pnl rejected corrupted row LTP %.2f for %s %.0f (spot=%.2f)",
                            symbol, ltp, option_type, strike, underlying,
                        )
                    else:
                        current_premium = ltp
                        break

        if current_premium is None:
            try:
                leg_expiry = str(leg.get("expiry") or book.get("expiry") or "").strip()
                if leg_expiry:
                    with get_read_conn() as conn:
                        opt_row = conn.execute(
                            "SELECT ltp FROM option_chain_snapshots WHERE (symbol=? OR symbol=?) AND ABS(strike - ?) < 0.01 AND option_type=? AND expiry=? AND ltp IS NOT NULL AND ltp > 0 ORDER BY fetched_at DESC LIMIT 1",
                            (symbol, base_sym, strike, option_type, leg_expiry)
                        ).fetchone()
                        if opt_row:
                            snap_ltp = float(opt_row["ltp"])
                            if underlying > 0 and not is_valid_option_premium(strike, option_type, snap_ltp, underlying):
                                log.warning(
                                    "[multileg-paper] %s: _calc_multileg_pnl rejected corrupted DB snapshot LTP %.2f for %s %.0f (spot=%.2f)",
                                    symbol, snap_ltp, option_type, strike, underlying,
                                )
                            else:
                                current_premium = snap_ltp
            except Exception:
                pass
            except Exception:
                pass

        if current_premium is None:
            # Estimate using delta movement rather than raw fallback
            if underlying > 0 and book.get("entry_underlying"):
                entry_und = float(book.get("entry_underlying") or underlying)
                und_move = underlying - entry_und
                delta = float(leg.get("delta") or 0.25)
                # For CE: price increases if spot increases; for PE: price increases if spot decreases
                delta_sign = delta if option_type == "CE" else -abs(delta)
                current_premium = max(0.05, entry_premium + delta_sign * und_move)
            else:
                current_premium = entry_premium

        leg["current_premium"] = current_premium

        # Persist MTM to DB so digest reads (via get_open_books_for_symbol)
        # can show live current_premium for open legs. Only when this leg came
        # from DB (has an id) — synthetic in-memory legs have no row to update.
        leg_id = leg.get("id")
        if leg_id:
            try:
                from src.models.schema import update_multi_leg_leg_current_premium
                update_multi_leg_leg_current_premium(int(leg_id), float(current_premium))
            except Exception as e:
                log.debug("[multileg-paper] %s: MTM persist failed for leg %s: %s", symbol, leg_id, e)

        if side == "SELL":
            pnl = (entry_premium - current_premium) * lots * lot_size
        else:
            pnl = (current_premium - entry_premium) * lots * lot_size

        total_pnl += pnl

    return total_pnl
