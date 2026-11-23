"""Tests for stock futures feature calculation."""

from src.stockfut.features import (
    classify_oi_flow,
    compute_atr,
    compute_features,
    market_regime,
)


def test_oi_flow_truth_table():
    # Long Buildup: Price ↑, OI ↑ (> 3% noise)
    assert classify_oi_flow(1.5, 4.0, noise=3.0) == "LONG_BUILDUP"
    # Short Buildup: Price ↓, OI ↑ (> 3% noise)
    assert classify_oi_flow(-2.0, 5.0, noise=3.0) == "SHORT_BUILDUP"
    # Short Covering: Price ↑, OI ↓ (< -3% noise)
    assert classify_oi_flow(1.2, -4.5, noise=3.0) == "SHORT_COVERING"
    # Long Unwinding: Price ↓, OI ↓ (< -3% noise)
    assert classify_oi_flow(-1.8, -5.2, noise=3.0) == "LONG_UNWINDING"
    # Below noise floor
    assert classify_oi_flow(2.0, 2.5, noise=3.0) == "NONE"
    assert classify_oi_flow(-1.0, -1.5, noise=3.0) == "NONE"
    # Zero price change
    assert classify_oi_flow(0.0, 10.0, noise=3.0) == "NONE"


def test_empty_history_returns_none():
    assert compute_features("RELIANCE", []) is None
    # Less than 25 candles
    assert compute_features("RELIANCE", [{"close": 100}] * 20) is None


def test_atr_is_daily():
    daily = [
        {"high": 105.0, "low": 95.0, "close": 100.0},
        {"high": 110.0, "low": 100.0, "close": 108.0},
        {"high": 112.0, "low": 102.0, "close": 104.0},
    ]
    atr = compute_atr(daily, period=2)
    assert atr > 0.0


def test_flow_uses_combined_near_next_oi():
    # Synthetic 30 daily bars
    daily = []
    base_price = 1000.0
    for i in range(30):
        p = base_price + i * 5
        daily.append({
            "open": p - 2,
            "high": p + 10,
            "low": p - 5,
            "close": p,
            "volume": 100000,
            "oi": 50000,
        })

    # Combined OI increases by 20% over 20 days
    combined_oi = [50000 + i * 1000 for i in range(30)]
    f = compute_features("TEST", daily, combined_oi_daily=combined_oi)
    assert f is not None
    assert f["oi_flow_20d"] == "LONG_BUILDUP"


def test_market_regime_classification():
    # High breadth, low vix -> RISK_ON
    regime = market_regime(
        index_daily=None,
        universe_breadth_pct_above_ema50=75.0,
        vix_value=13.5,
        vix_change_pct=-6.0,
    )
    assert regime == "RISK_ON"

    # Low breadth, high vix -> RISK_OFF
    regime_off = market_regime(
        index_daily=None,
        universe_breadth_pct_above_ema50=35.0,
        vix_value=22.0,
        vix_change_pct=10.0,
    )
    assert regime_off == "RISK_OFF"
