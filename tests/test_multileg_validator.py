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

def test_validate_multileg_symbol_specific_margin():
    # SENSEX at 75,000 spot: lot size is 20 (not 75).
    # Margin for 1 leg sell ratio 1: 75000 * 1 * 20 * 0.15 = 225,000 <= 500,000 (valid!)
    proposal_sensex = ParsedExecution(
        is_valid=True,
        strategy="BEAR_CALL_SPREAD",
        action="GO_SHORT",
        legs=[
            {"action": "SELL", "strike": 75500, "option_type": "CE", "entry_premium": 100.0, "ratio": 1},
            {"action": "BUY", "strike": 76000, "option_type": "CE", "entry_premium": 30.0, "ratio": 1}
        ]
    )
    res = validate_multileg_trade(
        proposal_sensex,
        engine_verdict="BEARISH",
        underlying=75000.0,
        max_margin_inr=500000.0,
        symbol="SENSEX"
    )
    assert res.is_valid is True


def test_validate_multileg_delta_normalization_scaled_lots():
    # 16-lot BEAR_CALL_SPREAD on NIFTY:
    # Sell CE delta 0.35, Buy CE delta 0.15.
    # Unit net delta is -0.35 + 0.15 = -0.20 (within ±0.60).
    # Scaled ratio 16 must NOT produce -3.20 delta breach.
    proposal = ParsedExecution(
        is_valid=True,
        strategy="BEAR_CALL_SPREAD",
        action="GO_SHORT",
        legs=[
            {"action": "SELL", "strike": 25950, "option_type": "CE", "entry_premium": 100.0, "ratio": 16, "delta": 0.35},
            {"action": "BUY", "strike": 26150, "option_type": "CE", "entry_premium": 40.0, "ratio": 16, "delta": 0.15},
        ]
    )
    res = validate_multileg_trade(
        proposal,
        engine_verdict="BEARISH",
        underlying=25900.0,
        max_margin_inr=5000000.0,
        max_net_delta=0.60,
        symbol="NIFTY",
    )
    assert res.is_valid is True
    assert res.rejection_reason == ""


def test_validate_multileg_bull_put_spread_delta():
    # BULL_PUT_SPREAD: Sell PE delta 0.30, Buy PE delta 0.15.
    # Selling PE is bullish (+0.30), Buying PE is bearish (-0.15) -> unit net delta = +0.15.
    proposal = ParsedExecution(
        is_valid=True,
        strategy="BULL_PUT_SPREAD",
        action="GO_LONG",
        legs=[
            {"action": "SELL", "strike": 25000, "option_type": "PE", "entry_premium": 120.0, "ratio": 10, "delta": 0.30},
            {"action": "BUY", "strike": 24800, "option_type": "PE", "entry_premium": 50.0, "ratio": 10, "delta": 0.15},
        ]
    )
    res = validate_multileg_trade(
        proposal,
        engine_verdict="BULLISH",
        underlying=25100.0,
        max_margin_inr=5000000.0,
        max_net_delta=0.60,
        symbol="NIFTY",
    )
    assert res.is_valid is True
    assert res.rejection_reason == ""


def test_validate_multileg_delta_breach():
    # Single deep ITM short call with delta 0.75 breaches ±0.60 cap even when scaled to 5 lots
    proposal = ParsedExecution(
        is_valid=True,
        strategy="NAKED_CALL",
        action="GO_SHORT",
        legs=[
            {"action": "SELL", "strike": 25000, "option_type": "CE", "entry_premium": 250.0, "ratio": 5, "delta": 0.75},
        ]
    )
    res = validate_multileg_trade(
        proposal,
        engine_verdict="BEARISH",
        underlying=25500.0,
        max_margin_inr=5000000.0,
        max_net_delta=0.60,
        symbol="NIFTY",
    )
    assert res.is_valid is False
    assert "Proposal net delta -0.75 exceeds cap ±0.60" in res.rejection_reason





def test_validate_multileg_itm_short_rejected():
    """Sold legs must not be ITM."""
    proposal = ParsedExecution(
        is_valid=True,
        strategy="BEAR_CALL_SPREAD",
        action="GO_SHORT",
        legs=[
            {"action": "SELL", "strike": 24000, "option_type": "CE", "entry_premium": 600.0, "ratio": 1},
            {"action": "BUY", "strike": 24200, "option_type": "CE", "entry_premium": 450.0, "ratio": 1},
        ]
    )
    # Spot is 24500, selling 24000 CE is ITM
    res = validate_multileg_trade(proposal, engine_verdict="BEARISH", underlying=24500.0)
    assert res.is_valid is False
    assert "ITM short leg rejected: CE strike 24000" in res.rejection_reason


def test_validate_multileg_danger_ceiling_breach():
    """Short leg delta exceeding active danger ceiling must be rejected."""
    proposal = ParsedExecution(
        is_valid=True,
        strategy="BULL_PUT_SPREAD",
        action="GO_LONG",
        legs=[
            {"action": "SELL", "strike": 24300, "option_type": "PE", "entry_premium": 150.0, "ratio": 1, "delta": 0.45},
            {"action": "BUY", "strike": 24100, "option_type": "PE", "entry_premium": 60.0, "ratio": 1, "delta": 0.15},
        ]
    )
    # Aggressive ceiling for credit spread short is 0.40; delta 0.45 breaches it
    res = validate_multileg_trade(proposal, engine_verdict="BULLISH", underlying=24500.0)
    assert res.is_valid is False
    assert "Delta danger ceiling breached" in res.rejection_reason


def test_validate_multileg_mcx_naked_ban():
    """MCX symbols must reject naked structures (SHORT_STRANGLE)."""
    proposal = ParsedExecution(
        is_valid=True,
        strategy="SHORT_STRANGLE",
        action="GO_SHORT",
        legs=[
            {"action": "SELL", "strike": 350, "option_type": "CE", "entry_premium": 12.0, "ratio": 1},
            {"action": "SELL", "strike": 320, "option_type": "PE", "entry_premium": 10.0, "ratio": 1},
        ]
    )
    res = validate_multileg_trade(proposal, engine_verdict="NEUTRAL", underlying=335.0, symbol="NATURALGAS")
    assert res.is_valid is False
    assert "Naked options structure 'SHORT_STRANGLE' is prohibited on MCX" in res.rejection_reason
