"""
Safe pipeline concurrency utilities.

This module intentionally keeps the trade-commit boundary serialized while
allowing bounded parallelism in the read/fetch layer.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class SingleFlightGate:
    """Fail-closed single-flight lock for scheduler-triggered pipeline runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def acquire_or_skip(self, label: str) -> Iterator[bool]:
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            log.warning("%s: duplicate tick detected; skipping overlapping pipeline run", label)
            yield False
            return
        try:
            yield True
        finally:
            self._lock.release()


class SerializedCommitGate:
    """Global mutex for risk validation, capital reservation, and broker/DB commits."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @contextmanager
    def section(self, label: str) -> Iterator[None]:
        log.debug("Entering serialized commit boundary: %s", label)
        with self._lock:
            yield
        log.debug("Exiting serialized commit boundary: %s", label)


@dataclass
class BoundedExecutor:
    max_workers: int = 4
    thread_name_prefix: str = "pipeline-io"

    def __post_init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=self.thread_name_prefix,
        )

    def submit(self, fn: Callable[..., T], /, *args, **kwargs) -> Future:
        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)


single_flight_gate = SingleFlightGate()
serialized_commit_gate = SerializedCommitGate()
pipeline_io_executor = BoundedExecutor(max_workers=16)
