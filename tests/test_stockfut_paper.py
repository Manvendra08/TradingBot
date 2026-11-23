"""Tests for paper execution engine."""

from src.models.schema import init_db, get_stockfut_trade_by_id
from src.stockfut.executor import PaperExecutor, journal_row
from src.stockfut.exits import ExitDecision
from src.stockfut.setups import WatchRow


def test_fill_uses_spread_and_costs():
    init_db()
    executor = PaperExecutor()
    row = WatchRow(
        scan_date="2026-11-23",
        scan_time="19:30:00",
        symbol="SBIN",
        tradingsymbol="SBIN26NOVFUT",
        expiry="2026-11-26",
        side="LONG",
        bias="BULL",
        score=75,
        planned_entry=800.0,
        sl=780.0,
        t1=835.0,
        t2=860.0,
        lots=2,
        lot_size=750,
        r_multiple=1.75,
    )
    quote = {"price": 801.0, "bid": 800.8, "ask": 801.2}
    trade_id = executor.enter(row, quote=quote)
    assert trade_id is not None
    trade = get_stockfut_trade_by_id(trade_id)
    assert trade["status"] == "OPEN"
    # Long fill price is at or above ltp due to slippage
    assert trade["entry_price"] >= 801.0


def test_t1_partial_reduces_remaining_qty():
    init_db()
    executor = PaperExecutor()
    row = WatchRow(
        scan_date="2026-11-23",
        scan_time="19:30:00",
        symbol="INFY",
        tradingsymbol="INFY26NOVFUT",
        expiry="2026-11-26",
        side="LONG",
        bias="BULL",
        score=80,
        planned_entry=1850.0,
        sl=1800.0,
        t1=1930.0,
        t2=1980.0,
        lots=4,
        lot_size=400,
        r_multiple=1.6,
    )
    quote_entry = {"price": 1850.0, "bid": 1849.9, "ask": 1850.1}
    trade_id = executor.enter(row, quote=quote_entry)
    assert trade_id is not None

    # T1 Hit decision
    dec = ExitDecision(rule="T1_HIT", action="PARTIAL_T1", lots=2, new_sl=1840.0)
    quote_exit = {"price": 1930.0, "bid": 1929.9, "ask": 1930.1}
    executor.apply_exit(trade_id, dec, quote=quote_exit)

    trade_updated = get_stockfut_trade_by_id(trade_id)
    assert trade_updated["status"] == "PARTIAL"
    # Remaining quantity should be reduced from 1600 (4 lots) to 800 (2 lots)
    assert trade_updated["remaining_qty"] == 800
    assert trade_updated["sl"] == 1840.0
    assert trade_updated["pnl"] > 0
