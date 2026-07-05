from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

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
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23200},
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23020},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22940,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 250.0}],
    }

    # Insert 5 scan summaries with underlying > 0 for regime detection
    with get_conn() as conn:
        base_ts = datetime.fromisoformat("2026-06-01T10:30:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
            )

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy(
            "NIFTY",
            scan_context,
            "digest-123",
            {"verdict_label": "Long Buildup", "confidence": 80},
        )

    # Assert that trade was opened
    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchall()
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
                "ohlc": {"open": 23050, "high": 23100, "low": 22980, "close": 22800},
                "prev_ohlc": {
                    "open": 23100,
                    "high": 23200,
                    "low": 23000,
                    "close": 23050,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 23020, "high": 23040, "low": 22990, "close": 22990},
                "prev_ohlc": {
                    "open": 23040,
                    "high": 23080,
                    "low": 23020,
                    "close": 23040,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 23200.0, "option_type": "PE", "ltp": 220.0}],
    }

    with get_conn() as conn:
        base_ts = datetime.fromisoformat("2026-06-01T10:30:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
            )

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy(
            "NIFTY",
            scan_context,
            "digest-123",
            {"verdict_label": "Short Buildup", "confidence": 80},
        )

    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchall()
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
            (
                "2026-06-01T11:30:00Z",
                "NIFTY",
                "LONG",
                "CE",
                22800.0,
                23000.0,
                250.0,
                "OPEN",
                "TIMEFRAME",
                1,
            ),
        )
        from config.settings import LOT_SIZES

        conn.execute(
            "UPDATE paper_trades SET lot_size=? WHERE symbol='NIFTY' AND status='OPEN'",
            (LOT_SIZES.get("NIFTY", 1),),
        )
        # Insert 5 scan summaries with underlying > 0 for regime detection
        base_ts = datetime.fromisoformat("2026-06-01T11:00:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
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
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                # 1H close 22900 is below prev low 22950
                "ohlc": {"open": 22980, "high": 23020, "low": 22900, "close": 22900},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22950,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 120.0}],
    }

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["status"] == "TF-1H-Cross"
        assert trade["exit_premium"] == 120.0
        assert trade["pnl_points"] == -130.0  # 120 - 250
        assert (
            trade["pnl_rupees"] == -8515.03
        )  # -130 * 65 (lot size) - 65.03 (tx costs)


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
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 308, "high": 312, "low": 308, "close": 311},
                "prev_ohlc": {"open": 305, "high": 309, "low": 305, "close": 308},
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
    }

    with get_conn() as conn:
        base_ts = datetime.fromisoformat("2026-06-01T10:30:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NATURALGAS", "2026-06-25", ts, 50000, 50000, 310.0),
            )

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy(
            "NATURALGAS",
            scan_context,
            "digest-123",
            {"verdict_label": "Long Buildup", "confidence": 80},
        )

    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NATURALGAS'"
        ).fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["verdict_label"] == "LONG"
        assert trade["side"] == "BUY"
        assert trade["option_type"] == "FUT"
        assert trade["entry_underlying"] == 310.0
        assert trade["entry_premium"] == 310.0
        assert trade["status"] == "OPEN"


def test_timeframe_strategy_natgas_future_short():
    scan_context = {
        "underlying": 300.0,
        "atm_strike": 300.0,
        "expiry": "2026-06-25",
        "total_ce_oi": 80000,
        "total_pe_oi": 50000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 305, "high": 312, "low": 304, "close": 299},
                "prev_ohlc": {"open": 308, "high": 312, "low": 304, "close": 308},
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 302, "high": 304, "low": 298, "close": 299},
                "prev_ohlc": {"open": 304, "high": 306, "low": 302, "close": 304},
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
    }

    with get_conn() as conn:
        base_ts = datetime.fromisoformat("2026-06-01T10:30:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NATURALGAS", "2026-06-25", ts, 50000, 50000, 300.0),
            )

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy(
            "NATURALGAS",
            scan_context,
            "digest-123",
            {"verdict_label": "Short Buildup", "confidence": 80},
        )

    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NATURALGAS'"
        ).fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["verdict_label"] == "SHORT"
        assert trade["side"] == "SELL"
        assert trade["option_type"] == "FUT"
        assert trade["entry_underlying"] == 300.0
        assert trade["entry_premium"] == 300.0
        assert trade["status"] == "OPEN"


