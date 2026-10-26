"""Regression tests for the 2026-10-22 exit-lifecycle fixes.

Covers:
- close_live_trade must close a row that the atomic exit claim moved to
  EXIT_PENDING (previously it filtered status='OPEN' and silently no-op'd,
  leaving every live exit stuck in EXIT_PENDING with pnl=0).
- recover_stale_exit_pending reclaims legacy/timed-out claims.
- GeminiAI FUT paper trades now get a local SL/target safety net.
"""
from datetime import datetime, timedelta, timezone

import pytest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_live(status: str, claimed_at: str | None, **overrides) -> int:
    from src.models.schema import insert_live_trade

    trade = {
        "opened_at": _now(),
        "symbol": "NATURALGAS",
        "expiry": "2026-10-27",
        "verdict_label": "Trend",
        "side": "SELL",
        "option_type": "CE",
        "strike": 300.0,
        "entry_underlying": 274.5,
        "entry_premium": 1.1,
        "sl_underlying": 280.0,
        "sl_premium": 1.32,
        "target_underlying": 260.0,
        "target_premium": 0.72,
        "lots": 1,
        "status": status,
        "reason": "test",
        "digest_id": f"digest-{_now()}",
        "trade_status": "TRIGGERED_CORE",
        "setup_type": "TREND_CONTINUATION",
        "decision_reason": "test",
        "signal_key": f"test-{_now()}",
        "broker_order_id": "sh-ord-test",
        "broker_status": "SHADOW",
        "exit_mode": "GTT",
        "exit_claimed_at": claimed_at,
    }
    trade.update(overrides)
    return insert_live_trade(trade)


def _get_live(trade_id: int) -> dict:
    from src.models.schema import get_conn

    with get_conn(read_only=True) as conn:
        return dict(
            conn.execute("SELECT * FROM live_trades WHERE id=?", (trade_id,)).fetchone()
        )


def test_close_live_trade_accepts_exit_pending_claim():
    """An exit that wins the atomic claim must still be recorded closed."""
    from src.models.schema import close_live_trade

    trade_id = _insert_live("EXIT_PENDING", _now())
    close_live_trade(
        trade_id,
        _now(),
        exit_underlying=265.0,
        exit_premium=0.8,
        status="CLOSED_TARGET",
        reason="cmp poll exit",
    )

    trade = _get_live(trade_id)
    assert trade["status"] == "CLOSED_TARGET"
    assert trade["closed_at"] is not None
    assert trade["pnl_rupees"] != 0.0
    assert trade["exit_claimed_at"] is None


