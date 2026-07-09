from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytz

if TYPE_CHECKING:
    from kiteconnect import KiteConnect

from config.runtime_config import load_runtime_config
from config.settings import LOT_SIZES, MIN_ENTRY_QUALITY_CORE, REVERSAL_MIN_CONFIDENCE
from config.symbol_classes import get_kite_exchange, get_symbol_class, market_window
from src.engine.capital_allocator import calculate_trade_lots
from src.engine.entry_quality import calculate_entry_quality
from src.engine.paper_plan import (
    build_paper_trade_plan,
    is_bearish_verdict,
    is_bullish_verdict,
    mcx_option_liquidity_ok,
)

# Phase 0: ML feature snapshot builder (shared with paper_trading)
from src.engine.paper_trading import _build_ml_feature_snapshot
from src.engine.risk_engine import check_live_risk_limits
from src.engine.symbol_resolver import get_expiry_for_tradingsymbol, resolve_instrument
from src.engine.trade_decision import make_trade_decision
from src.engine.trend_analysis import get_trend_alignment_score
from src.engine.verdict_sets import is_bearish, is_bullish
from src.models.schema import (
    close_live_trade,
    get_broker_config,
    get_latest_snapshots_for_symbol,
    get_open_live_timeframe_trades,
    get_open_live_trade,
    insert_live_trade,
    update_live_trade_entry,
)

log = logging.getLogger("nsebot.live_trading")
IST = pytz.timezone("Asia/Kolkata")


def _is_market_open(symbol: str) -> bool:
    now = datetime.now(IST)
    open_t, close_t, days = market_window(symbol)
    if now.weekday() not in days:
        return False
    from config.holidays import is_market_holiday

    if is_market_holiday(symbol, now):
        return False
    t = now.strftime("%H:%M")
    return open_t <= t <= close_t


_get_exchange = get_kite_exchange

import threading

_cached_kite_client = None
_cached_access_token = None
_cached_user_name = None
_profile_failure_ts = 0.0
_PROFILE_FAILURE_COOLDOWN_SECONDS = 30.0
_kite_client_lock = threading.RLock()


def clear_kite_client_cache() -> None:
    global \
        _cached_kite_client, \
        _cached_access_token, \
        _cached_user_name, \
        _profile_failure_ts
    with _kite_client_lock:
        _cached_kite_client = None
        _cached_access_token = None
        _cached_user_name = None
        _profile_failure_ts = 0.0


def get_cached_user_name() -> str | None:
    global _cached_user_name, _cached_kite_client, _profile_failure_ts
    if _cached_user_name:
        return _cached_user_name
    now = datetime.now(timezone.utc).timestamp()
    if (
        _profile_failure_ts
        and (now - _profile_failure_ts) < _PROFILE_FAILURE_COOLDOWN_SECONDS
    ):
        return None
    client = _cached_kite_client or get_kite_client()
    if client:
        try:
            profile = client.profile()
            _cached_user_name = profile.get("user_name")
            _profile_failure_ts = 0.0
            return _cached_user_name
        except Exception as e:
            try:
                from src.utils.tls_adapter import is_retryable_transport_error

                if is_retryable_transport_error(e):
                    clear_kite_client_cache()
            except Exception:
                pass
            _profile_failure_ts = now
            log.warning(
                "Failed to fetch Zerodha profile for user name; retry paused for %.0fs: %s",
                _PROFILE_FAILURE_COOLDOWN_SECONDS,
                e,
            )
    return None


def _get_public_ip() -> str:
    import urllib.request

    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as response:
            return response.read().decode("utf-8").strip()
    except Exception:
        try:
            with urllib.request.urlopen(
                "https://ifconfig.me/ip", timeout=3
            ) as response:
                return response.read().decode("utf-8").strip()
        except Exception:
            return "unknown"


def _handle_kite_ip_error(e: Exception) -> None:
    msg = str(e)
    if any(
        keyword in msg
        for keyword in [
            "No IPs configured",
            "Add allowed IPs",
            "static-ip",
            "IP whitelist",
            "unauthorized IP",
        ]
    ):
        try:
            public_ip = _get_public_ip()
            log.error(
                "\n============================================================\n"
                "🚨 ZERODHA KITE IP ERROR DETECTED 🚨\n"
                "Your public IP: %s\n"
                "Zerodha requires you to whitelist this IP on the Kite developer console.\n"
                "To resolve this for FREE ($0 cost):\n"
                "1. Log in to your Zerodha Developer Console (https://developers.kite.trade)\n"
                "2. Go to Profile -> IP Whitelist (top right menu)\n"
                "3. Add your current public IP: %s\n"
                "4. Click 'Update' (usually allows 1 change per week)\n"
                "============================================================\n",
                public_ip,
                public_ip,
            )
        except Exception as err:
            log.warning(
                "Failed to auto-resolve public IP for Kite error helper: %s", err
            )


def _kite_host_reachable() -> bool:
    """Quick DNS check: can we resolve api.kite.trade?"""
    import socket

    try:
        socket.getaddrinfo("api.kite.trade", 443, socket.AF_INET, socket.SOCK_STREAM)
        return True
    except socket.gaierror:
        log.debug(
            "get_kite_client: api.kite.trade DNS resolution failed — network unreachable"
        )
        return False


def get_kite_client() -> KiteConnect | None:
    global _cached_kite_client, _cached_access_token
    with _kite_client_lock:
        if not _kite_host_reachable():
            _cached_kite_client = None
            _cached_access_token = None
            return None

        config = get_broker_config()
        if not config or not config.get("api_key") or not config.get("access_token"):
            _cached_kite_client = None
            _cached_access_token = None
            return None

        from src.services.zerodha_auth import is_token_valid

        if not is_token_valid():
            _cached_kite_client = None
            _cached_access_token = None
            return None

        # Reuse cached client if access token matches
        if _cached_kite_client and _cached_access_token == config["access_token"]:
            return _cached_kite_client

        try:
            from kiteconnect import KiteConnect

            kite = KiteConnect(api_key=config["api_key"])
            kite.set_access_token(config["access_token"])

            # Mount resilient TLS adapter with pool-eviction retry logic
            try:
                from src.utils.tls_adapter import mount_resilient_tls

                mount_resilient_tls(kite.reqsession)
            except Exception as e:
                log.warning("Failed to configure TLS adapter: %s", e)

            _cached_kite_client = kite
            _cached_access_token = config["access_token"]

            # Asynchronously populate instrument cache during Kite client init if not ready
            try:
                import threading

                from src.engine.symbol_resolver import (
                    _instrument_cache_is_ready,
                    fetch_and_cache_instruments,
                )

                if not _instrument_cache_is_ready():
                    log.info(
                        "Instrument cache not ready. Spawning background thread to fetch instruments..."
                    )
                    threading.Thread(
                        target=fetch_and_cache_instruments, args=(kite,), daemon=True
                    ).start()
            except Exception as e:
                log.warning(
                    "Failed to spawn background thread for instrument cache: %s", e
                )

            return kite
        except Exception as e:
            _handle_kite_ip_error(e)
            log.exception("Failed to initialize Kite client")
            _cached_kite_client = None
            _cached_access_token = None
            return None


# Unified helpers imported from src.engine.trade_plan
from src.engine.trade_plan import (
    calculate_buy_sl_target,
    calculate_sell_sl_target,
    convert_underlying_sl_to_premium,
)
from src.engine.trade_plan import (
    get_option_premium as _get_option_premium,
)
from src.engine.trade_plan import (
    parse_verdict_and_confidence as _parse_verdict_and_confidence,
)


def _is_reversal_against_open_trade(
    open_trade: dict,
    verdict: str,
    confidence: int,
    symbol: str = "",
    option_type: str = "",
    strike: float = 0.0,
    ctx: dict | None = None,
) -> bool:
    """
    Return True only when a genuinely strong reversal signal contradicts the
    open trade direction.

    Aligned with paper_trading._is_reversal_against_open_trade (Fix C1):
      1. confidence >= REVERSAL_MIN_CONFIDENCE (default 75)
      2. entry_quality >= MIN_ENTRY_QUALITY_CORE (default 60)
      3. trend_alignment score <= 40 (trend no longer supports open direction)
    """
    # Guard 1: confidence threshold
    if confidence < REVERSAL_MIN_CONFIDENCE:
        log.debug(
            "%s: live reversal guard — confidence %d < REVERSAL_MIN_CONFIDENCE %d, ignoring.",
            symbol,
            confidence,
            REVERSAL_MIN_CONFIDENCE,
        )
        return False

    # Guard 2: entry quality (requires genuine setup, not noise)
    if ctx and option_type and strike:
        entry_quality, entry_reasons = calculate_entry_quality(
            symbol, option_type, strike, ctx
        )
        if entry_quality < MIN_ENTRY_QUALITY_CORE:
            log.debug(
                "%s: live reversal guard — entry_quality %d < MIN_ENTRY_QUALITY_CORE %d (%s), ignoring.",
                symbol,
                entry_quality,
                MIN_ENTRY_QUALITY_CORE,
                entry_reasons,
            )
            return False

    # Guard 3: trend alignment (ensure trend has actually shifted)
    trend_alignment = get_trend_alignment_score(symbol, verdict)
    if trend_alignment > 40:
        log.debug(
            "%s: live reversal guard — trend_alignment %d > 40, trend still supports open direction.",
            symbol,
            trend_alignment,
        )
        return False

    # Directional check: reversal must be against open trade
    ot = str(open_trade.get("option_type") or "").upper()
    side = str(open_trade.get("side") or "BUY").upper()

    is_open_bullish = (side == "BUY" and ot == "CE") or (side == "SELL" and ot == "PE")
    is_open_bearish = (side == "BUY" and ot == "PE") or (side == "SELL" and ot == "CE")

    new_is_bullish = is_bullish(verdict)
    new_is_bearish = is_bearish(verdict)

    if is_open_bullish and new_is_bearish:
        log.info(
            "%s: valid live reversal — closing bullish trade on bearish signal (conf=%d).",
            symbol,
            confidence,
        )
        return True
    if is_open_bearish and new_is_bullish:
        log.info(
            "%s: valid live reversal — closing bearish trade on bullish signal (conf=%d).",
            symbol,
            confidence,
        )
        return True

    return False


