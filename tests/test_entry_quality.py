import pytest
from src.engine.entry_quality import calculate_entry_quality

def test_entry_quality_missing_underlying():
    score, reasons = calculate_entry_quality("TEST", "CE", 100.0, {})
    assert score == 0
    assert "Missing underlying price" in reasons[0]

def test_entry_quality_price_near_level():
    # PE near support
    ctx = {
        "underlying": 101.0,
        "support": 100.0,
        "resistance": 110.0,
    }
    score, reasons = calculate_entry_quality("TEST", "PE", 100.0, ctx)
    assert score == 75
    assert any("bounce risk" in r for r in reasons)

    # CE near resistance
    ctx["underlying"] = 109.0
    score, reasons = calculate_entry_quality("TEST", "CE", 110.0, ctx)
    assert score == 75
    assert any("rejection risk" in r for r in reasons)

def test_entry_quality_poor_rr():
    ctx = {
        "underlying": 105.0,
        "support": 100.0,
        "resistance": 110.0,
        "sl_underlying": 90.0,   # distance 15
        "target_underlying": 110.0, # distance 5
    }
    score, reasons = calculate_entry_quality("TEST", "CE", 110.0, ctx)
    assert score == 75
    assert any("Poor R:R" in r for r in reasons)

def test_entry_quality_missing_rr():
    ctx = {
        "underlying": 105.0,
        "support": 100.0,
        "resistance": 110.0,
    }
    score, reasons = calculate_entry_quality("TEST", "CE", 110.0, ctx)
    assert score == 100
    assert any("Missing SL/target" in r for r in reasons)

def test_entry_quality_wide_spread():
    ctx = {
        "underlying": 105.0,
        "sl_underlying": 100.0,
        "target_underlying": 110.0,
        "option_rows": [
            {
                "strike": 105.0,
                "option_type": "CE",
                "bid": 9.0,
                "ask": 10.0,
                "ltp": 9.5
            }
        ]
    }
    # spread = 1 / 9.5 = 10.5%
    score, reasons = calculate_entry_quality("TEST", "CE", 105.0, ctx)
    assert score == 80
    assert any("Wide spread" in r for r in reasons)

def test_entry_quality_chasing():
    ctx = {
        "underlying": 105.0,
        "sl_underlying": 100.0,
        "target_underlying": 110.0,
        "price_change_pct": 2.0
    }
    score, reasons = calculate_entry_quality("TEST", "CE", 105.0, ctx)
    assert score == 85
    assert any("Chasing after" in r for r in reasons)

    ctx["price_change_pct"] = -2.0
    score, reasons = calculate_entry_quality("TEST", "PE", 105.0, ctx)
    assert score == 85
    assert any("Chasing after" in r for r in reasons)

def test_entry_quality_perfect_score():
    ctx = {
        "underlying": 105.0,
        "support": 90.0,
        "resistance": 120.0,
        "sl_underlying": 100.0,   # distance 5
        "target_underlying": 115.0, # distance 10
        "price_change_pct": 0.5,
        "option_rows": [
            {
                "strike": 105.0,
                "option_type": "CE",
                "bid": 9.9,
                "ask": 10.1,
                "ltp": 10.0
            }
        ]
    }
    score, reasons = calculate_entry_quality("TEST", "CE", 105.0, ctx)
    assert score == 100
    assert not reasons


def test_entry_quality_sell_option_no_penalties():
    # 1. PE (Put Writing) near support, side=SELL -> should NOT be penalized
    ctx = {
        "underlying": 101.0,
        "support": 100.0,
        "resistance": 110.0,
        "side": "SELL",
    }
    score, reasons = calculate_entry_quality("TEST", "PE", 100.0, ctx)
    assert score == 100
    assert not any("bounce risk" in r for r in reasons)

    # 2. CE (Call Writing) near resistance, side=SELL -> should NOT be penalized
    ctx["underlying"] = 109.0
    score, reasons = calculate_entry_quality("TEST", "CE", 110.0, ctx)
    assert score == 100
    assert not any("rejection risk" in r for r in reasons)

    # 3. Chasing check for side=SELL -> should NOT be penalized
    ctx_chase = {
        "underlying": 105.0,
        "sl_underlying": 100.0,
        "target_underlying": 110.0,
        "price_change_pct": 2.0,
        "side": "SELL",
    }
    score, reasons = calculate_entry_quality("TEST", "CE", 105.0, ctx_chase)
    assert score == 100
    assert not any("Chasing after" in r for r in reasons)

    ctx_chase["price_change_pct"] = -2.0
    score, reasons = calculate_entry_quality("TEST", "PE", 105.0, ctx_chase)
    assert score == 100
    assert not any("Chasing after" in r for r in reasons)
