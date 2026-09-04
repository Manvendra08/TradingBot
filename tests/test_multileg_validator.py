import pytest
from src.engine.multileg_validator import validate_multileg_trade, ValidationResult
from src.engine.execution_parser import ParsedExecution

def test_validate_multileg_direction_conflict():
    proposal = ParsedExecution(
        is_valid=True,
        strategy="BULL_PUT_SPREAD",
        action="GO_LONG",
        legs=[
            {"action": "SELL", "strike": 24500, "option_type": "PE", "entry_premium": 150.0, "ratio": 1},
            {"action": "BUY", "strike": 24300, "option_type": "PE", "entry_premium": 50.0, "ratio": 1}
        ]
    )
    # Engine verdict is BEARISH_BUILDUP, proposal is GO_LONG
    result = validate_multileg_trade(proposal, engine_verdict="BEARISH_BUILDUP", underlying=24600.0)
    assert result.is_valid is False
    assert "Direction conflict" in result.rejection_reason

def test_validate_multileg_margin_exceeded():
    proposal = ParsedExecution(
        is_valid=True,
        strategy="SHORT_STRADDLE",
        action="GO_SHORT",
        legs=[
            {"action": "SELL", "strike": 24500, "option_type": "CE", "entry_premium": 200.0, "ratio": 10},
            {"action": "SELL", "strike": 24500, "option_type": "PE", "entry_premium": 200.0, "ratio": 10}
        ]
    )
    result = validate_multileg_trade(proposal, engine_verdict="NEUTRAL", underlying=24500.0, max_margin_inr=500000.0)
    assert result.is_valid is False
    assert "Margin requirement" in result.rejection_reason or "margin" in result.rejection_reason.lower()

def test_validate_multileg_valid():
    proposal = ParsedExecution(
        is_valid=True,
        strategy="BEAR_CALL_SPREAD",
        action="GO_SHORT",
        legs=[
            {"action": "SELL", "strike": 24500, "option_type": "CE", "entry_premium": 100.0, "ratio": 1},
            {"action": "BUY", "strike": 24700, "option_type": "CE", "entry_premium": 30.0, "ratio": 1}
        ]
    )
    result = validate_multileg_trade(proposal, engine_verdict="BEARISH", underlying=24400.0)
    assert result.is_valid is True
    assert result.rejection_reason == ""

def test_validate_multileg_canonical_verdicts():
    proposal_long = ParsedExecution(
        is_valid=True,
        strategy="BULL_PUT_SPREAD",
        action="GO_LONG",
        legs=[
            {"action": "SELL", "strike": 24500, "option_type": "PE", "entry_premium": 100.0, "ratio": 1},
            {"action": "BUY", "strike": 24300, "option_type": "PE", "entry_premium": 30.0, "ratio": 1}
        ]
    )
    # Short Covering is canonical BULLISH — GO_LONG is aligned, so valid
    res_sc = validate_multileg_trade(proposal_long, engine_verdict="Short Covering", underlying=24600.0)
    assert res_sc.is_valid is True

    # Short Buildup is canonical BEARISH — GO_LONG should conflict
    res_sb = validate_multileg_trade(proposal_long, engine_verdict="Short Buildup", underlying=24600.0)
    assert res_sb.is_valid is False
    assert "Direction conflict" in res_sb.rejection_reason

