import logging
import re
from datetime import datetime, timezone
import pytz
from kiteconnect import KiteConnect
from src.models.schema import (
    get_open_live_trade,
    get_open_live_timeframe_trades,
    insert_live_trade,
    update_live_trade_entry,
    close_live_trade,
    get_broker_config,
    get_latest_snapshots_for_symbol,
)
from src.engine.symbol_resolver import resolve_instrument
from src.engine.capital_allocator import calculate_trade_lots
from src.engine.paper_plan import (
    build_paper_trade_plan,
    is_bearish_verdict,
    is_bullish_verdict,
)
from src.engine.trade_decision import make_trade_decision
from config.settings import LOT_SIZES
from config.symbol_classes import get_symbol_class, market_window
from config.runtime_config import load_runtime_config

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

def _get_exchange(symbol: str) -> str:
    if symbol.upper() in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"):
        return "MCX"
    return "NFO"

_cached_kite_client = None
_cached_access_token = None


def clear_kite_client_cache() -> None:
    global _cached_kite_client, _cached_access_token
    _cached_kite_client = None
    _cached_access_token = None

def get_kite_client() -> KiteConnect | None:
    global _cached_kite_client, _cached_access_token
    config = get_broker_config()
    if not config or not config.get("api_key") or not config.get("access_token"):
        _cached_kite_client = None
        _cached_access_token = None
        return None
    
    # Reuse cached client if access token matches
    if _cached_kite_client and _cached_access_token == config["access_token"]:
        return _cached_kite_client
        
    try:
        kite = KiteConnect(api_key=config["api_key"])
        kite.set_access_token(config["access_token"])
        
        # Mount retry adapter to requests session
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util import Retry
            
            retries = Retry(
                total=3,
                backoff_factor=0.2,
                status_forcelist=[500, 502, 503, 504],
                raise_on_status=False
            )
            adapter = HTTPAdapter(max_retries=retries)
            kite.reqsession.mount("https://", adapter)
        except Exception as e:
            log.warning("Failed to configure Retry Adapter: %s", e)
            
        _cached_kite_client = kite
        _cached_access_token = config["access_token"]
        
        # Do NOT synchronously populate instrument cache during Kite client init.
        # Instrument lookups will fall back to offline tradingsymbol generation if cache is empty.
        # (Cache refresh is failure-tolerant anyway, so only do it opportunistically on demand.)
        return kite
    except Exception:
        log.exception("Failed to initialize Kite client")
        _cached_kite_client = None
        _cached_access_token = None
        return None

def _get_option_premium(
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    option_rows: list[dict] | None = None,
) -> float | None:
    for row in option_rows or []:
        try:
            if (
                abs(float(row.get("strike") or 0) - strike) < 0.01
                and str(row.get("option_type") or "").upper() == option_type
            ):
                premium = float(row.get("ltp") or 0.0)
                return premium if premium > 0 else None
        except Exception:
            continue
    try:
        snapshots = get_latest_snapshots_for_symbol(symbol, expiry)
        for snap in snapshots:
            if (abs(snap.get("strike", 0) - strike) < 0.01 and
                snap.get("option_type") == option_type):
                return float(snap.get("ltp") or 0.0)
    except Exception:
        pass
    return None

def _is_reversal_against_open_trade(open_trade: dict, verdict: str, confidence: int) -> bool:
    if confidence < 70:
        return False
    ot = str(open_trade.get("option_type") or "").upper()
    side = open_trade.get("side") or "BUY"
    if ot == "CE" and side == "BUY" and is_bearish_verdict(verdict):
        return True
    if ot == "PE" and side == "BUY" and is_bullish_verdict(verdict):
        return True
    if ot == "CE" and side == "SELL" and is_bullish_verdict(verdict):
        return True
    if ot == "PE" and side == "SELL" and is_bearish_verdict(verdict):
        return True
    return False

