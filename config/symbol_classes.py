"""
Symbol class metadata — maps each watched symbol to its exchange class,
strike step size, and market window key.
"""
from __future__ import annotations

from config.settings import MARKET_WINDOWS

# symbol → (class_key, strike_step_points)
# strike_step_points is used for cluster-dedup width calculation.
_SYMBOL_META: dict[str, tuple[str, int]] = {
    "NIFTY":       ("NSE_INDEX", 50),
    "BANKNIFTY":   ("NSE_INDEX", 100),
    "FINNIFTY":    ("NSE_INDEX", 50),
    "MIDCPNIFTY":  ("NSE_INDEX", 25),
    "NATURALGAS":  ("MCX_COMMODITY", 5),
    "CRUDEOIL":    ("MCX_COMMODITY", 100),
    "GOLD":        ("MCX_COMMODITY", 100),
    "SILVER":      ("MCX_COMMODITY", 500),
}

_DEFAULT_CLASS       = "NSE_INDEX"
_DEFAULT_STRIKE_STEP = 50


def _base_symbol(symbol: str) -> str:
    """Normalize expiry/month variants like 'NATURALGAS MAY FUT'."""
    return str(symbol or "").upper().strip().split()[0]


def get_symbol_class(symbol: str) -> str:
    """Return the market-window class key for a symbol."""
    return _SYMBOL_META.get(_base_symbol(symbol), (_DEFAULT_CLASS, _DEFAULT_STRIKE_STEP))[0]


def get_strike_step(symbol: str) -> int:
    """
    Return the canonical strike step (in points) for a symbol.
    Used by dedup cluster-width logic so suppression radius is
    expressed in number-of-strikes, not raw points.
    Example: NIFTY → 50, BANKNIFTY → 100
    """
    return _SYMBOL_META.get(_base_symbol(symbol), (_DEFAULT_CLASS, _DEFAULT_STRIKE_STEP))[1]


def market_window(symbol: str) -> tuple[str, str, list[int]]:
    """Return (open, close, weekdays) for the symbol's configured market class."""
    class_key = get_symbol_class(symbol)
    return MARKET_WINDOWS.get(class_key, MARKET_WINDOWS[_DEFAULT_CLASS])
