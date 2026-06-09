import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from src.engine.paper_trading import run_timeframe_strategy
from src.models.schema import get_conn, init_db

@pytest.fixture(autouse=True)
def setup_test_db():
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM scan_summaries")

def test_timeframe_strategy_long_entry():
    scan_context = {
        "underlying": 23000.0,
        "atm_strike": 23000.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1500000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23050},
                "prev_ohlc": {"open": 22800, "high": 23000, "low": 22800, "close": 22950},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23020},
                "prev_ohlc": {"open": 22950, "high": 22990, "low": 22940, "close": 22980},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        },
        "option_rows": [
            {"strike": 22800.0, "option_type": "CE", "ltp": 250.0}
        ]
    }

    # Insert a scan summary from 1.5 hours ago to establish bullish bias
    # (PE OI increased by 500k, CE OI remained flat)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-01T10:30:00Z", 1000000, 1000000)
        )

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {"verdict_label": "Long Buildup", "confidence": 80})

    # Assert that trade was opened
    with get_conn() as conn:
        trades = conn.execute("SELECT * FROM paper_trades WHERE symbol='NIFTY'").fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["verdict_label"] == "LONG"
        assert trade["option_type"] == "CE"
        assert trade["strike"] == 22800.0  # ATM 23000 - 4 * 50 step = 22800
        assert trade["entry_premium"] == 250.0
        assert trade["setup_type"] == "TIMEFRAME"
        assert trade["status"] == "OPEN"

def test_timeframe_strategy_short_entry():
    scan_context = {
        "underlying": 23000.0,
        "atm_strike": 23000.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1500000,
        "total_pe_oi": 1000000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 23050, "high": 23100, "low": 22980, "close": 22950},
                "prev_ohlc": {"open": 23100, "high": 23200, "low": 23000, "close": 23050},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                "ohlc": {"open": 23020, "high": 23040, "low": 22990, "close": 22990},
                "prev_ohlc": {"open": 23040, "high": 23080, "low": 23020, "close": 23040},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        },
        "option_rows": [
            {"strike": 23200.0, "option_type": "PE", "ltp": 220.0}
        ]
    }

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-01T10:30:00Z", 1000000, 1000000)
        )

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {"verdict_label": "Short Buildup", "confidence": 80})

    with get_conn() as conn:
        trades = conn.execute("SELECT * FROM paper_trades WHERE symbol='NIFTY'").fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["verdict_label"] == "SHORT"
        assert trade["option_type"] == "PE"
        assert trade["strike"] == 23200.0  # ATM 23000 + 4 * 50 step = 23200
        assert trade["entry_premium"] == 220.0
        assert trade["setup_type"] == "TIMEFRAME"

def test_timeframe_strategy_exit_long():
    # Insert an open trade first
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, status, setup_type, lots)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-01T11:30:00Z", "NIFTY", "LONG", "CE", 22800.0, 23000.0, 250.0, "OPEN", "TIMEFRAME", 1)
        )
        # Establish bearish bias (CE OI increased by 500k, PE flat)
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-01T11:00:00Z", 1000000, 1000000)
        )

    scan_context = {
        "underlying": 22900.0,
        "atm_strike": 22900.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1500000,
        "total_pe_oi": 1000000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23020},
                "prev_ohlc": {"open": 22800, "high": 23000, "low": 22800, "close": 22950},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                # 1H close 22900 is below prev low 22950
                "ohlc": {"open": 22980, "high": 23020, "low": 22900, "close": 22900},
                "prev_ohlc": {"open": 22950, "high": 22990, "low": 22950, "close": 22980},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        },
        "option_rows": [
            {"strike": 22800.0, "option_type": "CE", "ltp": 120.0}
        ]
    }

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trades = conn.execute("SELECT * FROM paper_trades WHERE symbol='NIFTY'").fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["status"] == "CLOSED_MANUAL"
        assert trade["exit_premium"] == 120.0
        assert trade["pnl_points"] == -130.0  # 120 - 250
        assert trade["pnl_rupees"] == -3250.0  # -130 * 25 (lot size)

