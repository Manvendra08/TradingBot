"""
Lightweight health/state caches for Kite, position sync, and IP refresh.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

KITE_HEALTH_TTL_S = 12 * 60
POSITION_RECONCILE_TTL_S = 12 * 60


@dataclass
class TimedValue:
    value: Any
    expires_at: float
    updated_at: str


class TimedCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, TimedValue] = {}

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            if item.expires_at < now:
                self._store.pop(key, None)
                return None
            return item.value

    def put(self, key: str, value: Any, ttl_s: float) -> None:
        with self._lock:
            self._store[key] = TimedValue(
                value=value,
                expires_at=time.monotonic() + ttl_s,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


kite_health_cache = TimedCache()
position_sync_cache = TimedCache()


class PositionSyncDirtyState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dirty = True
        self._last_reason = "startup"

    def mark_dirty(self, reason: str) -> None:
        with self._lock:
            self._dirty = True
            self._last_reason = reason
            log.info("Position sync marked dirty: %s", reason)

    def clear(self) -> None:
        with self._lock:
            self._dirty = False
            self._last_reason = "clean"

    def consume(self) -> tuple[bool, str]:
        with self._lock:
            return self._dirty, self._last_reason


position_sync_dirty_state = PositionSyncDirtyState()
