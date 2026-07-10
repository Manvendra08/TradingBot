"""
Candle-aware chart cache.

Caches only completed candles so partially formed candles never change
indicator outputs for deterministic decision logic.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class ChartCacheEntry:
    value: Any
    source_candle_ts: str
    stored_at: str


class CandleAwareChartCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[tuple[str, str, str, str, str], ChartCacheEntry] = {}

    def make_key(
        self,
        *,
        symbol: str,
        timeframe: str,
        provider: str,
        instrument_identity: str,
        source_candle_ts: str,
    ) -> tuple[str, str, str, str, str]:
        return (symbol, timeframe, provider, instrument_identity, source_candle_ts)

    def get(self, key: tuple[str, str, str, str, str]) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            return entry.value if entry else None

    def put(
        self,
        key: tuple[str, str, str, str, str],
        value: Any,
        *,
        completed_candle: bool,
        source_candle_ts: str,
    ) -> None:
        if not completed_candle:
            return
        with self._lock:
            self._store[key] = ChartCacheEntry(
                value=value,
                source_candle_ts=source_candle_ts,
                stored_at=datetime.now(timezone.utc).isoformat(),
            )


chart_cache = CandleAwareChartCache()
