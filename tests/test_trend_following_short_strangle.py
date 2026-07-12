import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from src.models.schema import get_conn
from src.engine.trend_following_short_strangle import (
    normalize_core_verdict_to_tfss_intent,
    compute_persisted_trend,
    resolve_tfss_execution_side,
    is_confirmed_reversal,
    evaluate_reversal,
    PersistenceResult,
    TFSSIntent
)
from src.engine.trade_plan import select_candidate
from src.engine.decision_pipeline import step_tfss_handoff_core, PipelineContext

def test_normalize_core_verdict_to_tfss_intent():
    # Bullish verdicts
    for v in ["Long Buildup", "Short Covering", "GO_LONG", "Put Writing", "OI Bias Bullish"]:
        intent = normalize_core_verdict_to_tfss_intent(v)
        assert intent is not None
        assert intent.bias == "BULLISH"
        assert intent.execution_family == "TFSS_BULLISH"

    # Bearish verdicts
    for v in ["Short Buildup", "Long Unwinding", "GO_SHORT", "Call Writing", "OI Bias Bearish"]:
        intent = normalize_core_verdict_to_tfss_intent(v)
        assert intent is not None
        assert intent.bias == "BEARISH"
        assert intent.execution_family == "TFSS_BEARISH"

    # Neutral/Unmapped verdicts
    assert normalize_core_verdict_to_tfss_intent("Sideways") is None
    assert normalize_core_verdict_to_tfss_intent("FALLBACK") is None
    assert normalize_core_verdict_to_tfss_intent("random_verdict") is None

def test_resolve_tfss_execution_side():
    # Valid persisted trend: BULLISH
    persisted_bullish = PersistenceResult(is_valid=True, label="BULLISH")
    intent_bullish = TFSSIntent(bias="BULLISH", execution_family="TFSS_BULLISH")
    assert resolve_tfss_execution_side(intent_bullish, persisted_bullish) == "SELL_PE"

    # Valid persisted trend: BEARISH
    persisted_bearish = PersistenceResult(is_valid=True, label="BEARISH")
    intent_bearish = TFSSIntent(bias="BEARISH", execution_family="TFSS_BEARISH")
    assert resolve_tfss_execution_side(intent_bearish, persisted_bearish) == "SELL_CE"

    # Invalid persisted trend
    persisted_invalid = PersistenceResult(is_valid=False, reason="BELOW_MIN_MATCH")
    res = resolve_tfss_execution_side(intent_bullish, persisted_invalid)
    assert isinstance(res, dict)
    assert res["action"] == "BLOCK"
    assert "PERSISTENCE_NOT_CONFIRMED" in res["reason"]

def test_is_confirmed_reversal():
    assert is_confirmed_reversal("BULLISH", "SELL_CE") is True
    assert is_confirmed_reversal("BEARISH", "SELL_PE") is True
    assert is_confirmed_reversal("BULLISH", "SELL_PE") is False
    assert is_confirmed_reversal("BEARISH", "SELL_CE") is False
    assert is_confirmed_reversal("", "SELL_CE") is False
    assert is_confirmed_reversal("BULLISH", "") is False

