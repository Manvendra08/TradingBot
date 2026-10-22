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


def _is_market_open(symbol: str, expiry_str: str | None = None, operation: str = "ENTRY") -> bool:
    try:
        from src.engine.time_guards import is_trading_allowed_now
        allowed, _reason = is_trading_allowed_now(symbol, expiry_str=expiry_str, operation=operation)
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

    # Strict fail-closed shadow invariant:
    # In shadow mode (live_shadow_mode=True), NO real broker orders (ENTRY, EXIT, or GTT)
    # are permitted. All operations run in paper/shadow simulation.
    # Real broker exits are strictly prohibited while shadow mode is active.

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

    if operation != "EXIT" and config.get("trading_paused", True):
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason="Trading is currently paused",
            config_snapshot=config
        )

    enabled_symbols = config.get("live_enabled_broker_symbols", [])
    # EXIT operations bypass the symbol allowlist (and trading_paused above):
    # they only close existing exposure, so they must stay executable even for
    # a symbol that was removed from the monitor list while a trade was open.
    # Kill switches (shadow mode, broker disabled) still block ALL operations.
    if operation != "EXIT" and symbol not in enabled_symbols:
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason=f"Symbol {symbol} not in live_enabled_broker_symbols",
            config_snapshot=config
        )

    expiry_str = scan_context.get("expiry") if isinstance(scan_context, dict) else None
    if not _is_market_open(symbol, expiry_str=expiry_str, operation=operation):
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason=f"Market closed or trading restricted for {symbol}",
            config_snapshot=config
        )

    # ── Phase 5: Edge Decay Gate ─────────────────────────────────────────
    if operation == "ENTRY":
        try:
            from src.intelligence.edge_monitor import get_monitor
            monitor = get_monitor()
            health_reports = monitor.check_edge_health(strategy_filter={"symbol": symbol})
            if health_reports:
                h = health_reports[0]
                if (
                    h.health_score < 35
                    and h.win_rate_trend != "INSUFFICIENT_HISTORY"
                    and (h.win_rate_trend == "DECLINING" or h.pnl_trend == "DECLINING" or h.current_win_rate < 0.40)
                ):
                    return ExecutionAuthorization(
                        is_authorized=False,
                        is_shadow=False,
                        symbol=symbol,
                        operation=operation,
                        reason=f"Edge decay detected for {symbol} (health_score={h.health_score:.1f}, wr={h.current_win_rate:.0%}, trend={h.win_rate_trend}): live entry paused",
                        config_snapshot=config,
                    )
        except Exception as e:
            log.error("[broker-gate] %s: Edge decay check error (fail-closed): %s", symbol, e)
            return ExecutionAuthorization(
                is_authorized=False,
                is_shadow=False,
                symbol=symbol,
                operation=operation,
                reason=f"Edge decay check evaluation failed (fail-closed): {e}",
                config_snapshot=config,
            )

        # ── Walk-Forward Positive Expectancy Gate ─────────────────────────
        try:
            from src.engine.replay_backtester import verify_positive_expectancy
            exp_check = verify_positive_expectancy(symbol)
            # If backtested with sufficient trades and fails positive expectancy, block live entry
            if exp_check.get("trades", 0) >= 3 and not exp_check.get("passed"):
                return ExecutionAuthorization(
                    is_authorized=False,
                    is_shadow=False,
                    symbol=symbol,
                    operation=operation,
                    reason=f"Positive expectancy gate failed for {symbol}: {exp_check.get('reason')}",
                    config_snapshot=config,
                )
        except Exception as e:
            log.error("[broker-gate] %s: Positive expectancy gate error (fail-closed): %s", symbol, e)
            return ExecutionAuthorization(
                is_authorized=False,
                is_shadow=False,
                symbol=symbol,
                operation=operation,
                reason=f"Positive expectancy gate evaluation failed (fail-closed): {e}",
                config_snapshot=config,
            )

    return ExecutionAuthorization(
        is_authorized=True,
        is_shadow=False,
        symbol=symbol,
        operation=operation,
        reason="AUTHORIZED",
        config_snapshot=config
    )