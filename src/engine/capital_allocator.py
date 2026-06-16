import logging
from config.settings import LOT_SIZES
from config.runtime_config import load_runtime_config

log = logging.getLogger("nsebot.capital_allocator")

def calculate_trade_lots(symbol: str, entry_premium: float) -> int:
    """
    Calculate the number of lots to trade for a symbol based on settings and premium.
    """
    config = load_runtime_config()
    
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
    
    calculated = int(capital_per_trade // (entry_premium * instrument_lot_size))
    lots = max(1, calculated)
    log.info("%s: auto-calculated %d lots (capital: %g, premium: %g, lot_size: %d)",
             symbol, lots, capital_per_trade, entry_premium, instrument_lot_size)
    return lots
