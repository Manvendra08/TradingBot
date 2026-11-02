"""
Tests for Decision Matrix Engine.
Verifies regime structure selection, overrides (0-DTE, MCX naked ban, Event window),
and two-tier MCX strike liquidity gate with deterministic time injections.
"""

from datetime import datetime, timedelta, timezone
import pytest

from config.decision_matrix_loader import DEFAULTS
from src.engine.decision_matrix import (
    select_structure,
    is_mcx_strike_liquid,
    vol_bucket,
    delta_band,
)

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize(
    "direction,vol,expected_default,expected_allowed",
    [
        # Neutral
        ("NEUTRAL", "LOW", "SHORT_STRANGLE", ["SHORT_STRANGLE"]),
        ("NEUTRAL", "MID", "IRON_CONDOR", ["IRON_CONDOR", "SHORT_STRANGLE"]),
        ("NEUTRAL", "HIGH", "IRON_CONDOR", ["IRON_CONDOR"]),
        # Bullish
        ("BULLISH", "LOW", None, []),
        ("BULLISH", "MID", "BULL_PUT_SPREAD", ["BULL_PUT_SPREAD"]),
        ("BULLISH", "HIGH", "BULL_PUT_SPREAD", ["BULL_PUT_SPREAD", "JADE_LIZARD"]),
        # Bearish
        ("BEARISH", "LOW", None, []),
        ("BEARISH", "MID", "BEAR_CALL_SPREAD", ["BEAR_CALL_SPREAD"]),
        ("BEARISH", "HIGH", "BEAR_CALL_SPREAD", ["BEAR_CALL_SPREAD"]),
    ],
)
def test_regime_table_indices(direction, vol, expected_default, expected_allowed):
    """Verify core regime matrix mapping for Index options."""
    rec, allowed, reason = select_structure(
        direction=direction,
        vol=vol,
        symbol="NIFTY",
        cfg=DEFAULTS,
    )
    assert rec == expected_default
    assert allowed == expected_allowed
    if expected_default is None:
        assert "NO_TRADE" in reason


def test_mcx_no_naked_override():
    """MCX must never receive naked structures (SHORT_STRANGLE replaced with IRON_CONDOR)."""
    rec, allowed, reason = select_structure(
        direction="NEUTRAL",
        vol="LOW",
        symbol="NATURALGAS",
        cfg=DEFAULTS,
    )
    # Low vol neutral is SHORT_STRANGLE for indices, but becomes IRON_CONDOR on MCX
    assert rec == "IRON_CONDOR"
    assert "SHORT_STRANGLE" not in allowed
    assert allowed == ["IRON_CONDOR"]


def test_zero_dte_cutoff_after_1300_ist():
    """0-DTE trades must be rejected after 13:00 IST cutoff."""
    time_before_cutoff = datetime(2026, 10, 27, 12, 59, tzinfo=_IST)
    time_after_cutoff = datetime(2026, 10, 27, 13, 0o1, tzinfo=_IST)

    # Before cutoff: allowed
    rec1, allowed1, _ = select_structure(
        direction="NEUTRAL",
        vol="HIGH",
        dte=0,
        now_ist=time_before_cutoff,
        cfg=DEFAULTS,
    )
    assert rec1 == "IRON_CONDOR"

    # After cutoff: rejected
    rec2, allowed2, reason2 = select_structure(
        direction="NEUTRAL",
        vol="HIGH",
        dte=0,
        now_ist=time_after_cutoff,
        cfg=DEFAULTS,
    )
    assert rec2 is None
    assert allowed2 == []
    assert "0-DTE entry closed" in reason2


def test_event_window_filters_to_defined_risk():
    """Event window must eliminate naked strangles and force defined-risk structures only."""
    rec, allowed, _ = select_structure(
        direction="NEUTRAL",
        vol="MID",
        is_event=True,
        symbol="NIFTY",
        cfg=DEFAULTS,
    )
    assert rec == "IRON_CONDOR"
    assert "SHORT_STRANGLE" not in allowed
    assert allowed == ["IRON_CONDOR"]


# ── MCX Strike Liquidity Gate Tests ──────────────────────────────────────────

