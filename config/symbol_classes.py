"""
Symbol class classifier — maps symbol name to asset class and market window.
Classes: NSE_INDEX | NSE_STOCK | MCX_COMMODITY | MCX_AGRI
"""
import re
from config.settings import MARKET_WINDOWS

_MCX_ENERGY   = re.compile(r"NATURALGAS|CRUDEOIL|CRUDE|NATGAS|NG\b", re.I)
_MCX_METAL    = re.compile(r"\bGOLD\b|\bSILVER\b|\bCOPPER\b|\bZINC\b|\bLEAD\b|\bALUMINIUM\b|\bNICKEL\b", re.I)
_MCX_AGRI     = re.compile(r"COTTON|MENTHA|CASTOR|KAPAS|GUAR|TURMERIC|JEERA|CORIANDER|CPO|SOYA|PALM", re.I)
_NSE_INDEX    = re.compile(r"NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY|SENSEX|BANKEX", re.I)


def classify(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if _NSE_INDEX.search(s):
        return "NSE_INDEX"
    if _MCX_ENERGY.search(s):
        return "MCX_COMMODITY"
    if _MCX_METAL.search(s):
        return "MCX_COMMODITY"
    if _MCX_AGRI.search(s):
        return "MCX_AGRI"
    return "NSE_STOCK"


def market_window(symbol: str) -> tuple[str, str, list[int]]:
    """Return (open_hhmm, close_hhmm, weekdays) for the symbol's class."""
    cls = classify(symbol)
    return MARKET_WINDOWS.get(cls, MARKET_WINDOWS["NSE_INDEX"])
