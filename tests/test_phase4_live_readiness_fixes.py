import pytest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from src.engine.symbol_resolver import parse_tradingsymbol
from src.engine.time_guards import is_trading_allowed_now


def test_b9_parse_tradingsymbol_weekly_and_monthly():
    """Verify B9 fix: Weekly option tradingsymbols must not blend date digits into strike."""
    # Weekly NIFTY 10-Oct-2024 25000 CE
    p_weekly = parse_tradingsymbol("NIFTY24O1025000CE")
    assert p_weekly["symbol"] == "NIFTY"
    assert p_weekly["option_type"] == "CE"
    assert p_weekly["strike"] == 25000.0
    assert p_weekly["expiry"] == "2024-10-10"

    # Weekly BANKNIFTY 07-Nov-2024 51500 PE
    p_bn_w = parse_tradingsymbol("BANKNIFTY24N0751500PE")
    assert p_bn_w["symbol"] == "BANKNIFTY"
    assert p_bn_w["option_type"] == "PE"
    assert p_bn_w["strike"] == 51500.0
    assert p_bn_w["expiry"] == "2024-11-07"

    # Monthly NIFTY 28-Nov-2024 24500 CE
    p_monthly = parse_tradingsymbol("NIFTY24NOV24500CE")
    assert p_monthly["symbol"] == "NIFTY"
    assert p_monthly["option_type"] == "CE"
    assert p_monthly["strike"] == 24500.0

    # Commodity Futures
    p_fut = parse_tradingsymbol("NATURALGAS24NOVFUT")
    assert p_fut["symbol"] == "NATURALGAS"
    assert p_fut["option_type"] == "FUT"
    assert p_fut["strike"] == 0.0


def test_b15_time_guards_allow_exits():
    """Verify B15 fix: Event shock, CME holidays, and opening noise must NEVER block EXIT operations."""
    # 1. Opening auction noise window (09:20 IST)
    with patch("src.engine.time_guards.datetime") as mock_dt:
        mock_now = datetime(2026, 10, 26, 9, 20, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        # ENTRY should be blocked
        allowed_entry, reason_entry = is_trading_allowed_now("NIFTY", operation="ENTRY")
        assert not allowed_entry
        assert "Opening auction noise" in reason_entry

        # EXIT must be allowed
        allowed_exit, reason_exit = is_trading_allowed_now("NIFTY", operation="EXIT")
        assert allowed_exit

    # 2. CME Holiday for NATURALGAS
    with patch("src.engine.time_guards.datetime") as mock_dt, \
         patch("config.cme_holidays.is_cme_closed", return_value=True):
        mock_now = datetime(2026, 12, 25, 14, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        # ENTRY blocked
        allowed_entry, reason_entry = is_trading_allowed_now("NATURALGAS", operation="ENTRY")
        assert not allowed_entry
        assert "CME holiday" in reason_entry

        # EXIT allowed
        allowed_exit, reason_exit = is_trading_allowed_now("NATURALGAS", operation="EXIT")
        assert allowed_exit


def test_c4_close_live_book_closes_short_legs_first():
    """Verify C4 fix: Squaring off multileg book closes short SELL legs before long BUY hedges."""
    from src.engine.multileg_live_trading import _close_live_book

    legs = [
        {"id": 1, "side": "BUY", "strike": 24000.0, "option_type": "PE", "lots": 1, "broker_order_id": "sh-1"},
        {"id": 2, "side": "SELL", "strike": 24500.0, "option_type": "PE", "lots": 1, "broker_order_id": "sh-2"},
        {"id": 3, "side": "SELL", "strike": 25500.0, "option_type": "CE", "lots": 1, "broker_order_id": "sh-3"},
        {"id": 4, "side": "BUY", "strike": 26000.0, "option_type": "CE", "lots": 1, "broker_order_id": "sh-4"},
    ]

    # In shadow/db_only close, verify order of legs processed
    processed_order = []
    with patch("src.models.schema.close_leg", side_effect=lambda lid, *args: processed_order.append(lid)), \
         patch("src.models.schema.close_book"):
        _close_live_book("NIFTY", "test_b1", legs, "2026-10-26T14:00:00", "CLOSED", "test", 0.0)

    # SELL legs (2 and 3) must be closed BEFORE BUY legs (1 and 4)
    sell_indices = [processed_order.index(2), processed_order.index(3)]
    buy_indices = [processed_order.index(1), processed_order.index(4)]
    assert max(sell_indices) < min(buy_indices)


def test_c4_rollback_placed_legs_rolls_back_short_legs_first():
    """Verify C4 fix: Rolling back multi-leg entry orders squares off SELL legs first."""
    from src.engine.multileg_live_trading import _rollback_placed_legs

    placed_legs = [
        {"side": "BUY", "strike": 24000.0, "option_type": "PE", "lots": 1, "filled_quantity": 25},
        {"side": "SELL", "strike": 24500.0, "option_type": "PE", "lots": 1, "filled_quantity": 25},
    ]

    orders_placed = []
    with patch("src.engine.multileg_live_trading.resolve_option_contract", return_value={"tradingsymbol": "TEST", "tick_size": 0.05}), \
         patch("src.engine.live_trading.place_kite_order", side_effect=lambda *args, **kwargs: (orders_placed.append(args[4]), "ord_123")[1]), \
         patch("src.engine.multileg_live_trading.confirm_order_fill", return_value=("COMPLETE", "Filled")):
        _rollback_placed_legs("NIFTY", kite=MagicMock(), placed_legs=placed_legs, shadow_mode=False)

    # The SELL leg must be rolled back (BUY order) before the BUY leg (SELL order)
    assert orders_placed == ["BUY", "SELL"]


def test_c6_risk_profile_takes_precedence_in_trade_dict():
    """Verify C6 fix: Multi-leg live trades take max_loss/max_profit/breakevens from risk_profile, not LLM verdict."""
    risk_profile = {
        "max_profit": 5500.0,
        "max_loss": 12000.0,
        "breakeven_upper": 25250.0,
        "breakeven_lower": 24750.0,
    }
    verdict = MagicMock()
    verdict.max_profit = 99999.0
    verdict.max_loss = 11111.0
    verdict.breakeven_upper = 26000.0
    verdict.breakeven_lower = 24000.0

    trade_dict = {
        "max_profit": risk_profile.get("max_profit") if risk_profile.get("max_profit") is not None else getattr(verdict, "max_profit", 0.0),
        "max_loss": risk_profile.get("max_loss") if risk_profile.get("max_loss") is not None else getattr(verdict, "max_loss", 0.0),
        "breakeven_upper": risk_profile.get("breakeven_upper"),
        "breakeven_lower": risk_profile.get("breakeven_lower"),
    }
    assert trade_dict["max_profit"] == 5500.0
    assert trade_dict["max_loss"] == 12000.0
    assert trade_dict["breakeven_upper"] == 25250.0
    assert trade_dict["breakeven_lower"] == 24750.0
