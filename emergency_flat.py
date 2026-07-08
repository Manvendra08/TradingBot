"""
emergency_flat.py — OPS Agent Emergency Position Closer

Standalone script: cancels ALL open/pending orders, then closes all open live positions.
Restricted to cancel + close actions ONLY — no order-entry code path for new positions.

Usage:
    python emergency_flat.py              # Normal run
    python emergency_flat.py --dry-run    # Preview only, no actual orders
"""

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH

DRY_RUN = "--dry-run" in sys.argv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | emergency_flat | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("emergency_flat")


def _get_kite():
    """Get Kite client — standalone, no bot dependency."""
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        log.error("kiteconnect not installed")
        return None

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT api_key, access_token FROM broker_configs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row or not row["api_key"] or not row["access_token"]:
            log.error("No broker config found")
            return None
        kite = KiteConnect(api_key=row["api_key"])
        kite.set_access_token(row["access_token"])
        return kite
    except Exception as e:
        log.error("Failed to create Kite client: %s", e)
        return None


def _get_exchange(symbol: str) -> str:
    """Determine exchange for a symbol."""
    from config.symbol_classes import get_kite_exchange
    return get_kite_exchange(symbol)


def _resolve_tradingsymbol(kite, symbol: str, expiry: str, strike: float, option_type: str) -> str | None:
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
    cancelled = 0
    try:
        orders = kite.orders()
        open_orders = [o for o in orders if o["status"] in ("open", "trigger pending", "put order request pending")]
        log.info("Found %d open/pending orders to cancel", len(open_orders))
        for order in open_orders:
            try:
                if DRY_RUN:
                    log.info("[DRY-RUN] Would cancel order %s (%s)", order["order_id"], order.get("tradingsymbol"))
                else:
                    kite.cancel_order(order_id=order["order_id"])
                    log.info("Cancelled order %s (%s)", order["order_id"], order.get("tradingsymbol"))
                cancelled += 1
            except Exception as e:
                log.warning("Failed to cancel order %s: %s", order["order_id"], e)
    except Exception as e:
        log.error("Failed to fetch orders: %s", e)
    return cancelled


def _cancel_all_gtt(kite) -> int:
    """Cancel all GTT orders. Returns count cancelled."""
    cancelled = 0
    try:
        gtts = kite.gtt_orders()
        log.info("Found %d GTT orders to cancel", len(gtts))
        for gtt in gtts:
            try:
                if DRY_RUN:
                    log.info("[DRY-RUN] Would cancel GTT %s", gtt["id"])
                else:
                    kite.cancel_gtt(gtt["id"])
                    log.info("Cancelled GTT %s", gtt["id"])
                cancelled += 1
            except Exception as e:
                log.warning("Failed to cancel GTT %s: %s", gtt["id"], e)
    except Exception as e:
        log.error("Failed to fetch GTTs: %s", e)
    return cancelled


def _close_all_positions(kite) -> int:
    """Close all open live positions at market. Returns count closed."""
    closed = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        open_trades = conn.execute(
            "SELECT * FROM live_trades WHERE status='OPEN'"
        ).fetchall()
        conn.close()
    except Exception as e:
        log.error("Failed to read open positions: %s", e)
        return 0

    log.info("Found %d open live positions to close", len(open_trades))

    for trade_row in open_trades:
        trade = dict(trade_row)
        symbol = trade["symbol"]
        try:
            exchange = _get_exchange(symbol)
            tradingsymbol = _resolve_tradingsymbol(
                kite,
                symbol,
                trade.get("expiry") or "",
                trade.get("strike") or 0.0,
                trade.get("option_type") or "FUT",
            )
            if not tradingsymbol:
                log.warning("Could not resolve tradingsymbol for trade #%d %s — skipping", trade["id"], symbol)
                continue

            # Determine exit side
            exit_side = "SELL" if trade.get("side") == "BUY" else "BUY"

            # Get quantity
            from src.engine.symbol_resolver import resolve_instrument
            resolved = resolve_instrument(symbol, trade.get("expiry") or "", trade.get("strike") or 0.0, trade.get("option_type") or "FUT")
            lot_size = 1
            if resolved:
                lot_size = resolved.get("lot_size", 1)
            quantity = int(trade.get("lots") or 1) * lot_size

            # Get LTP for limit order
            full_symbol = f"{exchange}:{tradingsymbol}"
            ltp = 0.0
            try:
                quote = kite.ltp(full_symbol)
                ltp = quote.get(full_symbol, {}).get("last_price", 0.0)
            except Exception:
                pass

            if DRY_RUN:
                log.info(
                    "[DRY-RUN] Would close trade #%d: %s %s %s Qty=%d LTP=%.2f",
                    trade["id"], exit_side, exchange, tradingsymbol, quantity, ltp,
                )
            else:
                # Use limit order with slippage buffer (same logic as live_trading)
                is_future = "FUT" in tradingsymbol.upper()
                buffer_pct = 0.002 if is_future else 0.05
                if exit_side == "BUY":
                    limit_price = ltp * (1 + buffer_pct) if ltp > 0 else 0
                else:
                    limit_price = ltp * (1 - buffer_pct) if ltp > 0 else 0

                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=exit_side,
                    quantity=quantity,
                    order_type=kite.ORDER_TYPE_LIMIT if limit_price > 0 else kite.ORDER_TYPE_MARKET,
                    price=round(limit_price, 2) if limit_price > 0 else 0,
                    product=kite.PRODUCT_NRML,
                )
                log.info(
                    "Closed trade #%d: %s %s %s Qty=%d OrderID=%s",
                    trade["id"], exit_side, exchange, tradingsymbol, quantity, order_id,
                )

            # Update DB
            if not DRY_RUN:
                try:
                    from src.models.schema import get_conn
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE live_trades SET status='CLOSED_EMERGENCY', closed_at=?, reason='emergency_flat' WHERE id=?",
                            (now_iso, trade["id"]),
                        )
                except Exception as e:
                    log.warning("Failed to update trade #%d in DB: %s", trade["id"], e)

            closed += 1
        except Exception as e:
            log.error("Failed to close trade #%d %s: %s", trade["id"], symbol, e)

    return closed


def main():
    log.info("=== EMERGENCY FLAT STARTING (dry_run=%s) ===", DRY_RUN)

    kite = _get_kite()
    if not kite:
        log.error("Cannot proceed — Kite client unavailable")
        sys.exit(1)

    # Step 1: Cancel ALL open/pending orders
    log.info("--- Step 1: Cancelling all open/pending orders ---")
    orders_cancelled = _cancel_all_orders(kite)
    gtts_cancelled = _cancel_all_gtt(kite)
    log.info("Cancelled %d orders + %d GTTs", orders_cancelled, gtts_cancelled)

    # Step 2: Close all open live positions
    log.info("--- Step 2: Closing all open live positions ---")
    positions_closed = _close_all_positions(kite)
    log.info("Closed %d positions", positions_closed)

    # Summary
    log.info("=== EMERGENCY FLAT COMPLETE ===")
    log.info("Orders cancelled: %d", orders_cancelled)
    log.info("GTTs cancelled: %d", gtts_cancelled)
    log.info("Positions closed: %d", positions_closed)

    result = {
        "orders_cancelled": orders_cancelled,
        "gtts_cancelled": gtts_cancelled,
        "positions_closed": positions_closed,
        "dry_run": DRY_RUN,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