def test_timeframe_strategy_option_sl_hit():
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, sl_premium, status, setup_type, lots)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-01T11:30:00Z",
                "NIFTY",
                "LONG",
                "CE",
                22800.0,
                23000.0,
                250.0,
                187.50,
                "OPEN",
                "TIMEFRAME",
                1,
            ),
        )
        # Insert 5 scan summaries with underlying > 0 for regime detection
        base_ts = datetime.fromisoformat("2026-06-01T11:00:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1500000, 23000.0),
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
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23000},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22950,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 180.0}],
    }

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchall()
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
            (
                "2026-06-01T08:00:00Z",
                "NIFTY",
                "LONG",
                "CE",
                22800.0,
                23000.0,
                250.0,
                187.50,
                "OPEN",
                "TIMEFRAME",
                1,
                0.1,
            ),
        )
        base_ts = datetime.fromisoformat("2026-06-01T08:00:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1500000, 23000.0),
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
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23000},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22950,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 260.0}],
    }

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["status"] == "Dead Trade"
        assert "Dead trade exit" in trade["reason"]


def test_timeframe_strategy_pyramiding():
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, sl_premium, status, setup_type, lots, pyramid_level)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-01T08:00:00Z",
                "NIFTY",
                "LONG",
                "CE",
                22800.0,
                23000.0,
                250.0,
                187.50,
                "OPEN",
                "TIMEFRAME",
                4,
                1,
            ),
        )
        base_ts = datetime.fromisoformat("2026-06-01T10:30:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
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
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23080},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22950,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 300.0}],
    }

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
        patch("src.engine.paper_trading.DEFAULT_LOTS_PER_TRADE", 4),
    ):
        run_timeframe_strategy(
            "NIFTY",
            scan_context,
            "digest-123",
            {"verdict_label": "Long Buildup", "confidence": 80},
        )

    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY' ORDER BY opened_at ASC"
        ).fetchall()
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
            (
                "2026-06-01T11:30:00Z",
                "NIFTY",
                "LONG",
                "CE",
                22800.0,
                23000.0,
                250.0,
                "OPEN",
                "TIMEFRAME",
                1,
            ),
        )
        # Insert 5 scan summaries with underlying > 0 for regime detection
        base_ts = datetime.fromisoformat("2026-06-01T11:00:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
            )

    scan_context = {
        "underlying": 22890.0,
        "atm_strike": 22900.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1000000,
        "underlying": 22600.0,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23020},
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22600, "close": 22600},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22950,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 100.0}],
    }

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trade = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchone()
        assert trade["status"] == "TF-1H-Cross"
        assert "Large reversal move" in trade["reason"]


def test_timeframe_strategy_exit_long_small_move_with_oi():
    # Small move crossover (< 2x breakout buffer) WITH bearish OI bias should trigger exit
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, status, setup_type, lots)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-01T11:30:00Z",
                "NIFTY",
                "LONG",
                "CE",
                22800.0,
                23000.0,
                250.0,
                "OPEN",
                "TIMEFRAME",
                1,
            ),
        )
        # Insert 5 scan summaries with underlying > 0 for regime detection
        base_ts = datetime.fromisoformat("2026-06-01T11:00:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
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
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                # 1H close 22930 is below prev low 22950
                # crossover_size = 22950 - 22930 = 20
                # 2x breakout_buffer = 2x (22930 * 0.001) = 45.86 -> small move
                "ohlc": {"open": 22980, "high": 23020, "low": 22930, "close": 22930},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22950,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 130.0}],
    }

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trade = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchone()
        assert trade["status"] == "TF-1H-Cross"
        assert "Short OI bias" in trade["reason"]


def test_timeframe_strategy_exit_long_small_move_no_oi():
    # Small move crossover (< 2x breakout buffer) WITHOUT bearish OI bias should NOT trigger exit
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, verdict_label, option_type, strike, entry_underlying, entry_premium, status, setup_type, lots)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-01T11:30:00Z",
                "NIFTY",
                "LONG",
                "CE",
                22800.0,
                23000.0,
                250.0,
                "OPEN",
                "TIMEFRAME",
                1,
            ),
        )
        # Insert 5 scan summaries with underlying > 0 for regime detection
        base_ts = datetime.fromisoformat("2026-06-01T11:00:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
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
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                # crossover_size = 20 < 45.86 -> small move
                "ohlc": {"open": 22980, "high": 23020, "low": 22930, "close": 22930},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22950,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 130.0}],
    }

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})

    with get_conn() as conn:
        trade = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchone()
        assert trade["status"] == "OPEN"


