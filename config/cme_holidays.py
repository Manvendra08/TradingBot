"""
CME/NYMEX holiday dates and early closes (Energy complex).
Provides helper functions for time_guards to block trades when NYMEX is closed
or closing early, which kills price discovery for MCX Natural Gas.
"""

from datetime import date

# 2026 CME/NYMEX full-closure dates (energy complex)
CME_HOLIDAYS_2026: set[str] = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}

# 2026 CME/NYMEX early-close dates (energy floor early close)
CME_EARLY_CLOSE_2026: set[str] = {
    "2026-11-27", "2026-12-24",
}


def is_cme_closed(d: date) -> bool:
    """Return True if the CME is fully closed on the given date."""
    d_str = d.isoformat()
    return d_str in CME_HOLIDAYS_2026


def is_cme_early_close(d: date) -> bool:
    """Return True if the CME closes early on the given date."""
    d_str = d.isoformat()
    return d_str in CME_EARLY_CLOSE_2026
