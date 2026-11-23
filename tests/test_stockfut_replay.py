"""Tests for stock futures replay engine."""

from src.stockfut.replay import ReplayEngine, ReplayTrade


def test_replay_initialization():
    engine = ReplayEngine(capital=10000000.0)
    report = engine.run_replay(symbol_daily_data={}, lot_sizes={})
    assert report.total_trades == 0
    assert report.net_positive_rate == 0.0


def test_costs_reduce_pnl():
    # If gross pnl is 10,000, net pnl must be strictly less than 10,000
    t = ReplayTrade(
        symbol="RELIANCE",
        side="LONG",
        entry_date="2026-11-20",
        exit_date="2026-11-23",
        entry_price=1300.0,
        exit_price=1320.0,
        lots=2,
        lot_size=250,
        sl=1270.0,
        t1=1350.0,
        t2=1380.0,
        exit_rule="TIME_STOP",
        gross_pnl=10000.0,
        net_pnl=9200.0,
        tx_costs=800.0,
        pnl_r=0.61,
        days_held=3,
    )
    assert t.net_pnl < t.gross_pnl
    assert t.tx_costs > 0
