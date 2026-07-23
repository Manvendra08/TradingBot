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


def _base_symbol(symbol: str) -> str:
    return str(symbol or "").upper().strip().split()[0]


def _broker_mode_enabled(config: dict) -> bool:
    """Broker mode is on when order placement is not fully blocked."""
    return not bool(config.get("live_broker_disabled", False))


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
        log.debug("%s: BUY leg — skipping broker margin API", symbol)
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
            future = executor.submit(kite.order_margins, orders)
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


def _calculate_live_lots(
    symbol: str,
    entry_premium: float,
    side: str,
    config: dict,
    option_type: str | None = None,
    strike: float | None = None,
) -> int:
    """Live/broker lot sizing: per-symbol override or capital-based auto-calc."""
    base = _base_symbol(symbol)

    symbol_lots = config.get("live_symbol_lots") or {}
    if base in symbol_lots:
        lots = int(symbol_lots[base])
        log.debug("%s: using symbol-specific lot override of %d lots", base, lots)
        return max(1, lots)

    if not config.get("live_capital_per_trade_inr"):
        log.warning("%s: live_capital_per_trade_inr not set in runtime config, defaulting to ₹50,000", base)
    capital_per_trade = float(config.get("live_capital_per_trade_inr") or 50000.0)
    instrument_lot_size = LOT_SIZES.get(base, 1)

    if entry_premium <= 0:
        log.warning("%s: entry_premium <= 0, defaulting to 1 lot", base)
        return 1

    max_auto_lots = int(config.get("live_max_auto_lots") or _DEFAULT_MAX_AUTO_LOTS)

    if side.upper() == "SELL":
        # BUG-H01 FIX: Try broker margin API first for actual SPAN+exposure margin.
        # Falls back to static multiplier if API is unavailable or times out.
        broker_margin = None
        try:
            from src.engine.symbol_resolver import resolve_instrument
            expiry = config.get("_current_expiry")  # Set by caller if available
            if expiry:
                resolved_type = option_type if option_type in ("CE", "PE") else "FUT"
                resolved_strike = float(strike or 0.0)
                resolved = resolve_instrument(base, expiry, resolved_strike, resolved_type)
                if resolved and resolved.get("tradingsymbol"):
                    broker_margin = _fetch_broker_margin_requirement(
                        symbol=base,
                        tradingsymbol=resolved["tradingsymbol"],
                        exchange=resolved.get("exchange", "NFO"),
                        transaction_type="SELL",
                        quantity=instrument_lot_size,
                        premium=entry_premium,
                    )
        except Exception as e:
            log.debug("%s: broker margin lookup failed: %s", base, e)

        if broker_margin and broker_margin > 0:
            effective_cost_per_lot = broker_margin
            log.debug(
                "%s: SELL leg — using broker margin API: ₹%.2f per lot",
                base, effective_cost_per_lot,
            )
        else:
            effective_cost_per_lot = (
                entry_premium * instrument_lot_size * _SELL_MARGIN_PREMIUM_MULTIPLIER
            )
            log.debug(
                "%s: SELL leg — margin-adjusted cost/lot: %.2f "
                "(premium=%.2f * lot_size=%d * margin_mult=%.1f) [static fallback]",
                base, effective_cost_per_lot,
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
            base, calculated, max_auto_lots,
        )

    log.info(
        "%s: auto-calculated %d lots (capital: %g, premium: %g, lot_size: %d, "
        "side: %s, effective_cost/lot: %g, cap: %d)",
        base, lots, capital_per_trade, entry_premium, instrument_lot_size,
        side, effective_cost_per_lot, max_auto_lots,
    )
    return lots


def calculate_trade_lots(
    symbol: str,
    entry_premium: float,
    side: str = "BUY",
    is_paper: bool = False,
    pyramid_level: int = 1,
    setup_type: str | None = None,
    tranche_index: int = 0,
    option_type: str | None = None,
    strike: float | None = None,
) -> int:
    """
    Calculate the number of lots to trade for a symbol based on settings and premium.

    Paper trades (is_paper=True):
      - Broker mode ON (live_broker_disabled=False): mirror live_symbol_lots / live auto-calc.
      - Broker mode OFF: paper_symbol_lots per symbol, else global paper_lots (default 10).

    Live trades:
      - live_symbol_lots override, else capital-based auto-calc.
      
    Pyramiding (pyramid_level > 1):
      - Scale down lots for subsequent entries (50% for level 2, 25% for level 3+).

    TFSS Tranche Sizing (setup_type="TFSS"):
      - Scale down lots per tranche index using TRANCHE_SEQUENCE (50%, 30%, 20%).
    """
    config = load_runtime_config()
    base = _base_symbol(symbol)

    if is_paper:
        paper_symbol_lots = config.get("paper_symbol_lots") or {}
        if base in paper_symbol_lots:
            lots = max(1, int(paper_symbol_lots[base]))
            log.debug("%s: paper trade — using paper_symbol_lots=%d", base, lots)
        else:
            lots = max(1, int(config.get("paper_lots") or 10))
            log.debug("%s: paper trade — using global paper_lots=%d", base, lots)
    else:
        lots = _calculate_live_lots(base, entry_premium, side, config, option_type, strike)

    # Pyramiding Sizing (Flaw #10): Reduce size on scaling in
    original_lots = lots
    if setup_type and "TFSS" in str(setup_type).upper():
        try:
            from config.trend_following_short_strangle import TRANCHE_SEQUENCE
            idx = min(max(0, tranche_index), len(TRANCHE_SEQUENCE) - 1)
            scale = TRANCHE_SEQUENCE[idx]
            lots = max(1, int(lots * scale))
            if lots == original_lots:
                log.warning("%s: TFSS tranche index %d scale-down (%.0f%%) had no effect because lot count is %d", base, tranche_index, scale * 100, lots)
            else:
                log.info("%s: TFSS tranche index %d — scaling lot size to %d (%.0f%%)", base, tranche_index, lots, scale * 100)
        except Exception as exc:
            log.warning("%s: TFSS tranche lot scaling failed: %s", base, exc)
    elif pyramid_level == 2:
        lots = max(1, int(lots * 0.5))
        if lots == original_lots:
            log.warning("%s: Pyramiding level 2 scale-down to 50%% had no effect because lot count is %d", base, lots)
        else:
            log.info("%s: Pyramiding level 2 — scaling lot size to %d (50%%)", base, lots)
    elif pyramid_level >= 3:
        lots = max(1, int(lots * 0.25))
        if lots == original_lots:
            log.warning("%s: Pyramiding level %d scale-down to 25%% had no effect because lot count is %d", base, pyramid_level, lots)
        else:
            log.info("%s: Pyramiding level %d — scaling lot size to %d (25%%)", base, pyramid_level, lots)
 
    return lots
