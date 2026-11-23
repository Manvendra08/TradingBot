"""Tests for stock futures broker reconciliation."""

from unittest.mock import MagicMock
from src.models.schema import init_db, insert_stockfut_trade, get_stockfut_trade_by_id
from src.stockfut.reconcile import (
    StockfutReconciler,
    heartbeat_gap_alert,
    kite_token_alert,
)


def test_heartbeat_gap_alert():
    # 2 positions open, down for 45 minutes -> ALERT!
    assert heartbeat_gap_alert(open_positions_count=2, minutes_down=45) is True
    # 2 positions open, down for 15 minutes -> No alert
    assert heartbeat_gap_alert(open_positions_count=2, minutes_down=15) is False
    # 0 positions open, down for 60 minutes -> No alert
    assert heartbeat_gap_alert(open_positions_count=0, minutes_down=60) is False


def test_kite_token_alert():
    # Positions open, unauthenticated -> ALERT!
    assert kite_token_alert(open_positions_count=3, is_authenticated=False) is True
    # Positions open, authenticated -> No alert
    assert kite_token_alert(open_positions_count=3, is_authenticated=True) is False


def test_reconcile_db_open_broker_flat():
    init_db()
    # Insert open LIVE trade
    trade_id = insert_stockfut_trade({
        "symbol": "RELIANCE",
        "tradingsymbol": "RELIANCE26NOVFUT",
        "expiry": "2026-11-26",
        "side": "LONG",
        "lots": 2,
        "lot_size": 250,
        "entry_price": 1300.0,
        "entry_time": "2026-11-23T09:30:00+05:30",
        "sl": 1260.0,
        "t1": 1364.0,
        "t2": 1400.0,
        "t1_lots": 1,
        "t2_lots": 1,
        "remaining_qty": 500,
        "status": "OPEN",
        "mode": "LIVE",
    })

    mock_client = MagicMock()
    mock_kite = MagicMock()
    # Broker has NO open positions
    mock_kite.positions.return_value = {"net": []}
    mock_client.kite = mock_kite

    reconciler = StockfutReconciler(kite_client=mock_client)
    report = reconciler.reconcile(auto_fix_db=True)

    assert report.is_clean is False
    assert len(report.db_open_broker_flat) == 1
    # Check that trade was auto-closed in DB
    trade_after = get_stockfut_trade_by_id(trade_id)
    assert trade_after["status"] == "CLOSED"
    assert trade_after["exit_reason"] == "RECONCILE_BROKER_FLAT"
