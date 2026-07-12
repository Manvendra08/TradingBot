"""
Unit tests for A0: pattern_history module (ADR-007 v2).
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from src.engine.pattern_history import PatternStats, get_pattern_stats, refresh_pattern_stats_rollup


def test_get_pattern_stats_empty_symbol():
    stats = get_pattern_stats("")
    assert stats.n_trades == 0
    assert stats.win_rate == 0.0
    assert stats.avg_pnl == 0.0


@patch("src.engine.pattern_history.get_conn")
def test_get_pattern_stats_from_rollup(mock_get_conn):
    mock_conn = MagicMock()
    mock_get_conn.return_value.__enter__.return_value = mock_conn

    # Mock fetchone returning rollup data
    mock_row = {"n_trades": 25, "win_rate": 0.68, "avg_pnl": 1500.5}
    mock_conn.execute.return_value.fetchone.return_value = mock_row

    stats = get_pattern_stats("NIFTY", "Bullish Trend", "NORMAL")
    assert stats.n_trades == 25
    assert stats.win_rate == 0.68
    assert stats.avg_pnl == 1500.5


@patch("src.engine.pattern_history.get_conn")
def test_get_pattern_stats_fallback_live(mock_get_conn):
    mock_conn = MagicMock()
    mock_get_conn.return_value.__enter__.return_value = mock_conn

    # First fetchone (specific rollup) -> None
    # Second fetchone (general symbol rollup) -> None
    mock_conn.execute.return_value.fetchone.side_effect = [None, None]

    # Live query returns 3 paper trades and 2 live trades combined
    mock_rows = [
        {"pnl_rupees": 1000.0},
        {"pnl_rupees": -500.0},
        {"pnl_rupees": 2000.0},
        {"pnl_rupees": 1500.0},
        {"pnl_rupees": 100.0}
    ]

    mock_conn.execute.return_value.__iter__.return_value = iter(mock_rows)

    stats = get_pattern_stats("BANKNIFTY", "Bullish Trend", "NORMAL")
    assert stats.n_trades == 5
    assert stats.win_rate == 4 / 5  # 4 positive PnLs out of 5
    assert stats.avg_pnl == (1000 - 500 + 2000 + 1500 + 100) / 5


@patch("src.engine.pattern_history.get_conn")
def test_refresh_pattern_stats_rollup(mock_get_conn):
    mock_conn = MagicMock()
    mock_get_conn.return_value.__enter__.return_value = mock_conn

    mock_paper_rows = [
        {"symbol": "CRUDEOIL", "verdict_label": "Bullish Trend", "pnl_rupees": 3000.0},
        {"symbol": "CRUDEOIL", "verdict_label": "Bullish Trend", "pnl_rupees": 2000.0},
    ]
    mock_live_rows = [
        {"symbol": "CRUDEOIL", "verdict_label": "Bullish Trend", "pnl_rupees": -1000.0},
    ]

    mock_conn.execute.return_value.__iter__.side_effect = [
        iter(mock_paper_rows),
        iter(mock_live_rows)
    ]

    count = refresh_pattern_stats_rollup()
    assert count >= 1
