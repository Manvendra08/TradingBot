import pytest
from src.engine.regime_detector import (
    detect_market_regime,
    regime_score_for_trade,
    REGIME_TRENDING_UP,
    REGIME_TRENDING_DOWN,
    REGIME_RANGE,
    REGIME_VOLATILE,
    REGIME_NO_TRADE,
)
from src.models.schema import get_conn


def insert_scan_summary(conn, symbol, fetched_at, verdict_label, underlying, confidence):
    conn.execute(
        """
        INSERT INTO scan_summaries (symbol, fetched_at, verdict_label, underlying, confidence)
        VALUES (?, ?, ?, ?, ?)
        """,
        (symbol, fetched_at, verdict_label, underlying, confidence),
    )
    conn.commit()


def test_regime_no_trade_not_enough_data():
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.commit()
    # Less than 5 rows
    for i in range(4):
        with get_conn() as conn:
            insert_scan_summary(conn, "TEST_SYM", f"2026-05-28T10:0{i}:00Z", "Sideways", 100.0, 50)
    
    regime = detect_market_regime("TEST_SYM")
    assert regime == REGIME_NO_TRADE


def test_regime_trending_up():
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.commit()
    # 10 rows, increasing price, bullish verdicts
    for i in range(10):
        with get_conn() as conn:
            insert_scan_summary(conn, "TEST_SYM", f"2026-05-28T10:{i:02d}:00Z", "Put Writing", 100.0 + i, 80)
            
    regime = detect_market_regime("TEST_SYM")
    assert regime == REGIME_TRENDING_UP


def test_regime_trending_down():
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.commit()
    # 10 rows, decreasing price, bearish verdicts
    for i in range(10):
        with get_conn() as conn:
            insert_scan_summary(conn, "TEST_SYM", f"2026-05-28T10:{i:02d}:00Z", "Call Writing", 100.0 - i, 80)
            
    regime = detect_market_regime("TEST_SYM")
    assert regime == REGIME_TRENDING_DOWN


def test_regime_volatile():
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.commit()
    # 10 rows, price swings widely (range > 3%), mixed verdicts
    prices = [100, 105, 95, 104, 96, 103, 97, 106, 94, 105]
    for i, p in enumerate(prices):
        with get_conn() as conn:
            insert_scan_summary(conn, "TEST_SYM", f"2026-05-28T10:{i:02d}:00Z", "Sideways", p, 50)
            
    regime = detect_market_regime("TEST_SYM")
    assert regime == REGIME_VOLATILE


def test_regime_range():
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.commit()
    # 10 rows, price barely changes (change < 0.3%), balanced verdicts
    prices = [100.0, 100.1, 99.9, 100.2, 99.8, 100.1, 99.9, 100.0, 100.1, 100.0]
    verdicts = ["Put Writing", "Call Writing", "Sideways"] * 4
    for i, p in enumerate(prices):
        with get_conn() as conn:
            insert_scan_summary(conn, "TEST_SYM", f"2026-05-28T10:{i:02d}:00Z", verdicts[i], p, 50)
            
    regime = detect_market_regime("TEST_SYM")
    assert regime == REGIME_RANGE


def test_regime_score_for_trade():
    assert regime_score_for_trade(REGIME_TRENDING_UP, "CE") == 100
    assert regime_score_for_trade(REGIME_TRENDING_DOWN, "PE") == 100
    assert regime_score_for_trade(REGIME_TRENDING_UP, "PE") == 70
    assert regime_score_for_trade(REGIME_TRENDING_DOWN, "CE") == 70
    assert regime_score_for_trade(REGIME_RANGE, "CE") == 30
    assert regime_score_for_trade(REGIME_VOLATILE, "PE") == 40
    assert regime_score_for_trade(REGIME_NO_TRADE, "CE") == 50
    assert regime_score_for_trade("UNKNOWN_REGIME", "PE") == 50