def _parse_verdict_and_confidence(intel_text: str) -> tuple[str, int]:
    verdict = ""
    confidence = 0
    m_v = re.search(r"\*Verdict:\s*([^\*]+)\*", intel_text or "")
    if m_v:
        verdict = m_v.group(1).strip()
    m_c = re.search(r"Confidence:\s*(\d+)%", intel_text or "")
    if m_c:
        confidence = int(m_c.group(1))
    return verdict, confidence

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

    if option_type == "FUT":
        entry_premium = float(plan["entry_underlying"])
        sl_premium = float(plan["sl_underlying"])
        target_premium = float(plan["target_underlying"])
    else:
        entry_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows)
        if entry_premium is None or entry_premium <= 0:
            log.warning("%s: failed to resolve option premium for strike %g, type %s", symbol, strike, option_type)
            return None
        
        # Calculate premium-based SL/Target
        if side == "SELL":
            sl_premium = round(entry_premium * 1.50, 2)
            target_premium = round(entry_premium * 0.60, 2)
        else:
            sl_premium = round(entry_premium * 0.70, 2)
            target_premium = round(entry_premium * 1.50, 2)

    plan["entry_premium"] = entry_premium
    plan["sl_premium"] = sl_premium
    plan["target_premium"] = target_premium
    return plan

def check_live_risk_limits(symbol: str, setup_type: str | None = None) -> tuple[bool, str]:
    config = load_runtime_config()
    max_concurrent = int(config.get("live_max_concurrent_positions") or 2)
    
    # Check open positions count
    import sqlite3
    from src.models.schema import get_conn
    with get_conn() as conn:
        open_count = conn.execute("SELECT COUNT(*) AS c FROM live_trades WHERE status='OPEN'").fetchone()["c"]
        if open_count >= max_concurrent:
            return False, f"Max concurrent live positions reached ({open_count}/{max_concurrent})"
        
        if setup_type != 'TIMEFRAME':
            # Max 1 open per symbol
            symbol_open = conn.execute("SELECT COUNT(*) AS c FROM live_trades WHERE symbol=? AND status='OPEN'", (symbol,)).fetchone()["c"]
            if symbol_open >= 1:
                return False, f"Already have an open live trade for {symbol}"
                
    return True, "Risk limits OK"


def _resolve_trade_quantity(symbol: str, lots: int, resolved: dict | None) -> int:
    lot_multiplier = (resolved or {}).get("lot_size") or LOT_SIZES.get(symbol, 1)
    return int(lots * lot_multiplier)


def _reject_fallback_instrument(symbol: str, resolved: dict | None, shadow_mode: bool) -> str | None:
    if shadow_mode:
        return None
    if not resolved or not resolved.get("tradingsymbol"):
        return "Failed to resolve Kite tradingsymbol"
    if not resolved.get("instrument_token"):
        return f"Kite instrument cache miss for {symbol}; refusing live broker order on fallback tradingsymbol"
    return None

def place_kite_order(kite, symbol: str, exchange: str, tradingsymbol: str, transaction_type: str, quantity: int, shadow_mode: bool) -> str:
    if shadow_mode:
        import uuid
        sh_id = f"sh-ord-{uuid.uuid4().hex[:8]}"
        log.info("[SHADOW] Suppressed order placement for %s:%s Qty=%d, generated ID: %s", exchange, tradingsymbol, quantity, sh_id)
        return sh_id
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_MARKET
        )
        return order_id
    except Exception as e:
        log.error("Kite order placement failed: %s", e)
        raise e

def place_kite_gtt(kite, symbol: str, exchange: str, tradingsymbol: str, transaction_type: str, quantity: int, trigger_values: list[float], limit_prices: list[float], last_price: float, shadow_mode: bool) -> str:
    if shadow_mode:
        import uuid
        sh_id = f"sh-gtt-{uuid.uuid4().hex[:8]}"
        log.info("[SHADOW] Suppressed GTT placement for %s:%s Qty=%d, generated ID: %s", exchange, tradingsymbol, quantity, sh_id)
        return sh_id
    try:
        gtt_id = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_OCO,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            trigger_values=trigger_values,
            last_price=last_price,
            orders=[
                {
                    "transaction_type": transaction_type,
                    "quantity": quantity,
                    "product": kite.PRODUCT_NRML,
                    "order_type": kite.ORDER_TYPE_LIMIT,
                    "price": limit_prices[0]
                },
                {
                    "transaction_type": transaction_type,
                    "quantity": quantity,
                    "product": kite.PRODUCT_NRML,
                    "order_type": kite.ORDER_TYPE_LIMIT,
                    "price": limit_prices[1]
                }
            ]
        )
        return gtt_id
    except Exception as e:
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
    sym = symbol.upper()
    if sym.startswith("NATURALGAS"): return "NATURALGAS"
    if sym.startswith("NIFTY"): return "NIFTY"
    if sym.startswith("BANKNIFTY"): return "BANKNIFTY"
    if sym.startswith("CRUDEOIL"): return "CRUDEOIL"
    if sym.startswith("GOLD"): return "GOLD"
    if sym.startswith("MCX"): return "MCX"
    import re
    m = re.match(r"^[A-Z]+", sym)
    return m.group(0) if m else sym

