import pytest
from src.alerts.digest import _format_paper_trade_status

def test_format_paper_trade_status_none_values():
    # Test EXECUTED with None values
    status_executed = {
        "action": "EXECUTED",
        "setup_type": "CORE",
        "lots": None,
        "trade": {
            "option_type": "CE",
            "strike": None,
            "entry_premium": None,
            "entry_underlying": None,
            "sl_premium": None,
            "sl_underlying": None,
            "target_premium": None,
            "target_underlying": None,
            "side": None
        }
    }
    res = _format_paper_trade_status(status_executed)
    assert "*Status:* EXECUTED (CORE)" in res
    assert "Buy" in res or "BUY" in res  # Defaults to Buy/BUY
    assert "—" in res  # Placeholder for None values

    # Test EXECUTED FUT with None values
    status_fut = {
        "action": "EXECUTED",
        "setup_type": "CORE",
        "trade": {
            "option_type": "FUT",
            "strike": None,
            "entry_premium": None,
            "sl_premium": None,
            "target_premium": None
        }
    }
    res_fut = _format_paper_trade_status(status_fut)
    assert "FUT" in res_fut
    assert "SL: —" in res_fut
    assert "Target: —" in res_fut

    # Test CLOSED with None values
    status_closed = {
        "action": "CLOSED",
        "trade": {
            "option_type": "CE",
            "strike": None,
            "pnl_rupees": None,
            "side": "SELL"
        }
    }
    res_closed = _format_paper_trade_status(status_closed)
    assert "*Status:* CLOSED" in res_closed
    assert "Sell" in res_closed
    assert "P&L: ₹0.00" in res_closed  # Defaults to 0.0

    # Test HELD with None values
    status_held = {
        "action": "HELD",
        "trade": {
            "option_type": "CE",
            "strike": None,
            "opened_at": None
        }
    }
    res_held = _format_paper_trade_status(status_held)
    assert "*Status:* HELD" in res_held
    assert "—" in res_held

    # Test None/empty status
    assert "NO ACTION" in _format_paper_trade_status(None)
    assert "NO ACTION" in _format_paper_trade_status({})
