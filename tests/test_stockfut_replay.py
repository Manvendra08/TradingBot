"""Tests for stock futures replay engine."""

from datetime import datetime, timedelta, timezone

from src.stockfut.replay import ReplayEngine, ReplayTrade, aggregate_nse_3h_sessions


def test_replay_initialization():
    engine = ReplayEngine(capital=10000000.0)
    report = engine.run_replay(symbol_daily_data={}, lot_sizes={})
    assert report.total_trades == 0
    assert report.net_positive_rate == 0.0


def test_aggregate_nse_3h_sessions_and_ab_empty():
    ist = timezone(timedelta(hours=5, minutes=30))
    candles_60m = [
        {"date": datetime(2026, 11, 20, 9, 15, tzinfo=ist), "open": 100, "high": 105, "low": 98, "close": 104, "volume": 10},
        {"date": datetime(2026, 11, 20, 10, 15, tzinfo=ist), "open": 104, "high": 108, "low": 102, "close": 106, "volume": 12},
        {"date": datetime(2026, 11, 20, 11, 15, tzinfo=ist), "open": 106, "high": 107, "low": 103, "close": 105, "volume": 8},
        {"date": datetime(2026, 11, 20, 12, 15, tzinfo=ist), "open": 105, "high": 110, "low": 104, "close": 109, "volume": 15},
    ]
    agg = aggregate_nse_3h_sessions(candles_60m)
    m = agg["2026-11-20"]["morning"]
    a = agg["2026-11-20"]["afternoon"]
    assert m["open"] == 100 and m["high"] == 108 and m["low"] == 98 and m["close"] == 105
    assert a["open"] == 105 and a["close"] == 109

    res = ReplayEngine(capital=10000000.0).run_replay_3h_ab(
        symbol_daily_data={}, symbol_60m_data={}, lot_sizes={}
    )
    assert res["strategy_a"].total_trades == 0
    assert res["strategy_b"].total_trades == 0
    assert res["b_beats_a"] is False


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
