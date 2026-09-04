from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionAuthorization:
    is_authorized: bool
    is_shadow: bool
    symbol: str
    operation: str  # ENTRY, EXIT, ADJUSTMENT, GTT, RECONCILE
    reason: str
    config_snapshot: dict[str, Any]


def _is_market_open(symbol: str) -> bool:
    try:
        from src.engine.time_guards import is_trading_allowed_now
        allowed, _reason = is_trading_allowed_now(symbol)
        return bool(allowed)
    except Exception as e:
        log.warning("Could not check market hours for %s: %s", symbol, e)
        return False


def authorize_broker_execution(
    symbol: str,
    operation: str = "ENTRY",
    scan_context: dict | None = None
) -> ExecutionAuthorization:
    """
    Centralized broker authorization gate.
    All live execution paths (entry, exit, gtt, reconciliation) MUST call this block.
    """
    try:
        from config.runtime_config import load_runtime_config
        config = load_runtime_config()
    except Exception as e:
        log.error(f"Config load failed in broker gate: {e}")
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=True,
            symbol=symbol,
            operation=operation,
            reason="Config load failure — fail-closed",
            config_snapshot={}
        )

    # Note: shadow_mode applies to everything including exits in the safest implementation,
    # but some systems allow exist despite shadow mode on entries.
    # Wait, the spec says shadow mode prevents any real order placement.
    # Exits in shadow mode check this gate too? Unclear. It says "All live execution paths".
    # Wait, multileg paper trading doesn't use this. Live trades do. If it's a live trade being closed, it needs real broker order.
    # We should stick strict to the rules.

    if config.get("live_shadow_mode", True):
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=True,
            symbol=symbol,
            operation=operation,
            reason="Shadow mode enabled",
            config_snapshot=config
        )

    if config.get("live_broker_disabled", True):
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason="Live broker disabled in runtime config",
            config_snapshot=config
        )

    if config.get("trading_paused", True):
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason="Trading is currently paused",
            config_snapshot=config
        )

    enabled_symbols = config.get("live_enabled_broker_symbols", [])
    if symbol not in enabled_symbols:
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason=f"Symbol {symbol} not in live_enabled_broker_symbols",
            config_snapshot=config
        )

    if not _is_market_open(symbol):
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason=f"Market closed for {symbol}",
            config_snapshot=config
        )

    return ExecutionAuthorization(
        is_authorized=True,
        is_shadow=False,
        symbol=symbol,
        operation=operation,
        reason="AUTHORIZED",
        config_snapshot=config
    )