# _parse_verdict_and_confidence imported from trade_plan (see above)


def _trade_plan_from_verdict(verdict: str, confidence: int, ctx: dict) -> dict | None:
    plan = build_paper_trade_plan(verdict, confidence, ctx)
    if not plan:
        return None

    expiry = ctx.get("expiry", "")
    symbol = ctx.get("symbol", "")
    option_rows = ctx.get("option_rows") or []
    strike = float(plan["strike"])
    option_type = str(plan["option_type"])
    side = plan.get("side", "BUY")

    underlying = float(ctx.get("underlying") or plan.get("entry_underlying") or 0)

    if option_type == "FUT":
        entry_premium = underlying
    else:
        entry_premium = _get_option_premium(
            symbol, expiry, strike, option_type, option_rows
        )
        if entry_premium is None or entry_premium <= 0:
            log.warning(
                "%s: failed to resolve option premium for strike %g, type %s",
                symbol,
                strike,
                option_type,
            )
            return None

    # C4: Use ATR-based underlying SL/Target (unified via trade_plan.py)
    from config.symbol_classes import get_strike_step

    step = float(get_strike_step(symbol) or 50)
    option_type = str(plan.get("option_type", "CE")).upper()
    if side == "SELL":
        sl_ul, tgt_ul = calculate_sell_sl_target(
            entry_premium, underlying, ctx, step, option_type=option_type
        )
    else:
        sl_ul, tgt_ul = calculate_buy_sl_target(
            entry_premium, underlying, ctx, step, option_type=option_type
        )

    if sl_ul is None or tgt_ul is None:
        return None

    plan["sl_underlying"] = sl_ul
    plan["target_underlying"] = tgt_ul

    # Convert underlying distances to premium equivalents for GTT/polling
    sl_premium, target_premium = convert_underlying_sl_to_premium(
        underlying, sl_ul, tgt_ul, entry_premium, side, option_type, strike, option_rows
    )

    plan["entry_premium"] = entry_premium
    plan["sl_premium"] = sl_premium
    plan["target_premium"] = target_premium
    return plan


def _bg_pending_gtt_placer(
    kite,
    inserted_id: int,
    symbol: str,
    exchange: str,
    tradingsymbol: str,
    side: str,
    quantity: int,
    sl_trigger: float,
    target_trigger: float,
    sl_limit: float,
    target_limit: float,
    entry_premium: float,
    shadow_mode: bool,
) -> None:
    import time

    log.info(
        "%s: Spawned background GTT placer for pending order of trade id %d",
        symbol,
        inserted_id,
    )
    start_time = time.time()
    while time.time() - start_time < 300:
        time.sleep(5)
        try:
            trade = _latest_live_trade(inserted_id)
            if not trade or trade.get("status") != "OPEN":
                log.info(
                    "%s: Trade %d is no longer OPEN. Exiting background GTT thread.",
                    symbol,
                    inserted_id,
                )
                return

            if trade.get("broker_status") == "COMPLETE" and trade.get("gtt_order_id"):
                log.info(
                    "%s: Trade %d already has GTT. Exiting background GTT thread.",
                    symbol,
                    inserted_id,
                )
                return

            broker_order_id = trade.get("broker_order_id")
            if not broker_order_id:
                continue

            b_status, b_msg = confirm_order_fill(kite, broker_order_id, shadow_mode)
            if b_status == "COMPLETE":
                log.info(
                    "%s: Pending trade filled in background! Placing GTT...", symbol
                )
                gtt_order_id = place_kite_gtt(
                    kite,
                    symbol,
                    exchange,
                    tradingsymbol,
                    "SELL" if side == "BUY" else "BUY",
                    quantity,
                    [sl_trigger, target_trigger],
                    [sl_limit, target_limit],
                    entry_premium,
                    shadow_mode,
                )
                update_live_trade_entry(
                    inserted_id,
                    broker_status="COMPLETE",
                    broker_message="Filled and GTT placed in background",
                    gtt_order_id=gtt_order_id,
                    exit_mode="GTT",
                )
                from src.alerts.telegram_dispatcher import send_text

                send_text(
                    f"🤖 **[LIVE]** Background GTT Placed for `{symbol}` after pending fill. GTT ID: `{gtt_order_id}`"
                )
                return
            elif b_status in ("REJECTED", "CANCELLED"):
                log.info(
                    "%s: Pending trade %s in background. Cleaning up...",
                    symbol,
                    b_status,
                )
                update_live_trade_entry(
                    inserted_id,
                    status="REJECTED",
                    broker_status=b_status,
                    broker_message=b_msg,
                    reason=f"Order {b_status.lower()} in background check",
                )
                return
        except Exception as ex:
            log.warning("%s: Error in background GTT placer: %s", symbol, ex)
    log.warning(
        "%s: Background GTT placer for trade %d timed out after 5 minutes.",
        symbol,
        inserted_id,
    )


def _resolve_trade_quantity(symbol: str, lots: int, resolved: dict | None) -> int:
    lot_multiplier = (resolved or {}).get("lot_size") or LOT_SIZES.get(symbol, 1)
    return int(lots * lot_multiplier)


def _reject_fallback_instrument(
    symbol: str, resolved: dict | None, shadow_mode: bool
) -> str | None:
    if shadow_mode:
        return None
    if not resolved or not resolved.get("tradingsymbol"):
        return "Failed to resolve Kite tradingsymbol"
    if not resolved.get("instrument_token"):
        return f"Kite instrument cache miss for {symbol}; refusing live broker order on fallback tradingsymbol"
    return None


def place_kite_order(
    kite,
    symbol: str,
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    shadow_mode: bool,
    expected_price: float = 0.0,
    tick_size: float = 0.05,
) -> str:
    if shadow_mode:
        import uuid

        sh_id = f"sh-ord-{uuid.uuid4().hex[:8]}"
        log.info(
            "[SHADOW] Suppressed order placement for %s:%s Qty=%d, generated ID: %s",
            exchange,
            tradingsymbol,
            quantity,
            sh_id,
        )
        return sh_id
    try:
        # Kite API rejects bare MARKET orders. Use LIMIT with a price buffer
        # to simulate market execution. Fetch LTP, apply slippage buffer.
        full_symbol = f"{exchange}:{tradingsymbol}"
        ltp = None
        try:
            quote = kite.ltp(full_symbol)
            ltp = quote.get(full_symbol, {}).get("last_price")
        except Exception as qe:
            err_msg = str(qe)
            if "Insufficient permission" in err_msg:
                log.info(
                    "Kite LTP subscription missing for %s (falling back to expected price: %s)",
                    full_symbol,
                    expected_price,
                )
            else:
                log.warning(
                    "Could not fetch LTP for %s before order: %s", full_symbol, qe
                )

        if not ltp or ltp <= 0:
            ltp = expected_price

        if ltp and ltp > 0:
            is_future = "FUT" in tradingsymbol.upper()
            # Dynamic slippage buffer: 0.2% for futures to avoid circuit limits, 5% for options
            buffer_pct = 0.002 if is_future else 0.05
            # P2-05: Slippage buffer direction for SELL.
            # For BUY, paying above LTP ensures fill (hit the ask).
            # For SELL (writing), offering below LTP crosses the spread
            # to the bid side, guaranteeing execution.
            if transaction_type == "BUY":
                limit_price = ltp * (1 + buffer_pct)
            else:
                limit_price = ltp * (1 - buffer_pct)

            # Align to tick size
            t_size = tick_size or 0.05
            if t_size <= 0:
                t_size = 0.05
            decimals = 2 if t_size >= 0.01 else 4
            limit_price = round(round(limit_price / t_size) * t_size, decimals)
            # Ensure price is at least tick size
            limit_price = max(limit_price, t_size)

            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,
                quantity=quantity,
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=limit_price,
            )
        else:
            # Fallback: use MARKET but this usually fails without Market Protection
            log.warning(
                "No LTP and no expected price for %s, placing bare MARKET order",
                full_symbol,
            )
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,
                quantity=quantity,
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_MARKET,
            )
        return order_id
    except Exception as e:
        _handle_kite_ip_error(e)
        log.error("Kite order placement failed: %s", e)
        raise e