def _run_live_trading_legacy(symbol: str, scan_context: dict, digest_id: str, intel: dict) -> dict | None:
    if not _is_market_open(symbol):
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    config = load_runtime_config()
    shadow_mode = config.get("live_shadow_mode", True)
    
    # Initialize Kite Client
    kite = get_kite_client()
    if not kite and not shadow_mode:
        log.warning("Live trading skipped: Zerodha credentials / access token invalid or not logged in.")
        return {"action": "BLOCKED_AUTH", "reason": "Kite client not initialized"}

    now_iso = datetime.now(timezone.utc).isoformat()
    underlying = float(scan_context.get("underlying") or 0.0)
    expiry = scan_context.get("expiry", "")
    option_rows = scan_context.get("option_rows") or []

    intel_text = intel.get("telegram_text") or ""
    verdict, confidence = _parse_verdict_and_confidence(intel_text)

    # 1. Manage existing live trades
    current_open_trade = get_open_live_trade(symbol)
    if current_open_trade:
        # Check Trend Reversal
        if _is_reversal_against_open_trade(current_open_trade, verdict, confidence):
            log.info("%s: live trade reversed! Initiating market square-off...", symbol)
            exit_premium = None
            if current_open_trade["option_type"] != "FUT":
                exit_premium = _get_option_premium(symbol, current_open_trade["expiry"], current_open_trade["strike"], current_open_trade["option_type"], option_rows)
            
            # Place square-off order
            exit_side = "SELL" if current_open_trade["side"] == "BUY" else "BUY"
            exchange = _get_exchange(symbol)
            resolved = resolve_instrument(symbol, current_open_trade["expiry"], current_open_trade["strike"], current_open_trade["option_type"])
            tradingsymbol = resolved["tradingsymbol"] if resolved else symbol
            quantity = _resolve_trade_quantity(symbol, int(current_open_trade.get("lots") or 1), resolved)
            
            try:
                place_kite_order(kite, symbol, exchange, tradingsymbol, exit_side, quantity, shadow_mode)
                if current_open_trade.get("gtt_order_id"):
                    cancel_kite_gtt(kite, current_open_trade["gtt_order_id"], shadow_mode)
                
                close_live_trade(
                    current_open_trade["id"],
                    now_iso,
                    underlying,
                    exit_premium,
                    "CLOSED_REVERSAL" if not shadow_mode else "CLOSED_SHADOW",
                    f"Trend reversed against position (verdict: {verdict})"
                )
                
                closed_trade = None
                from src.models.schema import get_conn
                with get_conn() as conn:
                    row = conn.execute("SELECT * FROM live_trades WHERE id=?", (current_open_trade["id"],)).fetchone()
                    if row:
                        closed_trade = dict(row)

                from src.alerts.telegram_dispatcher import send_text
                prefix = "[SHADOW]" if shadow_mode else "🚨 [LIVE]"
                send_text(f"{prefix} **Trend Reversal Square-Off** | Closed `{symbol}` `{current_open_trade['option_type']}` position at underlying `{underlying}`.")
                return {"action": "CLOSED", "trade": closed_trade, "reason": "reversal"}
            except Exception as e:
                log.error("Failed to square-off reversed position: %s", e)
                return {"action": "ERROR", "reason": f"reversal square-off failed: {e}"}

        # Check Premium-polling Fallback (if GTT failed or in shadow mode / fallback exit)
        if current_open_trade.get("exit_mode") == "POLL" or shadow_mode or current_open_trade.get("option_type") == "FUT":
            exit_premium = None
            if current_open_trade["option_type"] != "FUT":
                exit_premium = _get_option_premium(symbol, current_open_trade["expiry"], current_open_trade["strike"], current_open_trade["option_type"], option_rows)
            else:
                exit_premium = underlying
                
            if exit_premium is not None:
                sl_premium = float(current_open_trade.get("sl_premium") or 0.0)
                target_premium = float(current_open_trade.get("target_premium") or 0.0)
                is_sell = (current_open_trade["side"] == "SELL")
                
                triggered = False
                close_status = ""
                close_reason = ""
                
                if is_sell:
                    if exit_premium >= sl_premium:
                        triggered, close_status, close_reason = True, "CLOSED_SL", "stop loss hit"
                    elif exit_premium <= target_premium:
                        triggered, close_status, close_reason = True, "CLOSED_TARGET", "target hit"
                else:
                    if exit_premium <= sl_premium:
                        triggered, close_status, close_reason = True, "CLOSED_SL", "stop loss hit"
                    elif exit_premium >= target_premium:
                        triggered, close_status, close_reason = True, "CLOSED_TARGET", "target hit"
                        
                if triggered:
                    exit_side = "SELL" if current_open_trade["side"] == "BUY" else "BUY"
                    exchange = _get_exchange(symbol)
                    resolved = resolve_instrument(symbol, current_open_trade["expiry"], current_open_trade["strike"], current_open_trade["option_type"])
                    tradingsymbol = resolved["tradingsymbol"] if resolved else symbol
                    quantity = _resolve_trade_quantity(symbol, int(current_open_trade.get("lots") or 1), resolved)
                    
                    try:
                        place_kite_order(kite, symbol, exchange, tradingsymbol, exit_side, quantity, shadow_mode)
                        close_live_trade(current_open_trade["id"], now_iso, underlying, exit_premium, close_status if not shadow_mode else "CLOSED_SHADOW", close_reason)
                        
                        closed_trade = None
                        from src.models.schema import get_conn
                        with get_conn() as conn:
                            row = conn.execute("SELECT * FROM live_trades WHERE id=?", (current_open_trade["id"],)).fetchone()
                            if row:
                                closed_trade = dict(row)

                        from src.alerts.telegram_dispatcher import send_text
                        prefix = "[SHADOW]" if shadow_mode else "🚨 [LIVE]"
                        send_text(f"{prefix} **Fallback Poll Exit** | Closed `{symbol}` `{current_open_trade['option_type']}` — `{close_reason}` at premium `{exit_premium}`.")
                        return {"action": "CLOSED", "trade": closed_trade, "reason": close_reason}
                    except Exception as e:
                        log.error("Failed fallback exit square-off: %s", e)
                        
        return {"action": "HELD", "trade": current_open_trade}

    broker_conf = get_broker_config()
    if broker_conf and broker_conf.get("kill_switch_active"):
        log.warning("Live trading skipped: Kill Switch is active!")
        return {"action": "BLOCKED_KILL_SWITCH", "reason": "Kill Switch active"}

    # 2. Evaluate new live entry
    base_sym = _get_base_symbol(symbol)
    enabled_symbols = config.get("live_enabled_broker_symbols")
    if enabled_symbols is not None and base_sym not in enabled_symbols:
        log.info("%s: Live trading is disabled in settings for %s. Skipping new entry.", symbol, base_sym)
        return {"action": "BLOCKED_DISABLED_SYMBOL", "reason": f"Live trading disabled for {base_sym}"}

    ctx = {**(scan_context or {}), "symbol": symbol, "expiry": expiry, "option_rows": option_rows}
    decision = make_trade_decision(symbol, intel, ctx)
    if decision["status"] == "BLOCKED":
        return {"action": "BLOCKED_DECISION", "reason": decision["reason"]}

    risk_ok, risk_reason = check_live_risk_limits(symbol, decision.get("setup_type"))
    if not risk_ok:
        log.info("%s: live trade blocked by risk engine — %s", symbol, risk_reason)
        return {"action": "BLOCKED_RISK", "reason": risk_reason}

    plan = _trade_plan_from_verdict(verdict, confidence, ctx)
    if not plan:
        return {"action": "BLOCKED_PLAN", "reason": "No valid trade plan"}

    entry_premium = plan["entry_premium"]
    lots = calculate_trade_lots(symbol, entry_premium)
    scores = decision.get("scores") or {}

    # Signal deduplication key
    today_date = datetime.now(IST).strftime("%Y%m%d")
    option_type_key = plan.get("option_type", "")
    strike_key = int(plan.get("strike") or 0)
    signal_key = f"{symbol}:{option_type_key}:{strike_key}:{verdict}:{today_date}:live"

    exchange = _get_exchange(symbol)
    resolved = resolve_instrument(symbol, expiry, plan["strike"], plan["option_type"])
    if not resolved or not resolved.get("tradingsymbol"):
        log.error("%s: failed to resolve Kite tradingsymbol, skipping live entry", symbol)
        return {"action": "BLOCKED_SYMBOL", "reason": "Failed to resolve Kite tradingsymbol"}

    tradingsymbol = resolved["tradingsymbol"]
    lot_multiplier = resolved.get("lot_size") or LOT_SIZES.get(symbol, 1)
    quantity = lots * lot_multiplier

    # Place Order on Kite
    try:
        order_id = place_kite_order(kite, symbol, exchange, tradingsymbol, plan["side"], quantity, shadow_mode)
    except Exception as e:
        log.error("%s: failed to place live order, skipping DB entry", symbol)
        return {"action": "BLOCKED_ORDER_FAILED", "reason": str(e)}

    # Place GTT target/SL Leg
    gtt_order_id = None
    exit_mode = "GTT"
    if plan["option_type"] != "FUT":
        try:
            # target/SL triggers
            sl_trigger = float(plan["sl_premium"])
            target_trigger = float(plan["target_premium"])
            # limit prices (usually offset slightly to ensure execution)
            sl_limit = round(sl_trigger * 0.95, 2) if plan["side"] == "BUY" else round(sl_trigger * 1.05, 2)
            target_limit = round(target_trigger * 0.95, 2) if plan["side"] == "BUY" else round(target_trigger * 1.05, 2)
            
            gtt_order_id = place_kite_gtt(
                kite, symbol, exchange, tradingsymbol,
                "SELL" if plan["side"] == "BUY" else "BUY",
                quantity,
                [sl_trigger, target_trigger],
                [sl_limit, target_limit],
                entry_premium,
                shadow_mode
            )
        except Exception as e:
            log.error("%s: GTT placement failed, switching to POLL exit fallback: %s", symbol, e)
            exit_mode = "POLL"
            from src.alerts.telegram_dispatcher import send_text
            send_text(f"⚠️ **[GTT FAILED]** `{symbol}` — GTT creation failed ({e}); falling back to premium-poll exit.")

    trade_data = {
        "opened_at":             now_iso,
        "symbol":                symbol,
        "expiry":                expiry,
        "verdict_label":         plan["verdict_label"],
        "side":                  plan.get("side", "BUY"),
        "option_type":           plan["option_type"],
        "strike":                plan["strike"],
        "entry_underlying":      plan["entry_underlying"],
        "entry_premium":         entry_premium,
        "sl_underlying":         plan["sl_underlying"],
        "sl_premium":            plan["sl_premium"],
        "target_underlying":     plan["target_underlying"],
        "target_premium":        plan["target_premium"],
        "lots":                  lots,
        "status":                "OPEN",
        "reason":                f"auto-live | {decision['reason']}",
        "digest_id":             digest_id,
        "trade_status":          decision["status"] if not shadow_mode else "SHADOW",
        "setup_type":            decision["setup_type"],
        "decision_reason":       decision["reason"],
        "confidence_score":      scores.get("confidence"),
        "entry_quality_score":   scores.get("entry_quality"),
        "trend_alignment_score": scores.get("trend_alignment"),
        "regime_score":          scores.get("regime_score"),
        "signal_key":            signal_key,
        "broker_order_id":       order_id,
        "gtt_order_id":          gtt_order_id,
        "broker_status":         "COMPLETE" if not shadow_mode else "SHADOW",
        "broker_message":        "Shadow trade executed" if shadow_mode else "Executed on Kite Connect",
        "exit_mode":             exit_mode
    }

    inserted_id = insert_live_trade(trade_data)
    if not inserted_id:
        log.warning("%s: live trade INSERT skipped — duplicate signal_key=%s", symbol, signal_key)
        return {"action": "DEDUP_SKIPPED", "reason": "duplicate signal key"}

    # Notify Telegram
    from src.alerts.telegram_dispatcher import send_text
    prefix = "[SHADOW]" if shadow_mode else "🟢 [LIVE]"
    send_text(f"{prefix} **Order Placed** | `{plan['side']}` `{symbol}` `{plan['option_type']}` Strike `{plan['strike']}`. Entry `{entry_premium}`, SL `{plan['sl_premium']}`, Target `{plan['target_premium']}`. Lots: `{lots}` (Qty: `{quantity}`).")

    return {
        "action":     "EXECUTED",
        "trade":      trade_data,
        "setup_type": decision["setup_type"],
        "lots":       lots
    }