def test_timeframe_strategy_natgas_future():
    scan_context = {
        "underlying": 310.0,
        "atm_strike": 310.0,
        "expiry": "2026-06-25",
        "total_ce_oi": 50000,
        "total_pe_oi": 80000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 305, "high": 312, "low": 304, "close": 311},
                "prev_ohlc": {"open": 300, "high": 308, "low": 300, "close": 305},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                "ohlc": {"open": 308, "high": 312, "low": 308, "close": 311},
                "prev_ohlc": {"open": 305, "high": 309, "low": 305, "close": 308},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        }
    }

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NATURALGAS", "2026-06-25", "2026-06-01T10:30:00Z", 50000, 50000)
        )

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")):
        run_timeframe_strategy("NATURALGAS", scan_context, "digest-123", {})

    with get_conn() as conn:
        trades = conn.execute("SELECT * FROM paper_trades WHERE symbol='NATURALGAS'").fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["verdict_label"] == "LONG"
        assert trade["option_type"] == "FUT"
        assert trade["entry_underlying"] == 310.0
        assert trade["entry_premium"] == 310.0
        assert trade["status"] == "OPEN"

def test_timeframe_strategy_option_sl_hit():
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, sl_premium, status, setup_type, lots)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-01T11:30:00Z", "NIFTY", "LONG", "CE", 22800.0, 23000.0, 250.0, 187.50, "OPEN", "TIMEFRAME", 1)
        )
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-01T11:00:00Z", 1000000, 1500000)
        )

    scan_context = {
        "underlying": 23000.0,
        "atm_strike": 23000.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1500000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23000},
                "prev_ohlc": {"open": 22800, "high": 23000, "low": 22800, "close": 22950},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23000},
                "prev_ohlc": {"open": 22950, "high": 22990, "low": 22950, "close": 22980},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        },
        "option_rows": [
            {"strike": 22800.0, "option_type": "CE", "ltp": 180.0}
        ]
    }

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trades = conn.execute("SELECT * FROM paper_trades WHERE symbol='NIFTY'").fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["status"] == "CLOSED_SL"
        assert trade["exit_premium"] == 180.0

def test_timeframe_strategy_dead_trade_exit():
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, sl_premium, status, setup_type, lots, max_favorable_r)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-01T08:00:00Z", "NIFTY", "LONG", "CE", 22800.0, 23000.0, 250.0, 187.50, "OPEN", "TIMEFRAME", 1, 0.1)
        )
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-01T08:00:00Z", 1000000, 1500000)
        )

    scan_context = {
        "underlying": 23000.0,
        "atm_strike": 23000.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1500000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23000},
                "prev_ohlc": {"open": 22800, "high": 23000, "low": 22800, "close": 22950},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23000},
                "prev_ohlc": {"open": 22950, "high": 22990, "low": 22950, "close": 22980},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        },
        "option_rows": [
            {"strike": 22800.0, "option_type": "CE", "ltp": 260.0}
        ]
    }

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trades = conn.execute("SELECT * FROM paper_trades WHERE symbol='NIFTY'").fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["status"] == "CLOSED_MANUAL"
        assert "Dead trade exit" in trade["reason"]

def test_timeframe_strategy_pyramiding():
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, sl_premium, status, setup_type, lots, pyramid_level)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-01T08:00:00Z", "NIFTY", "LONG", "CE", 22800.0, 23000.0, 250.0, 187.50, "OPEN", "TIMEFRAME", 4, 1)
        )
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-01T10:30:00Z", 1000000, 1000000)
        )

    scan_context = {
        "underlying": 23000.0,
        "atm_strike": 23000.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1500000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23080},
                "prev_ohlc": {"open": 22800, "high": 23000, "low": 22800, "close": 22950},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23080},
                "prev_ohlc": {"open": 22950, "high": 22990, "low": 22950, "close": 22980},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        },
        "option_rows": [
            {"strike": 22800.0, "option_type": "CE", "ltp": 300.0}
        ]
    }

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")), \
         patch("src.engine.paper_trading.DEFAULT_LOTS_PER_TRADE", 4):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trades = conn.execute("SELECT * FROM paper_trades WHERE symbol='NIFTY' ORDER BY opened_at ASC").fetchall()
        assert len(trades) == 2
        trade2 = dict(trades[1])
        assert trade2["lots"] == 3
        assert trade2["pyramid_level"] == 2
        assert trade2["signal_key"] == "NIFTY:TIMEFRAME:3H:LONG:2026-06-01T12:00:00Z"


