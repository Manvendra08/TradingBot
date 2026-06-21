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


def test_build_digest_wrapper_with_llm_verdict():
    from src.alerts.digest import build_digest_wrapper
    
    symbol = "NIFTY"
    alerts = [
        {
            "alert_type": "OI_SPIKE",
            "severity": "HIGH",
            "strike": 22000,
            "option_type": "CE",
            "detail_json": '{"pct_change": 45.5, "prev_oi": 100000, "curr_oi": 145500}'
        }
    ]
    scan_context = {
        "expiry": "2026-06-25",
        "underlying": 22010.5,
        "atm_strike": 22000,
        "pcr": 1.25,
        "support": 21900,
        "resistance": 22100,
        "price_change_pct": 0.45,
        "price_change_points": 98.2,
        "chart_indicators": {
            "1h": {"sentiment": "BULLISH"},
            "3h": {"sentiment": "BULLISH"}
        }
    }
    llm_verdict = {
        "bias": "BULLISH",
        "confidence": 85,
        "strategy": "Bull Call Spread",
        "strike_selection": "Buy 22000 CE, Sell 22100 CE",
        "reasoning": "Strong support holds at 21900 and option chain indicates high call buying activity.",
        "risk_rating": "LOW",
        "exit_advice": "SL below 21850",
        "news_synthesis": "No major negative domestic news"
    }
    
    digest_id, msg = build_digest_wrapper(
        symbol=symbol,
        alerts=alerts,
        fetched_at="2026-06-19T06:00:00Z",
        scan_context=scan_context,
        intelligence_text="rule verdict",
        detected_count=1,
        dedup_suppressed_count=0,
        llm_verdict=llm_verdict
    )
    
    # Assert consolidated template components are present
    assert "AI: BULLISH (85%)" in msg
    assert "Risk: LOW" in msg
    assert "Action: Buy 22000 CE, Sell 22100 CE → Bull Call Spread" in msg
    assert "Exit: SL below 21850" in msg
    assert "Reason: Strong supp holds" in msg or "Reason:" in msg
    assert "Levels: S:21900 | R:22100" in msg
    assert "Candles: 1H BULLISH ▲ | 3H BULLISH ▲" in msg


def test_build_digest_wrapper_fallback_no_llm():
    from src.alerts.digest import build_digest_wrapper
    
    symbol = "NIFTY"
    alerts = [
        {
            "alert_type": "OI_SPIKE",
            "severity": "HIGH",
            "strike": 22000,
            "option_type": "CE",
            "detail_json": '{"pct_change": 45.5, "prev_oi": 100000, "curr_oi": 145500}'
        }
    ]
    scan_context = {
        "expiry": "2026-06-25",
        "underlying": 22010.5,
        "atm_strike": 22000,
        "pcr": 1.25,
        "support": 21900,
        "resistance": 22100,
        "price_change_pct": 0.45,
        "price_change_points": 98.2,
        "chart_indicators": {
            "1h": {"sentiment": "BULLISH"},
            "3h": {"sentiment": "BULLISH"}
        }
    }
    
    digest_id, msg = build_digest_wrapper(
        symbol=symbol,
        alerts=alerts,
        fetched_at="2026-06-19T06:00:00Z",
        scan_context=scan_context,
        intelligence_text="*Verdict: Put Writing*\n_Desc text_\nConfidence: 80%\n*BULL FORCES*\n- Strong support\n*BEAR FORCES*\n- None\n*TRADE STRATEGY*\n- Action Plan: Sell PE at support\n- Critical Warning: Keep small size",
        detected_count=1,
        dedup_suppressed_count=0,
        llm_verdict=None
    )
    
    # Assert fallback template is used and text is compressed
    assert "TRADE: SELL PE" in msg
    assert "TRADING PLAN" in msg
    assert "Sell 21900 PE" in msg

