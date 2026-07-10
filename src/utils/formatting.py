"""Formatting helpers shared across modules."""
from __future__ import annotations


def safe_num(val, default=0.0) -> float:
    """Safely convert numeric-ish values, including comma-formatted strings."""
    try:
        if val is None:
            return default
        if isinstance(val, str):
            s = val.replace(",", "").strip()
            if not s or s in {"—", "-", "--", "NA", "N/A", "null", "None"}:
                return default
            val = s
        n = float(val)
        return n if n == n else default
    except (TypeError, ValueError):
        return default


def fmt_oi(n: int | float | str | None, is_change: bool = False) -> str:
    """Format OI/volume with Cr/L/K suffixes (NSE standard).
    
    BUG-M11 FIX: Added `is_change` parameter to distinguish OI change from absolute OI.
    OI change values display with +/- sign prefix; absolute OI always shows unsigned magnitude.
    Absolute OI should never be negative — if it is, it's likely a data error or actually
    an OI change value being passed incorrectly.
    """
    n = safe_num(n, 0)
    an = abs(n)
    
    # BUG-M11: For absolute OI, ensure we never display a negative value
    # (OI itself is always non-negative; negative indicates OI change)
    display_n = n if is_change else an
    
    sign = ""
    if is_change:
        sign = "+" if n >= 0 else "-"
    
    if an >= 1e7: return f"{sign}{an/1e7:.2f}Cr"
    if an >= 1e5: return f"{sign}{an/1e5:.2f}L"
    if an >= 1e3: return f"{sign}{an/1e3:.1f}K"
    return f"{sign}{int(an)}"


def fmt_pct(n: float | None) -> str:
    """Format percentages with sign and 1 decimal place."""
    if n is None: return "0.0%"
    return f"{n:+.1f}%"


def fmt_int(n: int | float | str | None) -> str:
    """Format whole counts without suffix, safe cast."""
    n = safe_num(n, 0)
    return str(int(n)) if float(n).is_integer() else f"{n:.1f}"