def confirm_order_fill(kite, order_id: str, shadow_mode: bool) -> tuple[str, str]:
    """
    Poll Kite API to confirm if the order is filled, rejected, or still pending.
    Returns a tuple of (broker_status, broker_message).
    """
    if shadow_mode or not order_id:
        return "SHADOW", "Shadow trade executed"

    import time

    max_retries = 5
    delay_sec = 0.5

    for attempt in range(max_retries):
        try:
            history = kite.order_history(order_id)
            if history:
                latest = history[-1]
                status = latest.get("status")
                reason = latest.get("status_message") or "No status message"

                if status == "COMPLETE":
                    return "COMPLETE", "Executed and filled on Kite Connect"
                elif status in ("REJECTED", "CANCELLED"):
                    return status, f"Order {status.lower()}: {reason}"

                log.info(
                    "Order %s status is %s (attempt %d/%d)...",
                    order_id,
                    status,
                    attempt + 1,
                    max_retries,
                )
            else:
                log.warning("No order history found for %s", order_id)
        except Exception as e:
            log.warning("Failed to fetch order history for %s: %s", order_id, e)

        time.sleep(delay_sec)

    # If still not complete/rejected/cancelled, return PENDING
    try:
        history = kite.order_history(order_id)
        if history:
            latest = history[-1]
            status = latest.get("status") or "PENDING"
            reason = (
                latest.get("status_message") or "Order is active/pending at exchange"
            )
            return "PENDING", f"Status: {status} | {reason}"
    except Exception:
        pass

    return "PENDING", "Order placed but fill confirmation timed out"


def place_kite_gtt(
    kite,
    symbol: str,
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    trigger_values: list[float],
    limit_prices: list[float],
    last_price: float,
    shadow_mode: bool,
) -> str:
    if shadow_mode:
        import uuid

        sh_id = f"sh-gtt-{uuid.uuid4().hex[:8]}"
        log.info(
            "[SHADOW] Suppressed GTT placement for %s:%s Qty=%d, generated ID: %s",
            exchange,
            tradingsymbol,
            quantity,
            sh_id,
        )
        return sh_id
    try:
        # Sort trigger_values and limit_prices as pairs by trigger_value ascending (BUG-002)
        pairs = sorted(zip(trigger_values, limit_prices), key=lambda x: x[0])
        sorted_triggers = [p[0] for p in pairs]
        sorted_limits = [p[1] for p in pairs]

        gtt_id = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_OCO,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            trigger_values=sorted_triggers,
            last_price=last_price,
            orders=[
                {
                    "transaction_type": transaction_type,
                    "quantity": quantity,
                    "product": kite.PRODUCT_NRML,
                    "order_type": kite.ORDER_TYPE_LIMIT,
                    "price": sorted_limits[0],
                },
                {
                    "transaction_type": transaction_type,
                    "quantity": quantity,
                    "product": kite.PRODUCT_NRML,
                    "order_type": kite.ORDER_TYPE_LIMIT,
                    "price": sorted_limits[1],
                },
            ],
        )
        return gtt_id
    except Exception as e:
        _handle_kite_ip_error(e)
        log.error("Kite GTT placement failed: %s", e)
        raise e


def cancel_kite_gtt(kite, gtt_id: str, shadow_mode: bool) -> None:
    if shadow_mode:
        log.info("[SHADOW] Suppressed GTT cancellation for ID: %s", gtt_id)
        return
    try:
        kite.cancel_gtt(gtt_id)
    except Exception as e:
        log.warning("Kite GTT cancellation failed for ID %s: %s", gtt_id, e)


def _get_base_symbol(symbol: str) -> str:
    parts = str(symbol).upper().strip().split()
    if not parts:
        return ""
    if parts[0] == "MCX" and len(parts) > 1:
        return parts[1]
    if parts[0].startswith("MCX") and len(parts[0]) > 3:
        return parts[0][3:]
    if parts[0].startswith("NIFTY"):
        return "NIFTY"
    if parts[0].startswith("BANKNIFTY"):
        return "BANKNIFTY"
    if parts[0].startswith("CRUDEOIL"):
        return "CRUDEOIL"
    if sym.startswith("GOLD"):
        return "GOLD"
    if sym.startswith("MCX"):
        return "MCX"
    import re

    m = re.match(r"^[A-Z]+", sym)
    return m.group(0) if m else sym


def _latest_live_trade(trade_id: int) -> dict | None:
    from src.models.schema import get_conn

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM live_trades WHERE id=?", (trade_id,)
        ).fetchone()
        return dict(row) if row else None


def _exit_open_live_trade(
    *,
    kite,
    symbol: str,
    trade: dict,
    underlying: float,
    exit_premium: float | None,
    status: str,
    reason: str,
    shadow_mode: bool,
    now_iso: str,
) -> dict:
    exit_side = "SELL" if trade.get("side") == "BUY" else "BUY"
    exchange = _get_exchange(symbol)
    resolved = resolve_instrument(
        symbol,
        trade.get("expiry") or "",
        trade.get("strike") or 0.0,
        trade.get("option_type") or "FUT",
    )
    reject_reason = _reject_fallback_instrument(symbol, resolved, shadow_mode)
    if reject_reason:
        raise RuntimeError(reject_reason)
    tradingsymbol = (
        resolved["tradingsymbol"]
        if resolved and resolved.get("tradingsymbol")
        else symbol
    )
    quantity = _resolve_trade_quantity(symbol, int(trade.get("lots") or 1), resolved)

    order_id = place_kite_order(
        kite,
        symbol,
        exchange,
        tradingsymbol,
        exit_side,
        quantity,
        shadow_mode,
        expected_price=exit_premium or 0.0,
        tick_size=resolved.get("tick_size", 0.05) if resolved else 0.05,
    )

    broker_status, broker_message = confirm_order_fill(kite, order_id, shadow_mode)
    if broker_status in ("REJECTED", "CANCELLED"):
        raise RuntimeError(f"Exit order {broker_status.lower()}: {broker_message}")

    if trade.get("gtt_order_id"):
        cancel_kite_gtt(kite, trade["gtt_order_id"], shadow_mode)

    close_live_trade(
        trade["id"],
        now_iso,
        underlying,
        exit_premium,
        status,
        reason,
    )
    return _latest_live_trade(trade["id"]) or trade


