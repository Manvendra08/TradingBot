import pytest
import json
from src.engine.intelligence import _price_oi_verdict, generate_intelligence
from src.alerts.digest import _build_market_structure, _build_trading_plan, build_enhanced_digest

def test_price_oi_verdict_trending_up_missing_quadrants():
    # Scenario: Price is UP significantly, CE open interest is growing, Put open interest is falling
    # This was the NaturalGas case which previously fell to sideways/Call Writing (bearish) logic.
    
    # 1. Price is UP, CE is growing, PE is falling/unwinding
    verdict, emoji, desc = _price_oi_verdict(
        price_pct=0.0644,
        net_oi_change=-6600,
        ce_oi_change=4100,
        pe_oi_change=-10700,
        pcr=2.21
    )
    assert verdict == "Long Buildup"
    assert emoji == "🟢"
    assert "Bullish" in desc

    # 2. Price is UP, CE and PE are flat or minor
    verdict, emoji, desc = _price_oi_verdict(
        price_pct=0.06,
        net_oi_change=0,
        ce_oi_change=0,
        pe_oi_change=0,
        pcr=1.0
    )
    assert verdict == "Long Buildup"
    assert emoji == "🟢"
    assert "upward price trend dominant" in desc

def test_price_oi_verdict_trending_dn_missing_quadrants():
    # Scenario: Price is DOWN significantly, PE open interest is growing, CE open interest is falling/unwinding
    
    # 1. Price is DOWN, PE is growing, CE is falling/unwinding
    verdict, emoji, desc = _price_oi_verdict(
        price_pct=-0.06,
        net_oi_change=-5000,
        ce_oi_change=-8000,
        pe_oi_change=3000,
        pcr=0.5
    )
    assert verdict == "Short Buildup"
    assert emoji == "🔴"
    assert "Bearish" in desc

    # 2. Price is DOWN, CE and PE are flat or minor
    verdict, emoji, desc = _price_oi_verdict(
        price_pct=-0.06,
        net_oi_change=0,
        ce_oi_change=0,
        pe_oi_change=0,
        pcr=1.0
    )
    assert verdict == "Short Buildup"
    assert emoji == "🔴"
    assert "downward price trend dominant" in desc

def test_trading_plan_avoid_logic():
    # Check that is_bear verdict avoids buying CEs and selling PEs
    plan_bear = _build_trading_plan(
        symbol="NATURALGAS",
        verdict="Call Writing",  # Bearish
        strength=85,
        scan_context={"support": 290.0, "resistance": 320.0, "atm_strike": 315.0},
        intel={"conflict": None}
    )
    assert "Avoid:" in plan_bear
    assert "Buying CEs (trend is downward)" in plan_bear
    assert "Selling PEs below 290.00" in plan_bear
    assert "Buying PEs" not in plan_bear  # Avoid PEs was the incorrect behavior

    # Check that is_bull verdict avoids buying PEs and selling CEs
    plan_bull = _build_trading_plan(
        symbol="NATURALGAS",
        verdict="Put Writing",  # Bullish
        strength=85,
        scan_context={"support": 290.0, "resistance": 320.0, "atm_strike": 315.0},
        intel={"conflict": None}
    )
    assert "Avoid:" in plan_bull
    assert "Buying PEs (trend is upward)" in plan_bull
    assert "Selling CEs above 320.00" in plan_bull
    assert "Buying CEs" not in plan_bull  # Avoid CEs was the incorrect behavior

def test_market_structure_headers():
    alerts = [
        {
            "alert_type": "OI_SPIKE",
            "strike": 320.0,
            "option_type": "CE",
            "severity": "HIGH",
            "detail_json": json.dumps({"pct_change": 45.0})
        },
        {
            "alert_type": "OI_UNWIND",
            "strike": 290.0,
            "option_type": "PE",
            "severity": "HIGH",
            "detail_json": json.dumps({"pct_change": -40.0})
        }
    ]
    
    # Bearish verdict should NOT hardcode bias in headers
    struct_bear = _build_market_structure(alerts, "Call Writing")
    assert "Call (CE) Activity:" in struct_bear
    assert "Put (PE) Unwinding:" in struct_bear
    assert "bearish" not in struct_bear.lower()
    assert "bullish" not in struct_bear.lower()

    # Bullish verdict check
    alerts_bull = [
        {
            "alert_type": "OI_SPIKE",
            "strike": 290.0,
            "option_type": "PE",
            "severity": "HIGH",
            "detail_json": json.dumps({"pct_change": 55.0})
        },
        {
            "alert_type": "OI_UNWIND",
            "strike": 320.0,
            "option_type": "CE",
            "severity": "HIGH",
            "detail_json": json.dumps({"pct_change": -35.0})
        }
    ]
    struct_bull = _build_market_structure(alerts_bull, "Put Writing")
    assert "Put (PE) Activity:" in struct_bull
    assert "Call (CE) Unwinding:" in struct_bull
    assert "bearish" not in struct_bull.lower()
    assert "bullish" not in struct_bull.lower()

