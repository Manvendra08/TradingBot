from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


def _reset_live_tables():
    from src.models.schema import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM live_trades")
        conn.execute("DELETE FROM broker_configs")
        conn.execute("DELETE FROM underlying_price")


def _insert_open_live_trade(**overrides) -> int:
    from src.models.schema import insert_live_trade

    trade = {
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "symbol": "NIFTY",
        "expiry": "2026-06-25",
        "verdict_label": "Long Buildup",
        "side": "BUY",
        "option_type": "FUT",
        "strike": 0.0,
        "entry_underlying": 100.0,
        "entry_premium": 100.0,
        "sl_underlying": 95.0,
        "sl_premium": 95.0,
        "target_underlying": 101.0,
        "target_premium": 101.0,
        "lots": 1,
        "status": "OPEN",
        "reason": "test",
        "digest_id": "digest-test",
        "trade_status": "TRIGGERED_CORE",
        "setup_type": "TREND_CONTINUATION",
        "decision_reason": "test",
        "signal_key": f"test-live-{datetime.now(timezone.utc).timestamp()}",
        "broker_order_id": "entry-1",
        "broker_status": "COMPLETE",
        "exit_mode": "GTT",
    }
    trade.update(overrides)
    return insert_live_trade(trade)


def _runtime_config(shadow=False):
    return {
        "live_shadow_mode": shadow,
        "live_enabled_broker_symbols": ["NIFTY", "BANKNIFTY"],
        "live_max_concurrent_positions": 5,
        "live_symbol_lots": {"NIFTY": 1},
    }


def test_kill_switch_still_allows_existing_fut_target_exit():
    _reset_live_tables()
    trade_id = _insert_open_live_trade()
    fake_kite = MagicMock()

    with patch("src.engine.live_trading._is_market_open", return_value=True), \
         patch("src.engine.live_trading.load_runtime_config", return_value=_runtime_config(shadow=False)), \
         patch("src.engine.live_trading.get_kite_client", return_value=fake_kite), \
         patch("src.engine.live_trading.get_broker_config", return_value={"kill_switch_active": 1}), \
         patch("src.engine.live_trading.resolve_instrument", return_value={"tradingsymbol": "NIFTY26JUNFUT", "instrument_token": 1, "lot_size": 25}), \
         patch("src.engine.live_trading.place_kite_order", return_value="exit-1") as mock_order:
        from src.engine.live_trading import run_live_trading

        result = run_live_trading(
            "NIFTY",
            {"underlying": 102.0, "expiry": "2026-06-25", "option_rows": []},
            "digest",
            {"telegram_text": "*Verdict: Long Buildup*\nConfidence: 80%"},
        )

    assert result["action"] == "CLOSED"
    assert result["reason"] == "target hit"
    mock_order.assert_called_once()

    from src.models.schema import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT status, exit_mode FROM live_trades WHERE id=?", (trade_id,)).fetchone()
    assert row["status"] == "CLOSED_TARGET"
    assert row["exit_mode"] == "GTT"


def test_live_entry_reserves_signal_before_broker_order_and_blocks_duplicate_order():
    _reset_live_tables()
    fake_kite = MagicMock()
    plan = {
        "verdict_label": "Long Buildup",
        "side": "BUY",
        "option_type": "CE",
        "strike": 22000.0,
        "entry_underlying": 22000.0,
        "entry_premium": 100.0,
        "sl_underlying": 21900.0,
        "sl_premium": 70.0,
        "target_underlying": 22200.0,
        "target_premium": 150.0,
    }

    patches = [
        patch("src.engine.live_trading._is_market_open", return_value=True),
        patch("src.engine.live_trading.load_runtime_config", return_value=_runtime_config(shadow=False)),
        patch("src.engine.live_trading.get_kite_client", return_value=fake_kite),
        patch("src.engine.live_trading.get_broker_config", return_value={"kill_switch_active": 0}),
        patch("src.engine.live_trading.make_trade_decision", return_value={
            "status": "TRIGGERED_CORE",
            "setup_type": "TREND_CONTINUATION",
            "reason": "test",
            "scores": {},
        }),
        patch("src.engine.live_trading._trade_plan_from_verdict", return_value=plan),
        patch("src.engine.live_trading.resolve_instrument", return_value={"tradingsymbol": "NIFTY26JUN22000CE", "instrument_token": 1, "lot_size": 25}),
        patch("src.engine.live_trading.place_kite_order", return_value="entry-1"),
        patch("src.engine.live_trading.place_kite_gtt", return_value="gtt-1"),
    ]

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as mock_order, patches[8]:
        from src.engine.live_trading import run_live_trading

        result1 = run_live_trading(
            "NIFTY",
            {"underlying": 22000.0, "expiry": "2026-06-25", "option_rows": []},
            "digest",
            {"telegram_text": "*Verdict: Long Buildup*\nConfidence: 80%"},
        )
        result2 = run_live_trading(
            "NIFTY",
            {"underlying": 22000.0, "expiry": "2026-06-25", "option_rows": []},
            "digest",
            {"telegram_text": "*Verdict: Long Buildup*\nConfidence: 80%"},
        )

    assert result1["action"] == "EXECUTED"
    assert result2["action"] == "HELD"
    mock_order.assert_called_once()


