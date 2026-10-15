"""
Macro Event Shock Registry & Circuit Breaker (Jev System-1).
============================================================
Tracks rapid event shocks detected from breaking headlines (e.g. surprise war,
emergency rate changes, OPEC shocks, pipeline catastrophes) to temporarily pause
new directional trade entries during high-gamma shock windows.

Expirations default to 20 minutes (configurable).
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# In-memory shock registry: symbol -> shock dict
# A shock on "GLOBAL" pauses entries across all symbols.
_SHOCK_REGISTRY: dict[str, dict[str, Any]] = {}


def record_event_shock(
    symbol: str,
    headline: str,
    severity: str = "EXTREME_SHOCK",
    duration_minutes: int = 20,
    reason: str = "",
) -> None:
    """Record an active macro event shock and trigger a temporary entry pause."""
    now = time.time()
    expires_at = now + (duration_minutes * 60)
    key = symbol.upper().strip()

    _SHOCK_REGISTRY[key] = {
        "symbol": key,
        "headline": headline,
        "severity": severity,
        "recorded_at": now,
        "expires_at": expires_at,
        "reason": reason or f"{severity} on {key}: {headline[:100]}",
    }
    log.critical(
        "🚨 [EVENT_SHOCK] %s active on %s for %d min | Headline: %s",
        severity, key, duration_minutes, headline[:120],
    )


def is_event_shock_active(symbol: str) -> tuple[bool, str]:
    """Check if an unexpired event shock is active for symbol or GLOBAL.

    Returns:
        (is_active: bool, reason: str)
    """
    now = time.time()
    key = symbol.upper().strip()

    # Check symbol-specific shock
    if key in _SHOCK_REGISTRY:
        data = _SHOCK_REGISTRY[key]
        if now < data["expires_at"]:
            remaining_m = int((data["expires_at"] - now) / 60) + 1
            return True, f"{data['reason']} ({remaining_m}m remaining)"
        else:
            del _SHOCK_REGISTRY[key]

    # Check GLOBAL shock
    if "GLOBAL" in _SHOCK_REGISTRY:
        data = _SHOCK_REGISTRY["GLOBAL"]
        if now < data["expires_at"]:
            remaining_m = int((data["expires_at"] - now) / 60) + 1
            return True, f"GLOBAL SHOCK: {data['reason']} ({remaining_m}m remaining)"
        else:
            del _SHOCK_REGISTRY["GLOBAL"]

    return False, ""


def clear_event_shock(symbol: str | None = None) -> None:
    """Clear active shock for a symbol, or all shocks if symbol is None."""
    global _SHOCK_REGISTRY
    if symbol is None:
        _SHOCK_REGISTRY.clear()
        log.info("[EVENT_SHOCK] All active shocks cleared")
    else:
        key = symbol.upper().strip()
        if key in _SHOCK_REGISTRY:
            del _SHOCK_REGISTRY[key]
            log.info("[EVENT_SHOCK] Shock cleared for %s", key)


def get_active_shocks() -> dict[str, dict[str, Any]]:
    """Return dict of currently active shocks."""
    now = time.time()
    active = {}
    for sym, data in list(_SHOCK_REGISTRY.items()):
        if now < data["expires_at"]:
            active[sym] = dict(data)
        else:
            del _SHOCK_REGISTRY[sym]
    return active
