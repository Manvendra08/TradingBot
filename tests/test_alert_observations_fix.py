"""
Targeted tests verifying fixes for Telegram alert observations:
1. Escaping fix: ( and ) not escaped with backslashes.
2. SENSEX direction fix: 'Put Writing' correctly classified as BULLISH.
3. Digest header status fix: prominent STAND ASIDE badge when not entered.
4. MCX live book monitoring on HOLDING: displays open books, age, and P&L.
5. Volatility outlier scrubbing: scrubs 1.0% and 300% IV artifacts.
"""
import pytest
from src.alerts.digest import _esc, build_tfss_timeframe_digest
from src.alerts.mcx_digest import build_mcx_timeframe_digest
from src.engine.decision_matrix import evaluate_matrix_decision
from src.engine.pipeline import _build_quant_inputs


def test_esc_does_not_escape_parentheses():
    text = "Put Writing (88%) and PE (+9.48M)"
    escaped = _esc(text)
    assert "\\(" not in escaped
    assert "\\)" not in escaped
    assert "(" in escaped
    assert ")" in escaped


def test_sensex_put_writing_classified_as_bullish():
    intel = {
        "verdict_label": "Put Writing",
        "direction": "Put Writing",
        "confidence": 88,
    }
    scan_context = {
        "verdict_label": "Put Writing",
        "underlying": 73342.0,
        "expiry": "2026-10-29",
    }
    decision = evaluate_matrix_decision("SENSEX", intel, scan_context)
    assert decision["direction"] == "BULLISH"
    # For BULLISH, it should never recommend BEAR_CALL_SPREAD
    assert decision["recommended_structure"] != "BEAR_CALL_SPREAD"


def test_digest_header_shows_stand_aside_when_no_entry():
    payload = {
        "header": {
            "symbol": "NIFTY",
            "scan_time": "13:15 IST",
            "expiry": "2026-11-03",
            "dte": 6,
            "spot": 22730.0,
            "trade_entered": False,
        },
        "quant_inputs": {
            "oi_verdict": "Long Buildup",
            "oi_confidence": 98,
            "pcr": 1.01,
            "max_pain": 22700.0,
            "support": 22500.0,
            "resistance": 23000.0,
            "chart_1h": "BULLISH",
            "chart_3h": "BEARISH",
        },
        "multileg": {
            "action": "NONE",
            "strategy_type": "NO_TRADE",
        },
        "ai_thesis": "AI chose STAND ASIDE (NO TRADE) despite engine=BULLISH due to 3H conflict.",
    }
    _, msg = build_tfss_timeframe_digest(payload)
    assert "⏸️ *STAND ASIDE*" in msg
    assert "AI chose STAND ASIDE" in msg


def test_mcx_holding_displays_open_books_and_age():
    payload = {
        "header": {
            "symbol": "NATURALGAS",
            "scan_time": "14:00 IST",
            "expiry": "2026-11-20",
            "dte": 23,
            "spot": 293.8,
            "trade_entered": False,
        },
        "quant_inputs": {
            "oi_verdict": "Rangebound",
            "oi_confidence": 60,
            "pcr": 1.09,
            "max_pain": 295.0,
            "support": 290.0,
            "resistance": 295.0,
            "chart_1h": "BULLISH",
            "chart_3h": "BULLISH",
            "atm_iv": 71.3,
            "iv_low": 65.0,
            "iv_high": 78.0,
        },
        "multileg": {
            "action": "HOLD",
            "confidence": 0,
            "live_books": [
                {
                    "book_id": "NATURALGAS:2026-10-28:TFSS",
                    "strategy_type": "SHORT_STRANGLE",
                    "opened_at": "2026-10-28T07:45:00+00:00",
                    "total_pnl": 4500.0,
                    "net_premium": 12.5,
                    "net_delta": 0.05,
                    "legs": [
                        {"side": "SELL", "strike": 285.0, "option_type": "PE", "entry_premium": 6.2, "current_premium": 4.1},
                        {"side": "SELL", "strike": 305.0, "option_type": "CE", "entry_premium": 6.3, "current_premium": 5.2},
                    ],
                }
            ],
        },
        "ai_thesis": "Monitoring active strangle book.",
    }
    _, msg = build_mcx_timeframe_digest(payload)
    assert "🔵 *HOLDING*" in msg
    assert "MULTI-LEG TRADES" in msg
    assert "SHORT STRANGLE" in msg
    assert "P&L ₹4,500" in msg
    assert "SELL 285.0 PE" in msg
    assert "Confidence 0% < 72% MCX Floor" not in msg


def test_volatility_outlier_scrubbing():
    option_rows = [
        {"strike": 200.0, "iv": 1.0},   # Deep OTM junk outlier
        {"strike": 280.0, "iv": 68.0},
        {"strike": 290.0, "iv": 71.5},  # ATM
        {"strike": 300.0, "iv": 74.0},
        {"strike": 400.0, "iv": 300.0}, # Deep OTM model artifact
    ]
    ctx = {
        "option_rows": option_rows,
        "atm_strike": 290.0,
        "pcr": 1.0,
    }
    intel = {"verdict_label": "Neutral", "confidence": 50}
    res = _build_quant_inputs("NATURALGAS", ctx, intel, {})
    assert res["atm_iv"] == 71.5
    assert res["iv_low"] >= 60.0
    assert res["iv_high"] <= 100.0


def test_validate_trade_leg_data_handles_symbol_and_fallbacks():
    from src.engine.data_validator import validate_trade_leg_data

    legs = [
        {"strike": 73300.0, "option_type": "CE", "entry_premium": 150.0},
        {"strike": 73300.0, "option_type": "PE", "entry_premium": 140.0},
    ]
    strikes = [
        {
            "strike_price": 73300.0,
            "ce_ltp": 150.0,
            "ce_bid": 149.0,
            "ce_ask": 151.0,
            "ce_oi": 10000,
            "ce_volume": 5000,
            "pe_ltp": 140.0,
            "pe_bid": 139.0,
            "pe_ask": 141.0,
            "pe_oi": 12000,
            "pe_volume": 6000,
        }
    ]

    # 1. With symbol passed explicitly (e.g. SENSEX) - previously raised NameError: name 'symbol' is not defined
    is_valid, issues = validate_trade_leg_data(legs, {"strikes": strikes}, 73342.0, symbol="SENSEX")
    assert is_valid is True
    assert issues == []

    # 2. With symbol inside oc_data
    is_valid, issues = validate_trade_leg_data(legs, {"symbol": "SENSEX", "strikes": strikes}, 73342.0)
    assert is_valid is True
    assert issues == []

    # 3. With symbol inside legs[0]
    legs_with_sym = [
        {"strike": 73300.0, "option_type": "CE", "symbol": "SENSEX", "entry_premium": 150.0},
        {"strike": 73300.0, "option_type": "PE", "symbol": "SENSEX", "entry_premium": 140.0},
    ]
    is_valid, issues = validate_trade_leg_data(legs_with_sym, {"strikes": strikes}, 73342.0)
    assert is_valid is True
    assert issues == []

    # 4. Without symbol at all (defaults to non-MCX fallback)
    is_valid, issues = validate_trade_leg_data(legs, {"strikes": strikes}, 73342.0)
    assert is_valid is True
    assert issues == []

