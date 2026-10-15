"""
emergency_flat.py — OPS Agent Emergency Position Closer

Standalone script: cancels ALL open/pending orders and GTTs, then closes all open live positions
(both single-leg and multi-leg option structures).
Restricted to cancel + close actions ONLY — no order-entry code path for new positions.

Supports both Live Broker execution (via Kite) and Shadow/Simulation execution.

Usage:
    python emergency_flat.py              # Interactive confirmation prompt
    python emergency_flat.py --auto       # Automated run (used by ops_agent)
    python emergency_flat.py --dry-run    # Preview only, no actual orders placed
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH
from src.models.schema import close_book, close_live_trade

DRY_RUN = "--dry-run" in sys.argv
AUTO_MODE = "--auto" in sys.argv or "NSEBOT_AUTO" in os.environ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | emergency_flat | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("emergency_flat")


def _is_shadow_mode() -> bool:
    """Check if runtime_config is configured for shadow/simulation mode."""
    try:
        from config.runtime_config import load_runtime_config
        cfg = load_runtime_config()
        return bool(cfg.get("live_shadow_mode", True) or cfg.get("live_broker_disabled", True))
    except Exception:
        return True


def _get_kite():
    """Get Kite client if available and broker configs exist in DB."""
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        log.warning("kiteconnect package not installed in environment")
        return None

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT api_key, access_token FROM broker_configs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row or not row["api_key"] or not row["access_token"]:
            log.warning("No active broker config found in DB")
            return None
        kite = KiteConnect(api_key=row["api_key"])
        kite.set_access_token(row["access_token"])
        return kite
    except Exception as e:
        log.error("Failed to initialize Kite client: %s", e)
        return None


def _get_exchange(symbol: str) -> str:
    """Determine exchange for a symbol."""
    try:
        from config.symbol_classes import get_kite_exchange
        return get_kite_exchange(symbol)
    except Exception:
        sym = (symbol or "").upper()
        if any(c in sym for c in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER", "COPPER")):
            return "MCX"
        if "SENSEX" in sym:
            return "BFO"
        return "NFO"


def _resolve_tradingsymbol(symbol: str, expiry: str, strike: float, option_type: str) -> str | None:
    """Resolve the trading symbol via instrument cache."""
    try:
        from src.engine.symbol_resolver import resolve_instrument
        resolved = resolve_instrument(symbol, expiry, strike, option_type)
        if resolved and resolved.get("tradingsymbol"):
            return resolved["tradingsymbol"]
    except Exception:
        pass
    return None


def _cancel_all_orders(kite) -> int:
    """Cancel all open/pending orders. Returns count cancelled."""
    if not kite:
        return 0
    cancelled = 0
    try:
        orders = kite.orders()
        open_orders = [
            o for o in orders
            if o.get("status", "").lower() in ("open", "trigger pending", "put order request pending")
        ]
        log.info("Found %d open/pending orders to cancel", len(open_orders))
        for order in open_orders:
            try:
                order_id = order.get("order_id")
                tradingsymbol = order.get("tradingsymbol", "")
                if DRY_RUN:
                    log.info("[DRY-RUN] Would cancel order %s (%s)", order_id, tradingsymbol)
                else:
                    kite.cancel_order(variety=order.get("variety", kite.VARIETY_REGULAR), order_id=order_id)
                    log.info("Cancelled order %s (%s)", order_id, tradingsymbol)
                cancelled += 1
            except Exception as e:
                log.warning("Failed to cancel order %s: %s", order.get("order_id"), e)
    except Exception as e:
        log.error("Failed to fetch orders from broker: %s", e)
    return cancelled


def _cancel_all_gtt(kite) -> int:
    """Cancel all GTT trigger orders. Returns count cancelled."""
    if not kite:
        return 0
    cancelled = 0
    try:
        gtts = kite.gtt_orders()
        log.info("Found %d GTT orders to cancel", len(gtts))
        for gtt in gtts:
            try:
                gtt_id = gtt.get("id")
                if DRY_RUN:
                    log.info("[DRY-RUN] Would cancel GTT #%s", gtt_id)
                else:
                    kite.delete_gtt(gtt_id)
                    log.info("Cancelled GTT #%s", gtt_id)
                cancelled += 1
            except Exception as e:
                log.warning("Failed to cancel GTT #%s: %s", gtt.get("id"), e)
    except Exception as e:
        log.error("Failed to fetch GTT orders from broker: %s", e)
    return cancelled


def _close_single_leg_positions(kite, is_shadow: bool) -> int:
    """Close all open live single-leg positions. Returns count closed."""
    closed = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        open_trades = conn.execute("SELECT * FROM live_trades WHERE status='OPEN'").fetchall()
        conn.close()
    except Exception as e:
        log.error("Failed to read open single-leg live positions: %s", e)
        return 0

    if not open_trades:
        log.info("No open single-leg live positions found.")
        return 0

    log.info("Found %d open single-leg live positions to close", len(open_trades))

    for trade_row in open_trades:
        trade = dict(trade_row)
        symbol = trade.get("symbol", "")
        trade_id = trade["id"]
        trade_is_shadow = is_shadow or trade.get("broker_status") == "SHADOW" or trade.get("trade_status") == "SHADOW"
        try:
            exchange = _get_exchange(symbol)
            tradingsymbol = _resolve_tradingsymbol(
                symbol,
                trade.get("expiry") or "",
                float(trade.get("strike") or 0.0),
                trade.get("option_type") or "FUT",
            )

            # Determine exit side
            entry_side = (trade.get("side") or "BUY").upper()
            exit_side = "SELL" if entry_side == "BUY" else "BUY"

            lots = int(trade.get("lots") or 1)
            lot_size = int(trade.get("lot_size") or 1)
            quantity = lots * lot_size

            ltp = 0.0
            if kite and tradingsymbol:
                full_symbol = f"{exchange}:{tradingsymbol}"
                try:
                    quote = kite.ltp(full_symbol)
                    ltp = quote.get(full_symbol, {}).get("last_price", 0.0)
                except Exception:
                    pass

            # Only place broker orders if NOT in shadow mode
            if not trade_is_shadow and kite and tradingsymbol:
                if DRY_RUN:
                    log.info(
                        "[DRY-RUN] Would place LIVE exit order for single-leg #%d: %s %s %s Qty=%d LTP=%.2f",
                        trade_id, exit_side, exchange, tradingsymbol, quantity, ltp,
                    )
                else:
                    try:
                        order_id = kite.place_order(
                            variety=kite.VARIETY_REGULAR,
                            exchange=exchange,
                            tradingsymbol=tradingsymbol,
                            transaction_type=exit_side,
                            quantity=quantity,
                            order_type=kite.ORDER_TYPE_MARKET,
                            product=kite.PRODUCT_NRML,
                        )
                        log.info("Order placed for single-leg #%d: %s OrderID=%s", trade_id, tradingsymbol, order_id)
                    except Exception as ord_err:
                        log.error("Broker order failed for single-leg #%d: %s", trade_id, ord_err)
            else:
                log.info(
                    "Single-leg #%d (%s) closing in %s mode (DB flat).",
                    trade_id, symbol, "SHADOW" if trade_is_shadow else "NO-BROKER",
                )

            # Update DB
            if not DRY_RUN:
                try:
                    option_type = trade.get("option_type") or "FUT"
                    exit_px = ltp if ltp > 0 else float(trade.get("entry_premium") or trade.get("entry_underlying") or 0.0)
                    if option_type == "FUT":
                        close_live_trade(
                            trade_id, now_iso, exit_px,
                            None, "CLOSED_EMERGENCY", "emergency_flat",
                        )
                    else:
                        close_live_trade(
                            trade_id, now_iso, float(trade.get("entry_underlying") or 0.0),
                            exit_px, "CLOSED_EMERGENCY", "emergency_flat",
                        )
                    log.info("Successfully updated single-leg #%d as CLOSED_EMERGENCY", trade_id)
                except Exception as e:
                    log.warning("Failed to update single-leg trade #%d in DB: %s", trade_id, e)

            closed += 1
        except Exception as e:
            log.error("Failed to close single-leg trade #%d %s: %s", trade_id, symbol, e)

    return closed


def _close_multileg_positions(kite, is_shadow: bool) -> int:
    """Close all open live multi-leg books. Returns count closed."""
    closed = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        open_books = conn.execute(
            "SELECT * FROM multi_leg_trades WHERE status='OPEN' AND UPPER(trade_mode)='LIVE'"
        ).fetchall()
        conn.close()
    except Exception as e:
        log.error("Failed to read open live multi-leg books: %s", e)
        return 0

    if not open_books:
        log.info("No open live multi-leg books found.")
        return 0

    log.info("Found %d open live multi-leg books to close", len(open_books))

    for book_row in open_books:
        book = dict(book_row)
        trade_id = book["id"]
        book_id = book.get("book_id") or str(trade_id)
        symbol = book.get("symbol", "")
        expiry = book.get("expiry", "")

        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
            conn.row_factory = sqlite3.Row
            legs = conn.execute(
                "SELECT * FROM multi_leg_legs WHERE trade_id=? AND status='OPEN'",
                (trade_id,),
            ).fetchall()
            conn.close()

            exchange = _get_exchange(symbol)
            leg_exits = []

            for leg_row in legs:
                leg = dict(leg_row)
                leg_id = leg["id"]
                strike = float(leg.get("strike") or 0.0)
                opt_type = leg.get("option_type") or "CE"
                leg_side = (leg.get("side") or "SELL").upper()
                exit_side = "BUY" if leg_side == "SELL" else "SELL"
                lots = int(leg.get("lots") or 1)

                from config.settings import LOT_SIZES
                base_sym = symbol.upper().split()[0]
                lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 1))
                quantity = lots * lot_size

                tradingsymbol = _resolve_tradingsymbol(symbol, expiry, strike, opt_type)
                ltp = 0.0

                if kite and tradingsymbol:
                    full_symbol = f"{exchange}:{tradingsymbol}"
                    try:
                        quote = kite.ltp(full_symbol)
                        ltp = quote.get(full_symbol, {}).get("last_price", 0.0)
                    except Exception:
                        pass

                if not is_shadow and kite and tradingsymbol:
                    if DRY_RUN:
                        log.info(
                            "[DRY-RUN] Would place LIVE order for ML leg #%d (%s): %s %s %s Qty=%d LTP=%.2f",
                            leg_id, book_id, exit_side, exchange, tradingsymbol, quantity, ltp,
                        )
                    else:
                        try:
                            order_id = kite.place_order(
                                variety=kite.VARIETY_REGULAR,
                                exchange=exchange,
                                tradingsymbol=tradingsymbol,
                                transaction_type=exit_side,
                                quantity=quantity,
                                order_type=kite.ORDER_TYPE_MARKET,
                                product=kite.PRODUCT_NRML,
                            )
                            log.info("Order placed for ML leg #%d: %s OrderID=%s", leg_id, tradingsymbol, order_id)
                        except Exception as ord_err:
                            log.error("Broker order failed for ML leg #%d: %s", leg_id, ord_err)
                else:
                    log.info("ML leg #%d (%s) closing in %s mode.", leg_id, book_id, "SHADOW" if is_shadow else "NO-BROKER")

                leg_exits.append({
                    "id": leg_id,
                    "exit_premium": ltp if ltp > 0 else float(leg.get("entry_premium") or 0.0),
                })

            if not DRY_RUN:
                try:
                    close_book(
                        book_id=book_id,
                        closed_at=now_iso,
                        status="CLOSED_EMERGENCY",
                        reason="emergency_flat",
                        total_pnl=None,
                        exit_underlying=float(book.get("entry_underlying") or 0.0),
                        leg_exits=leg_exits,
                    )
                    log.info("Closed multi-leg book %s in database as CLOSED_EMERGENCY.", book_id)
                except Exception as e:
                    log.warning("Failed to close multi-leg book %s in DB: %s", book_id, e)

            closed += 1
        except Exception as e:
            log.error("Failed to close multi-leg book %s: %s", book_id, e)

    return closed


def main():
    log.info("=== EMERGENCY FLAT INITIATED (dry_run=%s, auto=%s) ===", DRY_RUN, AUTO_MODE)

    if not DRY_RUN and not AUTO_MODE:
        confirmation = input("Type CONFIRM to proceed with emergency liquidation of all live positions: ").strip()
        if confirmation != "CONFIRM":
            log.info("Emergency flat cancelled — user did not confirm.")
            print("Cancelled. Type exactly 'CONFIRM' to execute.")
            sys.exit(0)
    elif AUTO_MODE:
        log.info("Running in AUTO_MODE — skipping interactive prompt.")

    # Check open live counts first
    try:
        from src.models.schema import get_open_live_positions_count_ro
        live_open_count = get_open_live_positions_count_ro()
    except Exception:
        live_open_count = 0

    log.info("Current open live positions in DB: %d", live_open_count)

    is_shadow = _is_shadow_mode()
    kite = _get_kite()

    if not is_shadow and not kite and live_open_count > 0:
        log.error("CRITICAL: Cannot proceed with LIVE broker liquidation — Kite unavailable and %d live positions open!", live_open_count)
        sys.exit(1)
    elif is_shadow:
        log.info("Running under SHADOW/SIMULATION mode (no live broker execution required).")

    # Step 1: Cancel ALL open/pending orders and GTT triggers
    log.info("--- Step 1: Cancelling open/pending orders & GTTs ---")
    orders_cancelled = _cancel_all_orders(kite)
    gtts_cancelled = _cancel_all_gtt(kite)
    log.info("Cancelled %d orders, %d GTTs", orders_cancelled, gtts_cancelled)

    # Step 2: Close live single-leg positions
    log.info("--- Step 2: Closing live single-leg positions ---")
    single_closed = _close_single_leg_positions(kite, is_shadow)

    # Step 3: Close live multi-leg books
    log.info("--- Step 3: Closing live multi-leg books ---")
    multileg_closed = _close_multileg_positions(kite, is_shadow)

    total_closed = single_closed + multileg_closed
    log.info("=== EMERGENCY FLAT COMPLETED: %d positions/books closed ===", total_closed)

    result = {
        "status": "success",
        "orders_cancelled": orders_cancelled,
        "gtts_cancelled": gtts_cancelled,
        "single_leg_closed": single_closed,
        "multi_leg_closed": multileg_closed,
        "positions_closed": total_closed,
        "dry_run": DRY_RUN,
        "shadow_mode": is_shadow,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