def run_live_trading(
    symbol: str, scan_context: dict, digest_id: str, intel: dict, ai_verdict=None
) -> dict | None:
    if not _is_market_open(symbol):
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    config = load_runtime_config()
    shadow_mode = config.get("live_shadow_mode", True)
    broker_disabled = config.get("live_broker_disabled", False)
    if broker_disabled:
        log.debug(
            "%s: live broker disabled via cockpit — skipping all order placement",
            symbol,
        )
        return {
            "action": "BLOCKED_BROKER_DISABLED",
            "reason": "Broker trades turned off in Cockpit",
        }
    kite = get_kite_client()
    if not kite and not shadow_mode:
        log.warning(
            "Live trading skipped: Zerodha credentials / access token invalid or not logged in."
        )
        return {"action": "BLOCKED_AUTH", "reason": "Kite client not initialized"}

    now_iso = datetime.now(timezone.utc).isoformat()
    scan_context = scan_context or {}
    underlying = float(scan_context.get("underlying") or 0.0)
    expiry = scan_context.get("expiry", "")
    option_rows = scan_context.get("option_rows") or []
    verdict, confidence = _parse_verdict_and_confidence(
        intel.get("telegram_text") or ""
    )

    current_open_trade = get_open_live_trade(symbol)
    if not current_open_trade:
        broker_conf = get_broker_config()
        if broker_conf and broker_conf.get("kill_switch_active"):
            log.warning("Live trading skipped: Kill Switch is active!")
            return {"action": "BLOCKED_KILL_SWITCH", "reason": "Kill Switch active"}

    if current_open_trade:
        if current_open_trade.get("setup_type") == "DIRECT_KITE" and not config.get(
            "manage_direct_kite_positions", False
        ):
            log.debug(
                "%s: Direct Kite position management is disabled. Skipping tracking.",
                symbol,
            )
            return {"action": "HELD_DIRECT_DISABLED", "trade": current_open_trade}

        # Check if the broker_status is PENDING. If so, reconcile/verify fill!
        if current_open_trade.get("broker_status") == "PENDING":
            log.info(
                "%s: Open trade is PENDING at broker. Checking for fill...", symbol
            )
            b_status, b_msg = confirm_order_fill(
                kite, current_open_trade.get("broker_order_id"), shadow_mode
            )
            if b_status == "COMPLETE":
                log.info(
                    "%s: PENDING trade filled! Updating database status to COMPLETE.",
                    symbol,
                )
                # Try placing GTT now that the order is complete
                gtt_order_id = None
                exit_mode = current_open_trade.get("exit_mode")
                if current_open_trade.get("option_type") != "FUT":
                    try:
                        resolved = resolve_instrument(
                            symbol,
                            current_open_trade["expiry"],
                            current_open_trade["strike"],
                            current_open_trade["option_type"],
                        )
                        tradingsymbol = (
                            resolved["tradingsymbol"] if resolved else symbol
                        )
                        quantity = _resolve_trade_quantity(
                            symbol, int(current_open_trade.get("lots") or 1), resolved
                        )
                        sl_trigger = float(current_open_trade["sl_premium"])
                        target_trigger = float(current_open_trade["target_premium"])
                        sl_limit = (
                            round(sl_trigger * 0.95, 2)
                            if current_open_trade["side"] == "BUY"
                            else round(sl_trigger * 1.05, 2)
                        )
                        target_limit = (
                            round(target_trigger * 0.95, 2)
                            if current_open_trade["side"] == "BUY"
                            else round(target_trigger * 1.05, 2)
                        )
                        gtt_order_id = place_kite_gtt(
                            kite,
                            symbol,
                            _get_exchange(symbol),
                            tradingsymbol,
                            "SELL" if current_open_trade["side"] == "BUY" else "BUY",
                            quantity,
                            [sl_trigger, target_trigger],
                            [sl_limit, target_limit],
                            current_open_trade["entry_premium"],
                            shadow_mode,
                        )
                        exit_mode = "GTT"
                    except Exception as ge:
                        log.warning(
                            "%s: GTT placement failed on fill reconciliation: %s",
                            symbol,
                            ge,
                        )

                update_live_trade_entry(
                    current_open_trade["id"],
                    broker_status="COMPLETE",
                    broker_message="Reconciled: order filled",
                    gtt_order_id=gtt_order_id,
                    exit_mode=exit_mode,
                )
                current_open_trade = _latest_live_trade(current_open_trade["id"])
            elif b_status in ("REJECTED", "CANCELLED"):
                log.warning(
                    "%s: PENDING trade was %s at broker! Cleaning up trade record.",
                    symbol,
                    b_status,
                )
                update_live_trade_entry(
                    current_open_trade["id"],
                    status="REJECTED",
                    broker_status=b_status,
                    broker_message=b_msg,
                    reason=f"Order {b_status.lower()} on reconciliation",
                )
                return {"action": "BLOCKED_ORDER_FAILED", "reason": b_msg}
            else:
                log.info(
                    "%s: PENDING trade still not filled. Status: %s. Holding...",
                    symbol,
                    b_msg,
                )
                return {"action": "HELD_PENDING", "trade": current_open_trade}

        # C1: aligned reversal guard with paper trading
        if _is_reversal_against_open_trade(
            current_open_trade,
            verdict,
            confidence,
            symbol=symbol,
            option_type=current_open_trade.get("option_type", ""),
            strike=float(current_open_trade.get("strike") or 0),
            ctx=scan_context,
        ):
            exit_premium = None
            if current_open_trade["option_type"] != "FUT":
                exit_premium = _get_option_premium(
                    symbol,
                    current_open_trade["expiry"],
                    current_open_trade["strike"],
                    current_open_trade["option_type"],
                    option_rows,
                )
            try:
                closed = _exit_open_live_trade(
                    kite=kite,
                    symbol=symbol,
                    trade=current_open_trade,
                    underlying=underlying,
                    exit_premium=exit_premium,
                    status="CLOSED_REVERSAL",
                    reason=f"Trend reversed against position (verdict: {verdict})",
                    shadow_mode=shadow_mode,
                    now_iso=now_iso,
                )
                return {"action": "CLOSED", "trade": closed, "reason": "reversal"}
            except Exception as e:
                log.error("Failed to square-off reversed position: %s", e)
                return {"action": "ERROR", "reason": f"reversal square-off failed: {e}"}

        if (
            current_open_trade.get("exit_mode") == "POLL"
            or shadow_mode
            or current_open_trade.get("option_type") == "FUT"
        ):
            if current_open_trade["option_type"] == "FUT":
                exit_premium = underlying
            else:
                exit_premium = _get_option_premium(
                    symbol,
                    current_open_trade["expiry"],
                    current_open_trade["strike"],
                    current_open_trade["option_type"],
                    option_rows,
                )

            if exit_premium is not None:
                sl_premium = float(current_open_trade.get("sl_premium") or 0.0)
                target_premium = float(current_open_trade.get("target_premium") or 0.0)
                is_sell = current_open_trade.get("side") == "SELL"
                close_status = ""
                close_reason = ""

                if is_sell and exit_premium >= sl_premium:
                    close_status, close_reason = "CLOSED_SL", "stop loss hit"
                elif is_sell and exit_premium <= target_premium:
                    close_status, close_reason = "CLOSED_TARGET", "target hit"
                elif not is_sell and exit_premium <= sl_premium:
                    close_status, close_reason = "CLOSED_SL", "stop loss hit"
                elif not is_sell and exit_premium >= target_premium:
                    close_status, close_reason = "CLOSED_TARGET", "target hit"

                if close_status:
                    try:
                        closed = _exit_open_live_trade(
                            kite=kite,
                            symbol=symbol,
                            trade=current_open_trade,
                            underlying=underlying,
                            exit_premium=exit_premium,
                            status=close_status,
                            reason=close_reason,
                            shadow_mode=shadow_mode,
                            now_iso=now_iso,
                        )
                        return {
                            "action": "CLOSED",
                            "trade": closed,
                            "reason": close_reason,
                        }
                    except Exception as e:
                        log.error("Failed fallback exit square-off: %s", e)
                        return {
                            "action": "ERROR",
                            "reason": f"poll square-off failed: {e}",
                        }

        return {"action": "HELD", "trade": current_open_trade}

    # Kill switch checked at top of function

    base_sym = _get_base_symbol(symbol)
    enabled_symbols = config.get("live_enabled_broker_symbols")
    if enabled_symbols is not None and base_sym not in enabled_symbols:
        return {
            "action": "BLOCKED_DISABLED_SYMBOL",
            "reason": f"Live trading disabled for {base_sym}",
        }

    ctx = {
        **scan_context,
        "symbol": symbol,
        "expiry": expiry,
        "option_rows": option_rows,
    }
    decision = make_trade_decision(symbol, intel, ctx, ai_verdict=ai_verdict)
    if decision["status"] == "BLOCKED":
        return {"action": "BLOCKED_DECISION", "reason": decision["reason"]}

    risk_ok, risk_reason = check_live_risk_limits(symbol)
    if not risk_ok:
        return {"action": "BLOCKED_RISK", "reason": risk_reason}

    plan = _trade_plan_from_verdict(verdict, confidence, ctx)
    if not plan:
        return {"action": "BLOCKED_PLAN", "reason": "No valid trade plan"}

    entry_premium = plan["entry_premium"]
    lots = calculate_trade_lots(
        symbol,
        entry_premium,
        side=plan.get("side", "BUY"),
        is_paper=False,
        pyramid_level=plan.get("pyramid_level", 1),
    )
    today_date = datetime.now(IST).strftime("%Y%m%d")
    signal_key = f"{symbol}:{plan.get('option_type', '')}:{int(plan.get('strike') or 0)}:{today_date}:live"
    exit_mode = "POLL" if plan["option_type"] == "FUT" else "GTT"
    scores = decision.get("scores") or {}
    trade_data = {
        "opened_at": now_iso,
        "symbol": symbol,
        "expiry": expiry,
        "verdict_label": plan["verdict_label"],
        "side": plan.get("side", "BUY"),
        "option_type": plan["option_type"],
        "strike": plan["strike"],
        "entry_underlying": plan["entry_underlying"],
        "entry_premium": entry_premium,
        "sl_underlying": plan["sl_underlying"],
        "sl_premium": plan["sl_premium"],
        "target_underlying": plan["target_underlying"],
        "target_premium": plan["target_premium"],
        "lots": lots,
        "status": "OPEN",
        "reason": f"auto-live | {decision['reason']}",
        "digest_id": digest_id,
        "trade_status": decision["status"] if not shadow_mode else "SHADOW",
        "setup_type": decision["setup_type"],
        "decision_reason": decision["reason"],
        "confidence_score": scores.get("confidence"),
        "entry_quality_score": scores.get("entry_quality"),
        "trend_alignment_score": scores.get("trend_alignment"),
        "regime_score": scores.get("regime_score"),
        "signal_key": signal_key,
        "broker_order_id": None,
        "gtt_order_id": None,
        "broker_status": "SHADOW" if shadow_mode else "PENDING",
        "broker_message": "Shadow trade pending"
        if shadow_mode
        else "Pending broker entry",
        "exit_mode": exit_mode,
        # Phase 0: ML feature columns (captured at trade open time)
        **_build_ml_feature_snapshot(ctx, ai_verdict),
    }

    inserted_id = insert_live_trade(trade_data)
    if not inserted_id:
        log.warning(
            "%s: live trade INSERT skipped - duplicate signal_key=%s",
            symbol,
            signal_key,
        )
        return {"action": "DEDUP_SKIPPED", "reason": "duplicate signal key"}

    exchange = _get_exchange(symbol)
    resolved = resolve_instrument(symbol, expiry, plan["strike"], plan["option_type"])
    reject_reason = _reject_fallback_instrument(symbol, resolved, shadow_mode)
    if reject_reason:
        update_live_trade_entry(
            inserted_id,
            status="REJECTED",
            broker_status="REJECTED",
            broker_message=reject_reason,
            reason=reject_reason,
        )
        return {"action": "BLOCKED_SYMBOL", "reason": reject_reason}

    tradingsymbol = resolved["tradingsymbol"]
    quantity = _resolve_trade_quantity(symbol, lots, resolved)
    try:
        order_id = place_kite_order(
            kite,
            symbol,
            exchange,
            tradingsymbol,
            plan["side"],
            quantity,
            shadow_mode,
            expected_price=entry_premium,
            tick_size=resolved.get("tick_size", 0.05) if resolved else 0.05,
        )
    except Exception as e:
        update_live_trade_entry(
            inserted_id,
            status="REJECTED",
            broker_status="REJECTED",
            broker_message=str(e),
            reason=f"Entry order failed: {e}",
        )
        return {"action": "BLOCKED_ORDER_FAILED", "reason": str(e)}

    # Verify order fill
    broker_status, broker_message = confirm_order_fill(kite, order_id, shadow_mode)
    if broker_status in ("REJECTED", "CANCELLED"):
        update_live_trade_entry(
            inserted_id,
            status="REJECTED",
            broker_status=broker_status,
            broker_message=broker_message,
            reason=f"Order not filled: {broker_message}",
        )
        return {"action": "BLOCKED_ORDER_FAILED", "reason": broker_message}

    gtt_order_id = None
    if plan["option_type"] != "FUT" and broker_status == "COMPLETE":
        # Only place GTT if order is complete/filled to avoid placing target/SL on unfilled orders
        try:
            sl_trigger = float(plan["sl_premium"])
            target_trigger = float(plan["target_premium"])
            sl_limit = (
                round(sl_trigger * 0.95, 2)
                if plan["side"] == "BUY"
                else round(sl_trigger * 1.05, 2)
            )
            target_limit = (
                round(target_trigger * 0.95, 2)
                if plan["side"] == "BUY"
                else round(target_trigger * 1.05, 2)
            )
            gtt_order_id = place_kite_gtt(
                kite,
                symbol,
                exchange,
                tradingsymbol,
                "SELL" if plan["side"] == "BUY" else "BUY",
                quantity,
                [sl_trigger, target_trigger],
                [sl_limit, target_limit],
                entry_premium,
                shadow_mode,
            )
        except Exception as e:
            exit_mode = "POLL"
            from src.alerts.telegram_dispatcher import send_text

            send_text(
                f"[GTT FAILED] `{symbol}` - {e}; falling back to premium-poll exit."
            )
    elif plan["option_type"] != "FUT" and broker_status == "PENDING":
        # Defer GTT and use POLL exit fallback for safety until resolved
        exit_mode = "POLL"
        # Spawn background thread to poll fill and place GTT (BUG-003)
        sl_trigger = float(plan["sl_premium"])
        target_trigger = float(plan["target_premium"])
        sl_limit = (
            round(sl_trigger * 0.95, 2)
            if plan["side"] == "BUY"
            else round(sl_trigger * 1.05, 2)
        )
        target_limit = (
            round(target_trigger * 0.95, 2)
            if plan["side"] == "BUY"
            else round(target_trigger * 1.05, 2)
        )
        t = threading.Thread(
            target=_bg_pending_gtt_placer,
            args=(
                kite,
                inserted_id,
                symbol,
                exchange,
                tradingsymbol,
                plan["side"],
                quantity,
                sl_trigger,
                target_trigger,
                sl_limit,
                target_limit,
                entry_premium,
                shadow_mode,
            ),
            daemon=True,
        )
        t.start()

    update_live_trade_entry(
        inserted_id,
        broker_order_id=order_id,
        gtt_order_id=gtt_order_id,
        broker_status=broker_status,
        broker_message=broker_message,
        exit_mode=exit_mode,
    )
    trade_data.update(
        {
            "broker_order_id": order_id,
            "gtt_order_id": gtt_order_id,
            "broker_status": broker_status,
            "broker_message": broker_message,
            "exit_mode": exit_mode,
        }
    )

    from src.alerts.telegram_dispatcher import send_text

    prefix = "[SHADOW]" if shadow_mode else "[LIVE]"
    send_text(
        f"{prefix} **Order Placed** | `{plan['side']}` `{symbol}` `{plan['option_type']}` "
        f"Strike `{plan['strike']}`. Entry `{entry_premium}`, SL `{plan['sl_premium']}`, "
        f"Target `{plan['target_premium']}`. Lots: `{lots}` (Qty: `{quantity}`). "
        f"Status: `{broker_status}`."
    )
    return {
        "action": "EXECUTED",
        "trade": trade_data,
        "setup_type": decision["setup_type"],
        "lots": lots,
    }


