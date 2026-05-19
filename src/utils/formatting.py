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


def fmt_oi(n: int | float | str | None) -> str:
    """Format OI/volume with Cr/L/K suffixes (NSE standard)."""
    n = safe_num(n, 0)
    an = abs(n)
    if an >= 1e7: return f"{n/1e7:.2f}Cr"
    if an >= 1e5: return f"{n/1e5:.2f}L"
    if an >= 1e3: return f"{n/1e3:.1f}K"
    return str(int(n))


def fmt_pct(n: float | None) -> str:
    """Format percentages with sign and 1 decimal place."""
    if n is None: return "0.0%"
    return f"{n:+.1f}%"


def fmt_int(n: int | float | str | None) -> str:
    """Format whole counts without suffix, safe cast."""
    n = safe_num(n, 0)
    return str(int(n)) if float(n).is_integer() else f"{n:.1f}"
