import pytest
from src.engine.execution_parser import parse_llm_execution, ParsedExecution

def test_parse_llm_execution_valid_credit_spread():
    raw_response = {
        "strategy": "BEAR_CALL_SPREAD",
        "action": "GO_SHORT",
        "legs": [
            {"action": "SELL", "strike": 24500, "option_type": "CE", "entry_premium": 150.0, "ratio": 1},
            {"action": "BUY", "strike": 24700, "option_type": "CE", "entry_premium": 50.0, "ratio": 1}
        ],
        "net_credit": 100.0,
        "max_risk": 100.0
    }
    result = parse_llm_execution(raw_response, underlying=24400.0)
    assert result.is_valid is True
    assert result.rejection_reason == ""
    assert len(result.legs) == 2
    assert result.strategy == "BEAR_CALL_SPREAD"

def test_parse_llm_execution_missing_legs():
    raw_response = {
        "strategy": "IRON_CONDOR",
        "action": "GO_SHORT",
        "legs": []
    }
    result = parse_llm_execution(raw_response, underlying=24400.0)
    assert result.is_valid is False
    assert "Invalid leg count" in result.rejection_reason

def test_parse_llm_execution_invalid_strike():
    raw_response = {
        "strategy": "SHORT_STRANGLE",
        "action": "GO_SHORT",
        "legs": [
            {"action": "SELL", "strike": 0, "option_type": "CE", "entry_premium": 100.0},
            {"action": "SELL", "strike": 24000, "option_type": "PE", "entry_premium": 90.0}
        ]
    }
    result = parse_llm_execution(raw_response, underlying=24400.0)
    assert result.is_valid is False
    assert "Invalid strike" in result.rejection_reason

def test_parse_llm_execution_invalid_action():
    raw_response = {
        "strategy": "CUSTOM",
        "action": "GO_SHORT",
        "legs": [
            {"action": "HOLD", "strike": 24500, "option_type": "CE", "entry_premium": 100.0}
        ]
    }
    result = parse_llm_execution(raw_response, underlying=24400.0)
    assert result.is_valid is False
    assert "Invalid leg action" in result.rejection_reason