def test_timeframe_strategy_exit_long_large_move_candle_only():
    # Large move (> 2x breakout buffer) should trigger exit regardless of OI support
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, status, setup_type, lots)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-01T11:30:00Z", "NIFTY", "LONG", "CE", 22800.0, 23000.0, 250.0, "OPEN", "TIMEFRAME", 1)
        )
        # Establish neutral OI context (no bias change)
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-01T11:00:00Z", 1000000, 1000000)
        )

    scan_context = {
        "underlying": 22890.0,
        "atm_strike": 22900.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1000000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23020},
                "prev_ohlc": {"open": 22800, "high": 23000, "low": 22800, "close": 22950},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                # 1H close 22890 is below prev low 22950
                # crossover_size = 22950 - 22890 = 60
                # 2x breakout_buffer = 2x (22890 * 0.001) = 45.78 -> large move
                "ohlc": {"open": 22980, "high": 23020, "low": 22890, "close": 22890},
                "prev_ohlc": {"open": 22950, "high": 22990, "low": 22950, "close": 22980},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        },
        "option_rows": [
            {"strike": 22800.0, "option_type": "CE", "ltp": 100.0}
        ]
    }

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trade = conn.execute("SELECT * FROM paper_trades WHERE symbol='NIFTY'").fetchone()
        assert trade["status"] == "CLOSED_MANUAL"
        assert "Large reversal move" in trade["reason"]

def test_timeframe_strategy_exit_long_small_move_with_oi():
    # Small move crossover (< 2x breakout buffer) WITH bearish OI bias should trigger exit
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, status, setup_type, lots)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-01T11:30:00Z", "NIFTY", "LONG", "CE", 22800.0, 23000.0, 250.0, "OPEN", "TIMEFRAME", 1)
        )
        # Establish base OI
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-01T11:00:00Z", 1000000, 1000000)
        )

    scan_context = {
        "underlying": 22930.0,
        "atm_strike": 22900.0,
        "expiry": "2026-06-11",
        # Bears added 500k CE OI, PE flat -> Bearish OI bias
        "total_ce_oi": 1500000,
        "total_pe_oi": 1000000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23020},
                "prev_ohlc": {"open": 22800, "high": 23000, "low": 22800, "close": 22950},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                # 1H close 22930 is below prev low 22950
                # crossover_size = 22950 - 22930 = 20
                # 2x breakout_buffer = 2x (22930 * 0.001) = 45.86 -> small move
                "ohlc": {"open": 22980, "high": 23020, "low": 22930, "close": 22930},
                "prev_ohlc": {"open": 22950, "high": 22990, "low": 22950, "close": 22980},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        },
        "option_rows": [
            {"strike": 22800.0, "option_type": "CE", "ltp": 130.0}
        ]
    }

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trade = conn.execute("SELECT * FROM paper_trades WHERE symbol='NIFTY'").fetchone()
        assert trade["status"] == "CLOSED_MANUAL"
        assert "Short OI bias" in trade["reason"]

def test_timeframe_strategy_exit_long_small_move_no_oi():
    # Small move crossover (< 2x breakout buffer) WITHOUT bearish OI bias should NOT trigger exit
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, status, setup_type, lots)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-01T11:30:00Z", "NIFTY", "LONG", "CE", 22800.0, 23000.0, 250.0, "OPEN", "TIMEFRAME", 1)
        )
        # Establish base OI
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-01T11:00:00Z", 1000000, 1000000)
        )

    scan_context = {
        "underlying": 22930.0,
        "atm_strike": 22900.0,
        "expiry": "2026-06-11",
        # Flat OI context -> no bias
        "total_ce_oi": 1000000,
        "total_pe_oi": 1000000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23020},
                "prev_ohlc": {"open": 22800, "high": 23000, "low": 22800, "close": 22950},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            },
            "1h": {
                # crossover_size = 20 < 45.86 -> small move
                "ohlc": {"open": 22980, "high": 23020, "low": 22930, "close": 22930},
                "prev_ohlc": {"open": 22950, "high": 22990, "low": 22950, "close": 22980},
                "bar_end_utc": "2026-06-01T12:00:00Z"
            }
        },
        "option_rows": [
            {"strike": 22800.0, "option_type": "CE", "ltp": 130.0}
        ]
    }

    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trade = conn.execute("SELECT * FROM paper_trades WHERE symbol='NIFTY'").fetchone()
        assert trade["status"] == "OPEN"

