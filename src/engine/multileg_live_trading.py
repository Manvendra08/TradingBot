"""Multi-leg live trading runner.

Implements the live-trading entry point for multi-leg strategies
(iron condors, strangles, straddles, spreads, jade lizards, custom).

Mirrors the paper trading flow but places real Kite orders with
sequential leg execution, rollback on partial fills, and broker
order tracking.

Flow:
    1. Market hours guard
    2. AI mode resolution from strategy_registry
    3. Open book monitoring (profit target, stop loss, time decay + broker polling)
    4. New book entry via LLM verdict + validation + risk checks
    5. Sequential Kite order placement with rollback on failure
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from config.settings import IST

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
    """Calculate the exact Stop Loss threshold in Rupees for a multi-leg book."""
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


def run_multileg_live_strategy(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict=None,
) -> dict | None:
    """Multi-leg live trading entry point.

    Monitors open books for exits and triggers new multi-leg entries
    via real Kite broker orders.

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
        return _run_multileg_live_strategy_inner(
            symbol, scan_context, digest_id, intel, ai_verdict
        )
    except Exception as e:
        log.error(
            "[multileg-live] %s: Unhandled exception in live strategy: %s",
            symbol,
            e,
            exc_info=True,
        )
        return None


def _run_multileg_live_strategy_inner(
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
            "[multileg-live] %s: skipped — outside market hours", symbol
        )
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    # ── 1b. Broker disabled guard ─────────────────────────────────────
    from config.runtime_config import load_runtime_config

    _rt_cfg = load_runtime_config()
    if _rt_cfg.get("live_broker_disabled", False):
        log.debug(
            "[multileg-live] %s: live broker disabled via cockpit — skipping",
            symbol,
        )
        return {
            "action": "SKIPPED_BROKER_DISABLED",
            "reason": "Broker trades turned off in Cockpit",
        }

    # ── 2. Basic validation ────────────────────────────────────────────
    from config.multileg_strategies import ALLOWED_SYMBOLS

    if symbol not in ALLOWED_SYMBOLS:
        log.debug(
            "[multileg-live] %s: not in ALLOWED_SYMBOLS %s",
            symbol,
            ALLOWED_SYMBOLS,
        )
        return None

    underlying = float((scan_context or {}).get("underlying") or 0.0)
    if underlying <= 0:
        log.debug("[multileg-live] %s: no underlying price", symbol)
        return None

    # ── 3. AI mode from strategy_registry ───────────────────────────────
    from src.engine.strategy_registry import get_ai_mode

    ai_mode = get_ai_mode("MULTILEG")
    log.debug("[multileg-live] %s: ai_mode=%s", symbol, ai_mode)

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
        mon_res = _monitor_open_books_live(
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

    # ── 5. Check book limit & attempt new entry ──────────────────────────
    rconf = load_runtime_config()
    max_books = rconf.get("max_concurrent_multileg_books_per_symbol", 3)

    if len(open_books) < max_books:
        new_res = _attempt_new_live_entry(
            symbol,
            scan_context,
            digest_id,
            intel,
            ai_verdict,
            ai_mode,
            now_iso,
            open_books=open_books,
        )
        return new_res if new_res else mon_res

    log.info("[multileg-live] %s: %d open books exist (max=%d) — skipping new entry attempt", symbol, len(open_books), max_books)
    return mon_res or {"action": "HOLD", "reason": f"Max open multileg books ({max_books}) reached"}


# ── Open Book Monitoring (Live) ──────────────────────────────────────────────


def _monitor_open_books_live(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    open_books: list[dict],
    ai_mode: str,
    now_iso: str,
) -> dict | None:
    """Monitor existing open books for exit conditions (live mode).

    Same exit logic as paper but includes broker order polling for
    GTT status and premium monitoring via live quotes.
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
        max_loss = float(book.get("max_loss") or 0.0)
        adjustment_count = int(book.get("adjustment_count") or 0)
        legs = book.get("legs") or get_open_book_legs(trade_id)

        # ── Update live PnL from current premiums ──────────────────────
        total_pnl = _update_live_book_pnl(symbol, book, legs, scan_context)
        if total_pnl != float(book.get("total_pnl") or 0.0):
            log.debug(
                "[multileg-live] %s: book %s PnL updated to ₹%.1f",
                symbol,
                book_id,
                total_pnl,
            )

        if not legs:
            log.warning(
                "[multileg-live] %s: book %s has no open legs — closing",
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
            from config.settings import LOT_SIZES
            base_symbol = symbol.upper().split()[0] if symbol else symbol.upper()
            lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_symbol, 1))
            total_lots = max((int(leg.get("lots") or 1) for leg in legs), default=1)
            max_profit_rupees = net_premium * lot_size * total_lots
            profit_pct = total_pnl / max_profit_rupees if max_profit_rupees > 0 else 0.0
            if profit_pct >= profit_target_pct:
                log.info(
                    "[multileg-live] %s: book %s hit profit target %.0f%% >= %.0f%%",
                    symbol,
                    book_id,
                    profit_pct * 100,
                    profit_target_pct * 100,
                )
                _close_live_book(
                    symbol, book_id, legs, now_iso,
                    "CLOSED",
                    f"PROFIT_TARGET ({profit_pct*100:.0f}% of max)",
                    total_pnl,
                )
                closed_actions.append({
                    "action": "CLOSED",
                    "book_id": book_id,
                    "reason": f"Profit target hit: {profit_pct*100:.0f}%",
                    "total_pnl": total_pnl,
                })
                continue

        # 4b. Stop loss
        stop_loss_threshold_rupees = _get_stop_loss_threshold_rupees(book, legs, symbol)
        if total_pnl <= -stop_loss_threshold_rupees:
            log.info(
                "[multileg-live] %s: book %s hit stop loss — loss ₹%.1f exceeds cap ₹%.1f",
                symbol,
                book_id,
                abs(total_pnl),
                stop_loss_threshold_rupees,
            )
            _close_live_book(
                symbol, book_id, legs, now_iso,
                "CLOSED",
                f"STOP_LOSS (loss ₹{abs(total_pnl):.0f} > cap ₹{stop_loss_threshold_rupees:.0f})",
                total_pnl,
            )
            closed_actions.append({
                "action": "CLOSED",
                "book_id": book_id,
                "reason": f"Stop loss hit: ₹{abs(total_pnl):.0f} loss",
                "total_pnl": total_pnl,
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
                "[multileg-live] %s: book %s time decay exit — DTE %d (time_decay_exit_dte=%d)",
                symbol,
                book_id,
                dte,
                time_decay_exit_dte,
            )
            _close_live_book(
                symbol, book_id, legs, now_iso,
                "CLOSED",
                exit_reason_str,
                total_pnl,
            )
            closed_actions.append({
                "action": "CLOSED",
                "book_id": book_id,
                "reason": exit_reason_str,
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
                            "[multileg-live] %s: book %s — AI recommends CLOSE: %s",
                            symbol,
                            book_id,
                            reasoning,
                        )
                        _close_live_book(
                            symbol, book_id, legs, now_iso,
                            "CLOSED",
                            f"AI_EXIT: {reasoning[:200]}",
                            total_pnl,
                        )
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
                                "[multileg-live] %s: book %s — AI recommends ADJUST: %s",
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
                                "[multileg-live] %s: book %s — ADJUST suggested but max adjustments (%d) reached",
                                symbol,
                                book_id,
                                adjustment_count,
                            )
            except Exception as e:
                log.warning(
                    "[multileg-live] %s: AI exit advice failed: %s",
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

    log.debug(
        "[multileg-live] %s: %d open book(s) — all within thresholds",
        symbol,
        len(open_books),
    )
    return {
        "action": "HOLD",
        "open_books": len(open_books),
        "reason": "No exit conditions met",
    }


def _update_live_book_pnl(
    symbol: str,
    book: dict,
    legs: list[dict],
    scan_context: dict,
) -> float:
    """Update book PnL using current option premiums from scan context or DB snapshot.

    For short options: PnL = (entry_premium - current_premium) * lots * lot_size
    summed across all legs.
    """
    from config.settings import LOT_SIZES
    from src.models.schema import get_read_conn

    option_rows = list((scan_context or {}).get("option_rows") or [])
    base_sym = symbol.upper().split()[0] if symbol else ""
    lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 1))

    total_pnl = 0.0
    for leg in legs:
        strike = float(leg.get("strike") or 0.0)
        option_type = (leg.get("option_type") or "").upper()
        entry_premium = float(leg.get("entry_premium") or 0.0)
        lots = int(leg.get("lots") or 1)
        side = (leg.get("side") or "SELL").upper()

        current_premium = None
        for row in option_rows:
            row_strike = float(row.get("strike") or 0.0)
            row_type = (row.get("option_type") or "").upper()
            if abs(row_strike - strike) < 0.01 and row_type == option_type:
                ltp = float(row.get("ltp") or row.get("premium") or 0.0)
                if ltp > 0:
                    current_premium = ltp
                    break

        if current_premium is None:
            try:
                with get_read_conn() as conn:
                    opt_row = conn.execute(
                        "SELECT ltp FROM option_chain_snapshots WHERE (symbol=? OR symbol=?) AND ABS(strike - ?) < 0.01 AND option_type=? AND expiry=? AND ltp IS NOT NULL AND ltp > 0 ORDER BY fetched_at DESC LIMIT 1",
                        (symbol, base_sym, strike, option_type, leg.get("expiry", ""))
                    ).fetchone()
                    if opt_row:
                        current_premium = float(opt_row["ltp"])
            except Exception:
                pass

        if current_premium is None:
            current_premium = entry_premium

        if side == "SELL":
            pnl = (entry_premium - current_premium) * lots * lot_size
        else:
            pnl = (current_premium - entry_premium) * lots * lot_size

        total_pnl += pnl

    return total_pnl


def _close_live_book(
    symbol: str,
    book_id: str,
    legs: list[dict],
    closed_at: str,
    status: str,
    reason: str,
    total_pnl: float,
) -> None:
    """Close a live book — squaring off all open legs via Kite orders.

    Attempts to place exit orders for each leg. If any exit fails,
    logs the failure but continues closing remaining legs.
    When live_broker_disabled is True, skips all broker interaction
    and closes the book in DB only.
    """
    from src.models.schema import close_book, close_leg

    # ── Check if broker is disabled ────────────────────────────────────
    from config.runtime_config import load_runtime_config
    broker_disabled = load_runtime_config().get("live_broker_disabled", False)

    # ── Attempt broker square-off for each leg ─────────────────────────
    kite = None
    if not broker_disabled:
        try:
            from src.engine.live_trading import get_kite_client
            kite = get_kite_client()
        except Exception as e:
            log.warning(
                "[multileg-live] %s: could not get Kite client for book close: %s",
                symbol,
                e,
            )
    else:
        log.info(
            "[multileg-live] %s: broker disabled — closing book %s in DB only (no order placement)",
            symbol,
            book_id,
        )

    from config.settings import LOT_SIZES
    from src.engine.symbol_resolver import resolve_instrument
    
    base_sym = symbol.upper().split()[0] if symbol else ""
    
    exit_results = []
    for leg in legs:
        leg_id = leg.get("id")
        broker_order_id = leg.get("broker_order_id")
        strike = float(leg.get("strike") or 0.0)
        option_type = (leg.get("option_type") or "").upper()
        lots = int(leg.get("lots") or 1)
        side = (leg.get("side") or "SELL").upper()
        leg_expiry = leg.get("expiry", "")

        if not leg_id:
            continue

        # Determine exit transaction type (opposite of entry)
        exit_transaction = "BUY" if side == "SELL" else "SELL"
        
        # Resolve instrument for this leg (needed for both broker orders and premium lookup)
        resolved = None
        if kite:
            try:
                resolved = resolve_instrument(
                    symbol=symbol, expiry=leg_expiry, strike=strike, option_type=option_type
                )
                if not resolved or not resolved.get("tradingsymbol"):
                    log.warning(
                        "[multileg-live] %s: could not resolve instrument for leg %d (strike=%s %s)",
                        symbol,
                        leg_id,
                        strike,
                        option_type,
                    )
                    exit_results.append({"leg_id": leg_id, "status": "UNRESOLVED"})
                    continue
            except Exception as resolve_err:
                log.warning(
                    "[multileg-live] %s: resolve_instrument failed for leg %d: %s",
                    symbol, leg_id, resolve_err
                )

        if kite and resolved:
            try:
                lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 1))
                quantity = lots * lot_size

                from src.engine.live_trading import place_kite_order

                exchange = resolved.get("exchange", "NFO")
                order_id = place_kite_order(
                    kite,
                    symbol,
                    exchange,
                    resolved["tradingsymbol"],
                    exit_transaction,
                    quantity,
                    shadow_mode=False,
                )
                log.info(
                    "[multileg-live] %s: square-off order placed for leg %d — %s %s %s Qty=%d, order_id=%s",
                    symbol,
                    leg_id,
                    exit_transaction,
                    resolved["tradingsymbol"],
                    option_type,
                    quantity,
                    order_id,
                )
                exit_results.append({
                    "leg_id": leg_id,
                    "status": "ORDER_PLACED",
                    "order_id": order_id,
                })
            except Exception as e:
                log.error(
                    "[multileg-live] %s: failed to square off leg %d: %s",
                    symbol,
                    leg_id,
                    e,
                )
                exit_results.append({"leg_id": leg_id, "status": "FAILED", "error": str(e)})
        else:
            log.debug(
                "[multileg-live] %s: no Kite client (or broker disabled) — leg %d not squared off via broker",
                symbol,
                leg_id,
            )
            exit_results.append({"leg_id": leg_id, "status": "NO_BROKER"})

        # Close leg in DB with actual exit premium from current market
        try:
            # Get current premium for accurate PnL tracking
            exit_premium = 0.0
            if kite and resolved:
                try:
                    # Attempt to fetch current LTP from broker quote
                    exchange = resolved.get("exchange", "NFO")
                    tradingsymbol = resolved["tradingsymbol"]
                    quote_key = f"{exchange}:{tradingsymbol}"
                    quote_data = kite.quote(quote_key)
                    if quote_data and isinstance(quote_data, dict):
                        instr_quote = quote_data.get(quote_key, {})
                        if isinstance(instr_quote, dict):
                            last_price = instr_quote.get("last_price", 0)
                            if last_price and float(last_price) > 0:
                                exit_premium = float(last_price)
                except Exception as ltp_err:
                    log.warning(
                        "[multileg-live] %s: could not fetch exit LTP for leg %d, using fallback: %s",
                        symbol, leg_id, ltp_err
                    )
            
            # Fallback: try DB snapshot with expiry filter
            if exit_premium <= 0:
                try:
                    from src.models.schema import get_read_conn
                    leg_expiry = leg.get("expiry", "")
                    with get_read_conn() as conn:
                        opt_row = conn.execute(
                            "SELECT ltp FROM option_chain_snapshots WHERE (symbol=? OR symbol=?) AND ABS(strike - ?) < 0.01 AND option_type=? AND expiry=? AND ltp IS NOT NULL AND ltp > 0 ORDER BY fetched_at DESC LIMIT 1",
                            (symbol, base_sym, strike, option_type, leg_expiry)
                        ).fetchone()
                        if opt_row:
                            exit_premium = float(opt_row["ltp"])
                except Exception:
                    pass
            
            close_leg(leg_id, closed_at, exit_premium, reason)
        except Exception as e:
            log.error(
                "[multileg-live] %s: failed to close leg %d in DB: %s",
                symbol,
                leg_id,
                e,
            )

    # ── Close the book record ──────────────────────────────────────────
    try:
        close_book(book_id, closed_at, status, reason, total_pnl)
        log.info(
            "[multileg-live] %s: book %s closed — %s, PnL ₹%.1f, %d legs exited via broker",
            symbol,
            book_id,
            reason,
            total_pnl,
            len([r for r in exit_results if r.get("status") == "ORDER_PLACED"]),
        )
    except Exception as e:
        log.error(
            "[multileg-live] %s: failed to close book %s in DB: %s",
            symbol,
            book_id,
            e,
        )


# ── New Book Entry (Live) ────────────────────────────────────────────────────


def _attempt_new_live_entry(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict,
    ai_mode: str,
    now_iso: str,
    open_books: list[dict],
) -> dict | None:
    """Attempt to enter a new multi-leg book via live Kite orders.

    Flow:
        1. Get LLM verdict (strategy type + legs)
        2. Validate legs against strategy constraints
        3. Compute Greeks, risk profile, margin
        4. Check book conflicts and entry quality
        5. Advisory mode: log only; full mode: place sequential Kite orders
    """
    from config.multileg_strategies import (
        MAX_BOOK_MARGIN,
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
        log.error("[multileg-live] %s: LLM verdict call failed: %s", symbol, e)
        return None

    if verdict is None:
        log.debug("[multileg-live] %s: LLM returned no verdict", symbol)
        return None

    st_upper = str(getattr(verdict, "strategy_type", "")).upper().strip()
    if st_upper in ("NONE", "NO_TRADE", "NO_SIGNAL", "SKIP", "N/A", "") or not getattr(verdict, "legs", None):
        log.info("[multileg-live] %s: LLM returned no-trade verdict (%s)", symbol, st_upper or "NO_LEGS")
        return {
            "action": "NO_TRADE",
            "strategy_type": getattr(verdict, "strategy_type", "NO_TRADE"),
            "reason": f"LLM multileg verdict: {getattr(verdict, 'strategy_type', 'NO_TRADE')}",
            "thesis": getattr(verdict, "thesis", ""),
            "confidence": getattr(verdict, "confidence", 0),
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
            "[multileg-live] %s: multileg_strategy module not available — "
            "inserting without engine validation",
            symbol,
        )
        validate_legs = None
        compute_book_greeks = None
        compute_book_risk_profile = None
        calculate_combined_margin = None
        score_entry_quality = None
        check_book_conflicts = None

    strategy_type = verdict.strategy_type
    # Convert Pydantic legs to mutable dicts and apply dashboard live lot sizing.
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
            is_paper=False,
            setup_type="MULTILEG",
        )
        for leg in legs:
            leg["lots"] = dashboard_lots
    except Exception as e:
        log.warning("[multileg-live] %s: lot sizing failed, defaulting to 1 lot: %s", symbol, e)
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
                "[multileg-live] %s: legs validation failed — %s",
                symbol,
                validation_msg,
            )
            return {
                "action": "REJECTED",
                "reason": f"Legs validation: {validation_msg}",
            }

    # ── Strict Binary Pre-Flight Validation for All Entry Legs (Safety Gate) ──
    # Mirrors the paper-trading safety check to ensure atomic leg data integrity
    # before risking real capital with the broker.
    from src.engine.data_validator import validate_trade_leg_data
    underlying_for_binary = float((scan_context or {}).get("underlying") or 0.0)
    oc_data_payload = {"strikes": list((scan_context or {}).get("option_rows") or [])}
    is_binary_valid, binary_issues = validate_trade_leg_data(legs, oc_data_payload, underlying_for_binary)
    if not is_binary_valid:
        log.warning(
            "[multileg-live] %s: atomic leg data validation failed — %s",
            symbol,
            binary_issues,
        )
        return {
            "action": "REJECTED",
            "reason": f"Leg data integrity rejected: {', '.join(binary_issues)}",
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
            log.warning("[multileg-live] %s: Greeks computation failed: %s", symbol, e)

    if compute_book_risk_profile is not None:
        try:
            risk_profile = compute_book_risk_profile(
                strategy_type, legs, net_premium, underlying
            ) or {}
        except Exception as e:
            log.warning(
                "[multileg-live] %s: Risk profile computation failed: %s",
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
                "[multileg-live] %s: Margin computation failed: %s", symbol, e
            )

    # ── 5d. Book conflict check ────────────────────────────────────────
    if check_book_conflicts is not None:
        try:
            has_conflict, conflict_msg = check_book_conflicts(
                symbol, strategy_type, open_books or get_open_books_for_symbol(symbol)
            )
            if has_conflict:
                log.info(
                    "[multileg-live] %s: book conflict — %s",
                    symbol,
                    conflict_msg,
                )
                return {
                    "action": "CONFLICT",
                    "reason": conflict_msg,
                }
        except Exception as e:
            log.warning(
                "[multileg-live] %s: Conflict check failed: %s", symbol, e
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
                "[multileg-live] %s: Entry quality scoring failed: %s",
                symbol,
                e,
            )

    # ── 5f. Margin cap check ──────────────────────────────────────────
    if combined_margin > MAX_BOOK_MARGIN:
        log.info(
            "[multileg-live] %s: combined margin ₹%.0f exceeds cap ₹%.0f",
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
            "[multileg-live] %s: net delta %.2f exceeds cap %.2f",
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
        "[multileg-live] %s: %s with %d legs, net premium ₹%.1f, "
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
            "[multileg-live]   leg %d: SELL %s %s @ ₹%.1f (delta=%.2f, lots=%s)",
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
            "[multileg-live] %s: advisory mode — logging verdict only, no live trade",
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
            "reason": "Advisory mode — no live trade placed",
        }

    # ── 5j. Full mode — place sequential Kite orders + insert trade ────
    book_id = f"ML-{symbol[:3]}-{uuid.uuid4().hex[:8]}"

    # ── Place Kite orders sequentially ──────────────────────────────────
    placed_legs: list[dict] = []
    failed = False

    kite = None
    try:
        from src.engine.live_trading import get_kite_client
        kite = get_kite_client()
    except Exception as e:
        log.warning("[multileg-live] %s: could not get Kite client: %s", symbol, e)

    if not kite:
        log.error(
            "[multileg-live] %s: no Kite client available — cannot place live orders",
            symbol,
        )
        return {
            "action": "BLOCKED",
            "reason": "No Kite client available",
        }

    from config.settings import LOT_SIZES
    from src.engine.live_trading import place_kite_order
    from src.engine.symbol_resolver import resolve_instrument

    base_sym = symbol.upper().split()[0] if symbol else ""
    lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 1))

    for i, leg in enumerate(legs):
        strike = float(leg.get("strike") or 0.0)
        option_type = leg.get("option_type", "")
        lots = int(leg.get("lots") or 1)
        premium = float(leg.get("premium") or 0.0)
        side = leg.get("side", "SELL")
        quantity = lots * lot_size

        try:
            resolved = resolve_instrument(
                symbol=symbol, expiry=expiry, strike=strike, option_type=option_type
            )
            if not resolved or not resolved.get("tradingsymbol"):
                log.error(
                    "[multileg-live] %s: could not resolve instrument for leg %d — %s %s",
                    symbol,
                    i + 1,
                    strike,
                    option_type,
                )
                failed = True
                break

            exchange = resolved.get("exchange", "NFO")
            order_id = place_kite_order(
                kite,
                symbol,
                exchange,
                resolved["tradingsymbol"],
                side,
                quantity,
                shadow_mode=False,
                expected_price=premium,
            )

            # Verify order status before proceeding to next leg (Risk 4 fix)
            from src.engine.live_trading import confirm_order_fill
            broker_status, broker_message = confirm_order_fill(kite, order_id, shadow_mode=False)

            if broker_status in ("REJECTED", "CANCELLED"):
                log.error(
                    "[multileg-live] %s: leg %d order %s by exchange — %s %s: %s",
                    symbol,
                    i + 1,
                    broker_status,
                    side,
                    option_type,
                    broker_message,
                )
                failed = True
                break

            if broker_status != "COMPLETE":
                log.error(
                    "[multileg-live] %s: leg %d order status is %s (not COMPLETE) — attempting cancel to prevent unhedged exposure",
                    symbol,
                    i + 1,
                    broker_status,
                )
                cancelled_ok = False
                try:
                    kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
                    c_status, _ = confirm_order_fill(kite, order_id, shadow_mode=False)
                    if c_status in ("CANCELLED", "REJECTED"):
                        cancelled_ok = True
                except Exception as cancel_err:
                    log.error(
                        "[multileg-live] %s: cancel order %s failed: %s",
                        symbol, order_id, cancel_err
                    )

                if not cancelled_ok:
                    # Cancel failed or order filled during cancel attempt; add to placed_legs so rollback squares it off
                    placed_legs.append({
                        "trade_id": 0,
                        "side": side,
                        "lots": lots,
                        "strike": strike,
                        "option_type": option_type,
                        "expiry": expiry,
                        "entry_premium": premium,
                        "exit_premium": 0.0,
                        "delta": float(leg.get("delta") or 0.0),
                        "theta": 0.0,
                        "vega": 0.0,
                        "iv": 0.0,
                        "rationale": leg.get("rationale", ""),
                        "status": "OPEN",
                        "closed_at": None,
                        "exit_reason": None,
                        "broker_order_id": order_id,
                    })
                failed = True
                break

            log.info(
                "[multileg-live] %s: leg %d order placed and verified — %s %s %s Qty=%d, order_id=%s, status=%s",
                symbol,
                i + 1,
                side,
                resolved["tradingsymbol"],
                option_type,
                quantity,
                order_id,
                broker_status,
            )

            placed_legs.append({
                "trade_id": 0,
                "side": side,
                "lots": lots,
                "strike": strike,
                "option_type": option_type,
                "expiry": expiry,
                "entry_premium": premium,
                "exit_premium": 0.0,
                "delta": float(leg.get("delta") or 0.0),
                "theta": 0.0,
                "vega": 0.0,
                "iv": 0.0,
                "rationale": leg.get("rationale", ""),
                "status": "OPEN",
                "closed_at": None,
                "exit_reason": None,
                "broker_order_id": order_id,
            })

        except Exception as e:
            log.error(
                "[multileg-live] %s: leg %d order failed — %s %s: %s",
                symbol,
                i + 1,
                side,
                option_type,
                e,
            )
            failed = True
            break

    # ── Rollback on partial fill ────────────────────────────────────────
    if failed and placed_legs:
        log.warning(
            "[multileg-live] %s: %d/%d legs placed before failure — rolling back",
            symbol,
            len(placed_legs),
            len(legs),
        )
        _rollback_placed_legs(symbol, kite, placed_legs)

        return {
            "action": "PARTIAL_FILL_ROLLBACK",
            "reason": f"Leg {len(placed_legs) + 1}/{len(legs)} failed — rolled back {len(placed_legs)} orders",
            "placed_count": len(placed_legs),
        }

    if failed and not placed_legs:
        log.error("[multileg-live] %s: no legs placed — entry aborted", symbol)
        return {
            "action": "ENTRY_FAILED",
            "reason": "First leg order failed — no positions opened",
        }

    # ── All legs placed — insert trade atomically ──────────────────────
    trade_dict = {
        "trade_ref": 0,
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

    try:
        inserted_id = insert_multileg_trade_atomically(trade_dict, placed_legs)
        if inserted_id:
            log.info(
                "[multileg-live] %s: LIVE book %s inserted (trade_id=%d) — %s, %d legs, net premium ₹%.1f",
                symbol,
                book_id,
                inserted_id,
                strategy_type,
                len(placed_legs),
                net_premium,
            )

            # Notify via Telegram
            try:
                from src.alerts.telegram_dispatcher import send_text

                legs_summary = ", ".join(
                    f"SELL {l.get('option_type','')} {l.get('strike','')} @ ₹{l.get('entry_premium',0):.1f}"
                    for l in placed_legs
                )
                send_text(
                    f"🤖 **[MULTILEG LIVE]** `{symbol}` {strategy_type}\n"
                    f"Legs: {legs_summary}\n"
                    f"Net Premium: ₹{net_premium:.1f} | Confidence: {verdict.confidence}%\n"
                    f"Book: `{book_id}`"
                )
            except Exception as e:
                log.warning(
                    "[multileg-live] %s: Telegram notification failed: %s",
                    symbol,
                    e,
                )

            return {
                "action": "ENTERED_LIVE",
                "trade_id": inserted_id,
                "book_id": book_id,
                "strategy_type": strategy_type,
                "legs_count": len(placed_legs),
                "net_premium": net_premium,
                "confidence": verdict.confidence,
                "entry_quality": entry_quality,
                "quality_reasons": quality_reasons,
                "thesis": verdict.thesis,
            }
        else:
            log.error(
                "[multileg-live] %s: insert_multileg_trade_atomically returned 0",
                symbol,
            )
            # Trade placed with broker but DB insert failed — attempt rollback
            log.warning(
                "[multileg-live] %s: DB insert failed after broker orders — rolling back",
                symbol,
            )
            _rollback_placed_legs(symbol, kite, placed_legs)
            return {
                "action": "DB_INSERT_FAILED_ROLLBACK",
                "reason": "DB insert failed after broker orders placed — rolled back",
            }
    except Exception as e:
        log.error(
            "[multileg-live] %s: Failed to insert trade: %s",
            symbol,
            e,
            exc_info=True,
        )
        _rollback_placed_legs(symbol, kite, placed_legs)
        return {
            "action": "DB_INSERT_FAILED_ROLLBACK",
            "reason": f"DB error: {e}",
        }


def _rollback_placed_legs(
    symbol: str, kite, placed_legs: list[dict]
) -> dict:
    """Square off all already-placed legs on a failed multi-leg entry.

    Places opposite-side orders to flatten any opened positions.
    Verifies rollback order fills and returns status summary.
    
    Returns:
        dict with rollback status: {"total": N, "filled": N, "failed": N, "pending": N}
    """
    if not placed_legs:
        return {"total": 0, "filled": 0, "failed": 0, "pending": 0}

    from config.settings import LOT_SIZES
    from src.engine.live_trading import place_kite_order
    from src.engine.symbol_resolver import resolve_instrument

    base_sym = symbol.upper().split()[0] if symbol else ""
    lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 1))
    
    rollback_results = []
    rollback_order_ids = []

    for leg in placed_legs:
        side = (leg.get("side") or "SELL").upper()
        exit_transaction = "BUY" if side == "SELL" else "SELL"
        strike = leg.get("strike", 0.0)
        option_type = leg.get("option_type", "")
        lots = leg.get("lots", 1)
        quantity = lots * lot_size

        if not kite:
            log.error(
                "[multileg-live] %s: CRITICAL — rollback skipped for %d legs, no Kite client!",
                symbol,
                len(placed_legs),
            )
            return {"total": len(placed_legs), "filled": 0, "failed": len(placed_legs), "pending": 0}

        try:
            leg_expiry = leg.get("expiry", "")
            resolved = resolve_instrument(
                symbol=symbol, expiry=leg_expiry, strike=strike, option_type=option_type
            )
            if not resolved or not resolved.get("tradingsymbol"):
                log.error(
                    "[multileg-live] %s: CRITICAL — rollback failed, could not resolve %s %s",
                    symbol,
                    strike,
                    option_type,
                )
                rollback_results.append({
                    "strike": strike, "option_type": option_type, "status": "RESOLVE_FAILED"
                })
                continue

            order_id = place_kite_order(
                kite,
                symbol,
                resolved.get("exchange", "NFO"),
                resolved["tradingsymbol"],
                exit_transaction,
                quantity,
                shadow_mode=False,
            )
            log.info(
                "[multileg-live] %s: rollback order placed — %s %s %s Qty=%d, order_id=%s",
                symbol,
                exit_transaction,
                resolved["tradingsymbol"],
                option_type,
                quantity,
                order_id,
            )
            rollback_order_ids.append({
                "order_id": order_id,
                "strike": strike,
                "option_type": option_type,
                "quantity": quantity,
            })
        except Exception as e:
            log.error(
                "[multileg-live] %s: CRITICAL — rollback order FAILED for %s %s — %s",
                symbol,
                strike,
                option_type,
                e,
            )
            rollback_results.append({
                "strike": strike, "option_type": option_type, "status": "ORDER_FAILED", "error": str(e)
            })

    # Verify rollback order fills (poll for up to 30 seconds)
    filled_count = 0
    failed_count = len([r for r in rollback_results if r["status"] in ("RESOLVE_FAILED", "ORDER_FAILED")])
    pending_count = 0
    
    if rollback_order_ids and kite:
        try:
            import time
            max_wait = 30  # seconds
            check_interval = 3  # seconds
            elapsed = 0
            
            while elapsed < max_wait:
                all_completed = True
                for rb_order in rollback_order_ids:
                    if "filled" in rb_order:
                        continue
                    
                    try:
                        order_info = kite.order_history(rb_order["order_id"])
                        if order_info:
                            # Check if order is COMPLETE (filled) or REJECTED/CANCELLED
                            latest_state = order_info[-1] if isinstance(order_info, list) else order_info
                            status = latest_state.get("status", "")
                            
                            if status == "COMPLETE":
                                rb_order["filled"] = True
                                filled_count += 1
                            elif status in ("REJECTED", "CANCELLED"):
                                rb_order["filled"] = False
                                failed_count += 1
                                log.error(
                                    "[multileg-live] %s: CRITICAL — rollback order %s was %s for %s %s!",
                                    symbol, rb_order["order_id"], status,
                                    rb_order["strike"], rb_order["option_type"]
                                )
                            else:
                                all_completed = False
                    except Exception as e:
                        log.warning(
                            "[multileg-live] %s: could not check rollback order %s status: %s",
                            symbol, rb_order["order_id"], e
                        )
                        all_completed = False
                
                if all_completed:
                    break
                
                time.sleep(check_interval)
                elapsed += check_interval
            
            # Count any still-pending orders
            pending_count = len([r for r in rollback_order_ids if "filled" not in r])
            if pending_count > 0:
                log.error(
                    "[multileg-live] %s: WARNING — %d rollback orders still pending after %ds!",
                    symbol, pending_count, max_wait
                )
        except Exception as e:
            log.error(
                "[multileg-live] %s: could not verify rollback order fills: %s",
                symbol, e
            )

    summary = {
        "total": len(placed_legs),
        "filled": filled_count,
        "failed": failed_count,
        "pending": pending_count,
    }
    
    if summary["failed"] > 0 or summary["pending"] > 0:
        log.error(
            "[multileg-live] %s: CRITICAL ROLLBACK SUMMARY — Total=%d, Filled=%d, Failed=%d, Pending=%d",
            symbol, summary["total"], summary["filled"], summary["failed"], summary["pending"]
        )
    else:
        log.info(
            "[multileg-live] %s: rollback complete — all %d legs squared off",
            symbol, summary["total"]
        )

    return summary


def _update_live_book_pnl(
    symbol: str, book: dict, legs: list[dict], scan_context: dict
) -> float:
    """Calculate and return updated total PnL for a live multi-leg book."""
    from config.settings import LOT_SIZES
    from src.models.schema import get_read_conn
    from src.engine.trade_plan import is_valid_option_premium

    base_sym = symbol.upper().split()[0] if symbol else ""
    lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 1))
    book_expiry = str(book.get("expiry") or "").strip()
    underlying = float(
        (scan_context or {}).get("underlying")
        or book.get("entry_underlying")
        or 0.0
    )
    option_rows = list((scan_context or {}).get("option_rows") or [])

    total_pnl = 0.0
    for leg in legs:
        strike = float(leg.get("strike") or 0.0)
        option_type = (leg.get("option_type") or "").upper()
        entry_premium = float(
            leg.get("entry_premium") or leg.get("premium") or 0.0
        )
        lots = int(leg.get("lots") or 1)
        side = (leg.get("side") or "SELL").upper()
        leg_expiry = str(leg.get("expiry") or book_expiry or "").strip()

        current_premium = None
        for row in option_rows:
            row_strike = float(row.get("strike") or 0.0)
            row_type = (row.get("option_type") or "").upper()
            if abs(row_strike - strike) < 0.01 and row_type == option_type:
                ltp = float(row.get("ltp") or row.get("premium") or 0.0)
                if ltp > 0:
                    if underlying > 0 and not is_valid_option_premium(
                        strike, option_type, ltp, underlying
                    ):
                        log.warning(
                            "[multileg-live] %s: rejected corrupted scan row LTP %.2f for %s %.0f",
                            symbol,
                            ltp,
                            option_type,
                            strike,
                        )
                    else:
                        current_premium = ltp
                        break

        if current_premium is None:
            try:
                with get_read_conn() as conn:
                    if leg_expiry:
                        opt_row = conn.execute(
                            "SELECT ltp FROM option_chain_snapshots WHERE (symbol=? OR symbol=?) AND expiry=? AND ABS(strike - ?) < 0.01 AND option_type=? AND ltp IS NOT NULL AND ltp > 0 ORDER BY fetched_at DESC LIMIT 1",
                            (symbol, base_sym, leg_expiry, strike, option_type),
                        ).fetchone()
                    else:
                        opt_row = conn.execute(
                            "SELECT ltp FROM option_chain_snapshots WHERE (symbol=? OR symbol=?) AND ABS(strike - ?) < 0.01 AND option_type=? AND ltp IS NOT NULL AND ltp > 0 ORDER BY fetched_at DESC LIMIT 1",
                            (symbol, base_sym, strike, option_type),
                        ).fetchone()
                    if opt_row:
                        snap_ltp = float(opt_row["ltp"])
                        if underlying > 0 and not is_valid_option_premium(
                            strike, option_type, snap_ltp, underlying
                        ):
                            log.warning(
                                "[multileg-live] %s: rejected corrupted snapshot LTP %.2f for %s %.0f",
                                symbol,
                                snap_ltp,
                                option_type,
                                strike,
                            )
                        else:
                            current_premium = snap_ltp
            except Exception:
                pass

        if current_premium is None:
            current_premium = entry_premium

        leg["current_premium"] = current_premium

        if side == "SELL":
            pnl = (entry_premium - current_premium) * lots * lot_size
        else:
            pnl = (current_premium - entry_premium) * lots * lot_size

        total_pnl += pnl

    return total_pnl
