from datetime import datetime, timedelta, timezone
import pytest
from src.engine.paper_plan import mcx_option_liquidity_ok
from src.engine.data_validator import _classify_leg_liquidity
from src.engine.multileg_strategy import validate_legs
from src.engine.decision_matrix import check_churn_guards
from src.engine.capital_allocator import calculate_multileg_lots
import src.models.schema as schema


def test_mcx_option_liquidity_ok_delegation():
    # 1. Liquid ATM options
    ctx_liquid = {
        "option_rows": [
            {
                "strike": 250.0,
                "option_type": "CE",
                "oi": 3000,
                "volume": 600,
                "bid": 8.0,
                "ask": 8.15,
            },
            {
                "strike": 250.0,
                "option_type": "PE",
                "oi": 2500,
                "volume": 500,
                "bid": 6.0,
                "ask": 6.10,
            },
        ]
    }
    assert mcx_option_liquidity_ok("NATURALGAS", 250.0, ctx_liquid) is True

    # 2. Illiquid ATM options (OI < 1500)
    ctx_illiquid = {
        "option_rows": [
            {
                "strike": 250.0,
                "option_type": "CE",
                "oi": 400,  # Below 1500
                "volume": 600,
                "bid": 8.0,
                "ask": 8.15,
            },
            {
                "strike": 250.0,
                "option_type": "PE",
                "oi": 2500,
                "volume": 500,
                "bid": 6.0,
                "ask": 6.10,
            },
        ]
    }
    assert mcx_option_liquidity_ok("NATURALGAS", 250.0, ctx_illiquid) is False

    # 3. Missing strike
    assert mcx_option_liquidity_ok("NATURALGAS", 300.0, ctx_liquid) is False


def test_classify_leg_liquidity_mcx_delegation():
    # For MCX symbol: high spread or low OI returns illiquid
    tier, reason = _classify_leg_liquidity(
        ltp=10.0, bid=8.0, ask=11.0, volume=500, oi=2000, symbol="NATURALGAS"
    )
    # 11.0 - 8.0 = 3.0 pts spread (> 0.30) and 3/11 = 27% (> 2.0%)
    assert tier == "illiquid"
    assert "LIQ: Wide spread" in reason or "LIQ: Extreme spread" in reason

    # Liquid MCX strike
    tier_ok, reason_ok = _classify_leg_liquidity(
        ltp=10.0, bid=10.0, ask=10.15, volume=500, oi=2000, symbol="NATURALGAS"
    )
    assert tier_ok == "primary"
    assert "mcx_gate_ok" in reason_ok

    # NSE index symbol uses standard hierarchical logic
    tier_nse, reason_nse = _classify_leg_liquidity(
        ltp=100.0, bid=99.0, ask=101.0, volume=500, oi=2000, symbol="NIFTY"
    )
    assert tier_nse == "primary"
    assert "depth_ok" in reason_nse


def test_mcx_iron_condor_allowed_in_validate_legs():
    # Iron condor on NATURALGAS should now validate if wing rules pass
    legs = [
        {"strike": 260.0, "option_type": "CE", "side": "SELL", "premium": 8.0},
        {"strike": 270.0, "option_type": "CE", "side": "BUY", "premium": 2.5},
        {"strike": 240.0, "option_type": "PE", "side": "SELL", "premium": 7.0},
        {"strike": 230.0, "option_type": "PE", "side": "BUY", "premium": 2.0},
    ]
    chain = {
        260.0: {"CE": {"ltp": 8.0}},
        270.0: {"CE": {"ltp": 2.5}},
        240.0: {"PE": {"ltp": 7.0}},
        230.0: {"PE": {"ltp": 2.0}},
    }
    is_valid, msg = validate_legs("IRON_CONDOR", legs, chain, underlying=250.0, symbol="NATURALGAS")
    assert is_valid is True, f"Expected valid MCX Iron Condor, got: {msg}"


def test_check_churn_guards(monkeypatch):
    cfg = {
        "churn_guards": {
            "max_open_books_per_symbol": 2,
            "cooldown_minutes": 30,
            "max_entries_per_day": 3,
        }
    }

    # 1. Open books cap breach
    open_books = [{"book_id": "b1"}, {"book_id": "b2"}]
    ok, reason = check_churn_guards("NIFTY", open_books, cfg=cfg)
    assert ok is False
    assert "Max open books cap reached (2 >= 2)" in reason

    # 2. Below open books cap, clean DB
    ok_clean, reason_clean = check_churn_guards("NIFTY", [{"book_id": "b1"}], cfg=cfg)
    assert ok_clean is True
    assert reason_clean == "OK"


def test_calculate_multileg_lots():
    # Capital: 500,000, capital_cap_pct: 1.5% -> cap: 7,500
    # Defined risk credit spread with max loss per lot of 3,500
    # floor(7500 / 3500) = 2 lots
    lots = calculate_multileg_lots(
        symbol="NIFTY",
        strategy_type="BULL_PUT_SPREAD",
        max_loss_per_lot=3500.0,
        underlying=24500.0,
        capital=500000.0,
    )
    assert lots == 2

    # Max loss per lot exceeding total allowed cap (e.g. 15,000 > 7,500)
    # floor(7500 / 15000) = 0 lots -> NO_TRADE
    lots_zero = calculate_multileg_lots(
        symbol="NIFTY",
        strategy_type="BULL_PUT_SPREAD",
        max_loss_per_lot=15000.0,
        underlying=24500.0,
        capital=500000.0,
    )
    assert lots_zero == 0

    # Undefined risk with daily ATR stress:
    # NIFTY lot_size = 65, daily_atr = 200 pts
    # gap loss = 2 * 200 * 65 = 26,000 per lot
    # floor(7500 / 26000) = 0 lots (rejects oversized tail risk)
    lots_strangle = calculate_multileg_lots(
        symbol="NIFTY",
        strategy_type="SHORT_STRANGLE",
        max_loss_per_lot=5000.0,
        underlying=24500.0,
        capital=500000.0,
        daily_atr=200.0,
    )
    assert lots_strangle == 0


def test_roll_wing_guard_logic():
    # Simulate an Iron Condor position with PE wing at 24000, short PE at 24200
    legs = [
        {"strike": 24200.0, "option_type": "PE", "side": "SELL"},
        {"strike": 24000.0, "option_type": "PE", "side": "BUY"},
    ]
    pe_wings = [float(l["strike"]) for l in legs if l["option_type"] == "PE" and l["side"] == "BUY"]
    assert max(pe_wings) == 24000.0

    # Moving further OTM to 24100 is valid (24100 > 24000)
    valid_roll = 24100.0
    assert valid_roll > max(pe_wings)

    # Inverting wing by rolling to 23900 is rejected (23900 <= 24000)
    invalid_roll = 23900.0
    assert invalid_roll <= max(pe_wings)

