"""
CME/NYMEX holiday dates and early closes (Energy complex).
Provides helper functions for time_guards to block trades when NYMEX is closed
or closing early, which kills price discovery for MCX Natural Gas.
"""

import logging
from datetime import date

log = logging.getLogger(__name__)

# Maximum year for which CME holiday data is configured.
# Update this and the holiday sets when new year data becomes available.
MAX_CONFIGURED_CME_YEAR = 2026

_WARNED_CME_YEARS = set()

# CME/NYMEX full-closure dates (energy complex)
CME_HOLIDAYS_2026: set[str] = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}

# CME/NYMEX early-close dates (energy floor early close)
CME_EARLY_CLOSE_2026: set[str] = {
    "2026-11-27", "2026-12-24",
}


def _check_cme_year_supported(d: date) -> bool:
    """Check if the CME holiday calendar supports the given year.
    Returns True if supported, False if year is beyond MAX_CONFIGURED_CME_YEAR.
    Logs a critical warning once per unsupported year."""
    if d.year > MAX_CONFIGURED_CME_YEAR:
        if d.year not in _WARNED_CME_YEARS:
            _WARNED_CME_YEARS.add(d.year)
            log.critical(
                "CME holiday calendar only configured up to year %d. Date %s (year %d) encountered; "
                "update config/cme_holidays.py with holiday data for year %d. "
                "Returning True (fail-closed) to prevent trading on unknown CME holidays.",
                MAX_CONFIGURED_CME_YEAR, d, d.year, d.year
            )
        return False
    return True


def is_cme_closed(d: date) -> bool:
    """Return True if the CME is fully closed on the given date."""
    # Fail-closed: if year not supported, assume closed to prevent trading
    if not _check_cme_year_supported(d):
        return True
    d_str = d.isoformat()
    return d_str in CME_HOLIDAYS_2026


def is_cme_early_close(d: date) -> bool:
    """Return True if the CME closes early on the given date."""
    # Fail-closed: if year not supported, assume early close to be safe
    if not _check_cme_year_supported(d):
        return True
    d_str = d.isoformat()
    return d_str in CME_EARLY_CLOSE_2026
