"""Tests for stock futures level & zone engine."""

from src.stockfut.levels import build_zones, shift_zones, nearest_zone, Zone


def test_basis_shift_moves_all_zones():
    z1 = Zone(low=100.0, high=102.0, kind="SUPPORT", strength=3)
    z2 = Zone(low=120.0, high=123.0, kind="RESISTANCE", strength=2)

    basis = 5.5  # Future trading 5.5 points above cash
    shifted = shift_zones([z1, z2], basis=basis)

    assert len(shifted) == 2
    assert shifted[0].low == 105.5
    assert shifted[0].high == 107.5
    assert shifted[1].low == 125.5
    assert shifted[1].high == 128.5


def test_no_zone_returns_none():
    assert build_zones([], atr=10.0) == []
    zones = [Zone(low=90.0, high=92.0, kind="SUPPORT", strength=3)]
    # Looking for resistance above 100 when none exists
    assert nearest_zone(zones, price=100.0, side="RESISTANCE") is None


def test_pivots_within_0_3_atr_merge():
    # Synthetic daily candles with two nearby swing lows: 95.0 and 95.5 (ATR = 10, 0.3*ATR = 3.0)
    daily = []
    # 60 candles with two distinct dips to ~95
    for i in range(60):
        low_p = 100.0
        high_p = 110.0
        if i == 15:
            low_p = 95.0
        elif i == 35:
            low_p = 95.5
        daily.append({
            "high": high_p,
            "low": low_p,
            "close": (high_p + low_p) / 2.0,
        })

    zones = build_zones(daily, atr=10.0)
    support_zones = [z for z in zones if z.kind == "SUPPORT"]
    # The dips at 95.0 and 95.5 are separated by 0.5 points (< 3.0 ATR buffer), so they should merge
    assert any(z.low <= 95.0 and z.high >= 95.5 for z in support_zones)


def test_more_touches_means_higher_strength():
    daily = []
    for i in range(80):
        low_p = 100.0
        high_p = 110.0
        if i in (10, 25, 45, 65):  # 4 touches at 90.0
            low_p = 90.0
        daily.append({
            "high": high_p,
            "low": low_p,
            "close": (high_p + low_p) / 2.0,
        })

    zones = build_zones(daily, atr=5.0)
    support_90 = next((z for z in zones if z.kind == "SUPPORT" and z.low <= 90.5), None)
    assert support_90 is not None
    assert support_90.touch_count >= 2
    assert support_90.strength >= 3
