"""
Provider fetch helpers with explicit deadlines and exception isolation.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import TimeoutError
from dataclasses import dataclass
from typing import Any, Callable

from src.engine.pipeline_concurrency import pipeline_io_executor

log = logging.getLogger(__name__)


@dataclass
class ProviderResult:
    name: str
    ok: bool
    data: Any
    error: str | None
    duration_ms: int
    timed_out: bool = False


DEFAULT_PROVIDER_TIMEOUTS = {
    "option_chain": 12.0,
    "chart": 8.0,
    "news": 4.0,
    "ip_refresh": 3.0,
}


def run_with_deadline(name: str, fn: Callable[[], Any], timeout_s: float | None = None) -> ProviderResult:
    timeout = timeout_s if timeout_s is not None else DEFAULT_PROVIDER_TIMEOUTS.get(name, 5.0)
    started = time.monotonic()
    fut = pipeline_io_executor.submit(fn)
    try:
        data = fut.result(timeout=timeout)
        return ProviderResult(
            name=name,
            ok=True,
            data=data,
            error=None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except TimeoutError:
        fut.cancel()
        return ProviderResult(
            name=name,
            ok=False,
            data=None,
            error=f"deadline exceeded after {timeout:.1f}s",
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=True,
        )
    except Exception as exc:
        log.debug("Provider %s failed: %s", name, exc)
        return ProviderResult(
            name=name,
            ok=False,
            data=None,
            error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