def run_live_timeframe_strategy(
    symbol: str, scan_context: dict, digest_id: str, intel: dict, ai_verdict=None
) -> dict | None:
    """
    Live timeframe breakout strategy (3H entry / 1H exit).
    C3 fix: Previously a stub returning None. Now mirrors paper_trading.run_timeframe_strategy
    but executes via Kite broker with GTT/poll exit management.
    """
    if not _is_market_open(symbol):
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    config = load_runtime_config()
    shadow_mode = config.get("live_shadow_mode", True)
    broker_disabled = config.get("live_broker_disabled", False)
    if broker_disabled:
        return {
            "action": "BLOCKED_BROKER_DISABLED",
            "reason": "Broker trades turned off in Cockpit",
        }
    kite = get_kite_client()
    if not kite and not shadow_mode:
        return {"action": "BLOCKED_AUTH", "reason": "Kite client not initialized"}

    ctx = scan_context or {}
    underlying = float(ctx.get("underlying") or 0.0)
    if underlying <= 0:
        return {"action": "SKIPPED", "reason": "Underlying price is zero or negative"}

    # Gating checks for scan frequency
    from config.runtime_config import get_scan_frequency_mcx, get_scan_frequency_nse
    from src.models.schema import (
        get_scan_summary_at_least_1h_old,
        get_scan_summary_n_scans_ago,
        get_today_scan_count,
    )

    sym_class = get_symbol_class(symbol)
    if sym_class == "MCX_COMMODITY":
        scan_freq = get_scan_frequency_mcx()
    else:
        scan_freq = get_scan_frequency_nse()
    fetched_at = ctx.get("fetched_at") or datetime.now(timezone.utc).isoformat()
    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        today_scans = get_today_scan_count(symbol, fetched_at)
        current_scan_idx = today_scans + 1
        if current_scan_idx % scans_needed != 0:
            return {
                "action": "SKIPPED_TIMEFRAME_BOUNDARY",
                "reason": f"Skipped scan {current_scan_idx}",
            }

    chart_indicators = ctx.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}

    pay_3h = tf_data.get("3h")
    pay_1h = tf_data.get("1h")
    if not pay_3h or not pay_1h:
        return {
            "action": "SKIPPED",
            "reason": "Missing 3h/1h chart data",
        }

    ohlc_3h = pay_3h.get("ohlc")
    prev_3h = pay_3h.get("prev_ohlc") or pay_3h.get("last_closed_ohlc")
    ohlc_1h = pay_1h.get("ohlc")
    prev_1h = pay_1h.get("prev_ohlc") or pay_1h.get("last_closed_ohlc")

    if not ohlc_3h or not prev_3h or not ohlc_1h or not prev_1h:
        return {
            "action": "SKIPPED",
            "reason": "Incomplete 3h/1h candle data",
        }

    c_3h_close = float(ohlc_3h["close"])
    p_3h_high = float(prev_3h["high"])
    p_3h_low = float(prev_3h["low"])
    c_1h_close = float(ohlc_1h["close"])
    p_1h_high = float(prev_1h["high"])
    p_1h_low = float(prev_1h["low"])

    current_ce = ctx.get("total_ce_oi")
    current_pe = ctx.get("total_pe_oi")
    if current_ce is None or current_pe is None:
        return {"action": "SKIPPED", "reason": "Missing total OI data"}

    from config.settings import TIMEFRAME_OI_MIN_DIFF_PCT

    min_diff_pct = TIMEFRAME_OI_MIN_DIFF_PCT

    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        older = get_scan_summary_n_scans_ago(symbol, scans_needed - 1)
    else:
        older = get_scan_summary_at_least_1h_old(symbol, fetched_at)
    if not older:
        return {
            "action": "SKIPPED",
            "reason": "Insufficient scan history for OI comparison",
        }

    prev_ce = older["total_ce_oi"]
    prev_pe = older["total_pe_oi"]
    ce_diff = current_ce - prev_ce
    pe_diff = current_pe - prev_pe
    long_oi_support = (pe_diff - ce_diff) > (prev_pe * min_diff_pct)
    short_oi_support = (ce_diff - pe_diff) > (prev_ce * min_diff_pct)

    # M1 fix: ATR-based breakout buffer (0.5x ATR) with 0.3% minimum floor.
    # Old 0.1% floor (e.g. 24pts on NIFTY, 0.3pts on NATURALGAS) was noise-level.
    from src.engine.trade_plan import get_atr as _get_atr_live

    atr_val = _get_atr_live(ctx)
    breakout_buffer = max((atr_val or 0) * 0.5, underlying * 0.003)
    now_iso = datetime.now(timezone.utc).isoformat()
    bar_end_1h = pay_1h.get("bar_end_utc")
    expiry = ctx.get("expiry", "")
    option_rows = ctx.get("option_rows") or []

    # ── 1. EXIT LOGIC for open live timeframe trades ──
    open_tf_trades = get_open_live_timeframe_trades(symbol)
    closed_trade = None

    # Parse AI verdict for exit checks
    ai_bias = "NEUTRAL"
    ai_conf = 50.0
    ai_risk = "LOW"
    if ai_verdict is not None:
        if isinstance(ai_verdict, dict):
            action = ai_verdict.get("action")
            bias_val = ai_verdict.get("bias")
            ai_conf = float(ai_verdict.get("confidence", 50))
            ai_risk = str(ai_verdict.get("risk_rating") or "LOW").upper()
        else:
            action = getattr(ai_verdict, "action", None)
            bias_val = getattr(ai_verdict, "bias", None)
            ai_conf = float(getattr(ai_verdict, "confidence", 50))
            ai_risk = str(getattr(ai_verdict, "risk_rating", "LOW")).upper()

        if action == "GO_LONG":
            ai_bias = "BULLISH"
        elif action == "GO_SHORT":
            ai_bias = "BEARISH"
        elif bias_val:
            ai_bias = str(bias_val).upper()

    for trade in open_tf_trades:
        exit_premium = None
        if trade["option_type"] in ("CE", "PE"):
            exit_premium = _get_option_premium(
                symbol, expiry, trade["strike"], trade["option_type"], option_rows
            )
        elif trade["option_type"] == "FUT":
            exit_premium = underlying

        # LLM Reversal Exit
        if ai_verdict is not None:
            is_reversal = False
            if trade["verdict_label"] == "LONG" and ai_bias == "BEARISH":
                is_reversal = True
            elif trade["verdict_label"] == "SHORT" and ai_bias == "BULLISH":
                is_reversal = True

            if is_reversal and ai_conf >= 70:
                try:
                    closed = _exit_open_live_trade(
                        kite=kite,
                        symbol=symbol,
                        trade=trade,
                        underlying=underlying,
                        exit_premium=exit_premium,
                        status="CLOSED_REVERSAL",
                        reason=f"LLM Reversal: bias {ai_bias} (confidence {ai_conf}%)",
                        shadow_mode=shadow_mode,
                        now_iso=now_iso,
                    )
                    prefix = "[SHADOW]" if shadow_mode else "🚨 [LIVE]"
                    from src.alerts.telegram_dispatcher import send_text

                    send_text(
                        f"{prefix} **TF Crossover Exit** | Closed `{symbol}` `{trade.get('option_type')}` — `LLM Reversal` at underlying `{underlying}`."
                    )
                    closed_trade = closed
                    break
                except Exception as e:
                    log.error("Failed timeframe LLM reversal exit: %s", e)
                    continue

        # Premium-poll SL/Target check
        if exit_premium is not None:
            sl_prem = float(trade.get("sl_premium") or 0)
            tgt_prem = float(trade.get("target_premium") or 0)
            is_sell = trade.get("side") == "SELL"
            close_status = ""
            close_reason = ""

            if is_sell and sl_prem > 0 and exit_premium >= sl_prem:
                close_status, close_reason = "CLOSED_SL", "timeframe SL hit"
            elif is_sell and tgt_prem > 0 and exit_premium <= tgt_prem:
                close_status, close_reason = "CLOSED_TARGET", "timeframe target hit"
            elif not is_sell and sl_prem > 0 and exit_premium <= sl_prem:
                close_status, close_reason = "CLOSED_SL", "timeframe SL hit"
            elif not is_sell and tgt_prem > 0 and exit_premium >= tgt_prem:
                close_status, close_reason = "CLOSED_TARGET", "timeframe target hit"

            if close_status:
                try:
                    closed = _exit_open_live_trade(
                        kite=kite,
                        symbol=symbol,
                        trade=trade,
                        underlying=underlying,
                        exit_premium=exit_premium,
                        status=close_status,
                        reason=close_reason,
                        shadow_mode=shadow_mode,
                        now_iso=now_iso,
                    )
                    closed_trade = closed
                    break
                except Exception as e:
                    log.error("Failed timeframe exit square-off: %s", e)
                    continue

        # 1H crossover exit
        if bar_end_1h and trade["opened_at"] < bar_end_1h:
            should_exit = False
            exit_reason = ""
            if trade["verdict_label"] == "LONG" and c_1h_close < p_1h_low:
                crossover_size = p_1h_low - c_1h_close
                if crossover_size > 2 * breakout_buffer or short_oi_support:
                    should_exit = True
                    exit_reason = f"TF-1H-Cross: 1H close {c_1h_close:.2f} < p1H_low {p_1h_low:.2f}"
            elif trade["verdict_label"] == "SHORT" and c_1h_close > p_1h_high:
                crossover_size = c_1h_close - p_1h_high
                if crossover_size > 2 * breakout_buffer or long_oi_support:
                    should_exit = True
                    exit_reason = f"TF-1H-Cross: 1H close {c_1h_close:.2f} > p1H_high {p_1h_high:.2f}"

            if should_exit:
                try:
                    closed = _exit_open_live_trade(
                        kite=kite,
                        symbol=symbol,
                        trade=trade,
                        underlying=underlying,
                        exit_premium=exit_premium,
                        status="CLOSED_TF_EXIT",
                        reason=exit_reason,
                        shadow_mode=shadow_mode,
                        now_iso=now_iso,
                    )
                    closed_trade = closed
                    break
                except Exception as e:
                    log.error("Failed timeframe crossover exit: %s", e)
                    continue

    if closed_trade:
        return {"action": "CLOSED", "trade": closed_trade, "reason": "timeframe exit"}

    # ── 2. ENTRY LOGIC ──
    bar_end_3h = pay_3h.get("bar_end_utc")
    if not bar_end_3h:
        return {"action": "SKIPPED", "reason": "Missing 3H bar end timestamp"}

    # Merge intel into scan_context so the pipeline can access verdict/confidence
    ctx_copy = dict(ctx)
    if intel:
        ctx_copy.setdefault("intel", {}).update(intel)
    ctx_copy["is_live"] = True

    from src.engine.decision_audit import log_decision, update_decision_audit
    from src.engine.decision_pipeline import PipelineContext, run_entry_pipeline

    # Initialise pipeline context
    pipeline_ctx = PipelineContext(
        engine="TIMEFRAME",
        symbol=symbol,
        direction=None,
        underlying=underlying,
        scan_context=ctx_copy,
        ai_verdict=ai_verdict,
        steps=[],
    )

    # Run entry pipeline
    run_entry_pipeline(pipeline_ctx)

    # Log to decision_audit SQLite table
    audit_row_id = log_decision(
        pipeline_ctx, action="TRADE" if pipeline_ctx.passed else "SKIP"
    )

    if not pipeline_ctx.passed:
        signal_step = next((s for s in pipeline_ctx.steps if s.name == "signal"), None)
        if (
            signal_step
            and not signal_step.passed
            and "No 3H breakout detected" in signal_step.reason
        ):
            return {
                "action": "SKIPPED",
                "reason": "No 3H breakout trigger detected",
            }
        if (
            signal_step
            and not signal_step.passed
            and "Missing or incomplete 3H candle data" in signal_step.reason
        ):
            return {
                "action": "SKIPPED",
                "reason": "Incomplete 3H/1h candle data",
            }

        block_act = "BLOCKED_PLAN"
        if pipeline_ctx.block_step == "risk":
            block_act = "BLOCKED_RISK"
        elif pipeline_ctx.block_step == "symbol":
            block_act = "BLOCKED_SYMBOL"

        return {
            "action": block_act,
            "reason": f"Timeframe entry skipped: {pipeline_ctx.block_reason}",
        }

    direction = pipeline_ctx.direction
    breakout_buffer = pipeline_ctx.scan_context.get("_breakout_buffer", breakout_buffer)
    signal_key = f"{symbol}:TIMEFRAME:3H:{direction}:{bar_end_3h}:live"

    from config.settings import DEFAULT_LOTS_PER_TRADE
    from config.symbol_classes import get_strike_step

    step = float(get_strike_step(symbol) or 1)
    atm = ctx.get("atm_strike") or round(underlying / step) * step
    is_mcx_commodity = "NATURALGAS" in symbol or "CRUDEOIL" in symbol
    use_mcx_options = is_mcx_commodity and mcx_option_liquidity_ok(symbol, atm, ctx)

    if direction == "LONG":
        if is_mcx_commodity and not use_mcx_options:
            opt_type = "FUT"
            strike = atm
            entry_premium = underlying
            sl_underlying = float(ohlc_3h["low"])
            if underlying - sl_underlying < underlying * 0.003:
                sl_underlying = underlying - underlying * 0.003
            tgt_underlying = underlying + 2 * (underlying - sl_underlying)
        else:
            opt_type = "CE"
            strike = atm if is_mcx_commodity else (atm - 4 * step)
            entry_premium = _get_option_premium(
                symbol, expiry, strike, "CE", option_rows
            )
            if not entry_premium or entry_premium <= 0:
                return {
                    "action": "BLOCKED_PLAN",
                    "reason": f"Option premium unavailable for CE {strike}",
                }
            sl_underlying = float(ohlc_3h["low"])
            tgt_underlying = underlying + 2 * (underlying - sl_underlying)
    else:
        if is_mcx_commodity and not use_mcx_options:
            opt_type = "FUT"
            strike = atm
            entry_premium = underlying
            sl_underlying = float(ohlc_3h["high"])
            if sl_underlying - underlying < underlying * 0.003:
                sl_underlying = underlying + underlying * 0.003
            tgt_underlying = underlying - 2 * (sl_underlying - underlying)
        else:
            opt_type = "PE"
            strike = atm if is_mcx_commodity else (atm + 4 * step)
            entry_premium = _get_option_premium(
                symbol, expiry, strike, "PE", option_rows
            )
            if not entry_premium or entry_premium <= 0:
                return {
                    "action": "BLOCKED_PLAN",
                    "reason": f"Option premium unavailable for PE {strike}",
                }
            sl_underlying = float(ohlc_3h["high"])
            tgt_underlying = underlying - 2 * (sl_underlying - underlying)

    # Convert underlying SL/Target to premium equivalents (unified via trade_plan.py)
    side = "BUY" if direction == "LONG" else ("SELL" if opt_type == "FUT" else "BUY")
    sl_premium, target_premium = convert_underlying_sl_to_premium(
        underlying,
        sl_underlying,
        tgt_underlying,
        entry_premium,
        side,
        opt_type,
        strike,
        option_rows,
    )

    lots = max(1, DEFAULT_LOTS_PER_TRADE)
    exchange = _get_exchange(symbol)
    resolved = resolve_instrument(symbol, expiry, strike, opt_type)
    reject_reason = _reject_fallback_instrument(symbol, resolved, shadow_mode)
    if reject_reason:
        return {"action": "BLOCKED_SYMBOL", "reason": reject_reason}

    tradingsymbol = resolved["tradingsymbol"]
    quantity = _resolve_trade_quantity(symbol, lots, resolved)

    try:
        order_id = place_kite_order(
            kite,
            symbol,
            exchange,
            tradingsymbol,
            side,
            quantity,
            shadow_mode,
            expected_price=entry_premium,
            tick_size=resolved.get("tick_size", 0.05) if resolved else 0.05,
        )
    except Exception as e:
        log.error("%s: failed to place live timeframe order: %s", symbol, e)
        return {"action": "BLOCKED_ORDER_FAILED", "reason": str(e)}

    # Verify order fill (BUG-004)
    broker_status, broker_message = confirm_order_fill(kite, order_id, shadow_mode)
    if broker_status in ("REJECTED", "CANCELLED"):
        update_decision_audit(
            audit_row_id,
            action="SKIP",
            block_step="broker",
            block_reason=f"Order not filled: {broker_message}",
        )
        return {"action": "BLOCKED_ORDER_FAILED", "reason": broker_message}

    gtt_order_id = None
    exit_mode = "POLL" if opt_type == "FUT" else "GTT"
    if opt_type != "FUT" and broker_status == "COMPLETE":
        # Only place GTT if order is complete
        try:
            sl_trigger = float(sl_premium)
            target_trigger = float(target_premium)
            sl_limit = (
                round(sl_trigger * 0.95, 2)
                if side == "BUY"
                else round(sl_trigger * 1.05, 2)
            )
            target_limit = (
                round(target_trigger * 0.95, 2)
                if side == "BUY"
                else round(target_trigger * 1.05, 2)
            )
            gtt_order_id = place_kite_gtt(
                kite,
                symbol,
                exchange,
                tradingsymbol,
                "SELL" if side == "BUY" else "BUY",
                quantity,
                [sl_trigger, target_trigger],
                [sl_limit, target_limit],
                entry_premium,
                shadow_mode,
            )
        except Exception as e:
            exit_mode = "POLL"
            from src.alerts.telegram_dispatcher import send_text

            send_text(
                f"⚠️ **[GTT FAILED]** `{symbol}` TF — {e}; falling back to premium-poll exit."
            )
    elif opt_type != "FUT" and broker_status == "PENDING":
        exit_mode = "POLL"

    trade_data = {
        "opened_at": now_iso,
        "symbol": symbol,
        "expiry": expiry,
        "verdict_label": direction,
        "side": side,
        "option_type": opt_type,
        "strike": strike,
        "entry_underlying": underlying,
        "entry_premium": entry_premium,
        "sl_underlying": sl_underlying,
        "sl_premium": sl_premium,
        "target_underlying": tgt_underlying,
        "target_premium": target_premium,
        "lots": lots,
        "status": "OPEN",
        "reason": f"timeframe entry | 3H {'close > high' if direction == 'LONG' else 'close < low'} | level 1",
        "digest_id": digest_id,
        "trade_status": "LIVE" if not shadow_mode else "SHADOW",
        "setup_type": "TIMEFRAME",
        "decision_reason": f"3H breakout + OI confirmation ({direction})",
        "signal_key": signal_key,
        "broker_order_id": order_id,
        "gtt_order_id": gtt_order_id,
        "broker_status": broker_status,
        "broker_message": broker_message,
        "exit_mode": exit_mode,
        # Phase 0: ML feature columns (captured at trade open time)
        **_build_ml_feature_snapshot(ctx, ai_verdict),
    }

    inserted_id = insert_live_trade(trade_data)
    if not inserted_id:
        update_decision_audit(
            audit_row_id,
            action="SKIP",
            block_step="signal",
            block_reason="duplicate signal key",
        )
        return {"action": "DEDUP_SKIPPED", "reason": "duplicate signal key"}

    update_decision_audit(audit_row_id, action="TRADE", trade_id=inserted_id)

    if opt_type != "FUT" and broker_status == "PENDING":
        sl_trigger = float(sl_premium)
        target_trigger = float(target_premium)
        sl_limit = (
            round(sl_trigger * 0.95, 2)
            if side == "BUY"
            else round(sl_trigger * 1.05, 2)
        )
        target_limit = (
            round(target_trigger * 0.95, 2)
            if side == "BUY"
            else round(target_trigger * 1.05, 2)
        )
        t = threading.Thread(
            target=_bg_pending_gtt_placer,
            args=(
                kite,
                inserted_id,
                symbol,
                exchange,
                tradingsymbol,
                side,
                quantity,
                sl_trigger,
                target_trigger,
                sl_limit,
                target_limit,
                entry_premium,
                shadow_mode,
            ),
            daemon=True,
        )
        t.start()

    from src.alerts.telegram_dispatcher import send_text

    prefix = "[SHADOW]" if shadow_mode else "🟢 [LIVE]"
    send_text(
        f"{prefix} **TF Order** | `{side}` `{symbol}` `{opt_type}` Strike `{strike}`. Entry `{entry_premium}`, SL `{sl_premium}`, Target `{target_premium}`. Lots: `{lots}`."
    )

    return {
        "action": "EXECUTED",
        "trade": trade_data,
        "setup_type": "TIMEFRAME",
        "lots": lots,
    }


