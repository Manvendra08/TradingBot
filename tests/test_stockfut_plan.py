"""Tests for trade plan and lot sizing math in stock futures."""

from src.engine.trade_plan import calculate_future_sl_target
from src.engine.capital_allocator import calculate_future_lots, split_lots
from src.stockfut.levels import Zone


def test_split_lots():
    assert split_lots(5) == (2, 3)
    assert split_lots(2) == (1, 1)
    assert split_lots(4) == (2, 2)
    assert split_lots(3) == (1, 2)


def test_calculate_future_lots_sizing():
    capital = 10000000.0  # 1 Cr
    budget_pct = 0.75     # ₹75,000 budget
    # Case 1: Moderate risk per lot = 20 pts * 500 = ₹10,000
    # floor(75k / 10k) = 7 lots
    lots, skip_reason = calculate_future_lots(
        sl_distance=20.0, lot_size=500, capital=capital, budget_pct=budget_pct
    )
    assert skip_reason is None
    assert lots == 7

    # Case 2: Budget gives 1 lot, but minimum 2 lots applies
    # sl_dist = 50, lot_size = 1000 -> risk_per_lot = 50k
    # floor(75k / 50k) = 1 lot -> bumped to 2 lots
    # 2 lots * 50k = 100k > hard cap (75k * 1.25 = 93.75k) -> skips with MIN_LOTS_BREACH
    lots_breach, skip_breach = calculate_future_lots(
        sl_distance=50.0, lot_size=1000, capital=capital, budget_pct=budget_pct
    )
    assert lots_breach == 0
    assert skip_breach == "MIN_LOTS_BREACH"

    # Case 3: 2 lots fits within 1.25x hard cap
    # sl_dist = 40, lot_size = 1000 -> risk_per_lot = 40k
    # 2 lots * 40k = 80k <= 93.75k -> succeeds with 2 lots
    lots_2, skip_2 = calculate_future_lots(
        sl_distance=40.0, lot_size=1000, capital=capital, budget_pct=budget_pct
    )
    assert skip_2 is None
    assert lots_2 == 2


def test_t1_under_1_6r_returns_none():
    # Long setup: entry = 1000, ATR = 20.
    # Support at 980 -> sl = 980 - 0.25*20 = 975 -> sl_dist = 25 (1.25x ATR).
    # Resistance at 1030 -> t1 = 1030 - 0.05*20 = 1029 -> t1_dist = 29 -> R:R = 29/25 = 1.16 < 1.6R
    # Should fail geometry gate
    zones = [
        Zone(low=978.0, high=980.0, kind="SUPPORT", strength=3),
        Zone(low=1030.0, high=1032.0, kind="RESISTANCE", strength=3),
    ]
    res = calculate_future_sl_target(entry=1000.0, side="LONG", zones=zones, atr=20.0)
    assert res is None


def test_sl_outside_atr_band_returns_none():
    # Long setup: ATR = 20. Permitted SL dist: 15 to 40 (0.75 - 2.0x ATR).
    # Support way too far at 940 -> sl_dist = 1000 - (940 - 5) = 65 > 40
    zones = [
        Zone(low=938.0, high=940.0, kind="SUPPORT", strength=3),
        Zone(low=1200.0, high=1205.0, kind="RESISTANCE", strength=3),
    ]
    res = calculate_future_sl_target(entry=1000.0, side="LONG", zones=zones, atr=20.0)
    assert res is None


def test_opposing_zone_too_close_returns_none():
    # Long setup: entry = 1000, ATR = 20.
    # Support at 980 -> sl = 975 -> sl_dist = 25.
    # Opposing zone buffer = 0.5 * sl_dist = 12.5.
    # If a resistance zone is at 1008 (distance 8 < 12.5), it must fail.
    zones = [
        Zone(low=978.0, high=980.0, kind="SUPPORT", strength=3),
        Zone(low=1008.0, high=1010.0, kind="RESISTANCE", strength=3),
        Zone(low=1100.0, high=1105.0, kind="RESISTANCE", strength=3),
    ]
    res = calculate_future_sl_target(entry=1000.0, side="LONG", zones=zones, atr=20.0)
    assert res is None


def test_short_is_mirror_of_long():
    # Short setup: entry = 1000.0, ATR = 20.0
    # Resistance at 1020 -> sl = 1020 + 0.25*20 = 1025 -> sl_dist = 25 (1.25x ATR)
    # Support at 950 -> t1 = 950 + 0.05*20 = 951 -> t1_dist = 49 -> R:R = 49/25 = 1.96 >= 1.6R
    # Second support at 900 -> t2 = 900 + 1 = 901
    zones = [
        Zone(low=1020.0, high=1022.0, kind="RESISTANCE", strength=3),
        Zone(low=948.0, high=950.0, kind="SUPPORT", strength=3),
        Zone(low=898.0, high=900.0, kind="SUPPORT", strength=3),
    ]
    plan = calculate_future_sl_target(entry=1000.0, side="SHORT", zones=zones, atr=20.0)
    assert plan is not None
    assert plan["sl"] == 1025.0
    assert plan["t1"] == 951.0
    assert plan["t2"] == 901.0
    assert plan["r_t1"] >= 1.6
