import pytest
from unittest.mock import MagicMock, patch
from src.engine.multileg_live_trading import _rollback_placed_legs

def test_rollback_placed_legs_successful():
    placed_legs = [
        {
            "side": "SELL",
            "lots": 1,
            "strike": 24500,
            "option_type": "CE",
            "expiry": "2026-09-10",
            "broker_order_id": "ORDER_123"
        }
    ]
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "ROLLBACK_ORDER_456"

    with patch("src.engine.multileg_live_trading.resolve_option_contract") as mock_resolve, \
         patch("src.engine.multileg_live_trading.confirm_order_fill") as mock_confirm:
        mock_resolve.return_value = {"tradingsymbol": "NIFTY26SEP24500CE"}
        mock_confirm.return_value = ("COMPLETE", "Filled")

        summary = _rollback_placed_legs("NIFTY", mock_kite, placed_legs)

        assert summary["total"] == 1
        assert summary["filled"] == 1
        assert summary["failed"] == 0
        mock_kite.place_order.assert_called_once()

def test_rollback_placed_legs_no_kite_client():
    placed_legs = [
        {
            "side": "SELL",
            "lots": 1,
            "strike": 24500,
            "option_type": "CE",
            "expiry": "2026-09-10",
            "broker_order_id": "ORDER_123"
        }
    ]
    summary = _rollback_placed_legs("NIFTY", None, placed_legs)
    assert summary["total"] == 1
    assert summary["failed"] == 1