def test_aggregate_bars_grid_filtering():
    import pytz

    from src.fetchers.chart_fetcher import _aggregate_bars_grid

    tz = pytz.timezone("Asia/Kolkata")
    bars = []
    times_ist = [
        datetime(2026, 6, 11, 9, 15),
        datetime(2026, 6, 11, 10, 15),
        datetime(2026, 6, 11, 11, 15),
        datetime(2026, 6, 11, 12, 15),
        datetime(2026, 6, 11, 13, 15),
        datetime(2026, 6, 11, 14, 15),
        datetime(2026, 6, 11, 15, 15),
    ]
    for dt_ist in times_ist:
        dt_local = tz.localize(dt_ist)
        ts_utc = dt_local.astimezone(timezone.utc).timestamp()
        bars.append(
            {"Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 105.0, "_ts": ts_utc}
        )

    mock_now = tz.localize(datetime(2026, 6, 11, 17, 0))

    class MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return mock_now

    with patch("src.fetchers.chart_fetcher.datetime", MockDateTime):
        # Test 1H timeframe aggregation (tf_mins = 60)
        agg_1h = _aggregate_bars_grid(bars, 60, "NIFTY")
        assert len(agg_1h) == 6
        assert agg_1h[-1]["_slot_start"].strftime("%H:%M") == "14:15"

        # Test 3H timeframe aggregation (tf_mins = 180)
        agg_3h = _aggregate_bars_grid(bars, 180, "NIFTY")
        assert len(agg_3h) == 2
        assert agg_3h[0]["_slot_start"].strftime("%H:%M") == "09:15"
        assert agg_3h[1]["_slot_start"].strftime("%H:%M") == "12:15"


def test_today_scan_count_and_n_scans_ago():
    from src.models.schema import get_scan_summary_n_scans_ago, get_today_scan_count

    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-11T10:00:00Z", 100, 200),
        )
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-11T10:15:00Z", 300, 400),
        )
        conn.execute(
            "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11", "2026-06-11T10:30:00Z", 500, 600),
        )

    assert get_today_scan_count("NIFTY", "2026-06-11T10:35:00Z") == 3

    ago_1 = get_scan_summary_n_scans_ago("NIFTY", 0)
    assert ago_1["total_ce_oi"] == 500

    ago_2 = get_scan_summary_n_scans_ago("NIFTY", 1)
    assert ago_2["total_ce_oi"] == 300

    ago_3 = get_scan_summary_n_scans_ago("NIFTY", 2)
    assert ago_3["total_ce_oi"] == 100


def test_run_timeframe_strategy_scan_frequency_gating():
    scan_context = {
        "underlying": 23000.0,
        "atm_strike": 23000.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1500000,
        "fetched_at": "2026-06-11T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23050},
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23020},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22950,
                    "close": 22980,
                },
            },
        },
    }

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        with patch("src.engine.paper_trading.get_scan_frequency_nse", return_value=15):
            res = run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})
            assert res["action"] == "SKIPPED_TIMEFRAME_BOUNDARY"

        with get_conn() as conn:
            conn.execute("DELETE FROM scan_summaries")
            for i in range(4):
                conn.execute(
                    "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
                    (
                        "NIFTY",
                        "2026-06-11",
                        f"2026-06-11T10:0{i}:00Z",
                        1000000,
                        1000000,
                    ),
                )

        with patch("src.engine.paper_trading.get_scan_frequency_nse", return_value=15):
            res = run_timeframe_strategy("NIFTY", scan_context, "digest-123", {})
            assert res["action"] == "SKIPPED_TIMEFRAME_BOUNDARY"

        with get_conn() as conn:
            conn.execute("DELETE FROM scan_summaries")
            for i in range(3):
                conn.execute(
                    "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi) VALUES (?, ?, ?, ?, ?)",
                    (
                        "NIFTY",
                        "2026-06-11",
                        f"2026-06-11T10:0{i}:00Z",
                        1000000,
                        1000000,
                    ),
                )

        with patch("src.engine.paper_trading.get_scan_frequency_nse", return_value=15):
            res = run_timeframe_strategy(
                "NIFTY",
                scan_context,
                "digest-123",
                {"verdict_label": "Long Buildup", "confidence": 80},
            )
            assert res is None or res.get("action") != "SKIPPED_TIMEFRAME_BOUNDARY"


class MockLLMVerdict:
    def __init__(
        self, bias="NEUTRAL", confidence=50, risk_rating="LOW", exit_advice=""
    ):
        self.bias = bias
        self.confidence = confidence
        self.risk_rating = risk_rating
        self.exit_advice = exit_advice


def test_timeframe_strategy_llm_gate_a_bias_blocking():
    scan_context = {
        "underlying": 23000.0,
        "atm_strike": 23000.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1500000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23200},
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23020},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22940,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 250.0}],
    }

    with get_conn() as conn:
        base_ts = datetime.fromisoformat("2026-06-01T10:30:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
            )

    ai_verdict = MockLLMVerdict(bias="BEARISH", confidence=80)

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        res = run_timeframe_strategy(
            "NIFTY",
            scan_context,
            "digest-123",
            {"verdict_label": "Long Buildup", "confidence": 80},
            ai_verdict=ai_verdict,
        )

    assert res is not None
    assert res["action"] == "BLOCKED_PLAN"
    assert "LLM bias alignment" in res["reason"]

    # Assert no trade was opened
    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchall()
        assert len(trades) == 0


