"""Tests for stock futures exit matrix rules and priority."""

from datetime import datetime, date, timedelta, timezone
from src.stockfut.exits import evaluate_future_exit, ExitDecision

IST = timezone(timedelta(hours=5, minutes=30))


def test_priority_hard_stop_beats_time_stop():
    # Long trade: entry 1000, SL 975, held for 6 sessions (eligible for TIME_STOP)
    # But current LTP = 970 (breaching SL) -> HARD_STOP must take priority!
    trade = {
        "symbol": "INFY",
        "side": "LONG",
        "lots": 4,
        "lot_size": 400,
        "remaining_qty": 1600,
        "entry_price": 1000.0,
        "sl": 975.0,
        "t1": 1050.0,
        "t2": 1100.0,
        "entry_time": (datetime.now(IST) - timedelta(days=8)).isoformat(),
        "status": "OPEN",
    }
    market = {"price": 970.0, "low": 968.0, "high": 980.0, "open": 980.0}
    dec = evaluate_future_exit(trade, market)
    assert dec is not None
    assert dec.rule == "HARD_STOP"
    assert dec.action == "EXIT_ALL"


def test_t1_books_floor_half_and_moves_stop_to_entry_minus_quarter_atr():
    # 5 lots trade: t1_lots = 2, t2_lots = 3
    trade = {
        "symbol": "RELIANCE",
        "side": "LONG",
        "lots": 5,
        "lot_size": 250,
        "t1_lots": 2,
        "t2_lots": 3,
        "remaining_qty": 1250,
        "entry_price": 1300.0,
        "sl": 1260.0,
        "t1": 1364.0,
        "t2": 1400.0,
        "atr": 32.0,
        "status": "OPEN",
    }
    market = {"price": 1365.0, "high": 1368.0, "low": 1340.0, "open": 1345.0}
    dec = evaluate_future_exit(trade, market)
    assert dec is not None
    assert dec.rule == "T1_HIT"
    assert dec.action == "PARTIAL_T1"
    assert dec.lots == 2
    # new_sl = 1300.0 - 0.25 * 32.0 = 1292.0
    assert dec.new_sl == 1292.0


def test_expiry_exit_5_sessions_before():
    today = date(2026, 11, 23)
    expiry = "2026-11-26"  # 3 trading sessions away (<= 5)
    trade = {
        "symbol": "SBIN",
        "side": "LONG",
        "lots": 2,
        "lot_size": 750,
        "remaining_qty": 1500,
        "entry_price": 800.0,
        "sl": 780.0,
        "t1": 840.0,
        "t2": 870.0,
        "expiry": expiry,
        "status": "OPEN",
    }
    market = {"price": 810.0, "open": 810.0, "high": 815.0, "low": 808.0}
    now_dt = datetime(2026, 11, 23, 9, 30, tzinfo=IST)
    dec = evaluate_future_exit(trade, market, now=now_dt)
    assert dec is not None
    assert dec.rule == "EXPIRY"
    assert dec.action == "EXIT_ALL"


def test_banned_held_name_exits_only():
    trade = {
        "symbol": "IDEA",
        "side": "LONG",
        "lots": 2,
        "lot_size": 10000,
        "remaining_qty": 20000,
        "entry_price": 10.0,
        "sl": 9.0,
        "t1": 12.0,
        "t2": 14.0,
        "status": "OPEN",
    }
    market = {"price": 10.2, "open": 10.1, "high": 10.3, "low": 10.0}
    dec = evaluate_future_exit(trade, market, ban_list={"IDEA"})
    assert dec is not None
    assert dec.rule == "EVENT"
    assert dec.action == "EXIT_ALL"


def test_short_mirrors_long():
    # Short trade: entry 1000, SL 1040, T1 936, ATR 32
    trade = {
        "symbol": "TATASTEEL",
        "side": "SHORT",
        "lots": 4,
        "lot_size": 5500,
        "t1_lots": 2,
        "t2_lots": 2,
        "remaining_qty": 22000,
        "entry_price": 1000.0,
        "sl": 1040.0,
        "t1": 936.0,
        "t2": 900.0,
        "atr": 32.0,
        "status": "OPEN",
    }
    # T1 touched on Short: low drops to 935 <= 936
    market = {"price": 935.0, "low": 935.0, "high": 960.0, "open": 955.0}
    dec = evaluate_future_exit(trade, market)
    assert dec is not None
    assert dec.rule == "T1_HIT"
    assert dec.action == "PARTIAL_T1"
    assert dec.lots == 2
    # new_sl on short = entry + 0.25 * atr = 1000 + 8 = 1008.0
    assert dec.new_sl == 1008.0
