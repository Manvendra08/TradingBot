import logging
from config.settings import LOT_SIZES
from config.runtime_config import load_runtime_config

log = logging.getLogger("nsebot.capital_allocator")

# Safety ceiling: auto-calculated lots will never exceed this value.
# Prevents runaway sizing on deep-OTM / very-low-premium options where
# (capital / premium) blows up to absurd lot counts.
_DEFAULT_MAX_AUTO_LOTS = 10


def calculate_trade_lots(symbol: str, entry_premium: float) -> int:
    """
    Calculate the number of lots to trade for a symbol based on settings and premium.

    Priority order:
      1. Symbol-specific override in runtime config (live_symbol_lots).
      2. Auto-calculate from capital_per_trade / (premium * lot_size),
         capped at max_auto_lots (default 10) to prevent blowup on cheap options.
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
    calculated = int(capital_per_trade // (entry_premium * instrument_lot_size))
    lots = min(max(1, calculated), max_auto_lots)

    if calculated > max_auto_lots:
        log.warning(
            "%s: auto-calc lots=%d exceeds cap=%d — clamped. "
            "Set live_max_auto_lots in runtime config to raise the ceiling intentionally.",
            symbol, calculated, max_auto_lots,
        )

    log.info(
        "%s: auto-calculated %d lots (capital: %g, premium: %g, lot_size: %d, cap: %d)",
        symbol, lots, capital_per_trade, entry_premium, instrument_lot_size, max_auto_lots,
    )
    return lots