def test_recover_stale_exit_pending():
    from src.models.schema import recover_stale_exit_pending

    from src.models.schema import get_conn

    legacy = _insert_live("EXIT_PENDING", None)
    stale = _insert_live(
        "EXIT_PENDING",
        (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
    )
    fresh = _insert_live("EXIT_PENDING", _now())

    # insert_live_trade() has a fixed column list, so set claim stamps directly.
    with get_conn() as conn:
        conn.execute(
            "UPDATE live_trades SET exit_claimed_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(), stale),
        )
        conn.execute(
            "UPDATE live_trades SET exit_claimed_at=? WHERE id=?",
            (_now(), fresh),
        )

    recover_stale_exit_pending(max_age_minutes=10)
    assert _get_live(legacy)["status"] == "OPEN"
    assert _get_live(stale)["status"] == "OPEN"
    assert _get_live(fresh)["status"] == "EXIT_PENDING"

    recover_stale_exit_pending(max_age_minutes=0)
    assert _get_live(fresh)["status"] == "OPEN"


def test_gemini_fut_target_breach_closes_paper_trade():
    """FUT GeminiAI trades were excluded from monitoring, so breached targets
    never closed. The local safety net must close them."""
    from src.engine.paper_trading import monitor_paper_trades
    from src.models.schema import get_conn, insert_paper_trade

    trade_id = insert_paper_trade(
        {
            "opened_at": _now(),
            "symbol": "NATURALGAS",
            "expiry": "2026-10-27",
            "verdict_label": "GeminiAI",
            "side": "SELL",
            "option_type": "FUT",
            "strike": 0.0,
            "entry_underlying": 271.2,
            "entry_premium": 271.2,
            "sl_underlying": 274.1,
            "target_underlying": 267.5,
            "lots": 1,
            "status": "OPEN",
            "reason": "GeminiAI Setup: NG_TEST_SELL",
            "digest_id": "gemini_test",
            "signal_key": "NG_TEST_SELL",
            "trade_status": "TRIGGERED_CORE",
            "setup_type": "GeminiAI",
            "decision_reason": "test",
            "regime": "GEMINI_SPARK",
            "lot_size": 1250,
        }
    )

    actions = monitor_paper_trades(
        "NATURALGAS", {"underlying": 261.0, "force_monitor": True}
    )
    assert any(a.get("trade_id") == trade_id for a in actions), actions

    with get_conn(read_only=True) as conn:
        row = conn.execute(
            "SELECT status, exit_reason, pnl_rupees FROM paper_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
    assert row["status"] == "CLOSED_TARGET"
    assert row["pnl_rupees"] > 0


def test_gemini_nifty_fut_excluded_from_spot_monitoring():
    """B1 FIX: NIFTY/BANKNIFTY futures have basis spread to spot.
    The local spot monitor must exclude them to prevent false spot stop-outs."""
    from src.engine.paper_trading import monitor_paper_trades
    from src.models.schema import get_conn, insert_paper_trade

    trade_id = insert_paper_trade(
        {
            "opened_at": _now(),
            "symbol": "NIFTY",
            "expiry": "2026-10-29",
            "verdict_label": "GeminiAI",
            "side": "BUY",
            "option_type": "FUT",
            "strike": 0.0,
            "entry_underlying": 25800.0,  # Futures price
            "entry_premium": 25800.0,
            "sl_underlying": 25700.0,
            "target_underlying": 26000.0,
            "lots": 1,
            "status": "OPEN",
            "reason": "GeminiAI Setup: NIFTY_TEST_BUY",
            "digest_id": "gemini_nifty_test",
            "signal_key": "NIFTY_TEST_BUY",
            "trade_status": "TRIGGERED_CORE",
            "setup_type": "GeminiAI",
            "decision_reason": "test",
            "regime": "GEMINI_SPARK",
            "lot_size": 25,
        }
    )

    # Spot is at 25100 (spot basis ~700 pts lower than futures entry 25800).
    # If the monitor evaluated spot against the futures SL (25700), it would falsely trigger SL.
    actions = monitor_paper_trades(
        "NIFTY", {"underlying": 25100.0, "force_monitor": True}
    )
    assert not any(a.get("trade_id") == trade_id for a in actions), actions

    with get_conn(read_only=True) as conn:
        row = conn.execute(
            "SELECT status FROM paper_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
    assert row["status"] == "OPEN"


def test_dst_aware_mcx_and_eia_times():
    """A18 & B19 FIX: EIA announcement and MCX close times adjust with US DST."""
    import pytz
    from src.engine.time_guards import get_mcx_close_time, get_eia_announcement_time

    IST = pytz.timezone("Asia/Kolkata")
    
    # Summer date (e.g. 2026-10-20, US EDT active)
    summer_dt = IST.localize(datetime(2026, 10, 20, 12, 0, 0))
    assert get_mcx_close_time(summer_dt) == (23, 30)
    assert get_eia_announcement_time(summer_dt) == (20, 0)

    # Winter date (e.g. 2026-11-05, US EST active after Nov 1 DST end)
    winter_dt = IST.localize(datetime(2026, 11, 5, 12, 0, 0))
    assert get_mcx_close_time(winter_dt) == (23, 55)
    assert get_eia_announcement_time(winter_dt) == (21, 0)


def test_c14_single_sided_breakeven_cushion():
    """C14 FIX: Credit spreads have one breakeven None, must not raise TypeError."""
    from src.engine.multileg_strategy import _score_entry_quality

    proposal = {
        "structure": "BULL_PUT_SPREAD",
        "net_credit": 35.0,
        "legs": [
            {"strike": 24000, "option_type": "PE", "side": "SELL", "lots": 1},
            {"strike": 23900, "option_type": "PE", "side": "BUY", "lots": 1},
        ],
    }
    # For a bull put spread, upper breakeven is None, lower breakeven is 23965
    risk_profile = {
        "max_profit": 3500.0,
        "max_loss": 6500.0,
        "breakeven_upper": None,
        "breakeven_lower": 23965.0,
    }
    market_data = {
        "underlying": 24500.0,
        "iv": 14.5,
        "atm_strike": 24500.0,
    }

    # Should succeed without TypeError and compute a positive score
    score, reasons = _score_entry_quality(proposal, market_data, risk_profile)
    assert score > 0
    assert any("breakeven cushion" in r.lower() for r in reasons)