def test_full_digest_integration_with_up_trend():
    # Construct NaturalGas scenario like the user's scan with multiple alerts to get high confidence
    alerts = [
        {
            "fired_at": "2026-05-29T11:04:00",
            "symbol": "NATURALGAS",
            "alert_type": "OI_UNWIND",
            "strike": 350.0,
            "option_type": "CE",
            "severity": "HIGH",
            "detail_json": json.dumps({"pct_change": -66.7, "prev_oi": 15, "curr_oi": 9}),
            "telegram_sent": 0
        },
        {
            "fired_at": "2026-05-29T11:04:00",
            "symbol": "NATURALGAS",
            "alert_type": "OI_SPIKE",
            "strike": 325.0,
            "option_type": "PE",
            "severity": "HIGH",
            "detail_json": json.dumps({"pct_change": 23.9, "prev_oi": 109, "curr_oi": 135}),
            "telegram_sent": 0
        },
        {
            "fired_at": "2026-05-29T11:04:00",
            "symbol": "NATURALGAS",
            "alert_type": "OI_SPIKE",
            "strike": 330.0,
            "option_type": "PE",
            "severity": "HIGH",
            "detail_json": json.dumps({"pct_change": 45.0, "prev_oi": 100, "curr_oi": 145}),
            "telegram_sent": 0
        }
    ]
    scan_context = {
        "underlying": 315.60,
        "atm_strike": 315.0,
        "pcr": 2.21,
        "support": 290.0,
        "resistance": 320.0,
        "price_change_pct": 0.0644,
        "price_change_points": 19.10,
        "ce_oi_change": -5000,
        "pe_oi_change": 25000,
        "chart_indicators": {
            "NATURALGAS": {
                "1h": {"sentiment": "BULLISH", "ohlc": {}},
                "3h": {"sentiment": "BULLISH", "ohlc": {}}
            }
        }
    }
    
    # Run build_enhanced_digest
    digest_id, msg = build_enhanced_digest(
        symbol="NATURALGAS",
        alerts=alerts,
        fetched_at="2026-05-29T11:04:00",
        scan_context=scan_context
    )
    
    # We should have a BULLISH verdict (Long Buildup) because price is up +6.44%
    assert "BULLISH" in msg or "Long Buildup" in msg
    assert "Avoid:\n• Buying PEs" in msg or "Avoid:\n• Buying PEs" in msg.replace("\u2022", "•")
    # Verify that the market structure header doesn't say (bullish)
    assert "Put (PE) Activity:" in msg
    assert "PE Buildup (bullish):" not in msg


def test_price_oi_verdict_contradiction_handling():
    # Price up slightly, but massive call writing dominant
    verdict, emoji, desc = _price_oi_verdict(
        price_pct=0.06,
        net_oi_change=11000,
        ce_oi_change=10000,
        pe_oi_change=1000,
        pcr=0.8
    )
    assert verdict == "Call Writing"
    assert emoji == "🔴"

    # Price down slightly, but massive put writing dominant
    verdict, emoji, desc = _price_oi_verdict(
        price_pct=-0.06,
        net_oi_change=11000,
        ce_oi_change=1000,
        pe_oi_change=10000,
        pcr=1.3
    )
    assert verdict == "Put Writing"
    assert emoji == "🟢"


def test_confidence_contradictory_alerts_subtraction():
    from src.engine.intelligence import _compute_confidence
    scan_ctx = {
        "price_change_pct": 0.06,
        "ce_oi_change": 1000,
        "pe_oi_change": 5000,
        "pcr": 1.2
    }
    # Verdict is Long Buildup (Bullish). Let's pass a Bearish alert (CE spike).
    # A Bearish high-severity alert should subtract 20 points from base (10), ending up at 0 (bounded).
    alerts = [
        {
            "alert_type": "OI_SPIKE",
            "option_type": "CE",
            "severity": "HIGH",
            "detail_json": "{}"
        }
    ]
    conf, conflict = _compute_confidence(scan_ctx, alerts, verdict_label="Long Buildup")
    assert conf == 0  # 10 (base) - 20 (contradictory high severity CE spike) -> bounded to 0

    # If the alert aligns (PE spike is Bullish, matching Long Buildup), it should add 20 points
    alerts_align = [
        {
            "alert_type": "OI_SPIKE",
            "option_type": "PE",
            "severity": "HIGH",
            "detail_json": "{}"
        }
    ]
    conf_align, _ = _compute_confidence(scan_ctx, alerts_align, verdict_label="Long Buildup")
    assert conf_align == 30  # 10 (base) + 20 (aligned high severity PE spike) -> 30


def test_key_levels_global_max_walls():
    from src.engine.anomaly_detector import _key_levels
    strikes_data = [
        {"strike": 54000.0, "option_type": "PE", "oi": 10000},
        {"strike": 54100.0, "option_type": "CE", "oi": 12000},
        {"strike": 54200.0, "option_type": "CE", "oi": 2000},
        {"strike": 54200.0, "option_type": "PE", "oi": 2000},
        {"strike": 54300.0, "option_type": "CE", "oi": 1000},
    ]
    # Spot is at 54200. The global max CE is at 54100 (below spot), global max PE is at 54000 (below spot).
    # Support should be 54000, and Resistance should be 54100.
    levels = _key_levels(strikes_data, underlying=54200.0)
    assert levels["support"] == 54000.0
    assert levels["resistance"] == 54100.0


def test_trade_decision_chart_conflict_hard_block():
    from src.engine.trade_decision import make_trade_decision
    from unittest.mock import patch
    intel = {
        "verdict_label": "Long Buildup",
        "confidence": 80,
        "chart_conflict": True
    }
    ctx = {
        "underlying": 54200.0,
        "atm_strike": 54200.0,
        "support": 54000.0,
        "resistance": 54500.0,
        "expiry": "2026-06-04"
    }
    with patch("src.engine.trade_decision.calculate_entry_quality", return_value=(70, [])):
        decision = make_trade_decision("NIFTY", intel, ctx)
    assert decision["status"] == "TRIGGERED_EXPERIMENTAL"
    assert "CHART_CONFLICT" in decision.get("soft_conflicts", [])


