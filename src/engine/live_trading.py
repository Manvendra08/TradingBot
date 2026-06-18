"""Live order execution engine — Zerodha Kite.

FIX #14: threading.Lock added around module-level Kite client cache to prevent
         race condition when scheduler and reversal-close handler call
         get_kite_client() concurrently during a token refresh cycle.

FIX #5 (partial): confirm_order_filled() polls kite.order_history() to verify
         COMPLETE status before inserting an OPEN trade record.  Full phantom-
         position prevention requires a DB migration to add order_id column;
         this fix at minimum logs and raises on unconfirmed orders.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

from config.settings import (
    ZERODHA_API_KEY,
    ZERODHA_API_SECRET,
    ZERODHA_ACCESS_TOKEN,
    LOT_SIZES,
)
from config.runtime_config import load_runtime_config
from src.models.schema import (
    get_conn,
    get_open_live_trade,
    insert_live_trade,
    close_live_trade,
)
from src.engine.capital_allocator import calculate_trade_lots
from src.engine.risk_engine import check_live_risk_limits

# ---------------------------------------------------------------------------
# FIX #14: Thread-safe Kite client cache.
# _kite_client_lock must be held for any read or write of _cached_kite_client
# and _cached_access_token.  Background instrument-cache thread acquires it
# only during client creation, not during the long instrument download.
# ---------------------------------------------------------------------------
_kite_client_lock: threading.Lock = threading.Lock()
_cached_kite_client = None
_cached_access_token: Optional[str] = None


def _build_kite_client(access_token: str):
    """Create and return a configured KiteConnect instance (no lock held)."""
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        log.error("kiteconnect package not installed. Run: pip install kiteconnect")
        return None
    kite = KiteConnect(api_key=ZERODHA_API_KEY)
    kite.set_access_token(access_token)
    return kite


def get_kite_client():
    """
    Return the cached Kite client, creating it if the access token has changed.
    FIX #14: All cache reads and writes are serialised via _kite_client_lock.
    """
    global _cached_kite_client, _cached_access_token

    rconf = load_runtime_config()
    access_token = (
        rconf.get("zerodha_access_token")
        or ZERODHA_ACCESS_TOKEN
        or ""
    )

    with _kite_client_lock:
        if _cached_kite_client is not None and access_token == _cached_access_token:
            return _cached_kite_client

        if not ZERODHA_API_KEY or not access_token:
            log.warning("Zerodha API key or access token not configured.")
            return None

        log.info("Creating new Kite client (token changed or first call).")
        client = _build_kite_client(access_token)
        _cached_kite_client = client
        _cached_access_token = access_token
        return _cached_kite_client


def confirm_order_filled(
    kite,
    order_id: str,
    retries: int = 3,
    backoff_secs: float = 1.0,
) -> bool:
    """
    FIX #5 (partial): Poll kite.order_history() to confirm COMPLETE status
    before recording a trade as OPEN in the DB.

    Returns True only when the terminal status is 'COMPLETE'.
    Returns False on REJECTED, CANCELLED, or after exhausting retries.
    Raises on unexpected exceptions so callers can handle them explicitly.

    Note: Full phantom-position elimination also requires storing order_id
    in the live_trades schema so orphaned orders can be reconciled at startup.
    """
    terminal_ok   = {"COMPLETE"}
    terminal_fail = {"REJECTED", "CANCELLED", "TRIGGER PENDING"}

    for attempt in range(1, retries + 1):
        try:
            history = kite.order_history(order_id=order_id)
            if not history:
                log.warning("confirm_order_filled: empty history for order_id=%s (attempt %d/%d)",
                            order_id, attempt, retries)
            else:
                latest_status = str(history[-1].get("status", "")).upper()
                log.debug("order_id=%s status=%s (attempt %d/%d)",
                          order_id, latest_status, attempt, retries)
                if latest_status in terminal_ok:
                    return True
                if latest_status in terminal_fail:
                    log.error("Order %s reached terminal failure status: %s", order_id, latest_status)
                    return False
        except Exception as exc:
            log.warning("confirm_order_filled: exception on attempt %d/%d for order_id=%s: %s",
                        attempt, retries, order_id, exc)
            if attempt == retries:
                raise

        if attempt < retries:
            time.sleep(backoff_secs)

    log.error("confirm_order_filled: order_id=%s not COMPLETE after %d attempts — treating as unconfirmed.",
              order_id, retries)
    return False


def _compute_option_sl_target(
    entry_premium: float,
    side: str,
    atr: float = 0.0,
) -> tuple[float, float]:
    """
    ATR-based SL/Target for live option trades.
    BUY:  SL = entry - max(1.5*ATR, 20% premium); Target = entry + max(2*ATR, 40% premium)
    SELL: SL = entry + max(1.5*ATR, 20% premium); Target = entry - max(1.5*ATR, 30% premium)
    """
    if entry_premium <= 0:
        return 0.0, 0.0
    if side.upper() == "SELL":
        sl_dist  = max(entry_premium * 0.20, 1.5 * atr)
        tgt_dist = max(entry_premium * 0.30, 1.5 * atr)
        return round(max(0.5, entry_premium + sl_dist), 2), round(max(0.5, entry_premium - tgt_dist), 2)
    else:
        sl_dist  = max(entry_premium * 0.20, 1.5 * atr)
        tgt_dist = max(entry_premium * 0.40, 2.0 * atr)
        return round(max(0.5, entry_premium - sl_dist), 2), round(max(0.5, entry_premium + tgt_dist), 2)


def place_live_order(
    symbol: str,
    scan_context: dict,
    verdict: str,
    confidence: int,
    option_type: str,
    strike: float,
    side: str,
    expiry: str,
    digest_id: str,
    entry_premium: float,
) -> dict:
    """
    Place a live order via Kite and record it in live_trades.

    FIX #5 (partial): After placing the order, poll confirm_order_filled()
    before inserting the OPEN record.  A REJECTED order is logged and the
    function returns an error dict instead of creating a phantom position.
    """
    kite = get_kite_client()
    if not kite:
        return {"action": "ERROR", "reason": "Kite client unavailable"}

    risk_ok, risk_reason = check_live_risk_limits(symbol)
    if not risk_ok:
        log.info("%s: live order blocked by risk engine — %s", symbol, risk_reason)
        return {"action": "BLOCKED_RISK", "reason": risk_reason}

    lot_size = LOT_SIZES.get(symbol.upper(), 1)
    lots = calculate_trade_lots(symbol, entry_premium, side)
    quantity = lots * lot_size

    atr = float((scan_context or {}).get("atr") or 0.0)
    sl_premium, target_premium = _compute_option_sl_target(entry_premium, side, atr)

    underlying = float((scan_context or {}).get("underlying") or 0.0)
    now_iso = datetime.now(timezone.utc).isoformat()

    rconf = load_runtime_config()
    exchange = rconf.get("live_exchange", "NFO")
    product  = rconf.get("live_product", "MIS")
    order_type = "MARKET"

    trading_symbol = f"{symbol}{expiry}{int(strike)}{option_type}"

    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=trading_symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL,
            quantity=quantity,
            product=kite.PRODUCT_MIS if product == "MIS" else kite.PRODUCT_NRML,
            order_type=kite.ORDER_TYPE_MARKET,
        )
    except Exception as exc:
        log.error("%s: Kite place_order failed — %s", symbol, exc)
        return {"action": "ERROR", "reason": str(exc)}

    log.info("%s: order placed — order_id=%s qty=%d side=%s premium=%.2f",
             symbol, order_id, quantity, side, entry_premium)

    # FIX #5 (partial): Confirm fill before recording OPEN trade.
    try:
        filled = confirm_order_filled(kite, str(order_id))
    except Exception as exc:
        log.error("%s: confirm_order_filled raised for order_id=%s — %s", symbol, order_id, exc)
        filled = False

    if not filled:
        log.error(
            "%s: order_id=%s not confirmed COMPLETE — skipping live_trades insert to prevent phantom position.",
            symbol, order_id,
        )
        return {
            "action": "ORDER_UNCONFIRMED",
            "reason": f"Order {order_id} did not reach COMPLETE status within retry window.",
            "order_id": str(order_id),
        }

    trade_data = {
        "opened_at":         now_iso,
        "symbol":            symbol,
        "expiry":            expiry,
        "verdict_label":     verdict,
        "side":              side,
        "option_type":       option_type,
        "strike":            strike,
        "entry_underlying":  underlying,
        "entry_premium":     entry_premium,
        "sl_premium":        sl_premium,
        "target_premium":    target_premium,
        "lots":              lots,
        "quantity":          quantity,
        "status":            "OPEN",
        "order_id":          str(order_id),
        "digest_id":         digest_id,
        "confidence_score":  confidence,
    }

    inserted_id = insert_live_trade(trade_data)
    if not inserted_id:
        log.warning("%s: live trade INSERT failed for order_id=%s", symbol, order_id)
        return {"action": "ERROR", "reason": "DB insert failed"}

    return {
        "action":   "EXECUTED",
        "trade":    trade_data,
        "order_id": str(order_id),
        "lots":     lots,
    }


def monitor_live_trades(symbol: str, scan_context: dict) -> list[dict]:
    """
    Check all open live trades for SL/target hit and close via Kite if triggered.
    Returns list of close-action dicts.
    """
    kite = get_kite_client()
    if not kite:
        log.warning("%s: monitor_live_trades — Kite client unavailable", symbol)
        return []

    underlying = float((scan_context or {}).get("underlying") or 0.0)
    expiry     = (scan_context or {}).get("expiry", "")
    now_iso    = datetime.now(timezone.utc).isoformat()
    results    = []

    with get_conn() as conn:
        open_trades = conn.execute(
            "SELECT * FROM live_trades WHERE symbol=? AND status='OPEN'",
            (symbol,),
        ).fetchall()

    for row in open_trades:
        trade = dict(row)
        trade_id    = trade["id"]
        opt_type    = trade.get("option_type", "")
        strike      = float(trade.get("strike") or 0.0)
        side        = trade.get("side") or "BUY"
        sl_prem     = trade.get("sl_premium")
        tgt_prem    = trade.get("target_premium")
        entry_prem  = float(trade.get("entry_premium") or 0.0)

        # Fetch live premium from option chain context.
        exit_premium = None
        option_rows  = (scan_context or {}).get("option_rows") or []
        for orow in option_rows:
            try:
                if (
                    abs(float(orow.get("strike") or 0) - strike) < 0.01
                    and str(orow.get("option_type") or "").upper() == opt_type
                ):
                    exit_premium = float(orow.get("ltp") or 0.0)
                    break
            except Exception:
                continue

        if exit_premium is None:
            log.debug("%s: live trade id=%d — exit premium unavailable, skipping", symbol, trade_id)
            continue

        close_reason = None
        close_status = None

        if side == "SELL":
            if sl_prem and exit_premium >= float(sl_prem):
                close_reason = f"SL hit: premium {exit_premium:.2f} >= SL {sl_prem}"
                close_status = "SL_HIT"
            elif tgt_prem and exit_premium <= float(tgt_prem):
                close_reason = f"Target hit: premium {exit_premium:.2f} <= Target {tgt_prem}"
                close_status = "TARGET_HIT"
        else:  # BUY
            if sl_prem and exit_premium <= float(sl_prem):
                close_reason = f"SL hit: premium {exit_premium:.2f} <= SL {sl_prem}"
                close_status = "SL_HIT"
            elif tgt_prem and exit_premium >= float(tgt_prem):
                close_reason = f"Target hit: premium {exit_premium:.2f} >= Target {tgt_prem}"
                close_status = "TARGET_HIT"

        if close_reason:
            trading_symbol = f"{symbol}{expiry}{int(strike)}{opt_type}"
            rconf = load_runtime_config()
            exchange = rconf.get("live_exchange", "NFO")
            product  = rconf.get("live_product", "MIS")
            exit_side = kite.TRANSACTION_TYPE_SELL if side == "BUY" else kite.TRANSACTION_TYPE_BUY
            qty = int(trade.get("quantity") or (int(trade.get("lots") or 1) * LOT_SIZES.get(symbol.upper(), 1)))
            try:
                exit_order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=trading_symbol,
                    transaction_type=exit_side,
                    quantity=qty,
                    product=kite.PRODUCT_MIS if product == "MIS" else kite.PRODUCT_NRML,
                    order_type=kite.ORDER_TYPE_MARKET,
                )
                log.info("%s: exit order placed — order_id=%s reason=%s", symbol, exit_order_id, close_reason)
            except Exception as exc:
                log.error("%s: exit order failed for trade id=%d — %s", symbol, trade_id, exc)
                continue

            pnl = 0.0
            lot_size = LOT_SIZES.get(symbol.upper(), 1)
            if side == "BUY":
                pnl = (exit_premium - entry_prem) * qty
            else:
                pnl = (entry_prem - exit_premium) * qty

            close_live_trade(trade_id, now_iso, underlying, exit_premium, close_status, close_reason, pnl)
            results.append({
                "action":        "CLOSED",
                "trade_id":      trade_id,
                "close_reason":  close_reason,
                "exit_premium":  exit_premium,
                "pnl":           pnl,
                "exit_order_id": str(exit_order_id),
            })

    return results