def test_live_entry_order_failure_is_recorded_not_untracked():
    _reset_live_tables()
    fake_kite = MagicMock()
    plan = {
        "verdict_label": "Long Buildup",
        "side": "BUY",
        "option_type": "CE",
        "strike": 22000.0,
        "entry_underlying": 22000.0,
        "entry_premium": 100.0,
        "sl_underlying": 21900.0,
        "sl_premium": 70.0,
        "target_underlying": 22200.0,
        "target_premium": 150.0,
    }

    with patch("src.engine.live_trading._is_market_open", return_value=True), \
         patch("src.engine.live_trading.load_runtime_config", return_value=_runtime_config(shadow=False)), \
         patch("src.engine.live_trading.get_kite_client", return_value=fake_kite), \
         patch("src.engine.live_trading.get_broker_config", return_value={"kill_switch_active": 0}), \
         patch("src.engine.live_trading.make_trade_decision", return_value={
             "status": "TRIGGERED_CORE",
             "setup_type": "TREND_CONTINUATION",
             "reason": "test",
             "scores": {},
         }), \
         patch("src.engine.live_trading._trade_plan_from_verdict", return_value=plan), \
         patch("src.engine.live_trading.resolve_instrument", return_value={"tradingsymbol": "NIFTY26JUN22000CE", "instrument_token": 1, "lot_size": 25}), \
         patch("src.engine.live_trading.place_kite_order", side_effect=Exception("broker down")):
        from src.engine.live_trading import run_live_trading

        result = run_live_trading(
            "NIFTY",
            {"underlying": 22000.0, "expiry": "2026-06-25", "option_rows": []},
            "digest",
            {"telegram_text": "*Verdict: Long Buildup*\nConfidence: 80%"},
        )

    assert result["action"] == "BLOCKED_ORDER_FAILED"
    from src.models.schema import get_conn
    with get_conn() as conn:
        rows = conn.execute("SELECT status, broker_status, reason FROM live_trades").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "REJECTED"
    assert rows[0]["broker_status"] == "REJECTED"
    assert "broker down" in rows[0]["reason"]


def test_live_entry_blocks_fallback_symbol_before_broker_order():
    _reset_live_tables()
    fake_kite = MagicMock()
    plan = {
        "verdict_label": "Long Buildup",
        "side": "BUY",
        "option_type": "CE",
        "strike": 58000.0,
        "entry_underlying": 58000.0,
        "entry_premium": 100.0,
        "sl_underlying": 57900.0,
        "sl_premium": 70.0,
        "target_underlying": 58200.0,
        "target_premium": 150.0,
    }

    with patch("src.engine.live_trading._is_market_open", return_value=True), \
         patch("src.engine.live_trading.load_runtime_config", return_value=_runtime_config(shadow=False)), \
         patch("src.engine.live_trading.get_kite_client", return_value=fake_kite), \
         patch("src.engine.live_trading.get_broker_config", return_value={"kill_switch_active": 0}), \
         patch("src.engine.live_trading.make_trade_decision", return_value={
             "status": "TRIGGERED_CORE",
             "setup_type": "TREND_CONTINUATION",
             "reason": "test",
             "scores": {},
         }), \
         patch("src.engine.live_trading._trade_plan_from_verdict", return_value=plan), \
         patch("src.engine.live_trading.resolve_instrument", return_value={
             "tradingsymbol": "BANKNIFTY26JUN58000CE",
             "instrument_token": None,
             "lot_size": None,
         }), \
         patch("src.engine.live_trading.place_kite_order") as mock_order:
        from src.engine.live_trading import run_live_trading

        result = run_live_trading(
            "BANKNIFTY",
            {"underlying": 58000.0, "expiry": "2026-06-30", "option_rows": []},
            "digest",
            {"telegram_text": "*Verdict: Long Buildup*\nConfidence: 80%"},
        )

    assert result["action"] == "BLOCKED_SYMBOL"
    assert "refusing live broker order" in result["reason"]
    mock_order.assert_not_called()

    from src.models.schema import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT status, broker_status, reason FROM live_trades").fetchone()
    assert row["status"] == "REJECTED"
    assert row["broker_status"] == "REJECTED"
    assert "fallback tradingsymbol" in row["reason"]
