from datetime import datetime, time, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytz

from src.engine.decision_pipeline import PipelineContext, step_risk
from src.engine.time_guards import is_trading_allowed_now
from src.models.schema import get_conn, init_db, insert_paper_trade
from src.scheduler.job_runner import exit_all_positions_friday


def _make_ist_dt(h: int, m: int, weekday: int = 0) -> datetime:
    """Return a timezone-aware IST datetime on a fixed date with given weekday."""
    # Use a known Monday (2026-06-29 is a Monday, weekday=0)
    base_day = {0: 29, 1: 30, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}  # June/July 2026
    month = 7 if weekday >= 2 else 6
    day = base_day[weekday]
    ist = pytz.timezone("Asia/Kolkata")
    return ist.localize(datetime(2026, month, day, h, m, 0))


@pytest.fixture(autouse=True)
def setup_test_db():
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM live_trades")
        conn.execute("DELETE FROM underlying_price")
        conn.execute("DELETE FROM option_chain_snapshots")
        conn.execute("DELETE FROM scan_summaries")


class TestExpiryCutoffs:
    """Tests for expiry day time cutoffs (2:30 pm for NSE/BSE, 8 pm for MCX)."""

    @patch("src.engine.time_guards.datetime")
    def test_nse_expiry_cutoff(self, mock_dt):
        # Fix: mock strptime to return real dates, so expiry date comparison works
        import datetime as _real_dt

        mock_dt.strptime.side_effect = lambda s, f: _real_dt.datetime.strptime(s, f)

        # 1. On expiry day, after 14:30 (2:30 pm) -> blocked
        mock_dt.now.return_value = _make_ist_dt(14, 31, weekday=3)  # Thu July 2, 2026
        allowed, reason = is_trading_allowed_now("NIFTY", "2026-07-02")
        assert allowed is False
        assert "Expiry day trading cutoff" in reason

        # 2. On expiry day, before 14:30 -> allowed
        mock_dt.now.return_value = _make_ist_dt(13, 0, weekday=3)
        allowed, reason = is_trading_allowed_now("NIFTY", "2026-07-02")
        assert allowed is True

        # 3. Not expiry day, after 14:30 -> allowed
        mock_dt.now.return_value = _make_ist_dt(14, 31, weekday=3)
        allowed, reason = is_trading_allowed_now("NIFTY", "2026-07-09")
        assert allowed is True

    @patch("src.engine.time_guards.datetime")
    def test_mcx_expiry_cutoff(self, mock_dt):
        import datetime as _real_dt

        mock_dt.strptime.side_effect = lambda s, f: _real_dt.datetime.strptime(s, f)

        # Use Tuesday (not Thursday) to avoid EIA window collision
        # Tuesday June 30, 2026
        # 1. On expiry day, after 20:00 (8:00 pm) -> blocked
        mock_dt.now.return_value = _make_ist_dt(20, 1, weekday=1)  # Tue June 30, 2026
        allowed, reason = is_trading_allowed_now("NATURALGAS", "2026-06-30")
        assert allowed is False
        assert "Expiry day trading cutoff" in reason

        # 2. On expiry day, before 20:00 -> allowed
        mock_dt.now.return_value = _make_ist_dt(19, 0, weekday=1)
        allowed, reason = is_trading_allowed_now("NATURALGAS", "2026-06-30")
        assert allowed is True

        # 3. Not expiry day, after 20:00 -> allowed
        mock_dt.now.return_value = _make_ist_dt(20, 1, weekday=1)
        allowed, reason = is_trading_allowed_now("NATURALGAS", "2026-07-28")
        assert allowed is True


class TestOptionBuyingExpiryDay:
    """Tests for option buying block on expiry day in step_risk."""

    @patch("src.engine.decision_pipeline.datetime")
    def test_core_option_buying_on_expiry_day(self, mock_dt):
        # Fix: mock strptime to return real dates, so expiry date comparison works
        import datetime as _real_dt

        mock_dt.strptime.side_effect = lambda s, f: _real_dt.datetime.strptime(s, f)
        mock_dt.now.return_value = _make_ist_dt(10, 0, weekday=3)  # July 2, 2026

        ctx = PipelineContext(
            engine="CORE_OI",
            symbol="NIFTY",
            direction="LONG",
            underlying=22000.0,
            scan_context={
                "expiry": "2026-07-02",
                "_pipeline_plan": {
                    "side": "BUY",
                    "option_type": "CE",
                    "strike": 22000.0,
                },
            },
            ai_verdict=None,
            steps=[],
        )

        with patch(
            "src.engine.decision_pipeline._check_risk_limits_for_table",
            return_value=(True, "", ""),
        ):
            # Case 1: Expiry day option buy -> blocked
            res = step_risk(ctx)
            assert res.passed is False
            assert "Option buying (BUY CE) is blocked on expiry day" in res.reason

            # Case 2: Expiry day option sell -> allowed
            ctx.scan_context["_pipeline_plan"]["side"] = "SELL"
            res = step_risk(ctx)
            assert res.passed is True

            # Case 3: Future expiry option buy -> allowed
            ctx.scan_context["expiry"] = "2026-07-09"
            ctx.scan_context["_pipeline_plan"]["side"] = "BUY"
            res = step_risk(ctx)
            assert res.passed is True


class TestFridayWeekendAutoExit:
    """Tests for Friday auto-exiting open trades."""

    @patch("src.scheduler.job_runner._is_open_for", return_value=True)
    @patch("src.fetchers.router.fetch_option_chain")
    @patch("src.engine.live_trading.get_kite_client")
    @patch("src.engine.live_trading._exit_open_live_trade")
    def test_friday_exit_closes_open_trades(
        self, mock_exit_live, mock_kite, mock_fetch, mock_is_open
    ):
        # Setup mocks
        mock_fetch.return_value = {
            "underlying_price": 22000.0,
            "expiry": "2026-07-09",
            "strikes": [{"strike": 22000.0, "option_type": "CE", "ltp": 150.0}],
        }
        mock_kite.return_value = MagicMock()

        # Insert open paper trade
        trade_id = insert_paper_trade(
            {
                "opened_at": "2026-07-01T10:00:00Z",
                "symbol": "NIFTY",
                "expiry": "2026-07-09",
                "verdict_label": "Long Buildup",
                "side": "BUY",
                "option_type": "CE",
                "strike": 22000.0,
                "entry_underlying": 22000.0,
                "entry_premium": 150.0,
                "lots": 1,
                "status": "OPEN",
            }
        )

        # Insert open live trade (mock format)
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO live_trades (
                    opened_at, symbol, expiry, verdict_label, side, option_type, strike,
                    entry_underlying, entry_premium, lots, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-07-01T10:00:00Z",
                    "NIFTY",
                    "2026-07-09",
                    "Long Buildup",
                    "BUY",
                    "CE",
                    22000.0,
                    22000.0,
                    150.0,
                    1,
                    "OPEN",
                ),
            )
            live_trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Call exit_all_positions_friday
        exit_all_positions_friday("NSE_INDEX")

        # Verify paper trade is closed
        with get_conn() as conn:
            p_trade = conn.execute(
                "SELECT * FROM paper_trades WHERE id=?", (trade_id,)
            ).fetchone()
            assert p_trade["status"] == "CLOSED_WEEKEND"
            assert p_trade["exit_premium"] == 150.0

            # Verify live trade status closed in DB (since mock_exit_live will be called and mock closing should update it)
            assert mock_exit_live.call_count == 1
            args, kwargs = mock_exit_live.call_args
            assert kwargs["status"] == "CLOSED_WEEKEND"
            assert kwargs["symbol"] == "NIFTY"
