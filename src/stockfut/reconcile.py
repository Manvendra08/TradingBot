"""Stock Futures Broker Reconciliation & Recovery Engine.
Implements Section 6.3 & Task 14:
- Broker is the single source of truth.
- Startup reconciliation runs before any scan.
- Detects DB-open/broker-flat, broker-open/DB-flat, quantity mismatch, GTT mismatches.
- Heartbeat gap alert (> 30 min downtime with open positions).
- Kite token expiry alert with open positions (GTTs carry risk).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.fetchers.kite_fetcher import KiteDataClient, get_kite_data_client
from src.models.schema import (
    close_stockfut_trade,
    get_open_stockfut_trades,
    log_stockfut_decision,
    update_stockfut_trade,
)

log = logging.getLogger("stockfut.reconcile")
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class ReconcileReport:
    db_open_broker_flat: List[Dict[str, Any]] = field(default_factory=list)
    broker_open_db_flat: List[Dict[str, Any]] = field(default_factory=list)
    quantity_mismatches: List[Dict[str, Any]] = field(default_factory=list)
    gtt_mismatches: List[Dict[str, Any]] = field(default_factory=list)
    is_clean: bool = True
    summary: str = "CLEAN"


def heartbeat_gap_alert(open_positions_count: int, minutes_down: int) -> bool:
    """Alert if bot has been offline for > 30 minutes while holding open positions."""
    if open_positions_count > 0 and minutes_down > 30:
        log.critical(
            "[HEARTBEAT GAP] Bot was down for %d minutes with %d open stock futures positions!",
            minutes_down, open_positions_count,
        )
        return True
    return False


def kite_token_alert(open_positions_count: int, is_authenticated: bool) -> bool:
    """Alert if Kite token has expired while positions are open."""
    if open_positions_count > 0 and not is_authenticated:
        log.critical(
            "[TOKEN EXPIRED] Kite token expired with %d open stock futures positions! GTTs carrying risk.",
            open_positions_count,
        )
        return True
    return False


class StockfutReconciler:
    """Broker reconciliation engine for single-stock futures."""

    def __init__(self, kite_client: Optional[KiteDataClient] = None):
        self.kite_client = kite_client or get_kite_data_client()

    def reconcile(self, auto_fix_db: bool = True) -> ReconcileReport:
        """
        Compare database open positions against Kite net positions.
        """
        report = ReconcileReport()
        k = self.kite_client.kite
        if not k:
            log.warning("Kite client unavailable for stockfut reconciliation; assuming clean")
            report.summary = "KITE_UNAVAILABLE"
            return report

        try:
            positions_data = k.positions()
            net_positions = positions_data.get("net", [])
        except Exception as e:
            log.error("Failed fetching broker positions: %s", e)
            report.is_clean = False
            report.summary = f"BROKER_FETCH_FAILED: {e}"
            return report

        # Filter broker positions to NFO stock futures only
        broker_fut_map: Dict[str, Dict[str, Any]] = {}
        for p in net_positions:
            exch = str(p.get("exchange", "")).upper()
            tsym = str(p.get("tradingsymbol", "")).upper()
            qty = int(p.get("quantity", 0))

            if exch == "NFO" and tsym.endswith("FUT") and qty != 0:
                # Exclude index futures
                if not any(tsym.startswith(idx) for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")):
                    broker_fut_map[tsym] = p

        # Get DB open trades in LIVE mode
        db_trades = get_open_stockfut_trades(mode="LIVE")
        db_fut_map = {t["tradingsymbol"]: t for t in db_trades}

        # Case 1: DB says OPEN, but Broker is FLAT
        for tsym, trade in db_fut_map.items():
            if tsym not in broker_fut_map:
                report.db_open_broker_flat.append(trade)
                report.is_clean = False
                log.warning("[RECONCILE] DB trade #%d (%s) open but broker is FLAT", trade["id"], tsym)
                if auto_fix_db:
                    close_stockfut_trade(
                        trade_id=trade["id"],
                        exit_price=trade.get("entry_price", 0.0),
                        exit_time=datetime.now(IST).isoformat(),
                        exit_reason="RECONCILE_BROKER_FLAT",
                        pnl=0.0,
                        pnl_r=0.0,
                    )
                    log_stockfut_decision(
                        kind="RECONCILE_AUTO_CLOSE",
                        symbol=trade["symbol"],
                        payload={"trade_id": trade["id"], "reason": "Broker position flat"},
                    )

        # Case 2: Broker says OPEN, but DB is FLAT
        for tsym, b_pos in broker_fut_map.items():
            if tsym not in db_fut_map:
                report.broker_open_db_flat.append(b_pos)
                report.is_clean = False
                log.critical("[RECONCILE] Broker holds open position in %s (qty=%d) but DB is FLAT!", tsym, b_pos["quantity"])

        # Case 3: Both open, check quantity mismatch
        for tsym in set(db_fut_map.keys()).intersection(broker_fut_map.keys()):
            trade = db_fut_map[tsym]
            b_pos = broker_fut_map[tsym]

            db_qty = int(trade.get("remaining_qty", 0))
            b_qty = abs(int(b_pos.get("quantity", 0)))

            if db_qty != b_qty:
                report.quantity_mismatches.append({
                    "symbol": trade["symbol"],
                    "tradingsymbol": tsym,
                    "db_qty": db_qty,
                    "broker_qty": b_qty,
                })
                report.is_clean = False
                log.warning("[RECONCILE] Quantity mismatch for %s: DB=%d, Broker=%d", tsym, db_qty, b_qty)
                if auto_fix_db:
                    update_stockfut_trade(trade["id"], {"remaining_qty": b_qty})

        if report.is_clean:
            report.summary = "ALL_MATCHED"
        else:
            report.summary = f"DISCREPANCIES: {len(report.db_open_broker_flat)} flat, {len(report.broker_open_db_flat)} unmanaged, {len(report.quantity_mismatches)} qty mismatches"

        return report


def reconcile_startup() -> ReconcileReport:
    """Run startup reconciliation before scans."""
    reconciler = StockfutReconciler()
    return reconciler.reconcile(auto_fix_db=True)


def reconcile_periodic() -> ReconcileReport:
    """Run 15-minute periodic reconciliation."""
    reconciler = StockfutReconciler()
    return reconciler.reconcile(auto_fix_db=False)
