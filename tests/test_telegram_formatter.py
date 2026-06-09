import pytest
from src.engine.telegram_formatter import (
    format_user_friendly_message,
    format_compact_message,
    format_detailed_message,
    _get_simple_explanation,
    _get_bar,
    _get_action_plan
)

def test_get_simple_explanation():
    assert "Smart money is buying puts" in _get_simple_explanation("Put Writing", 80)
    assert "Buyers are accumulating" in _get_simple_explanation("Long Buildup", 60)
    assert "High Confidence" in _get_simple_explanation("Long Buildup", 75)
    assert "Low Confidence" in _get_simple_explanation("Long Buildup", 50)
    assert "Mixed signals" in _get_simple_explanation("Unknown", 90)

def test_get_bar():
    bar = _get_bar(80, 100)
    assert "🟢" in bar
    assert bar.count("█") == 8
    
    bar_yellow = _get_bar(60, 100)
    assert "🟡" in bar_yellow
    
    bar_red = _get_bar(40, 100)
    assert "🔴" in bar_red

def test_get_action_plan():
    actions = _get_action_plan("Put Writing", 80, "TRIGGERED_CORE")
    assert any("Bot approved" in a for a in actions)
    assert any("Sell Put" in a for a in actions)
    
    actions = _get_action_plan("Call Writing", 80, "TRIGGERED_EXPERIMENTAL")
    assert any("marginal setup" in a for a in actions)
    assert any("Sell Call" in a for a in actions)
    
    # Futures case for Natural Gas
    actions = _get_action_plan("Put Writing", 80, "TRIGGERED_CORE", "NATURALGAS")
    assert any("Buy Futures" in a for a in actions)

    actions = _get_action_plan("Call Writing", 80, "TRIGGERED_CORE", "NATURALGAS")
    assert any("Sell Futures" in a for a in actions)

    actions = _get_action_plan("Sideways", 40, "BLOCKED")
    assert any("not ready" in a for a in actions)
    assert any("LOW CONFIDENCE" in a for a in actions)

def test_format_user_friendly_message_bullish_triggered():
    intel = {
        "symbol": "NATURALGAS",
        "verdict_label": "Put Writing",
        "confidence": 85,
        "bias": "BULLISH",
        "trend": "Strong Uptrend",
        "chart_conflict": False
    }
    decision = {
        "status": "TRIGGERED_CORE",
        "setup_type": "TREND_CONTINUATION",
        "scores": {
            "confidence": 85,
            "entry_quality": 90,
            "trend_alignment": 100,
            "regime_score": 80,
            "momentum_score": 75
        }
    }
    risk_info = {
        "blocked": False,
        "open_trades": 1,
        "max_trades": 4,
        "daily_loss": 100,
        "max_loss": 5000
    }
    msg = format_user_friendly_message(intel, decision, risk_info)
    assert "NATURALGAS — TRADING SIGNAL" in msg
    assert "🟢 BUY SIGNAL" in msg
    assert "✅ GO AHEAD" in msg
    assert "Open Trades: 1/4" in msg
    assert "Strong Uptrend" in msg
    assert "Momentum" in msg

def test_format_user_friendly_message_bearish_blocked_with_conflict():
    intel = {
        "symbol": "NIFTY",
        "verdict_label": "Call Writing",
        "confidence": 60,
        "bias": "BEARISH",
        "chart_conflict": True
    }
    decision = {
        "status": "BLOCKED",
        "setup_type": "EXPERIMENTAL_SETUP",
        "scores": {}
    }
    risk_info = {
        "blocked": True,
        "reason": "Max loss hit"
    }
    msg = format_user_friendly_message(intel, decision, risk_info)
    assert "🔴 SELL SIGNAL" in msg
    assert "❌ WAIT" in msg
    assert "❌ BLOCKED: Max loss hit" in msg
    assert "CHART WARNING" in msg

def test_format_compact_message():
    intel = {
        "symbol": "NATURALGAS",
        "verdict_label": "Put Writing",
        "confidence": 85,
        "bias": "BULLISH"
    }
    decision = {
        "status": "TRIGGERED_CORE",
        "setup_type": "CONFIRMED_REVERSAL",
        "scores": {
            "confidence": 85,
            "entry_quality": 90,
            "trend_alignment": 100,
            "regime_score": 80,
        }
    }
    msg = format_compact_message(intel, decision)
    assert "🟢 BUY | NATURALGAS | Put Writing | Conf: 85%" in msg
    assert "Decision: ✅ GO | Type: Reversal" in msg
    assert "Scores: Conf:85% EQ:90 TA:100 Reg:80" in msg

def test_format_detailed_message():
    intel = {
        "symbol": "NATURALGAS",
        "verdict_label": "Put Writing",
        "confidence": 85,
        "bias": "BULLISH",
        "trend": "Strong Uptrend"
    }
    decision = {
        "status": "TRIGGERED_CORE",
        "setup_type": "CONFIRMED_REVERSAL",
        "reason": "All good",
        "scores": {
            "confidence": 85,
            "entry_quality": 90,
            "trend_alignment": 100,
            "regime_score": 80,
            "momentum_score": 75
        }
    }
    scan_context = {
        "underlying": 250.50,
        "support": 240.0,
        "resistance": 260.0,
        "pcr": 1.5
    }
    msg = format_detailed_message(intel, decision, scan_context)
    assert "🤖 NSEBOT INTELLIGENCE" in msg
    assert "🟢 BULLISH SIGNAL" in msg
    assert "TRADE APPROVED" in msg
    assert "Setup Type: CONFIRMED_REVERSAL" in msg
    assert "Future: 250.50" in msg
    assert "Support: 240" in msg
    assert "Resistance: 260" in msg
    assert "PCR: 1.50" in msg
    assert "BROADER TREND: Strong Uptrend" in msg
    assert "Review the setup" in msg
