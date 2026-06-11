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


@pytest.mark.asyncio
async def test_manual_close_paper_trade():
    from src.models.schema import get_conn
    from dashboard_server import manual_close_paper_trade
    
    with get_conn() as conn:
        conn.execute("DELETE FROM paper_trades WHERE id=1001")
        conn.execute(
            """
            INSERT INTO paper_trades (id, symbol, option_type, strike, entry_underlying, entry_premium, lots, status, verdict_label, setup_type, side, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1001, "NIFTY", "CE", 22000.0, 22000.0, 150.0, 10, "OPEN", "LONG", "TIMEFRAME", "BUY", "2026-06-11T08:00:00Z")
        )
        conn.execute(
            "INSERT INTO underlying_price (symbol, price, fetched_at) VALUES (?, ?, ?)",
            ("NIFTY", 22100.0, "2026-06-11T09:00:00Z")
        )
        conn.execute(
            "INSERT INTO option_chain_snapshots (symbol, fetched_at, strike, option_type, ltp, oi, oi_change, volume, iv, expiry) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("NIFTY", "2026-06-11T09:00:00Z", 22000.0, "CE", 220.0, 1000, 100, 10, 15.0, "2026-06-11")
        )
        conn.commit()

    try:
        res = await manual_close_paper_trade(trade_id=1001)
        assert res == {"ok": True, "trade_id": 1001}

        with get_conn() as conn:
            trade = conn.execute("SELECT * FROM paper_trades WHERE id=1001").fetchone()
            assert trade is not None
            assert trade["status"] == "CLOSED_MANUAL"
            assert trade["exit_underlying"] == 22100.0
            assert trade["exit_premium"] == 220.0
            assert trade["pnl_points"] == 70.0  # 220.0 - 150.0
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM paper_trades WHERE id=1001")
            conn.execute("DELETE FROM underlying_price WHERE symbol='NIFTY' AND price=22100.0")
            conn.execute("DELETE FROM option_chain_snapshots WHERE symbol='NIFTY' AND ltp=220.0")
            conn.commit()

