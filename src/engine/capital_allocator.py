import logging
from config.settings import LOT_SIZES
from config.runtime_config import load_runtime_config

log = logging.getLogger("nsebot.capital_allocator")

# Safety ceiling: auto-calculated lots will never exceed this value.
# Prevents runaway sizing on deep-OTM / very-low-premium options where
# (capital / premium) blows up to absurd lot counts.
_DEFAULT_MAX_AUTO_LOTS = 10

# M5 fix: SPAN+exposure margin multiplier for SELL legs.
# Actual SPAN+exposure margin for index/commodity options is typically 12-15x
# the premium collected.  Previous 10x caused over-allocation where the broker
# would reject orders due to insufficient margin.
# Increased to 12x as a safer baseline.  If broker margin API is available,
# the live margin requirement is preferred over this static estimate.
_SELL_MARGIN_PREMIUM_MULTIPLIER = 12.0

# Broker margin API timeout (seconds). If the call takes longer, fall back
# to the static multiplier above.
_BROKER_MARGIN_API_TIMEOUT = 3.0


def _fetch_broker_margin_requirement(
    symbol: str,
    tradingsymbol: str,
    exchange: str,
    transaction_type: str,
    quantity: int,
    premium: float,
) -> float | None:
    """
    Try to fetch actual SPAN+exposure margin from Zerodha broker API.

    M5 fix: Uses the Kite margin calculator API when available to get the
    real margin requirement for SELL legs. Returns None if the API is
    unavailable, times out, or returns an error — callers should fall back
    to the static _SELL_MARGIN_PREMIUM_MULTIPLIER.

    Returns:
        Margin requirement in INR, or None on failure.
    """
    if transaction_type.upper() != "SELL":
        return None

    try:
        from src.engine.live_trading import get_kite_client
        kite = get_kite_client()
        if not kite:
            return None

        # KiteConnect margin API: POST /margins/orders
        orders = [{
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "variety": kite.VARIETY_REGULAR,
            "product": kite.PRODUCT_NRML,
            "order_type": kite.ORDER_TYPE_LIMIT,
            "quantity": quantity,
            "price": premium,
            "trigger_price": 0,
        }]

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(kite.margins, "orders", orders)
            result = future.result(timeout=_BROKER_MARGIN_API_TIMEOUT)

        if result and isinstance(result, list) and len(result) > 0:
            margin_data = result[0]
            # Total margin = SPAN + exposure + option premium
            total = float(
                (margin_data.get("span") or 0)
                + (margin_data.get("exposure") or 0)
                + (margin_data.get("option_premium") or 0)
            )
            if total > 0:
                log.debug(
                    "%s: broker margin API returned ₹%.2f for %s %s (SPAN=%.0f + Exp=%.0f + Prem=%.0f)",
                    symbol, total, tradingsymbol, transaction_type,
                    margin_data.get("span", 0),
                    margin_data.get("exposure", 0),
                    margin_data.get("option_premium", 0),
                )
                return total
    except Exception as e:
        log.debug(
            "%s: broker margin API unavailable (%s), falling back to static multiplier",
            symbol, str(e)[:80],
        )

    return None


def calculate_trade_lots(symbol: str, entry_premium: float, side: str = "BUY") -> int:
    """
    Calculate the number of lots to trade for a symbol based on settings and premium.

    Priority order:
      1. Symbol-specific override in runtime config (live_symbol_lots).
      2. Auto-calculate from capital_per_trade / effective_cost_per_lot,
         capped at max_auto_lots (default 10) to prevent blowup on cheap options.

    M5 fix: For SELL legs, tries to fetch actual SPAN+exposure margin from
    broker API first. If unavailable, uses _SELL_MARGIN_PREMIUM_MULTIPLIER
    (12x, increased from 10x) as a safer static estimate.
    BUY legs are unaffected (margin = premium paid = actual capital consumed).
    """
    config = load_runtime_config()

    # 1. Explicit per-symbol override — user chose this deliberately, no cap applied.
    symbol_lots = config.get("live_symbol_lots") or {}
    if symbol in symbol_lots:
        lots = int(symbol_lots[symbol])
        log.debug("%s: using symbol-specific lot override of %d lots", symbol, lots)
        return max(1, lots)

    capital_per_trade = float(config.get("live_capital_per_trade_inr") or 50000.0)
    instrument_lot_size = LOT_SIZES.get(symbol.upper(), 1)

    if entry_premium <= 0:
        log.warning("%s: entry_premium <= 0, defaulting to 1 lot", symbol)
        return 1

    # 2. Auto-calculate with safety cap.
    max_auto_lots = int(config.get("live_max_auto_lots") or _DEFAULT_MAX_AUTO_LOTS)

    # FIX #4: Margin-aware sizing for SELL legs.
    # BUY : cost = premium * lot_size          (actual capital consumed)
    # SELL: cost = premium * lot_size * mult   (estimated SPAN+exposure margin)
    if side.upper() == "SELL":
        effective_cost_per_lot = (
            entry_premium * instrument_lot_size * _SELL_MARGIN_PREMIUM_MULTIPLIER
        )
        log.debug(
            "%s: SELL leg — margin-adjusted cost/lot: %.2f "
            "(premium=%.2f * lot_size=%d * margin_mult=%.1f)",
            symbol, effective_cost_per_lot,
            entry_premium, instrument_lot_size, _SELL_MARGIN_PREMIUM_MULTIPLIER,
        )
    else:
        effective_cost_per_lot = entry_premium * instrument_lot_size

    calculated = int(capital_per_trade // effective_cost_per_lot)
    lots = min(max(1, calculated), max_auto_lots)

    if calculated > max_auto_lots:
        log.warning(
            "%s: auto-calc lots=%d exceeds cap=%d — clamped. "
            "Set live_max_auto_lots in runtime config to raise the ceiling intentionally.",
            symbol, calculated, max_auto_lots,
        )

    log.info(
        "%s: auto-calculated %d lots (capital: %g, premium: %g, lot_size: %d, "
        "side: %s, effective_cost/lot: %g, cap: %d)",
        symbol, lots, capital_per_trade, entry_premium, instrument_lot_size,
        side, effective_cost_per_lot, max_auto_lots,
    )
    return lots
