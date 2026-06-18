import logging
from config.settings import LOT_SIZES
from config.runtime_config import load_runtime_config

log = logging.getLogger("nsebot.capital_allocator")

# Safety ceiling: auto-calculated lots will never exceed this value.
# Prevents runaway sizing on deep-OTM / very-low-premium options where
# (capital / premium) blows up to absurd lot counts.
_DEFAULT_MAX_AUTO_LOTS = 10

# FIX #4: Approximate SPAN+exposure margin multiplier for SELL legs.
# Index/commodity option selling requires full SPAN+exposure margin, which
# is typically ~10-12x the premium collected.  Using 10x as a conservative
# lower bound.  If the broker API exposes live margin data, prefer that.
_SELL_MARGIN_PREMIUM_MULTIPLIER = 10.0


def calculate_trade_lots(symbol: str, entry_premium: float, side: str = "BUY") -> int:
    """
    Calculate the number of lots to trade for a symbol based on settings and premium.

    Priority order:
      1. Symbol-specific override in runtime config (live_symbol_lots).
      2. Auto-calculate from capital_per_trade / effective_cost_per_lot,
         capped at max_auto_lots (default 10) to prevent blowup on cheap options.

    FIX #4: For SELL legs, effective_cost_per_lot uses an estimated margin
    (premium * lot_size * _SELL_MARGIN_PREMIUM_MULTIPLIER) instead of just
    premium * lot_size.  This prevents over-allocation where a 50k capital
    config would attempt to sell 10 lots needing 5-8L SPAN margin, resulting
    in rejected orders and phantom OPEN positions in the DB.
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