def test_compute_persisted_trend(isolated_db):
    # Setup test data in in-memory scan_summaries DB
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")

    # Case 1: Insufficient scan history
    res = compute_persisted_trend("NIFTY")
    assert res.is_valid is False
    assert res.reason == "INSUFFICIENT_SCAN_HISTORY"

    # Insert 5 scans (most recent is neutral)
    now = datetime.now(timezone.utc)
    scans = [
        ("Sideways", now.isoformat(), 0),
        ("Long Buildup", now.isoformat(), 0),
        ("Long Buildup", now.isoformat(), 0),
        ("Long Buildup", now.isoformat(), 0),
        ("Long Buildup", now.isoformat(), 0)
    ]
    with get_conn() as conn:
        for verdict, ts, is_fallback in scans:
            conn.execute(
                "INSERT INTO scan_summaries (symbol, verdict_label, fetched_at, is_fallback) VALUES ('NIFTY', ?, ?, ?)",
                (verdict, ts, is_fallback)
            )

    # Case 2: Most recent is neutral
    res = compute_persisted_trend("NIFTY")
    assert res.is_valid is False
    assert res.reason == "MOST_RECENT_IS_NEUTRAL"

    # Case 3: Below min match (most recent is Long Buildup, but only 2 of 5 are bullish)
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
    scans_below_match = [
        ("Long Buildup", now.isoformat(), 0),
        ("Short Buildup", now.isoformat(), 0),
        ("Short Buildup", now.isoformat(), 0),
        ("Short Buildup", now.isoformat(), 0),
        ("Sideways", now.isoformat(), 0)
    ]
    with get_conn() as conn:
        for verdict, ts, is_fallback in scans_below_match:
            conn.execute(
                "INSERT INTO scan_summaries (symbol, verdict_label, fetched_at, is_fallback) VALUES ('NIFTY', ?, ?, ?)",
                (verdict, ts, is_fallback)
            )
    res = compute_persisted_trend("NIFTY")
    assert res.is_valid is False
    assert res.reason == "BELOW_MIN_MATCH"
    assert res.agreeing_count == 1 # only 1 bullish scan

    # Case 4: Valid persisted trend (4 out of 5 are bullish)
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
    scans_valid = [
        ("Long Buildup", now.isoformat(), 0),
        ("Long Buildup", now.isoformat(), 0),
        ("Long Buildup", now.isoformat(), 0),
        ("Sideways", now.isoformat(), 0),
        ("Long Buildup", now.isoformat(), 0)
    ]
    with get_conn() as conn:
        for verdict, ts, is_fallback in scans_valid:
            conn.execute(
                "INSERT INTO scan_summaries (symbol, verdict_label, fetched_at, is_fallback) VALUES ('NIFTY', ?, ?, ?)",
                (verdict, ts, is_fallback)
            )
    
    with patch("src.engine.trend_following_short_strangle.check_trend_persistence", return_value=(True, "Corroborated")):
        res = compute_persisted_trend("NIFTY")
        assert res.is_valid is True
        assert res.label == "BULLISH"
        assert res.agreeing_count == 4

def test_select_candidate():
    option_chain = [
        {"strike": 22000.0, "option_type": "PE", "delta": -0.06, "premium": 25.0},
        {"strike": 22100.0, "option_type": "PE", "delta": -0.12, "premium": 45.0},
        {"strike": 22200.0, "option_type": "PE", "delta": -0.18, "premium": 75.0},
        {"strike": 22300.0, "option_type": "CE", "delta": 0.08, "premium": 30.0},
        {"strike": 22400.0, "option_type": "CE", "delta": 0.14, "premium": 50.0},
    ]

    # Test candidate selection for SELL_PE with DTE = 1 (Band base delta range: 0.05 - 0.15)
    candidate = select_candidate(
        side="SELL_PE",
        persisted_label="BULLISH",
        dte=1,
        atr_state={"is_tightened": False},
        option_chain=option_chain
    )
    assert candidate is not None
    assert candidate["strike"] == 22100.0
    assert candidate["option_type"] == "PE"
    assert candidate["premium"] == 45.0

    # Test candidate selection for SELL_CE with DTE = 1
    candidate_ce = select_candidate(
        side="SELL_CE",
        persisted_label="BEARISH",
        dte=1,
        atr_state={"is_tightened": False},
        option_chain=option_chain
    )
    assert candidate_ce is not None
    assert candidate_ce["strike"] == 22300.0
    assert candidate_ce["option_type"] == "CE"

def test_step_tfss_handoff_core(isolated_db):
    # Setup scan context to qualify
    scan_ctx = {
        "intel": {
            "verdict_label": "Long Buildup",
            "confidence": 90
        }
    }
    
    # Run handoff step when scan history is insufficient
    ctx = PipelineContext(
        engine="CORE_OI",
        symbol="NIFTY",
        direction="LONG",
        underlying=22000.0,
        scan_context=scan_ctx,
        steps=[],
        ai_verdict=None
    )
    
    # Since DB is empty, should block persistence
    res = step_tfss_handoff_core(ctx)
    assert res.passed is False
    assert "TFSS Persistence Blocked" in res.reason

    # Add sufficient scans
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        for _ in range(5):
            conn.execute(
                "INSERT INTO scan_summaries (symbol, verdict_label, fetched_at, is_fallback) VALUES ('NIFTY', 'Long Buildup', ?, 0)",
                (now.isoformat(),)
            )

    with patch("src.engine.trend_following_short_strangle.check_trend_persistence", return_value=(True, "Corroborated")):
        res = step_tfss_handoff_core(ctx)
        assert res.passed is True
        assert ctx.scan_context["_tfss_intent"] == "BULLISH"
        assert ctx.scan_context["_tfss_execution_side"] == "SELL_PE"
