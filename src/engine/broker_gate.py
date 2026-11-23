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
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason="Config load failure — fail-closed",
            config_snapshot={}
        )

    # ── 1. Hard Kill Switch: Live Broker Disabled ─────────────────────────
    # When live broker is disabled, ALL live execution and live shadow simulation
    # are completely suppressed (fail-closed).
    if config.get("live_broker_disabled", True):
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason="Live broker disabled in runtime config",
            config_snapshot=config
        )

    # ── 2. Database Broker Kill Switch ─────────────────────────────────────
    try:
        from src.models.schema import get_conn
        with get_conn(read_only=True) as conn:
            bcfg = conn.execute(
                "SELECT kill_switch_active FROM broker_configs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if bcfg and bcfg["kill_switch_active"]:
                return ExecutionAuthorization(
                    is_authorized=False,
                    is_shadow=False,
                    symbol=symbol,
                    operation=operation,
                    reason="Broker kill switch active in database",
                    config_snapshot=config
                )
    except Exception as e:
        log.warning("[broker-gate] Could not check broker_configs kill_switch: %s", e)

    # ── 3. Global Trading Pause Gate ───────────────────────────────────────
    # When trading is paused, new entries are blocked (both real and shadow).
    # Protective exits and protective GTT placements bypass this gate to avoid trapping open risk on the broker.
    is_protective_op = operation in ("EXIT", "GTT_PROTECTION", "PROTECTIVE_GTT")
    if not is_protective_op and config.get("trading_paused", True):
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=False,
            symbol=symbol,
            operation=operation,
            reason="Trading is currently paused",
            config_snapshot=config
        )

    # ── 3b. Strategy Specific Check: Stock Futures ─────────────────────────
    is_stockfut = False
    if scan_context and (scan_context.get("strategy") == "STOCKFUT" or scan_context.get("asset_class") == "STOCKFUT"):
        is_stockfut = True

    if is_stockfut:
        from config.stockfut_loader import load_stockfut_config
        sf_cfg = load_stockfut_config()
        if not sf_cfg.get("stockfut_live_enabled", False):
            return ExecutionAuthorization(
                is_authorized=False,
                is_shadow=False,
                symbol=symbol,
                operation=operation,
                reason="Stock futures live trading disabled in config",
                config_snapshot=config,
            )

    # ── 4. Strict fail-closed shadow invariant ─────────────────────────────
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

    # ── Pre-Trade RMS: Daily Loss Lockout Gate ───────────────────────────
    if operation == "ENTRY":
        try:
            from datetime import datetime, timezone
            import pytz
            from config.settings import MAX_DAILY_LOSS_RUPEES
            from src.models.schema import get_conn

            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(timezone.utc).astimezone(ist)
            today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_utc = today_start_ist.astimezone(pytz.utc).isoformat()

            max_daily_loss = float(config.get("live_max_daily_loss_rupees") or MAX_DAILY_LOSS_RUPEES or 25000.0)

            with get_conn(read_only=True) as conn:
                r1 = conn.execute(
                    "SELECT COALESCE(SUM(pnl_rupees), 0) AS total FROM live_trades WHERE closed_at >= ? AND (broker_status != 'SHADOW' OR broker_status IS NULL)",
                    (today_start_utc,)
                ).fetchone()
                single_realized = float(r1["total"]) if r1 else 0.0

                r2 = conn.execute(
                    "SELECT COALESCE(SUM(total_pnl), 0) AS total FROM multi_leg_trades WHERE closed_at >= ? AND UPPER(trade_mode) = 'LIVE'",
                    (today_start_utc,)
                ).fetchone()
                multileg_realized = float(r2["total"]) if r2 else 0.0

                try:
                    r3 = conn.execute(
                        "SELECT COALESCE(SUM(pnl), 0) AS total FROM stockfut_trades WHERE exit_time >= ? AND mode = 'LIVE'",
                        (today_start_utc,)
                    ).fetchone()
                    stockfut_realized = float(r3["total"]) if r3 else 0.0
                except Exception:
                    stockfut_realized = 0.0

                total_realized_loss = single_realized + multileg_realized + stockfut_realized

                if total_realized_loss <= -abs(max_daily_loss):
                    log.critical(
                        "[broker-gate] %s: Max daily loss threshold breached (realized=₹%.2f, cap=₹%.2f) — FAIL-CLOSED",
                        symbol, total_realized_loss, -abs(max_daily_loss)
                    )
                    return ExecutionAuthorization(
                        is_authorized=False,
                        is_shadow=False,
                        symbol=symbol,
                        operation=operation,
                        reason=f"Daily loss limit breached: realized loss ₹{abs(total_realized_loss):,.2f} >= cap ₹{abs(max_daily_loss):,.2f}",
                        config_snapshot=config,
                    )
        except Exception as e:
            log.error("[broker-gate] %s: Daily loss check error (fail-closed): %s", symbol, e)
            return ExecutionAuthorization(
                is_authorized=False,
                is_shadow=False,
                symbol=symbol,
                operation=operation,
                reason=f"Daily loss check evaluation failed (fail-closed): {e}",
                config_snapshot=config,
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