"""
Scan Context Cache
AI_INTELLIGENCE_ROADMAP_v3.0 — Phase 2 Integration

Stores the latest scan context per symbol so the ML prediction dashboard
endpoint can hydrate full feature context before predicting. Without this,
the dashboard endpoint would pass a near-empty vector (every OI/distance/RSI
feature = 0) and return a DIFFERENT probability than the pipeline.

This is an in-memory cache with TTL. It is NOT persisted to disk.
The cache is updated at the end of each pipeline scan cycle.

Thread Safety:
    All operations are protected by a module-level lock. The pipeline runs
    synchronously per symbol, but the dashboard server runs in a separate
    process/thread and may read concurrently.
"""
import logging
import threading
from time import time as _time

log = logging.getLogger(__name__)

# ── Module-level cache ─────────────────────────────────────────────────────
# Key: symbol (str, uppercase)
# Value: dict with "context" (scan_context dict) and "ts" (timestamp)
_scan_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 600  # 10 minutes — scans run every 3-5 min


def update_scan_snapshot(symbol: str, scan_context: dict) -> None:
    """
    Store the latest scan context for a symbol.

    Called at the end of each pipeline scan cycle for each symbol.
    The scan_context dict is copied to prevent external mutation.

    Args:
        symbol: Trading symbol (e.g., "NIFTY", "BANKNIFTY")
        scan_context: Full scan context dict from anomaly_detector
    """
    if not symbol or not scan_context:
        return

    symbol_key = symbol.upper().strip()
    if not symbol_key:
        return

    # Copy to prevent external mutation of cached data
    try:
        context_copy = dict(scan_context)
    except Exception:
        log.debug("Failed to copy scan_context for %s", symbol)
        return

    with _cache_lock:
        _scan_cache[symbol_key] = {
            "context": context_copy,
            "ts": _time(),
        }

    log.debug("Scan snapshot cached for %s", symbol_key)


def get_latest_scan_snapshot(symbol: str) -> dict | None:
    """
    Retrieve the latest cached scan context for a symbol.

    Returns None if no snapshot exists or if the cached data is stale
    (older than CACHE_TTL_SECONDS).

    Used by the dashboard ML prediction endpoint to hydrate full feature
    context before predicting, ensuring the dashboard and pipeline produce
    identical predictions for the same trade.

    Args:
        symbol: Trading symbol (e.g., "NIFTY", "BANKNIFTY")

    Returns:
        Scan context dict, or None if unavailable/stale.
    """
    if not symbol:
        return None

    symbol_key = symbol.upper().strip()
    if not symbol_key:
        return None

    with _cache_lock:
        entry = _scan_cache.get(symbol_key)
        if entry is None:
            return None

        # Check TTL
        age = _time() - entry.get("ts", 0)
        if age > CACHE_TTL_SECONDS:
            log.debug(
                "Scan snapshot for %s is stale (%.0fs > %ds TTL)",
                symbol_key, age, CACHE_TTL_SECONDS,
            )
            return None

        # Return a copy to prevent external mutation
        try:
            return dict(entry.get("context", {}))
        except Exception:
            return None


def clear_scan_cache() -> None:
    """Clear all cached scan snapshots. Used for testing or manual reset."""
    with _cache_lock:
        _scan_cache.clear()
    log.info("Scan cache cleared")


def get_all_cached_symbols() -> list[str]:
    """Return list of symbols with fresh (non-stale) cached snapshots."""
    now = _time()
    with _cache_lock:
        return [
            sym for sym, entry in _scan_cache.items()
            if (now - entry.get("ts", 0)) <= CACHE_TTL_SECONDS
        ]