def sync_direct_kite_positions() -> None:
    from config.runtime_config import load_runtime_config

    config = load_runtime_config()
    shadow_mode = config.get("live_shadow_mode", True)
    if not config.get("manage_direct_kite_positions", False):
        return

    kite = get_kite_client()
    if not kite:
        return

    try:
        positions = kite.positions()
        net_positions = positions.get("net", [])
    except Exception as e:
        log.error("Failed to fetch Kite positions for direct sync: %s", e)
        # Clear Kite client cache if fetching positions failed, to force re-initialization
        clear_kite_client_cache()
        log.warning("Cleared Kite client cache due to position sync failure.")
        return

    monitored_bases = ["NIFTY", "BANKNIFTY", "SENSEX", "NATURALGAS", "CRUDEOIL"]

    import re
    from datetime import datetime, timezone

    from src.models.schema import get_conn, insert_live_trade

    with get_conn() as conn:
        db_trades = conn.execute(
            "SELECT id, symbol, option_type, strike, side FROM live_trades WHERE status='OPEN'"
        ).fetchall()
        open_db_signatures = []
        for dt in db_trades:
            sym = dt["symbol"]
            ot = dt["option_type"]
            stk = int(dt["strike"] or 0)
            sd = dt["side"]
            open_db_signatures.append(f"{sym}:{ot}:{stk}:{sd}")

        # Also exclude DIRECT_KITE positions that were adopted and closed today.
        # Without this, a position closed by CMP poll exit immediately re-adopts on
        # the next sync because it's no longer in status='OPEN'.
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        closed_today = conn.execute(
            "SELECT symbol, option_type, strike, side FROM live_trades "
            "WHERE setup_type='DIRECT_KITE' AND status!='OPEN' AND opened_at LIKE ?",
            (f"{today_prefix}%",),
        ).fetchall()
        for ct in closed_today:
            sig = f"{ct['symbol']}:{ct['option_type']}:{int(ct['strike'] or 0)}:{ct['side']}"
            if sig not in open_db_signatures:
                open_db_signatures.append(sig)

        now_iso = datetime.now(timezone.utc).isoformat()
        for p in net_positions:
            net_qty = p.get("quantity", 0)
            if net_qty == 0:
                continue

            avg_price = float(p.get("average_price") or 0.0)
            ts = p.get("tradingsymbol", "")
            base_sym = None
            for mb in monitored_bases:
                if ts.startswith(mb):
                    base_sym = mb
                    break

            if not base_sym:
                continue

            side = "BUY" if net_qty > 0 else "SELL"

            option_type = "FUT"
            strike = 0.0
            if ts.endswith("CE"):
                option_type = "CE"
            elif ts.endswith("PE"):
                option_type = "PE"

            if option_type in ("CE", "PE"):
                m = re.search(r"(\d+(?:\.\d+)?)(?:CE|PE)$", ts)
                if m:
                    strike = float(m.group(1))

            sig = f"{base_sym}:{option_type}:{int(strike)}:{side}"

            if sig in open_db_signatures:
                continue

            init_mode = config.get("direct_kite_initialization_mode", "fixed_pct")
            sl_premium = 0.0
            tgt_premium = 0.0
            sl_underlying = 0.0
            tgt_underlying = 0.0
            underlying_price = 0.0

            # Fetch latest underlying price (try live LTP first for indices, fallback to DB)
            underlying_price = 0.0
            live_resolved = False
            mapping = {
                "NIFTY": "NSE:NIFTY 50",
                "BANKNIFTY": "NSE:NIFTY BANK",
                "SENSEX": "BSE:SENSEX",
            }
            symbol_key = mapping.get(base_sym)

            if not symbol_key and base_sym in ("NATURALGAS", "CRUDEOIL"):
                try:
                    from config.symbol_classes import get_futures_expiry
                    from src.engine.symbol_resolver import resolve_instrument

                    fut_expiry = get_futures_expiry(base_sym)
                    if fut_expiry:
                        resolved_fut = resolve_instrument(
                            base_sym, fut_expiry, 0.0, "FUT"
                        )
                        if resolved_fut and resolved_fut.get("tradingsymbol"):
                            symbol_key = f"MCX:{resolved_fut['tradingsymbol']}"
                except Exception as e:
                    log.warning(
                        "Failed to resolve futures symbol for %s: %s", base_sym, e
                    )

            if symbol_key:
                try:
                    res = kite.ltp(symbol_key)
                    if res and symbol_key in res:
                        underlying_price = float(
                            res[symbol_key].get("last_price") or 0.0
                        )
                        if underlying_price > 0:
                            live_resolved = True
                except Exception as ltp_err:
                    log.warning(
                        "Failed to fetch live LTP for index %s: %s", symbol_key, ltp_err
                    )

            if not live_resolved:
                from src.models.schema import get_previous_underlying

                prev_und = get_previous_underlying(base_sym)
                if prev_und:
                    underlying_price = float(prev_und["price"] or 0.0)

            atr = None
            if init_mode == "dynamic" and underlying_price > 0:
                try:
                    from src.fetchers.chart_fetcher import get_chart_fetcher

                    chart_data = get_chart_fetcher().fetch(
                        base_sym, reference_price=underlying_price
                    )
                    if chart_data and base_sym in chart_data:
                        tf_data = chart_data[base_sym]
                        pay_3h = tf_data.get("3h") or {}
                        pay_1h = tf_data.get("1h") or {}
                        atr = pay_3h.get("atr_14") or pay_1h.get("atr_14")
                except Exception as chart_err:
                    log.warning(
                        "Failed to fetch chart indicators for direct dynamic SL: %s",
                        chart_err,
                    )

            if option_type == "FUT":
                entry_val = avg_price
                if init_mode == "dynamic" and atr and atr > 0:
                    if side == "BUY":
                        sl_underlying = round(entry_val - 1.5 * atr, 2)
                        tgt_underlying = round(entry_val + 2.0 * atr, 2)
                    else:
                        sl_underlying = round(entry_val + 1.5 * atr, 2)
                        tgt_underlying = round(entry_val - 2.0 * atr, 2)
                    log.info(
                        "Dynamic FUT SL/Tgt computed for manual trade of %s: atr=%s, SL=%s, Tgt=%s",
                        base_sym,
                        atr,
                        sl_underlying,
                        tgt_underlying,
                    )
                else:
                    sl_pct = (
                        float(config.get("direct_kite_default_sl_pct", 30.0)) / 100.0
                    )
                    tgt_pct = (
                        float(config.get("direct_kite_default_tgt_pct", 50.0)) / 100.0
                    )
                    if side == "BUY":
                        sl_underlying = round(entry_val * (1 - sl_pct), 2)
                        tgt_underlying = round(entry_val * (1 + tgt_pct), 2)
                    else:
                        sl_underlying = round(entry_val * (1 + sl_pct), 2)
                        tgt_underlying = round(entry_val * (1 - tgt_pct), 2)
                sl_premium = sl_underlying
                tgt_premium = tgt_underlying
            else:
                # Options (CE/PE)
                if init_mode == "dynamic" and atr and atr > 0 and underlying_price > 0:
                    vol_pct = atr / underlying_price
                    sl_pct = max(0.15, min(0.45, vol_pct * 40.0))
                    tgt_pct = max(0.25, min(0.75, vol_pct * 60.0))
                    log.info(
                        "Dynamic option SL/Tgt computed for manual trade of %s: vol_pct=%.4f, sl_pct=%.2f, tgt_pct=%.2f",
                        base_sym,
                        vol_pct,
                        sl_pct,
                        tgt_pct,
                    )
                else:
                    sl_pct = (
                        float(config.get("direct_kite_default_sl_pct", 30.0)) / 100.0
                    )
                    tgt_pct = (
                        float(config.get("direct_kite_default_tgt_pct", 50.0)) / 100.0
                    )

                if side == "BUY":
                    sl_premium = round(avg_price * (1 - sl_pct), 2)
                    tgt_premium = round(avg_price * (1 + tgt_pct), 2)
                else:
                    sl_premium = round(avg_price * (1 + sl_pct), 2)
                    tgt_premium = round(avg_price * (1 - tgt_pct), 2)

            lots = 1
            from config.settings import LOT_SIZES

            if base_sym in LOT_SIZES:
                lots = max(1, abs(net_qty) // LOT_SIZES[base_sym])

            expiry_val = get_expiry_for_tradingsymbol(ts)
            if not expiry_val:
                # P2-06: If expiry cannot be resolved (unknown symbol, new contract,
                # non-standard naming), skip adoption. Empty expiry causes zero PnL
                # in close_live_trade() because the expiry= lookup returns no rows.
                log.warning(
                    "Skipping direct Kite position adoption for %s — "
                    "could not resolve expiry from tradingsymbol '%s'",
                    base_sym,
                    ts,
                )
                continue

            trade_data = {
                "opened_at": now_iso,
                "symbol": base_sym,
                "expiry": expiry_val,
                "verdict_label": "DIRECT KITE",
                "side": side,
                "option_type": option_type,
                "strike": strike,
                "entry_underlying": avg_price
                if option_type == "FUT"
                else underlying_price,
                "entry_premium": avg_price if option_type != "FUT" else 0.0,
                "sl_underlying": sl_underlying,
                "sl_premium": sl_premium if option_type != "FUT" else 0.0,
                "target_underlying": tgt_underlying,
                "target_premium": tgt_premium if option_type != "FUT" else 0.0,
                "lots": lots,
                "status": "OPEN",
                "reason": "Direct Kite Manual Entry",
                "digest_id": "manual",
                "trade_status": "LIVE" if not shadow_mode else "SHADOW",
                "setup_type": "DIRECT_KITE",
                "decision_reason": f"Adopted {ts} from Kite",
                "signal_key": f"kite_direct_{ts}",
                "broker_order_id": "direct",
                "broker_status": "COMPLETE",
                "broker_message": "Adopted manually placed position",
                "exit_mode": "POLL",
            }

            inserted_id = insert_live_trade(trade_data)
            if inserted_id:
                open_db_signatures.append(sig)
                log.info("Adopted Kite direct position: %s as %s", ts, sig)
                from src.alerts.telegram_dispatcher import send_text

                send_text(
                    f"🤖 **[KITE DIRECT]** Adopted manual position `{ts}` ({side} Qty: {abs(net_qty)}) at `₹{avg_price}`. AI Exit Advisor will monitor it (SL: `₹{sl_premium}`, Target: `₹{tgt_premium}`)."
                )