def test_timeframe_strategy_llm_gate_b_risk_blocking():
    scan_context = {
        "underlying": 23000.0,
        "atm_strike": 23000.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1500000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23200},
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23020},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22940,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 250.0}],
    }

    with get_conn() as conn:
        base_ts = datetime.fromisoformat("2026-06-01T10:30:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
            )

    ai_verdict = MockLLMVerdict(bias="BULLISH", confidence=85, risk_rating="HIGH")

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        res = run_timeframe_strategy(
            "NIFTY",
            scan_context,
            "digest-123",
            {"verdict_label": "Long Buildup", "confidence": 80},
            ai_verdict=ai_verdict,
        )

    assert res is not None
    assert res["action"] == "BLOCKED_PLAN"
    assert "LLM risk rating" in res["reason"]


def test_timeframe_strategy_llm_gate_c_sl_override():
    scan_context = {
        "underlying": 23000.0,
        "atm_strike": 23000.0,
        "expiry": "2026-06-11",
        "total_ce_oi": 1000000,
        "total_pe_oi": 1500000,
        "fetched_at": "2026-06-01T12:00:00Z",
        "chart_indicators": {
            "3h": {
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23200},
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23020},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22940,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 250.0}],
    }

    with get_conn() as conn:
        base_ts = datetime.fromisoformat("2026-06-01T10:30:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
            )

    ai_verdict = MockLLMVerdict(
        bias="BULLISH",
        confidence=85,
        exit_advice="Suggest placing structural SL at \u20b922915.0 to protect capital.",
    )

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        res = run_timeframe_strategy(
            "NIFTY",
            scan_context,
            "digest-123",
            {"verdict_label": "Long Buildup", "confidence": 80},
            ai_verdict=ai_verdict,
        )

    assert res is not None
    assert res["action"] == "EXECUTED"

    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE symbol='NIFTY'"
        ).fetchall()
        assert len(trades) == 1
        trade = dict(trades[0])
        assert trade["sl_underlying"] == 22915.0


def test_timeframe_strategy_llm_gate_d_reversal_exit():
    # Insert an open trade first
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (
                opened_at, symbol, expiry, verdict_label, side, option_type, strike,
                entry_underlying, entry_premium, sl_underlying, sl_premium, lots, status, setup_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                "2026-06-01T10:00:00Z",
                "NIFTY",
                "2026-06-11",
                "LONG",
                "BUY",
                "CE",
                22800.0,
                23000.0,
                250.0,
                None,
                187.5,
                1,
                "OPEN",
                "TIMEFRAME",
            ),
        )
        base_ts = datetime.fromisoformat("2026-06-01T11:00:00Z")
        for i in range(5):
            ts = (base_ts - timedelta(hours=i * 1.5)).isoformat()
            conn.execute(
                "INSERT INTO scan_summaries (symbol, expiry, fetched_at, total_ce_oi, total_pe_oi, underlying) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIFTY", "2026-06-11", ts, 1000000, 1000000, 23000.0),
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
                "ohlc": {"open": 22950, "high": 23050, "low": 22900, "close": 23050},
                "prev_ohlc": {
                    "open": 22800,
                    "high": 23000,
                    "low": 22800,
                    "close": 22950,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
            "1h": {
                "ohlc": {"open": 22980, "high": 23020, "low": 22970, "close": 23020},
                "prev_ohlc": {
                    "open": 22950,
                    "high": 22990,
                    "low": 22940,
                    "close": 22980,
                },
                "bar_end_utc": "2026-06-01T12:00:00Z",
            },
        },
        "option_rows": [{"strike": 22800.0, "option_type": "CE", "ltp": 240.0}],
    }

    ai_verdict = MockLLMVerdict(bias="BEARISH", confidence=75)

    with (
        patch("src.engine.paper_trading._is_market_open", return_value=True),
        patch("src.engine.paper_trading.check_risk_limits", return_value=(True, "")),
    ):
        res = run_timeframe_strategy(
            "NIFTY", scan_context, "digest-123", {}, ai_verdict=ai_verdict
        )

    assert res is not None
    assert res["action"] == "CLOSED"
    assert res["trade"]["status"] == "LLM_REVERSAL"
    assert "LLM sentiment reversal" in res["trade"]["reason"]
