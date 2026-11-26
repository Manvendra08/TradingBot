"""Tests for stock futures configuration and storage."""

import json
from pathlib import Path
from config.stockfut_loader import load_stockfut_config, load_stockfut_sectors, DEFAULT_STOCKFUT_CONFIG
from src.models.schema import (
    init_db,
    insert_stockfut_trade,
    get_open_stockfut_trades,
    get_stockfut_trade_by_id,
    close_stockfut_trade,
    log_stockfut_decision,
    insert_stockfut_watchlist,
    get_latest_stockfut_watchlist,
    update_stockfut_watchlist_status,
    insert_stockfut_order_intent,
    get_stockfut_order_intent,
    update_stockfut_order_intent,
    get_read_conn,
)


def test_loader_fails_closed_on_bad_json(tmp_path: Path):
    bad_file = tmp_path / "bad_stockfut.json"
    bad_file.write_text("{ corrupt json ...", encoding="utf-8")
    cfg = load_stockfut_config(bad_file)
    assert cfg["stockfut_live_enabled"] is False
    assert cfg["min_lots"] == DEFAULT_STOCKFUT_CONFIG["min_lots"]
    assert cfg["target_r_ratio"] == 1.6


def test_loader_loads_valid_config():
    cfg = load_stockfut_config()
    assert cfg["stockfut_live_enabled"] is False
    assert cfg["min_lots"] == 2
    assert cfg["target_r_ratio"] == 1.6
    assert "STRONG_BULL" in cfg["matrix_permissions"]
    sectors = load_stockfut_sectors()
    assert sectors.get("RELIANCE") == "OIL_GAS"
    assert sectors.get("HDFCBANK") == "BANKING"


def test_trade_roundtrip_persists_split_lots():
    init_db()
    trade_data = {
        "symbol": "RELIANCE",
        "tradingsymbol": "RELIANCE26NOVFUT",
        "expiry": "2026-11-26",
        "side": "LONG",
        "lots": 5,
        "lot_size": 250,
        "entry_price": 1300.0,
        "entry_time": "2026-11-23T09:30:00+05:30",
        "sl": 1260.0,
        "initial_sl": 1260.0,
        "t1": 1364.0,
        "t2": 1400.0,
        "t1_lots": 2,
        "t2_lots": 3,
        "remaining_qty": 1250,
        "gtt_ids": ["gtt-123", "gtt-456"],
        "status": "OPEN",
        "snapshot_id": "snap-abc-123",
        "mode": "PAPER",
        "order_tag": "SF001",
    }
    trade_id = insert_stockfut_trade(trade_data)
    assert trade_id > 0

    fetched = get_stockfut_trade_by_id(trade_id)
    assert fetched is not None
    assert fetched["symbol"] == "RELIANCE"
    assert fetched["lots"] == 5
    assert fetched["t1_lots"] == 2
    assert fetched["t2_lots"] == 3
    assert fetched["remaining_qty"] == 1250
    assert fetched["status"] == "OPEN"
    assert "gtt-123" in fetched["gtt_ids"]

    open_trades = get_open_stockfut_trades(mode="PAPER")
    assert any(t["id"] == trade_id for t in open_trades)

    close_stockfut_trade(
        trade_id=trade_id,
        exit_price=1364.0,
        exit_time="2026-11-24T14:15:00+05:30",
        exit_reason="T1_HIT",
        pnl=32000.0,
        pnl_r=0.8,
        gross_pnl=35000.0,
        tx_costs=3000.0,
    )
    closed = get_stockfut_trade_by_id(trade_id)
    assert closed["status"] == "CLOSED"
    assert closed["exit_reason"] == "T1_HIT"
    assert closed["pnl"] == 32000.0


def test_decision_log_records_skip_reason():
    init_db()
    log_stockfut_decision(
        kind="SKIP",
        symbol="TCS",
        payload={"reason": "MIN_LOTS_BREACH", "lots": 2, "risk": 110000.0, "cap": 93750.0},
        snapshot_id="snap-test-skip",
    )
    with get_read_conn() as conn:
        row = conn.execute(
            "SELECT * FROM stockfut_decisions WHERE symbol = 'TCS' AND kind = 'SKIP' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert payload["reason"] == "MIN_LOTS_BREACH"
        assert row["snapshot_id"] == "snap-test-skip"


def test_watchlist_roundtrip():
    init_db()
    row_data = {
        "scan_date": "2026-11-23",
        "scan_time": "19:30:00",
        "symbol": "INFY",
        "tradingsymbol": "INFY26NOVFUT",
        "expiry": "2026-11-26",
        "side": "LONG",
        "bias": "BULL",
        "score": 75,
        "planned_entry": 1850.0,
        "sl": 1800.0,
        "t1": 1930.0,
        "t2": 1980.0,
        "lots": 2,
        "lot_size": 400,
        "r_multiple": 1.6,
        "status": "ARMED",
        "snapshot_id": "snap-infy-1",
        "atr": 32.5,
    }
    w_id = insert_stockfut_watchlist(row_data)
    assert w_id > 0

    latest = get_latest_stockfut_watchlist(scan_date="2026-11-23")
    infy_armed = next(r for r in latest if r["symbol"] == "INFY")
    assert infy_armed["atr"] == 32.5

    update_stockfut_watchlist_status(w_id, status="TRIGGERED")
    latest_updated = get_latest_stockfut_watchlist(scan_date="2026-11-23")
    infy_row = next(r for r in latest_updated if r["id"] == w_id)
    assert infy_row["status"] == "TRIGGERED"


def test_order_intent_roundtrip():
    init_db()
    intent_data = {
        "intent_id": "intent-sf-001",
        "order_tag": "SF001",
        "symbol": "SBIN",
        "tradingsymbol": "SBIN26NOVFUT",
        "side": "BUY",
        "quantity": 1500,
        "order_type": "LIMIT",
        "price": 810.5,
        "status": "PENDING",
    }
    insert_stockfut_order_intent(intent_data)
    intent = get_stockfut_order_intent("intent-sf-001")
    assert intent is not None
    assert intent["symbol"] == "SBIN"
    assert intent["status"] == "PENDING"

    update_stockfut_order_intent("intent-sf-001", status="FILLED", broker_order_id="kite-999")
    intent_filled = get_stockfut_order_intent("intent-sf-001")
    assert intent_filled["status"] == "FILLED"
    assert intent_filled["broker_order_id"] == "kite-999"
