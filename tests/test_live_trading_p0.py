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
        patch("src.engine.live_trading.confirm_order_fill", return_value=("COMPLETE", "Filled")),
    ]

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as mock_order, patches[8], patches[9]:
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


def test_place_kite_order_pricing_rules():
    from src.engine.live_trading import place_kite_order
    fake_kite = MagicMock()

    # 1. Futures (transaction_type="BUY", tick_size=0.05, 0.2% buffer)
    # expected limit_price: ltp = 300.0, price = 300.0 * 1.002 = 300.60
    fake_kite.ltp.return_value = {"MCX:NATURALGAS26JUNFUT": {"last_price": 300.0}}
    place_kite_order(fake_kite, "NATURALGAS", "MCX", "NATURALGAS26JUNFUT", "BUY", 1250, shadow_mode=False, tick_size=0.05)
    fake_kite.place_order.assert_called_with(
        variety=fake_kite.VARIETY_REGULAR,
        exchange="MCX",
        tradingsymbol="NATURALGAS26JUNFUT",
        transaction_type="BUY",
        quantity=1250,
        product=fake_kite.PRODUCT_MIS,
        order_type=fake_kite.ORDER_TYPE_LIMIT,
        price=300.60
    )

    fake_kite.place_order.reset_mock()

    # 2. Options (transaction_type="BUY", tick_size=0.05, 5% buffer, check tick rounding)
    # expected limit_price: ltp = 496.91 * 1.05 = 521.7555, rounded to 0.05 multiple = 521.75
    fake_kite.ltp.return_value = {"NFO:BANKNIFTY26JUN58000CE": {"last_price": 496.91}}
    place_kite_order(fake_kite, "BANKNIFTY", "NFO", "BANKNIFTY26JUN58000CE", "BUY", 30, shadow_mode=False, tick_size=0.05)
    fake_kite.place_order.assert_called_with(
        variety=fake_kite.VARIETY_REGULAR,
        exchange="NFO",
        tradingsymbol="BANKNIFTY26JUN58000CE",
        transaction_type="BUY",
        quantity=30,
        product=fake_kite.PRODUCT_MIS,
        order_type=fake_kite.ORDER_TYPE_LIMIT,
        price=521.75
    )


def test_llm_caching_and_cooldown():
    from src.engine import llm_enrichment
    from src.engine.llm_enrichment import get_llm_verdict, LLMTradeVerdict
    import os

    # Enable API client path in tests
    llm_enrichment.genai = MagicMock()
    os.environ["GEMINI_API_KEY"] = "fake-api-key"

    # Reset caches and state
    llm_enrichment._VERDICT_CACHE = {}
    llm_enrichment._API_QUOTA_EXHAUSTED_UNTIL = 0.0

    dummy_verdict = LLMTradeVerdict(
        action="GO_LONG",
        confidence=80,
        instrument="NIFTY 24500 CE 27Jun",
        entry_trigger="Underlying breaks above 24520",
        entry_premium_range="180-195",
        stop_loss="Premium 140",
        target_1="Premium 230",
        target_2="Premium 280",
        risk_reward="1:1.8",
        thesis="Short covering at support",
        invalidation="Below 24400 on 1H",
        risk_rating="LOW",
        catalyst="EIA report Thursday"
    )

    with patch("src.engine.llm_enrichment._call_llm_api", return_value=dummy_verdict) as mock_api:
        intel = {"verdict_label": "Long Buildup", "confidence": 80}
        scan_ctx = {"underlying": 24000.0}

        # 1. First call -> should query API
        v1 = get_llm_verdict("NIFTY", intel, scan_ctx)
        assert v1 == dummy_verdict
        assert mock_api.call_count == 1

        # 2. Second call with same parameters and minor price change -> should reuse cache
        scan_ctx_minor = {"underlying": 24010.0}  # 10 / 24000 = 0.04% move (< 0.2%)
        v2 = get_llm_verdict("NIFTY", intel, scan_ctx_minor)
        assert v2 == dummy_verdict
        assert mock_api.call_count == 1  # call count still 1

        # 3. Third call with significant price change -> should bypass cache and query API
        scan_ctx_major = {"underlying": 24100.0}  # 100 / 24000 = 0.41% move (> 0.2%)
        v3 = get_llm_verdict("NIFTY", intel, scan_ctx_major)
        assert v3 == dummy_verdict
        assert mock_api.call_count == 2

        # 4. Fourth call triggering a trade -> should bypass cache
        trade_decision = {"status": "TRIGGERED_CORE"}
        v4 = get_llm_verdict("NIFTY", intel, scan_ctx_major, trade_decision=trade_decision)
        assert v4 == dummy_verdict
        assert mock_api.call_count == 3


def test_llm_alternative_fallbacks():
    from src.engine import llm_enrichment
    from src.engine.llm_enrichment import _call_llm_api, LLMTradeVerdict
    import os

    # Mock requests.post
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"action": "GO_SHORT", "confidence": 75, "instrument": "NIFTY 24000 PE 27Jun", "entry_trigger": "Underlying below 24000", "entry_premium_range": "180-195", "stop_loss": "Premium 140", "target_1": "Premium 230", "target_2": "Premium 280", "risk_reward": "1:1.8", "thesis": "Short", "invalidation": "Invalid", "risk_rating": "LOW", "catalyst": "None"}'
            }
        }]
    }

    # Enable alternative provider keys
    os.environ["GROQ_API_KEY"] = "fake-groq-key"
    old_openrouter_key = os.environ.pop("OPENROUTER_API_KEY", None)
    old_opencode_key = os.environ.pop("OPENCODE_API_KEY", None)
    old_gemini_key = os.environ.pop("GEMINI_API_KEY", None)

    try:
        with patch("requests.Session.post", return_value=mock_resp) as mock_post:
            # Call it with no Gemini/OpenRouter/OpenCode key -> should go straight to alternative (Groq)
            result = _call_llm_api("NIFTY", "dummy prompt", LLMTradeVerdict)
            assert result is not None
            assert result.action == "GO_SHORT"
            assert result.confidence == 75
            assert mock_post.call_count >= 1
    finally:
        # Restore keys
        if old_openrouter_key:
            os.environ["OPENROUTER_API_KEY"] = old_openrouter_key
        if old_opencode_key:
            os.environ["OPENCODE_API_KEY"] = old_opencode_key
        if old_gemini_key:
            os.environ["GEMINI_API_KEY"] = old_gemini_key