def _latest_live_trade(trade_id: int) -> dict | None:
    from src.models.schema import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM live_trades WHERE id=?", (trade_id,)).fetchone()
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
    resolved = resolve_instrument(symbol, trade.get("expiry") or "", trade.get("strike") or 0.0, trade.get("option_type") or "FUT")
    reject_reason = _reject_fallback_instrument(symbol, resolved, shadow_mode)
    if reject_reason:
        raise RuntimeError(reject_reason)
    tradingsymbol = resolved["tradingsymbol"] if resolved and resolved.get("tradingsymbol") else symbol
    quantity = _resolve_trade_quantity(symbol, int(trade.get("lots") or 1), resolved)

    place_kite_order(kite, symbol, exchange, tradingsymbol, exit_side, quantity, shadow_mode)
    if trade.get("gtt_order_id"):
        cancel_kite_gtt(kite, trade["gtt_order_id"], shadow_mode)

    close_live_trade(
        trade["id"],
        now_iso,
        underlying,
        exit_premium,
        status if not shadow_mode else "CLOSED_SHADOW",
        reason,
    )
    return _latest_live_trade(trade["id"]) or trade


def run_live_trading(symbol: str, scan_context: dict, digest_id: str, intel: dict) -> dict | None:
    if not _is_market_open(symbol):
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    config = load_runtime_config()
    shadow_mode = config.get("live_shadow_mode", True)
    kite = get_kite_client()
    if not kite and not shadow_mode:
        log.warning("Live trading skipped: Zerodha credentials / access token invalid or not logged in.")
        return {"action": "BLOCKED_AUTH", "reason": "Kite client not initialized"}

    now_iso = datetime.now(timezone.utc).isoformat()
    scan_context = scan_context or {}
    underlying = float(scan_context.get("underlying") or 0.0)
    expiry = scan_context.get("expiry", "")
    option_rows = scan_context.get("option_rows") or []
    verdict, confidence = _parse_verdict_and_confidence(intel.get("telegram_text") or "")

    current_open_trade = get_open_live_trade(symbol)
    if current_open_trade:
        if _is_reversal_against_open_trade(current_open_trade, verdict, confidence):
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

        if current_open_trade.get("exit_mode") == "POLL" or shadow_mode or current_open_trade.get("option_type") == "FUT":
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
                        return {"action": "CLOSED", "trade": closed, "reason": close_reason}
                    except Exception as e:
                        log.error("Failed fallback exit square-off: %s", e)
                        return {"action": "ERROR", "reason": f"poll square-off failed: {e}"}

        return {"action": "HELD", "trade": current_open_trade}

    broker_conf = get_broker_config()
    if broker_conf and broker_conf.get("kill_switch_active"):
        log.warning("Live trading skipped: Kill Switch is active!")
        return {"action": "BLOCKED_KILL_SWITCH", "reason": "Kill Switch active"}

    base_sym = _get_base_symbol(symbol)
    enabled_symbols = config.get("live_enabled_broker_symbols")
    if enabled_symbols is not None and base_sym not in enabled_symbols:
        return {"action": "BLOCKED_DISABLED_SYMBOL", "reason": f"Live trading disabled for {base_sym}"}

    ctx = {**scan_context, "symbol": symbol, "expiry": expiry, "option_rows": option_rows}
    decision = make_trade_decision(symbol, intel, ctx)
    if decision["status"] == "BLOCKED":
        return {"action": "BLOCKED_DECISION", "reason": decision["reason"]}

    risk_ok, risk_reason = check_live_risk_limits(symbol, decision.get("setup_type"))
    if not risk_ok:
        return {"action": "BLOCKED_RISK", "reason": risk_reason}

    plan = _trade_plan_from_verdict(verdict, confidence, ctx)
    if not plan:
        return {"action": "BLOCKED_PLAN", "reason": "No valid trade plan"}

    entry_premium = plan["entry_premium"]
    lots = calculate_trade_lots(symbol, entry_premium)
    today_date = datetime.now(IST).strftime("%Y%m%d")
    signal_key = f"{symbol}:{plan.get('option_type', '')}:{int(plan.get('strike') or 0)}:{verdict}:{today_date}:live"
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
        "broker_message": "Shadow trade pending" if shadow_mode else "Pending broker entry",
        "exit_mode": exit_mode,
    }

    inserted_id = insert_live_trade(trade_data)
    if not inserted_id:
        log.warning("%s: live trade INSERT skipped - duplicate signal_key=%s", symbol, signal_key)
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
        order_id = place_kite_order(kite, symbol, exchange, tradingsymbol, plan["side"], quantity, shadow_mode)
    except Exception as e:
        update_live_trade_entry(
            inserted_id,
            status="REJECTED",
            broker_status="REJECTED",
            broker_message=str(e),
            reason=f"Entry order failed: {e}",
        )
        return {"action": "BLOCKED_ORDER_FAILED", "reason": str(e)}

    gtt_order_id = None
    if plan["option_type"] != "FUT":
        try:
            sl_trigger = float(plan["sl_premium"])
            target_trigger = float(plan["target_premium"])
            sl_limit = round(sl_trigger * 0.95, 2) if plan["side"] == "BUY" else round(sl_trigger * 1.05, 2)
            target_limit = round(target_trigger * 0.95, 2) if plan["side"] == "BUY" else round(target_trigger * 1.05, 2)
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
            send_text(f"[GTT FAILED] `{symbol}` - {e}; falling back to premium-poll exit.")

    broker_status = "SHADOW" if shadow_mode else "COMPLETE"
    broker_message = "Shadow trade executed" if shadow_mode else "Executed on Kite Connect"
    update_live_trade_entry(
        inserted_id,
        broker_order_id=order_id,
        gtt_order_id=gtt_order_id,
        broker_status=broker_status,
        broker_message=broker_message,
        exit_mode=exit_mode,
    )
    trade_data.update({
        "broker_order_id": order_id,
        "gtt_order_id": gtt_order_id,
        "broker_status": broker_status,
        "broker_message": broker_message,
        "exit_mode": exit_mode,
    })

    from src.alerts.telegram_dispatcher import send_text
    prefix = "[SHADOW]" if shadow_mode else "[LIVE]"
    send_text(
        f"{prefix} **Order Placed** | `{plan['side']}` `{symbol}` `{plan['option_type']}` "
        f"Strike `{plan['strike']}`. Entry `{entry_premium}`, SL `{plan['sl_premium']}`, "
        f"Target `{plan['target_premium']}`. Lots: `{lots}` (Qty: `{quantity}`)."
    )
    return {"action": "EXECUTED", "trade": trade_data, "setup_type": decision["setup_type"], "lots": lots}


def run_live_timeframe_strategy(symbol: str, scan_context: dict, digest_id: str, intel: dict) -> dict | None:
    # Timeframe breakout live trading is structured similarly but using candle crossovers.
    # For simplicity, Phase 1 details option chain execution. We can expand this similarly as needed.
    return None
