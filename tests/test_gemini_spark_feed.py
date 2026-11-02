import pytest
from src.engine.gemini_spark_feed import execute_gemini_setups, _extract_reconciled_trade_exit


def test_gemini_spark_setup_id_resolution_and_nifty_schema(monkeypatch):
    """Verify NIFTY feed with 'setup_id' is recognized and not dropped as None duplicate."""
    executed_trades = []

    monkeypatch.setattr("src.engine.gemini_spark_feed.insert_paper_trade", lambda t: executed_trades.append(t) or 999)
    monkeypatch.setattr("src.engine.gemini_spark_feed.push_execution_feedback_to_ledger", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.engine.gemini_spark_feed.sync_active_positions_snapshot", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.engine.gemini_spark_feed._send_telegram_notification", lambda *args, **kwargs: None)

    # NIFTY payload format from Google Apps Script bridge
    nifty_payload = {
        "symbol": "NIFTY_FUT",
        "market_state": "OPEN",
        "cmp_inr": 25700.0,
        "setups": [
            {
                "setup_id": "NF_20261028_99_TEST",
                "direction": "BUY",
                "action": "BUY",
                "setup_type": "MOMENTUM_BREAKOUT",
                "entry_trigger": 25650.0,
                "stop_loss": 25600.0,
                "targets": [25800.0],
                "status": "ACTIVE_RUNNER",
            }
        ],
    }

    execute_gemini_setups(nifty_payload, is_new_scan=True)
    assert len(executed_trades) == 1
    t = executed_trades[0]
    assert t["symbol"] == "NIFTY"
    assert t["side"] == "BUY"
    assert t["signal_key"] == "NF_20261028_99_TEST"
    assert "NF_20261028_99_TEST" in t["reason"]
    assert t["entry_underlying"] == 25700.0


def test_gemini_spark_pullback_vs_breakout_waiting_trigger(monkeypatch):
    """Verify that a pullback sell does not prematurely trigger before reaching resistance zone."""
    executed_trades = []

    monkeypatch.setattr("src.engine.gemini_spark_feed.insert_paper_trade", lambda t: executed_trades.append(t) or 999)
    monkeypatch.setattr("src.engine.gemini_spark_feed.push_execution_feedback_to_ledger", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.engine.gemini_spark_feed.sync_active_positions_snapshot", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.engine.gemini_spark_feed._send_telegram_notification", lambda *args, **kwargs: None)

    # Pullback sell: price is at 291.50, resistance entry zone is at 293.30.
    # Must wait for price to rally into resistance!
    ng_pullback_sell_payload = {
        "symbol": "NATURALGAS",
        "market_state": "OPEN",
        "cmp_inr": 291.50,
        "setups": [
            {
                "id": "NG_20261028_TEST_PULLBACK_REJECT",
                "direction": "SELL",
                "action": "SELL",
                "type": "PULLBACK_RESISTANCE_REJECT_SHORT",
                "entry_zone": {"recommended": 293.30},
                "stop_loss": 296.10,
                "targets": [{"price": 288.80}],
                "status": "WAITING_TRIGGER",
            }
        ],
    }

    execute_gemini_setups(ng_pullback_sell_payload, is_new_scan=True)
    # Should NOT have executed because price (291.50) has not rallied into resistance (293.30)
    assert len(executed_trades) == 0

    # Once price rallies to 293.50 (into resistance), it should trigger!
    ng_pullback_sell_payload["cmp_inr"] = 293.50
    execute_gemini_setups(ng_pullback_sell_payload, is_new_scan=True)
    assert len(executed_trades) == 1
    assert executed_trades[0]["signal_key"] == "NG_20261028_TEST_PULLBACK_REJECT"


def test_gemini_spark_reconciled_id_fallback():
    """Verify reconciled exit extracts setup_id when id key is absent."""
    payload = {
        "daily_reconciled_trades": [
            {
                "setup_id": "NF_20261028_02",
                "status": "TARGET_HIT_CLOSED",
                "exit_price": 25580.0,
                "pnl_pts": 103.0,
                "notes": "Target 2 hit",
            }
        ]
    }
    rec = _extract_reconciled_trade_exit(payload, "NF_20261028_02")
    assert rec is not None
    assert rec["exit_price"] == 25580.0
    assert rec["status"] == "CLOSED_TARGET"
