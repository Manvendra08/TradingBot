import pytest
from unittest.mock import patch

from src.engine.capital_allocator import calculate_trade_lots


@pytest.mark.parametrize(
    "config, symbol, expected",
    [
        (
            {
                "live_broker_disabled": True,
                "paper_symbol_lots": {"NIFTY": 3, "SENSEX": 5},
                "paper_lots": 10,
            },
            "NIFTY",
            3,
        ),
        (
            {
                "live_broker_disabled": True,
                "paper_symbol_lots": {"NIFTY": 3},
                "paper_lots": 10,
            },
            "SENSEX",
            10,
        ),
        (
            {
                "live_broker_disabled": False,
                "live_symbol_lots": {"SENSEX": 7},
                "paper_symbol_lots": {"SENSEX": 2},
                "paper_lots": 10,
            },
            "SENSEX",
            7,
        ),
        (
            {
                "live_broker_disabled": False,
                "live_symbol_lots": {"NIFTY": 4},
                "paper_symbol_lots": {"NIFTY": 2},
            },
            "NIFTY",
            4,
        ),
    ],
)
def test_calculate_trade_lots_paper_modes(config, symbol, expected):
    with patch("src.engine.capital_allocator.load_runtime_config", return_value=config):
        assert calculate_trade_lots(symbol, 100.0, side="BUY", is_paper=True) == expected


def test_calculate_trade_lots_live_uses_symbol_override():
    config = {
        "live_broker_disabled": False,
        "live_symbol_lots": {"SENSEX": 6},
        "live_capital_per_trade_inr": 50000,
    }
    with patch("src.engine.capital_allocator.load_runtime_config", return_value=config):
        assert calculate_trade_lots("SENSEX", 100.0, side="BUY", is_paper=False) == 6
