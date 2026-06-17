import pytest
from datetime import datetime, timezone
from src.models.schema import get_conn
from dashboard_server import get_risk_metrics

def test_risk_metrics_endpoint(isolated_db):
    # Clean database before running to ensure isolation
    with get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM daily_equity_peaks")

    # Initial state
    metrics = get_risk_metrics(mode="paper")
    assert metrics["mode"] == "paper"
    assert metrics["available_cash"] == 1000000.0
    assert metrics["total_open_pnl"] == 0.0
    assert metrics["drawdown_abs"] == 0.0
    assert metrics["profit_factor"] == 0.0
    assert metrics["avg_rr"] == 0.0
    assert metrics["total_notional_exposure"] == 0.0

    # 1. Insert a winning closed trade and a losing closed trade to check Profit Factor
    now_str = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, closed_at, symbol, option_type, entry_underlying, status, pnl_rupees)
            VALUES (?, ?, 'NIFTY', 'CE', 22000.0, 'CLOSED_TARGET', 5000.0)
            """,
            (now_str, now_str)
        )
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, closed_at, symbol, option_type, entry_underlying, status, pnl_rupees)
            VALUES (?, ?, 'NIFTY', 'CE', 22000.0, 'CLOSED_SL', -2000.0)
            """,
            (now_str, now_str)
        )

    metrics = get_risk_metrics(mode="paper")
    assert metrics["available_cash"] == 1003000.0  # 1,000,000 + 5000 - 2000
    assert metrics["profit_factor"] == 2.5  # 5000 / 2000

    # 2. Insert active open trades to verify R:R and Notional Exposure
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (opened_at, symbol, option_type, strike, entry_underlying, entry_premium, sl_premium, target_premium, lots, status)
            VALUES (?, 'NIFTY', 'CE', 22000.0, 22000.0, 100.0, 50.0, 200.0, 2, 'OPEN')
            """,
            (now_str,)
        )

    metrics = get_risk_metrics(mode="paper")
    # NIFTY lot size is 65 in settings or fallback. In settings, it is 65.
    # Qty = 2 lots * 65 = 130
    # Notional Exposure = 130 * 22000 = 2,860,000
    assert metrics["total_notional_exposure"] == 2860000.0
    # R:R for CE: (200 - 100) / (100 - 50) = 100 / 50 = 2.0
    assert metrics["avg_rr"] == 2.0

    # 3. Test drawdown updating peak equity
    # First call sets peak to 1,003,000 (available cash) + 0 (MTM) = 1,003,000
    assert metrics["current_equity"] == 1003000.0
    assert metrics["peak_equity"] == 1003000.0
    assert metrics["drawdown_abs"] == 0.0

    # Simulate equity drop by forcing open MTM to -5000.
    with get_conn() as conn:
        conn.execute("UPDATE paper_trades SET pnl_rupees=-5000.0 WHERE status='OPEN'")
    
    metrics = get_risk_metrics(mode="paper")
    assert metrics["current_equity"] == 998000.0  # 1,003,000 - 5000
    assert metrics["peak_equity"] == 1003000.0  # Peak is cached as 1003000
    assert metrics["drawdown_abs"] == 5000.0
    assert metrics["drawdown_pct"] == round((5000.0 / 1003000.0) * 100.0, 2)
