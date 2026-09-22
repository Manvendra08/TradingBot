"""
Asynchronous News Sentiment Background Worker & Fast Gate Cache.
================================================================
Decouples news fetching/scoring from the critical scan pipeline tick.

Architecture:
- Background Worker: Periodically polls news (every 15 min by default),
  evaluates sentiment score (-1.0 to 1.0), and atomically persists to
  data/cache/news_sentiment.json.
- Fast Gate: Synchronous, in-memory reader that applies exponential time-decay
  and returns sentiment in < 0.1 ms without blocking the scan pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import DATA_DIR, WATCH_SYMBOLS

log = logging.getLogger(__name__)

CACHE_DIR = Path(DATA_DIR) / "cache"
NEWS_CACHE_FILE = CACHE_DIR / "news_sentiment.json"
DEFAULT_POLL_INTERVAL_SECONDS = 15 * 60  # 15 minutes

# In-memory fast cache
_LOCK = threading.Lock()
_IN_MEMORY_NEWS: dict[str, dict[str, Any]] = {}
_WORKER_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()


def _dir_label(score: float) -> str:
    if score >= 0.35:
        return "BULLISH"
    if score <= -0.35:
        return "BEARISH"
    return "MIXED"


def _load_disk_cache() -> dict[str, dict[str, Any]]:
    """Load cached sentiment from disk if available."""
    if not NEWS_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(NEWS_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.warning("[news_worker] Failed reading news cache file %s: %s", NEWS_CACHE_FILE, exc)
    return {}


def _save_disk_cache(cache_data: dict[str, dict[str, Any]]) -> None:
    """Atomically write cache to disk."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        content = json.dumps(cache_data, indent=2)
        tmp_file = NEWS_CACHE_FILE.with_suffix(f".{os.getpid()}_{threading.get_ident()}.tmp")
        tmp_file.write_text(content, encoding="utf-8")
        os.replace(tmp_file, NEWS_CACHE_FILE)
    except Exception as exc:
        log.error("[news_worker] Failed saving news cache file %s: %s", NEWS_CACHE_FILE, exc)


def evaluate_and_cache_news(symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Poll news feeds for target symbols, score sentiment, and update cache."""
    from src.fetchers.news_fetcher import fetch_news

    targets = symbols or list(WATCH_SYMBOLS)
    updated: dict[str, dict[str, Any]] = {}
    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    for sym in targets:
        sym_clean = sym.upper().strip().split()[0]
        try:
            # fetch_news handles symbol-specific scrapers and deduplication
            res = fetch_news(sym_clean)
            if not isinstance(res, dict):
                continue

            raw_score = float(res.get("news_score_current", 0.0))
            # Clamp to bounded float [-1.0, 1.0]
            raw_score = max(-1.0, min(1.0, raw_score))

            entry = {
                "symbol": sym_clean,
                "raw_score": round(raw_score, 3),
                "direction": res.get("current_news_direction") or _dir_label(raw_score),
                "count_24h": int(res.get("count_24h", 0)),
                "news_score_day": float(res.get("news_score_day", raw_score)),
                "evaluated_at": now_iso,
                "evaluated_timestamp": now_ts,
                "items": res.get("items", [])[:10],
            }
            updated[sym_clean] = entry
            log.info(
                "[news_worker] Evaluated %s: score=%.3f direction=%s articles=%d",
                sym_clean, entry["raw_score"], entry["direction"], entry["count_24h"],
            )
        except Exception as exc:
            log.warning("[news_worker] Failed evaluating news for %s: %s", sym_clean, exc)

    if updated:
        with _LOCK:
            _IN_MEMORY_NEWS.update(updated)
            # Merge with existing on-disk cache
            current_disk = _load_disk_cache()
            current_disk.update(_IN_MEMORY_NEWS)
            _save_disk_cache(current_disk)

    return updated


def get_cached_news_sentiment(
    symbol: str,
    half_life_minutes: float = 60.0,
    max_age_minutes: float = 240.0,
) -> dict[str, Any]:
    """Microsecond Fast Gate reader: returns decayed sentiment in < 0.1 ms.

    Computes exponential half-life decay:
        S_effective = S_raw * 0.5 ** (age_minutes / half_life_minutes)
    If age > max_age_minutes (4 hours default), sentiment decays to 0.0 (neutral).
    """
    sym = symbol.upper().strip().split()[0]

    data: dict[str, Any] | None = None
    with _LOCK:
        data = _IN_MEMORY_NEWS.get(sym)

    if data is None:
        disk_data = _load_disk_cache()
        if disk_data and sym in disk_data:
            with _LOCK:
                _IN_MEMORY_NEWS.update(disk_data)
                data = _IN_MEMORY_NEWS.get(sym)

    if not data:
        return {
            "items": [],
            "count_24h": 0,
            "current_news_direction": "MIXED",
            "news_score_current": 0.0,
            "news_score_day": 0.0,
            "age_minutes": None,
            "evaluated_at": None,
            "cached": True,
        }

    now_ts = time.time()
    eval_ts = float(data.get("evaluated_timestamp", now_ts))
    age_minutes = max(0.0, (now_ts - eval_ts) / 60.0)

    raw_score = float(data.get("raw_score", 0.0))
    if age_minutes > max_age_minutes:
        decay_factor = 0.0
    else:
        decay_factor = 0.5 ** (age_minutes / half_life_minutes)

    effective_score = round(raw_score * decay_factor, 3)
    direction = _dir_label(effective_score)

    return {
        "items": data.get("items", []),
        "count_24h": data.get("count_24h", 0),
        "current_news_direction": direction,
        "news_score_current": effective_score,
        "news_score_day": round(float(data.get("news_score_day", raw_score)), 3),
        "age_minutes": round(age_minutes, 1),
        "evaluated_at": data.get("evaluated_at"),
        "cached": True,
    }


def _worker_loop(poll_interval: int) -> None:
    """Daemon thread loop executing periodic news evaluations."""
    log.info("[news_worker] Asynchronous News Sentiment Worker started (poll interval=%ds)", poll_interval)
    while not _STOP_EVENT.is_set():
        try:
            evaluate_and_cache_news()
        except Exception as exc:
            log.exception("[news_worker] Unexpected error in news worker cycle: %s", exc)

        # Sleep with stop event interruptibility
        if _STOP_EVENT.wait(timeout=poll_interval):
            break
    log.info("[news_worker] News Sentiment Worker stopped")


def start_news_worker(poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS) -> None:
    """Start the asynchronous news background worker daemon."""
    global _WORKER_THREAD
    with _LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            log.debug("[news_worker] Worker thread already running")
            return

        _STOP_EVENT.clear()
        _WORKER_THREAD = threading.Thread(
            target=_worker_loop,
            args=(poll_interval_seconds,),
            daemon=True,
            name="NewsSentimentWorker",
        )
        _WORKER_THREAD.start()
        log.info("[news_worker] Dispatched NewsSentimentWorker background thread")


def stop_news_worker() -> None:
    """Signal background worker to stop gracefully."""
    _STOP_EVENT.set()
