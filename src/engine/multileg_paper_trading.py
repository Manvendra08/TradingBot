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

log = logging.getLogger(__name__)


def _dte_from_expiry(expiry: str) -> int:
    """Calculate days to expiry from YYYY-MM-DD string using IST timezone date."""
    try:
        from config.settings import IST

        exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        today = datetime.now(IST).date()
        return max(0, (exp_date - today).days)
    except Exception:
        return 999


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

    if open_books:
        return _monitor_open_books(
            symbol,
            scan_context,
            digest_id,
            intel,
            open_books,
            ai_mode,
            now_iso,
        )

    # ── 5. No open books — attempt new entry ──────────────────────────
    return _attempt_new_entry(
        symbol,
        scan_context,
        digest_id,
        intel,
        ai_verdict,
        ai_mode,
        now_iso,
        open_books=[],
    )


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

    for book in open_books:
        book_id = book.get("book_id", "")
        trade_id = book.get("id", 0)
        strategy_type = book.get("strategy_type", "")
        net_premium = float(book.get("net_premium") or 0.0)
        total_pnl = float(book.get("total_pnl") or 0.0)
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
        legs = book.get("legs") or get_open_book_legs(trade_id)

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
            max_profit = net_premium  # For short options, max profit = premium collected
            profit_pct = total_pnl / max_profit if max_profit > 0 else 0.0
            if profit_pct >= profit_target_pct:
                log.info(
                    "[multileg-paper] %s: book %s hit profit target %.0f%% >= %.0f%%",
                    symbol,
                    book_id,
                    profit_pct * 100,
                    profit_target_pct * 100,
                )
                close_book(
                    book_id, now_iso, "CLOSED",
                    f"PROFIT_TARGET ({profit_pct*100:.0f}% of max)",
                    total_pnl,
                )
                for leg in legs:
                    leg_id = leg.get("id")
                    if leg_id:
                        close_leg(leg_id, now_iso, 0.0, "BOOK_CLOSED_PROFIT_TARGET")
                closed_actions.append({
                    "action": "CLOSED",
                    "book_id": book_id,
                    "reason": f"Profit target hit: {profit_pct*100:.0f}%",
                    "total_pnl": total_pnl,
                })
                continue

        # 4b. Stop loss
        max_loss = float(book.get("max_loss") or 0.0)
        if max_loss > 0 and total_pnl <= -(max_loss * stop_loss_pct):
            log.info(
                "[multileg-paper] %s: book %s hit stop loss — loss ₹%.1f exceeds %.0f%% of max loss ₹%.1f",
                symbol,
                book_id,
                abs(total_pnl),
                stop_loss_pct * 100,
                max_loss,
            )
            close_book(
                book_id, now_iso, "CLOSED",
                f"STOP_LOSS (loss ₹{abs(total_pnl):.0f} > {stop_loss_pct*100:.0f}% of max)",
                total_pnl,
            )
            for leg in legs:
                leg_id = leg.get("id")
                if leg_id:
                    close_leg(leg_id, now_iso, 0.0, "BOOK_CLOSED_STOP_LOSS")
            closed_actions.append({
                "action": "CLOSED",
                "book_id": book_id,
                "reason": f"Stop loss hit: ₹{abs(total_pnl):.0f} loss",
                "total_pnl": total_pnl,
            })
            continue

        # 4c. Time decay exit
        dte = _dte_from_expiry(expiry)
        if dte <= time_decay_exit_dte:
            log.info(
                "[multileg-paper] %s: book %s time decay exit — DTE %d <= %d",
                symbol,
                book_id,
                dte,
                time_decay_exit_dte,
            )
            close_book(
                book_id, now_iso, "CLOSED",
                f"TIME_DECAY (DTE {dte} <= {time_decay_exit_dte})",
                total_pnl,
            )
            for leg in legs:
                leg_id = leg.get("id")
                if leg_id:
                    close_leg(leg_id, now_iso, 0.0, "BOOK_CLOSED_TIME_DECAY")
            closed_actions.append({
                "action": "CLOSED",
                "book_id": book_id,
                "reason": f"Time decay exit: DTE {dte}",
                "total_pnl": total_pnl,
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
                    reasoning = advice.get("reasoning", "")

                    if action == "CLOSE":
                        log.info(
                            "[multileg-paper] %s: book %s — AI recommends CLOSE: %s",
                            symbol,
                            book_id,
                            reasoning,
                        )
                        close_book(
                            book_id, now_iso, "CLOSED",
                            f"AI_EXIT: {reasoning[:200]}",
                            total_pnl,
                        )
                        for leg in legs:
                            leg_id = leg.get("id")
                            if leg_id:
                                close_leg(leg_id, now_iso, 0.0, "BOOK_CLOSED_AI_EXIT")
                        closed_actions.append({
                            "action": "CLOSED",
                            "book_id": book_id,
                            "reason": f"AI exit advice: {reasoning[:100]}",
                            "total_pnl": total_pnl,
                        })
                        continue

                    if action == "ADJUST":
                        adjustment_details = advice.get("adjustment", {})
                        if adjustment_details and adjustment_count < 3:
                            log.info(
                                "[multileg-paper] %s: book %s — AI recommends ADJUST: %s",
                                symbol,
                                book_id,
                                reasoning,
                            )
                            increment_adjustment_count(book_id)
                            closed_actions.append({
                                "action": "ADJUSTED",
                                "book_id": book_id,
                                "reason": reasoning[:200],
                                "details": adjustment_details,
                            })
                            continue
                        else:
                            log.info(
                                "[multileg-paper] %s: book %s — ADJUST suggested but max adjustments (%d) reached",
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

    if closed_actions:
        return {
            "action": "MONITORED",
            "closed": closed_actions,
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
        "open_books": len(open_books),
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
    """Attempt to enter a new multi-leg book.

    Flow:
        1. Get LLM verdict (strategy type + legs)
        2. Validate legs against strategy constraints
        3. Compute Greeks, risk profile, margin
        4. Check book conflicts and entry quality
        5. Advisory mode: log only; full mode: insert trade
    """
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
                "reason": f"Legs validation: {validation_msg}",
            }

    # ── 5c. Compute Greeks and risk profile ────────────────────────────
    expiry = (scan_context or {}).get("expiry", "")
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    option_rows = list((scan_context or {}).get("option_rows") or [])
    net_premium = verdict.net_premium

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
            combined_margin = calculate_combined_margin(legs, symbol)
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
            "strategy_type": strategy_type,
            "legs_count": len(legs),
            "net_premium": net_premium,
            "confidence": verdict.confidence,
            "entry_quality": entry_quality,
            "quality_reasons": quality_reasons,
            "reason": "Advisory mode — no trade inserted",
        }

    # ── 5j. Full mode — insert trade atomically ───────────────────────
    book_id = f"ML-{symbol[:3]}-{uuid.uuid4().hex[:8]}"
    trade_id = int(uuid.uuid4().int % 999999999)  # Fallback; DB generates real ID

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
        "reason": None,
        "profit_factor": 0.0,
        "book_id": book_id,
        "strategy_type": strategy_type,
        "expiry": expiry,
        "entry_underlying": underlying,
        "exit_underlying": None,
        "net_delta": book_greeks.get("net_delta", verdict.net_delta),
        "net_theta": book_greeks.get("net_theta", verdict.net_theta),
        "net_vega": book_greeks.get("net_vega", verdict.net_vega),
        "max_profit": verdict.max_profit,
        "max_loss": verdict.max_loss,
        "breakeven_upper": verdict.breakeven_upper,
        "breakeven_lower": verdict.breakeven_lower,
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
                "trade_id": inserted_id,
                "book_id": book_id,
                "strategy_type": strategy_type,
                "legs_count": len(leg_dicts),
                "net_premium": net_premium,
                "confidence": verdict.confidence,
                "entry_quality": entry_quality,
                "quality_reasons": quality_reasons,
                "thesis": verdict.thesis,
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
