"""
Startup validation for multileg runtime state (F5/F16/F17).

Call after init_db() and before the first pipeline run.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# ── Schema Validation ──────────────────────────────────────────────

def validate_multileg_schema() -> None:
    """Ensure multi_leg_trades and multi_leg_legs have required columns.

    Raises RuntimeError if trade_mode or snapshot_id columns are missing
    (i.e. migrations M113/M139 have not been applied).
    """
    from src.models.schema import get_conn

    required_columns = {
        "multi_leg_trades": {"trade_mode", "snapshot_id"},
        "multi_leg_legs": {"trade_mode", "snapshot_id"},
    }

    with get_conn() as conn:
        for table, columns in required_columns.items():
            existing = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            missing = columns - existing
            if missing:
                raise RuntimeError(
                    f"Multileg schema incomplete: {table} missing columns {missing}. "
                    f"Run DB migrations first."
                )

    log.info("[multileg-startup] Schema validation passed — trade_mode and snapshot_id columns present")


# ── Runtime Config Validation ──────────────────────────────────────

def validate_runtime_config_for_multileg() -> list[str]:
    """Validate runtime_config.json has required keys for multileg operation.

    Returns a list of warning strings for missing/invalid values.
    """
    import json
    from pathlib import Path

    warnings: list[str] = []
    config_path = Path(__file__).resolve().parents[2] / "data" / "runtime_config.json"

    if not config_path.exists():
        return ["runtime_config.json not found — using defaults"]

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config: dict[str, Any] = json.load(f)
    except Exception as e:
        return [f"Failed to read runtime_config.json: {e}"]

    required_bool_keys = [
        "live_ai_exit_advisor_enabled",
        "live_shadow_mode",
    ]
    required_list_keys = [
        "live_enabled_broker_symbols",
    ]
    required_str_keys = [
        "live_ai_decision_mode",
    ]

    for key in required_bool_keys:
        if key not in config:
            warnings.append(f"Missing bool key: {key} (defaulting to false)")
        elif not isinstance(config[key], bool):
            warnings.append(f"Invalid type for {key}: expected bool, got {type(config[key]).__name__}")

    for key in required_list_keys:
        if key not in config:
            warnings.append(f"Missing list key: {key} (defaulting to [])")
        elif not isinstance(config[key], list):
            warnings.append(f"Invalid type for {key}: expected list, got {type(config[key]).__name__}")

    for key in required_str_keys:
        if key not in config:
            warnings.append(f"Missing str key: {key} (defaulting to 'advisory')")
        elif not isinstance(config[key], str):
            warnings.append(f"Invalid type for {key}: expected str, got {type(config[key]).__name__}")

    if warnings:
        for w in warnings:
            log.warning("[multileg-startup] Config validation: %s", w)
    else:
        log.info("[multileg-startup] Config validation passed")

    return warnings


# ── Position Reconciliation ────────────────────────────────────────

def reconcile_multileg_books_at_startup(symbol: str | None = None) -> list[dict[str, Any]]:
    """Reconcile DB-open live multileg books against broker positions.

    For each DB-open live book, fetch Kite positions and compare.
    - If broker shows 0 qty but DB shows OPEN, auto-close leg with reason EXTERNAL_CLOSE.
    - If broker shows qty but DB shows CLOSED, flag for operator review.

    Returns a list of reconciliation actions taken.
    """
    from src.models.schema import get_conn, close_leg
    from src.engine.broker_gate import authorize_broker_execution

    actions: list[dict[str, Any]] = []

    with get_conn() as conn:
        if symbol:
            open_books = conn.execute(
                "SELECT * FROM multi_leg_trades WHERE symbol=? AND status='OPEN' AND trade_mode IN ('LIVE', 'SHADOW')",
                (symbol,)
            ).fetchall()
        else:
            open_books = conn.execute(
                "SELECT * FROM multi_leg_trades WHERE status='OPEN' AND trade_mode IN ('LIVE', 'SHADOW')"
            ).fetchall()

    if not open_books:
        log.info("[multileg-startup] No open live multileg books to reconcile")
        return actions

    for book in open_books:
        book_id = book["id"]
        book_symbol = book["symbol"]
        trade_mode = book.get("trade_mode", "PAPER")

        if trade_mode == "SHADOW":
            continue

        auth = authorize_broker_execution(book_symbol, operation="READ")
        if not auth.is_authorized:
            log.warning(
                "[multileg-startup] Skipping reconciliation for %s book %s: %s",
                book_symbol, book_id, auth.reason,
            )
            continue

        try:
            from src.engine.live_trading import get_kite_client
            kite = get_kite_client()
            if not kite:
                continue

            positions = kite.positions().get("net", [])
            broker_qty_map = {
                p["tradingsymbol"]: int(p.get("quantity", 0))
                for p in positions
            }

            with get_conn() as conn:
                legs = conn.execute(
                    "SELECT * FROM multi_leg_legs WHERE trade_id=? AND status='OPEN'",
                    (book_id,)
                ).fetchall()

                for leg in legs:
                    tradingsymbol = leg.get("tradingsymbol", "")
                    broker_qty = broker_qty_map.get(tradingsymbol, 0)
                    db_qty = int(leg.get("filled_quantity") or leg.get("lots", 0))

                    if broker_qty == 0 and db_qty > 0:
                        close_leg(
                            leg["id"],
                            exit_premium=0.0,
                            exit_underlying=0.0,
                            status="CLOSED",
                            exit_reason="EXTERNAL_CLOSE",
                        )
                        actions.append({
                            "book_id": book_id,
                            "symbol": book_symbol,
                            "leg_id": leg["id"],
                            "action": "AUTO_CLOSED",
                            "reason": "EXTERNAL_CLOSE",
                        })
                        log.info(
                            "[multileg-startup] Auto-closed leg %s for %s (broker shows 0 qty)",
                            leg["id"], book_symbol,
                        )
                    elif broker_qty > 0 and db_qty == 0:
                        actions.append({
                            "book_id": book_id,
                            "symbol": book_symbol,
                            "leg_id": leg["id"],
                            "action": "FLAG_FOR_OPERATOR",
                            "reason": "DB_CLOSED_BUT_BROKER_OPEN",
                        })
                        log.warning(
                            "[multileg-startup] Leg %s for %s is CLOSED in DB but OPEN at broker (qty=%d)",
                            leg["id"], book_symbol, broker_qty,
                        )

        except Exception as e:
            log.error(
                "[multileg-startup] Reconciliation failed for %s book %s: %s",
                book_symbol, book_id, e,
            )

    return actions
