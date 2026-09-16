"""Pre-persistence validation and post-persistence trade price & P&L accounting checks."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from math import isclose
from typing import Any

from config.settings import LOT_SIZES

log = logging.getLogger(__name__)


def _number(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _alert(table: str, trade_id: int, fields: dict[str, object]) -> None:
    fingerprint = hashlib.sha256(
        repr(sorted(fields.items())).encode("utf-8")
    ).hexdigest()[:24]
    dedup_key = f"trade-audit|{table}|{trade_id}|{fingerprint}"

    from src.models.schema import get_conn

    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_conn() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO alert_dedup (dedup_key, last_fired_at, severity) VALUES (?, ?, ?)",
                (dedup_key, now, "HIGH"),
            ).rowcount

        if not inserted:
            return
    except Exception as e:
        log.warning("trade_audit: failed to record alert in alert_dedup: %s", e)

    from src.alerts.telegram_dispatcher import send_text

    details = "\n".join(f"{key}: `{value}`" for key, value in fields.items())
    try:
        send_text(
            "🚨 TRADE ACCOUNTING DISCREPANCY\n"
            f"**Table:** `{table}` | **ID:** `{trade_id}`\n"
            f"{details}"
        )
    except Exception as e:
        log.warning("trade_audit: failed to send telegram alert: %s", e)


def audit_trade_entry(table: str, trade_id: int) -> None:
    """Check that a newly persisted trade has usable accounting inputs."""
    from src.models.schema import get_conn

    with get_conn(read_only=True) as conn:
        row = conn.execute(
            f"SELECT symbol, side, option_type, entry_underlying, entry_premium, lots, lot_size FROM {table} WHERE id=?",
            (trade_id,),
        ).fetchone()
    if not row:
        return

    issues: dict[str, object] = {}
    if _number(row["entry_underlying"]) is None or _number(row["entry_underlying"]) <= 0:
        issues["entry_underlying"] = row["entry_underlying"]
    if row["option_type"] in ("CE", "PE") and (
        _number(row["entry_premium"]) is None or _number(row["entry_premium"]) <= 0
    ):
        issues["entry_premium"] = row["entry_premium"]
    if row["side"] not in ("BUY", "SELL"):
        issues["side"] = row["side"]
    if int(row["lots"] or 0) <= 0:
        issues["lots"] = row["lots"]
    if int(row["lot_size"] or 0) <= 0:
        issues["lot_size"] = row["lot_size"]
    if issues:
        _alert(table, trade_id, {"symbol": row["symbol"], **issues})


def audit_trade_close(table: str, trade_id: int) -> None:
    """Recompute stored single-leg P&L and alert on a persisted mismatch."""
    from src.models.schema import _calc_transaction_costs, get_conn

    with get_conn(read_only=True) as conn:
        row = conn.execute(
            f"SELECT symbol, side, option_type, entry_underlying, exit_underlying, entry_premium, exit_premium, lots, lot_size, pnl_points, pnl_rupees FROM {table} WHERE id=?",
            (trade_id,),
        ).fetchone()
    if not row:
        return

    entry_underlying = _number(row["entry_underlying"])
    exit_underlying = _number(row["exit_underlying"])
    entry_premium = _number(row["entry_premium"])
    exit_premium = _number(row["exit_premium"])
    lot_size = int(row["lot_size"] or 0)
    lots = int(row["lots"] or 0)
    if not entry_underlying or not exit_underlying or lot_size <= 0 or lots <= 0:
        return

    option_type = row["option_type"]
    if option_type in ("CE", "PE"):
        if not entry_premium or not exit_premium:
            return
        pnl_points = (
            entry_premium - exit_premium
            if row["side"] == "SELL"
            else exit_premium - entry_premium
        )
    else:
        pnl_points = (
            entry_underlying - exit_underlying
            if row["side"] == "SELL"
            else exit_underlying - entry_underlying
        )

    expected_pnl = pnl_points * lot_size * lots - _calc_transaction_costs(
        option_type,
        row["side"],
        entry_premium or 0.0,
        entry_underlying,
        exit_premium or 0.0,
        exit_underlying,
        lot_size,
        lots,
        symbol=row["symbol"],
    )
    stored_points = _number(row["pnl_points"])
    stored_rupees = _number(row["pnl_rupees"])
    if stored_points is None or stored_rupees is None:
        return
    if isclose(stored_points, pnl_points, abs_tol=0.01) and isclose(
        stored_rupees, expected_pnl, abs_tol=0.05
    ):
        return

    _alert(
        table,
        trade_id,
        {
            "symbol": row["symbol"],
            "entry": entry_premium if option_type in ("CE", "PE") else entry_underlying,
            "exit": exit_premium if option_type in ("CE", "PE") else exit_underlying,
            "stored_pnl": round(stored_rupees, 2),
            "expected_pnl": round(expected_pnl, 2),
            "stored_points": round(stored_points, 4),
            "expected_points": round(pnl_points, 4),
        },
    )


# ── In-Memory Pre-Persistence Validation ──────────────────────────────────────


def validate_trade_entry_data(trade: dict) -> tuple[bool, str, dict[str, object]]:
    """Validate single-leg trade data in-memory before writing to SQLite."""
    issues: dict[str, object] = {}
    symbol = str(trade.get("symbol") or "").strip()
    if not symbol:
        issues["symbol"] = "Empty symbol"

    entry_underlying = _number(trade.get("entry_underlying"))
    if entry_underlying is None or entry_underlying <= 0:
        issues["entry_underlying"] = trade.get("entry_underlying")

    option_type = str(trade.get("option_type") or "").strip().upper()
    if option_type in ("CE", "PE"):
        entry_premium = _number(trade.get("entry_premium"))
        if entry_premium is None or entry_premium <= 0:
            issues["entry_premium"] = trade.get("entry_premium")

    side = str(trade.get("side") or "").strip().upper()
    if side not in ("BUY", "SELL"):
        issues["side"] = trade.get("side")

    try:
        lots = int(trade.get("lots") or 0)
        if lots <= 0:
            issues["lots"] = trade.get("lots")
    except (TypeError, ValueError):
        issues["lots"] = trade.get("lots")

    try:
        lot_size = int(trade.get("lot_size") or 0)
        if lot_size <= 0:
            issues["lot_size"] = trade.get("lot_size")
    except (TypeError, ValueError):
        issues["lot_size"] = trade.get("lot_size")

    if issues:
        reason = f"Single-leg entry validation failed on {len(issues)} field(s)"
        return False, reason, issues
    return True, "", {}


def validate_multileg_entry_data(
    trade: dict, legs: list[dict]
) -> tuple[bool, str, dict[str, object]]:
    """Validate multi-leg book and leg parameters in-memory before SQLite insertion."""
    issues: dict[str, object] = {}
    symbol = str(trade.get("symbol") or "").strip()
    if not symbol:
        issues["symbol"] = "Empty symbol"

    if not legs or len(legs) > 6:
        issues["legs_count"] = len(legs) if legs else 0

    entry_underlying = _number(trade.get("entry_underlying"))
    if entry_underlying is None or entry_underlying <= 0:
        issues["entry_underlying"] = trade.get("entry_underlying")

    sell_prem_sum = 0.0
    buy_prem_sum = 0.0

    for idx, leg in enumerate(legs or []):
        leg_prefix = f"leg_{idx}"
        l_side = str(leg.get("side") or "").strip().upper()
        if l_side not in ("BUY", "SELL"):
            issues[f"{leg_prefix}_side"] = leg.get("side")

        l_opt = str(leg.get("option_type") or "").strip().upper()
        if l_opt not in ("CE", "PE"):
            issues[f"{leg_prefix}_option_type"] = leg.get("option_type")

        l_strike = _number(leg.get("strike"))
        if l_strike is None or l_strike <= 0:
            issues[f"{leg_prefix}_strike"] = leg.get("strike")

        l_prem = _number(leg.get("entry_premium") or leg.get("premium"))
        if l_prem is None or l_prem <= 0:
            issues[f"{leg_prefix}_entry_premium"] = leg.get("entry_premium")
        else:
            if l_side == "SELL":
                sell_prem_sum += l_prem
            elif l_side == "BUY":
                buy_prem_sum += l_prem

        try:
            l_lots = int(leg.get("lots") or 0)
            if l_lots <= 0:
                issues[f"{leg_prefix}_lots"] = leg.get("lots")
        except (TypeError, ValueError):
            issues[f"{leg_prefix}_lots"] = leg.get("lots")

    # Net premium verification if provided
    stored_net = _number(trade.get("net_premium"))
    if stored_net is not None and not issues:
        expected_net = round(sell_prem_sum - buy_prem_sum, 2)
        if expected_net > 0 and not isclose(stored_net, expected_net, abs_tol=0.25):
            issues["net_premium_mismatch"] = (
                f"stored {stored_net} vs leg sum {expected_net}"
            )

    if issues:
        reason = f"Multi-leg entry validation failed on {len(issues)} check(s)"
        return False, reason, issues
    return True, "", {}


def validate_trade_close_data(
    table: str,
    trade_id: int,
    exit_underlying: float | None,
    exit_premium: float | None,
) -> tuple[bool, str]:
    """Validate exit data before closing single-leg trade."""
    if trade_id <= 0:
        return False, f"Invalid trade_id: {trade_id}"
    if exit_underlying is not None and exit_underlying <= 0:
        return False, f"Invalid exit_underlying: {exit_underlying}"
    if exit_premium is not None and exit_premium < 0:
        return False, f"Negative exit_premium: {exit_premium}"
    return True, ""


def validate_multileg_close_data(
    book_id: str,
    exit_underlying: float | None,
    leg_exits: list[dict] | None,
) -> tuple[bool, str]:
    """Validate exit data before closing a multi-leg book."""
    if not book_id:
        return False, "Empty book_id"
    if exit_underlying is not None and exit_underlying <= 0:
        return False, f"Invalid exit_underlying: {exit_underlying}"
    if leg_exits:
        for idx, le in enumerate(leg_exits):
            ep = _number(le.get("exit_premium"))
            if ep is not None and ep < 0:
                return False, f"Negative exit_premium on leg {le.get('id', idx)}: {ep}"
    return True, ""


# ── Multi-Leg Post-Persistence Audits ─────────────────────────────────────────


def audit_multileg_entry(trade_id: int) -> None:
    """Post-persistence audit for multi_leg_trades and its associated legs."""
    from src.models.schema import get_conn

    with get_conn(read_only=True) as conn:
        book = conn.execute(
            "SELECT id, symbol, structure, net_premium, entry_underlying, status FROM multi_leg_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if not book:
            return
        legs = conn.execute(
            "SELECT id, side, lots, strike, option_type, entry_premium, status FROM multi_leg_legs WHERE trade_id=?",
            (trade_id,),
        ).fetchall()

    issues: dict[str, object] = {}
    symbol = str(book["symbol"] or "")
    if not symbol:
        issues["symbol"] = "Missing symbol"

    und = _number(book["entry_underlying"])
    if und is None or und <= 0:
        issues["entry_underlying"] = book["entry_underlying"]

    if not legs or len(legs) > 6:
        issues["legs_count"] = len(legs)

    sell_sum = 0.0
    buy_sum = 0.0
    for l in legs:
        lid = l["id"]
        side = str(l["side"] or "").upper()
        opt = str(l["option_type"] or "").upper()
        strike = _number(l["strike"])
        ep = _number(l["entry_premium"])
        lots = int(l["lots"] or 0)

        if side not in ("BUY", "SELL"):
            issues[f"leg_{lid}_side"] = l["side"]
        if opt not in ("CE", "PE"):
            issues[f"leg_{lid}_type"] = l["option_type"]
        if strike is None or strike <= 0:
            issues[f"leg_{lid}_strike"] = l["strike"]
        if ep is None or ep <= 0:
            issues[f"leg_{lid}_entry_premium"] = l["entry_premium"]
        else:
            if side == "SELL":
                sell_sum += ep
            elif side == "BUY":
                buy_sum += ep
        if lots <= 0:
            issues[f"leg_{lid}_lots"] = l["lots"]

    stored_net = _number(book["net_premium"])
    if stored_net is not None and not issues:
        expected_net = round(sell_sum - buy_sum, 2)
        if expected_net > 0 and not isclose(stored_net, expected_net, abs_tol=0.25):
            issues["net_premium_mismatch"] = f"stored {stored_net} vs leg sum {expected_net}"

    if issues:
        _alert("multi_leg_trades", trade_id, {"symbol": symbol, **issues})


def audit_multileg_close(trade_id: int) -> None:
    """Recompute realized multi-leg P&L across all legs and alert on discrepancies."""
    from src.models.schema import get_conn

    with get_conn(read_only=True) as conn:
        book = conn.execute(
            "SELECT id, symbol, structure, net_premium, total_pnl, status FROM multi_leg_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if not book:
            return
        legs = conn.execute(
            "SELECT id, side, lots, strike, option_type, entry_premium, exit_premium, status FROM multi_leg_legs WHERE trade_id=?",
            (trade_id,),
        ).fetchall()

    symbol = str(book["symbol"] or "")
    base_sym = symbol.upper().split()[0] if symbol else ""
    lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 1))

    issues: dict[str, object] = {}
    computed_pnl = 0.0
    unclosed_legs: list[int] = []
    synthetic_exits: list[int] = []

    for l in legs:
        lid = int(l["id"])
        side = str(l["side"] or "SELL").upper()
        lots = int(l["lots"] or 1)
        entry_prem = _number(l["entry_premium"]) or 0.0
        exit_prem = _number(l["exit_premium"])

        if exit_prem is None or str(l["status"] or "").upper() != "CLOSED":
            unclosed_legs.append(lid)
            continue

        if exit_prem < 0:
            issues[f"leg_{lid}_negative_exit"] = exit_prem

        # Check for fallback dummy exit premium (where exit equals entry)
        if isclose(entry_prem, exit_prem, abs_tol=0.001):
            synthetic_exits.append(lid)

        pnl_pts = (entry_prem - exit_prem) if side == "SELL" else (exit_prem - entry_prem)
        computed_pnl += pnl_pts * lot_size * lots

    stored_pnl = _number(book["total_pnl"])
    if stored_pnl is not None:
        # If all legs had exit == entry but stored_pnl was significantly non-zero
        if len(synthetic_exits) == len(legs) and len(legs) > 0 and abs(stored_pnl) > 10.0:
            issues["synthetic_exit_anomaly"] = (
                f"All {len(legs)} legs exit==entry (computed ₹0.0) but stored PnL is ₹{stored_pnl:.2f}"
            )
        elif not isclose(stored_pnl, computed_pnl, abs_tol=1.0):
            issues["stored_pnl"] = round(stored_pnl, 2)
            issues["expected_pnl"] = round(computed_pnl, 2)
            issues["pnl_diff"] = round(abs(stored_pnl - computed_pnl), 2)

    if issues:
        _alert("multi_leg_trades", trade_id, {"symbol": symbol, **issues})


def audit_multileg_adjustment(
    trade_id: int, close_leg_id: int, new_leg: dict
) -> None:
    """Verify that a leg roll/adjustment was executed consistently in SQLite."""
    from src.models.schema import get_conn

    with get_conn(read_only=True) as conn:
        closed_leg = conn.execute(
            "SELECT id, status, exit_premium FROM multi_leg_legs WHERE id=? AND trade_id=?",
            (close_leg_id, trade_id),
        ).fetchone()
        book = conn.execute(
            "SELECT id, symbol, adjustment_count FROM multi_leg_trades WHERE id=?",
            (trade_id,),
        ).fetchone()

    issues: dict[str, object] = {}
    if not closed_leg:
        issues["closed_leg"] = f"Leg {close_leg_id} not found"
    else:
        if str(closed_leg["status"] or "").upper() != "CLOSED":
            issues["closed_leg_status"] = closed_leg["status"]
        if _number(closed_leg["exit_premium"]) is None or float(closed_leg["exit_premium"]) < 0:
            issues["closed_leg_exit_premium"] = closed_leg["exit_premium"]

    if _number(new_leg.get("entry_premium")) is None or float(new_leg.get("entry_premium", 0)) <= 0:
        issues["new_leg_entry_premium"] = new_leg.get("entry_premium")
    if _number(new_leg.get("strike")) is None or float(new_leg.get("strike", 0)) <= 0:
        issues["new_leg_strike"] = new_leg.get("strike")

    if issues:
        symbol = str(book["symbol"] if book else "")
        _alert("multi_leg_trades:ADJUSTMENT", trade_id, {"symbol": symbol, **issues})

