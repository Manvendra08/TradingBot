import pytest
from unittest.mock import patch
from src.engine.trend_analysis import (
    get_trend_alignment_score,
    detect_reversal_from_scans,
    get_broader_trend_from_alerts,
    check_trend_persistence,
    calculate_momentum_score,
)
from src.models.schema import get_conn


def insert_scan_summary(conn, symbol, fetched_at, verdict_label, confidence):
    conn.execute(
        """
        INSERT INTO scan_summaries (symbol, fetched_at, verdict_label, underlying, confidence)
        VALUES (?, ?, ?, 100.0, ?)
        """,
        (symbol, fetched_at, verdict_label, confidence),
    )
    conn.commit()


def test_get_trend_alignment_score():
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.commit()
    
    # 5 rows: 4 Put Writing, 1 Sideways -> 80% bullish alignment
    verdicts = ["Put Writing", "Put Writing", "Put Writing", "Put Writing", "Sideways"]
    for i, v in enumerate(verdicts):
        with get_conn() as conn:
            insert_scan_summary(conn, "TEST_SYM", f"2026-05-28T10:{i:02d}:00Z", v, 80)
            
    score = get_trend_alignment_score("TEST_SYM", "Put Writing")
    assert score == 80


def test_detect_reversal_from_scans():
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.commit()
        
    # Broader trend (older 8): Bearish (Call Writing)
    # Recent trend (last 2): Bullish (Put Writing)
    # With skip_latest=True, offset 1 is used, so the first row in DB (current scan)
    # is ignored. We need the next 2 rows (historical scans) to be Call Writing (opposite),
    # and the current verdict to be Put Writing (bullish).
    verdicts = ["Put Writing", "Call Writing", "Call Writing"] + ["Call Writing"] * 7
    for i, v in enumerate(verdicts):
        # We need fetched_at to be descending, so oldest is higher i. Wait, limit 10 order desc.
        # So lower i should be higher fetched_at (newer).
        with get_conn() as conn:
            # 59 - i to make i=0 the newest
            insert_scan_summary(conn, "TEST_SYM", f"2026-05-28T10:{59-i:02d}:00Z", v, 80)
            
    reversing, reason = detect_reversal_from_scans("TEST_SYM", "Put Writing", 80)
    assert reversing is True
    assert "Reversal confirmed" in reason
    
    reversing_low_conf, _ = detect_reversal_from_scans("TEST_SYM", "Put Writing", 70)
    assert reversing_low_conf is False


@patch("src.engine.trend_analysis.get_recent_alerts_for_symbol")
def test_get_broader_trend_from_alerts(mock_alerts):
    # Mocking alerts history returning dicts with verdict_label
    mock_alerts.return_value = [
        {"verdict_label": "Long Buildup"},
        {"verdict_label": "Long Buildup"},
        {"verdict_label": "Put Writing"},
        {"verdict_label": "Put Writing"},
        {"verdict_label": "Put Writing"},
    ] * 2  # Strong bullish factors
    
    trend = get_broader_trend_from_alerts("TEST_SYM")
    assert "Strong Bullish" in trend


@patch("src.engine.trend_analysis.get_broader_trend_from_alerts")
def test_check_trend_persistence(mock_broader):
    mock_broader.return_value = "🟢 Strong Bullish Trend"
    
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.commit()
    
    for i in range(5):
        with get_conn() as conn:
            insert_scan_summary(conn, "TEST_SYM", f"2026-05-28T10:{59-i:02d}:00Z", "Put Writing", 80)
            
    # Should pass
    should_trade, reason = check_trend_persistence("TEST_SYM", "Put Writing", 80, {})
    assert should_trade is True
    
    # Low confidence
    mock_broader.return_value = "Mixed/Unclear Trend"
    should_trade, reason = check_trend_persistence("TEST_SYM", "Put Writing", 65, {})
    assert should_trade is False
    assert "Mixed trend + low confidence" in reason


@patch("src.engine.trend_analysis.get_broader_trend_from_alerts")
def test_calculate_momentum_score(mock_broader):
    mock_broader.return_value = "🟢 Strong Bullish Trend" # +40
    
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.commit()
    
    for i in range(5):
        with get_conn() as conn:
            insert_scan_summary(conn, "TEST_SYM", f"2026-05-28T10:{59-i:02d}:00Z", "Put Writing", 80) # 100% consistency = +30
            
    ctx = {
        "chart_indicators": {
            "1h": {"verdict": "Long Buildup"},
            "3h": {"verdict": "Long Buildup"} # +20
        }
    }
    
    # Broader: 40, Scan: 30, Chart: 20, Conf: 8 -> Total = 98
    score = calculate_momentum_score("TEST_SYM", "Put Writing", 80, ctx)
    assert score == 98
