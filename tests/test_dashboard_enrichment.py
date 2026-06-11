import pytest
from dashboard_server import _enrich_trade_details, _explain_verdict

def test_enrich_trade_details_timeframe():
    # Test case 1: setup_type is TIMEFRAME
    rows = [
        {
            "setup_type": "TIMEFRAME",
            "verdict_label": "LONG",
            "option_type": "CE",
            "reason": "some reason",
            "opened_at": "2026-06-11T09:00:00Z",
            "closed_at": None
        }
    ]
    _enrich_trade_details(rows)
    assert rows[0]["verdict_label"] == "TF-LONG"
    assert rows[0]["verdict_explanation"]["bias"] == "TF-Bullish"
    assert rows[0]["verdict_explanation"]["emoji"] == "🟦"

    # Test case 2: setup_type is not TIMEFRAME but reason contains timeframe
    rows = [
        {
            "setup_type": None,
            "verdict_label": "SHORT",
            "option_type": "PE",
            "reason": "timeframe exit | crossover",
            "opened_at": "2026-06-11T09:00:00Z",
            "closed_at": None
        }
    ]
    _enrich_trade_details(rows)
    assert rows[0]["verdict_label"] == "TF-SHORT"
    assert rows[0]["verdict_explanation"]["bias"] == "TF-Bearish"
    assert rows[0]["verdict_explanation"]["emoji"] == "🟦"

    # Test case 3: Not a timeframe trade
    rows = [
        {
            "setup_type": "EXPERIMENTAL_SETUP",
            "verdict_label": "Long Buildup",
            "option_type": "CE",
            "reason": "auto | Marginal setup",
            "opened_at": "2026-06-11T09:00:00Z",
            "closed_at": None
        }
    ]
    _enrich_trade_details(rows)
    assert rows[0]["verdict_label"] == "Long Buildup"
    assert rows[0]["verdict_explanation"]["bias"] == "Bullish"
    assert rows[0]["verdict_explanation"]["emoji"] == "📗"