def test_mcx_strike_liquidity_oi_floor():
    """OI floor (1500) check."""
    now = datetime(2026, 10, 27, 10, 0, tzinfo=_IST)
    # 1499 OI fails
    ok, reason = is_mcx_strike_liquid(
        {"oi": 1499, "bid": 5.0, "ask": 5.2, "volume": 100},
        now_ist=now,
        cfg=DEFAULTS,
    )
    assert not ok
    assert "Low OI (1499 < 1500)" in reason

    # 1500 OI passes
    ok, _ = is_mcx_strike_liquid(
        {"oi": 1500, "bid": 5.0, "ask": 5.2, "volume": 100},
        now_ist=now,
        cfg=DEFAULTS,
    )
    assert ok


def test_mcx_strike_liquidity_quote_truth():
    """Bid and Ask must be > 0 and not crossed."""
    now = datetime(2026, 10, 27, 10, 0, tzinfo=_IST)

    # Zero bid
    ok, reason = is_mcx_strike_liquid(
        {"oi": 2000, "bid": 0.0, "ask": 5.0, "volume": 100},
        now_ist=now,
        cfg=DEFAULTS,
    )
    assert not ok
    assert "Zero quote" in reason

    # Crossed book
    ok, reason = is_mcx_strike_liquid(
        {"oi": 2000, "bid": 5.5, "ask": 5.0, "volume": 100},
        now_ist=now,
        cfg=DEFAULTS,
    )
    assert not ok
    assert "Crossed book" in reason


def test_mcx_strike_liquidity_spread_branches():
    """
    Spread test: passes if abs_spread <= 0.30 OR spread_pct <= 2.0%.
    Fails only when both are exceeded.
    """
    now = datetime(2026, 10, 27, 10, 0, tzinfo=_IST)

    # Branch A: abs_spread = 0.25 (<= 0.30) even though pct is 5.0% (> 2.0%) -> PASSES
    ok, _ = is_mcx_strike_liquid(
        {"oi": 2000, "bid": 4.75, "ask": 5.0, "volume": 100},
        now_ist=now,
        cfg=DEFAULTS,
    )
    assert ok

    # Branch B: abs_spread = 1.0 (> 0.30) but pct is 1.0% (<= 2.0%) on a 100 strike -> PASSES
    ok, _ = is_mcx_strike_liquid(
        {"oi": 2000, "bid": 99.0, "ask": 100.0, "volume": 100},
        now_ist=now,
        cfg=DEFAULTS,
    )
    assert ok

    # Branch C: abs_spread = 0.50 (> 0.30) AND pct is 5.0% (> 2.0%) -> FAILS
    ok, reason = is_mcx_strike_liquid(
        {"oi": 2000, "bid": 9.5, "ask": 10.0, "volume": 100},
        now_ist=now,
        cfg=DEFAULTS,
    )
    assert not ok
    assert "Wide spread" in reason


def test_mcx_strike_liquidity_time_tiered_volume():
    """
    Volume tiers:
    < 12:00 IST: 50
    < 17:00 IST: 150
    >= 17:00 IST: 400
    """
    t_1159 = datetime(2026, 10, 27, 11, 59, tzinfo=_IST)
    t_1200 = datetime(2026, 10, 27, 12, 0, tzinfo=_IST)
    t_1659 = datetime(2026, 10, 27, 16, 59, tzinfo=_IST)
    t_1700 = datetime(2026, 10, 27, 17, 0, tzinfo=_IST)

    base = {"oi": 2000, "bid": 5.0, "ask": 5.2}

    # At 11:59: vol 60 passes (>= 50)
    ok, _ = is_mcx_strike_liquid({**base, "volume": 60}, now_ist=t_1159, cfg=DEFAULTS)
    assert ok

    # At 12:00: vol 60 fails (< 150)
    ok, reason = is_mcx_strike_liquid({**base, "volume": 60}, now_ist=t_1200, cfg=DEFAULTS)
    assert not ok
    assert "Low volume (60 < 150" in reason

    # At 16:59: vol 160 passes (>= 150)
    ok, _ = is_mcx_strike_liquid({**base, "volume": 160}, now_ist=t_1659, cfg=DEFAULTS)
    assert ok

    # At 17:00: vol 160 fails (< 400)
    ok, reason = is_mcx_strike_liquid({**base, "volume": 160}, now_ist=t_1700, cfg=DEFAULTS)
    assert not ok
    assert "Low volume (160 < 400" in reason

    # At 17:00: vol 450 passes (>= 400)
    ok, _ = is_mcx_strike_liquid({**base, "volume": 450}, now_ist=t_1700, cfg=DEFAULTS)
    assert ok
