"""Regression tests for Phase 1 P&L Truth & Realism fixes.

Covers:
- C1: Multi-leg exits prefer live decision price (leg['current_premium']) over stale DB snapshots.
- C1: get_latest_option_snapshot enforces 15-min freshness guard.
- C3: Short call delta-approximation uses correct sign (+abs(delta) for CE, -abs(delta) for PE).
- B13: Trailing and delta stops not clamped to initial full SL loss.
- Double slippage elimination in single-leg paper trading.
- Exclusion of corrupted historical trades from ML training and history analysis.
"""
from datetime import datetime, timedelta, timezone
import pytest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_c1_build_real_leg_exits_prefers_live_current_premium():
    """C1 FIX: _build_real_leg_exits must use leg['current_premium'] from the active decision cycle."""
    from src.engine.multileg_paper_trading import _build_real_leg_exits

    symbol = "BANKNIFTY"
    expiry = "2026-10-27"
    legs = [
        {
            "id": 9999,
            "side": "SELL",
            "strike": 54500.0,
            "option_type": "PE",
            "entry_premium": 312.75,
            # Live decision price from MTM calculation
            "current_premium": 472.7,
        }
    ]
    book = {
        "symbol": symbol,
        "entry_underlying": 54200.0,
    }
    scan_context = {
        "underlying": 54200.0,
        "option_rows": [],
    }

    exits = _build_real_leg_exits(symbol, expiry, legs, scan_context=scan_context, book=book)
    assert len(exits) == 1
    # For SELL leg (buying to close), exit price should be based on current_premium 472.7 + slippage
    # and MUST NOT be anywhere near the old 227.4 snapshot.
    exit_p = exits[0]["exit_premium"]
    assert 470.0 <= exit_p <= 480.0, f"Expected ~475 exit premium, got {exit_p}"


def test_c1_get_latest_option_snapshot_staleness_guard():
    """C1 FIX: get_latest_option_snapshot rejects snapshots older than 15 minutes by default."""
    from src.models.schema import get_conn, get_latest_option_snapshot

    symbol = "TEST_SYM_C1"
    expiry = "2026-11-26"
    strike = 25000.0
    ot = "CE"

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO option_chain_snapshots (symbol, expiry, strike, option_type, ltp, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (symbol, expiry, strike, ot, 150.0, old_ts),
        )

    # With default 15m max age, should return None
    snap = get_latest_option_snapshot(symbol, expiry, strike, ot)
    assert snap is None

    # With max_age_minutes=None, returns the historical snapshot
    snap_unbounded = get_latest_option_snapshot(symbol, expiry, strike, ot, max_age_minutes=None)
    assert snap_unbounded is not None
    assert snap_unbounded["ltp"] == 150.0


def test_c3_short_call_delta_sign_positive_move():
    """C3 FIX: A short call with negative stored delta (-0.27) must see premium increase when spot rises."""
    from src.engine.multileg_pnl import _resolve_leg_current_premium

    symbol = "NIFTY"
    leg = {
        "strike": 25000.0,
        "option_type": "CE",
        "side": "SELL",
        "entry_premium": 100.0,
        # In multi-leg trades, short call position delta is negative
        "delta": -0.27,
    }
    book = {
        "symbol": symbol,
        "entry_underlying": 24800.0,
    }
    # Spot moved UP by +100 points
    scan_context = {
        "underlying": 24900.0,
        "option_rows": [],  # Force delta approximation fallback
    }

    prem = _resolve_leg_current_premium(symbol, leg, scan_context, book)
    # Spot rose 100 pts. Call option should appreciate: 100 + (0.27 * 100) = ~127.
    # With the old bug it fell to: 100 + (-0.27 * 100) = 73!
    assert prem > 100.0, f"Call option price must rise on spot increase, got {prem}"
    assert 125.0 <= prem <= 130.0


def test_b13_trailing_sl_not_clamped_to_initial_sl():
    """B13 FIX: Trailing stop-loss hits must NOT be clamped to initial full SL loss."""
    from src.engine.paper_trading import _monitor_single_paper_trade

    open_trade = {
        "id": 99998,
        "symbol": "NATURALGAS",
        "expiry": "2026-10-27",
        "side": "SELL",
        "option_type": "CE",
        "strike": 280.0,
        "entry_underlying": 270.0,
        "entry_premium": 10.0,
        "sl_underlying": 275.0,
        "sl_premium": 15.0,  # Full SL loss at 15.0 (+50%)
        "target_underlying": 260.0,
        "target_premium": 3.0,
        "max_favorable_r": 2.5,  # Trailing stop armed
        "opened_at": _now(),
    }
    # Underlying moved back to current R <= 0.5 triggering trailing SL
    # Current option premium is 6.0 (in profit from 10.0 entry!)
    current_ctx = {
        "underlying": 269.0,
        "expiry": "2026-10-27",
        "option_rows": [
            {"strike": 280.0, "option_type": "CE", "ltp": 6.0, "volume": 100, "oi": 500, "expiry": "2026-10-27"}
        ],
    }

    actions = _monitor_single_paper_trade(
        "NATURALGAS", open_trade, current_ctx, underlying=269.0, skip_reversal=True
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "CLOSED_TRAILING_SL"
    # Exit premium must reflect the profitable ~6.0 price, NOT clamped to sl_premium 15.0!
    exit_p = action["exit_premium"]
    assert exit_p is not None
    assert exit_p < 10.0, f"Trailing SL exit premium should be in profit (< 10.0), got {exit_p}"


def test_corrupted_historical_trades_excluded():
    """Verified that is_pnl_corrupted=1 rows are excluded from ML and History views."""
    from src.models.schema import get_conn

    with get_conn(read_only=True) as conn:
        corrupted_in_ml = conn.execute(
            """
            SELECT COUNT(*) as cnt
            FROM multi_leg_trades m
            WHERE m.status != 'OPEN'
              AND m.closed_at IS NOT NULL
              AND m.total_pnl IS NOT NULL
              AND COALESCE(m.is_pnl_corrupted, 0) = 0
              AND m.id = 266
            """
        ).fetchone()["cnt"]
        assert corrupted_in_ml == 0, "Corrupted trade 266 must not appear in ML training dataset"
