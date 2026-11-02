"""Shared utility helpers.

Includes error tracking integration for Sentry/Rollbar.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# ── Error Tracking (Sentry Integration) ────────────────────────────────────
# Provides structured exception capture for production deployments.
# If SENTRY_DSN is not set, errors are logged locally only.

_sentry_client = None
_sentry_initialized = False


def _init_sentry() -> bool:
    """Initialize Sentry SDK if DSN is configured."""
    global _sentry_client, _sentry_initialized

    if _sentry_initialized:
        return _sentry_client is not None

    _sentry_initialized = True

    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        log.debug("error_tracking: SENTRY_DSN not configured — using local logging only")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_logging = LoggingIntegration(
            level=logging.INFO,
            event_level=logging.ERROR,
        )

        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("NSEBOT_ENV", "development"),
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            integrations=[sentry_logging],
            send_default_pii=False,
            ignore_errors=[KeyboardInterrupt, SystemExit],
        )

        _sentry_client = sentry_sdk
        log.info("error_tracking: Sentry initialized (DSN configured)")
        return True

    except ImportError:
        log.warning(
            "error_tracking: sentry_sdk not installed — install with `pip install sentry-sdk`"
        )
        return False
    except Exception as e:
        log.error("error_tracking: Sentry initialization failed: %s", e)
        return False


def set_user(user_id: str | None = None, username: str | None = None) -> None:
    """Set user context for error tracking."""
    if _init_sentry() and _sentry_client:
        try:
            _sentry_client.set_user({
                "id": user_id,
                "username": username,
            })
        except Exception as e:
            log.debug("error_tracking: set_user failed: %s", e)


def set_tag(key: str, value: Any) -> None:
    """Set a tag for error grouping."""
    if _init_sentry() and _sentry_client:
        try:
            _sentry_client.set_tag(key, value)
        except Exception as e:
            log.debug("error_tracking: set_tag failed: %s", e)


def capture_exception(
    exception: Exception | None = None,
    context: dict[str, Any] | None = None,
    level: str = "error",
) -> None:
    """Capture an exception and send to error tracking service.

    Args:
        exception: The exception to capture. If None, captures the current exception.
        context: Additional context to attach (symbol, operation, trade_id, etc.)
        level: Severity level (error, warning, info)
    """
    # Always log locally first
    if exception:
        log.error(
            "error_tracking: %s | context=%s",
            str(exception),
            context or {},
            exc_info=exception,
        )
    else:
        log.error("error_tracking: capture_exception called without exception", exc_info=True)

    # Send to Sentry if available
    if _init_sentry() and _sentry_client:
        try:
            if context:
                with _sentry_client.push_scope() as scope:
                    for key, value in context.items():
                        scope.set_extra(key, value)
                    if exception:
                        _sentry_client.capture_exception(exception)
                    else:
                        _sentry_client.capture_exception()
            else:
                if exception:
                    _sentry_client.capture_exception(exception)
                else:
                    _sentry_client.capture_exception()
        except Exception as e:
            log.debug("error_tracking: Sentry capture failed: %s", e)


def capture_message(message: str, level: str = "info", context: dict[str, Any] | None = None) -> None:
    """Capture a message (non-exception event)."""
    log.info("error_tracking: %s | context=%s", message, context or {})

    if _init_sentry() and _sentry_client:
        try:
            if context:
                with _sentry_client.push_scope() as scope:
                    for key, value in context.items():
                        scope.set_extra(key, value)
                    _sentry_client.capture_message(message, level=level)
            else:
                _sentry_client.capture_message(message, level=level)
        except Exception as e:
            log.debug("error_tracking: Sentry capture_message failed: %s", e)


def add_breadcrumb(message: str, category: str = "default", data: dict[str, Any] | None = None) -> None:
    """Add a breadcrumb for debugging context."""
    if _init_sentry() and _sentry_client:
        try:
            _sentry_client.add_breadcrumb(
                message=message,
                category=category,
                data=data or {},
            )
        except Exception:
            pass  # Breadcrumbs are non-critical


# Initialize on import (lazy — only if DSN is set)
_init_sentry()
