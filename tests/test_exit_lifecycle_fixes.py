"""Regression tests for the 2026-10-22 exit-lifecycle fixes.

Covers:
- close_live_trade must close a row that the atomic exit claim moved to
  EXIT_PENDING (previously it filtered status='OPEN' and silently no-op'd,
  leaving every live exit stuck in EXIT_PENDING with pnl=0).
- recover_stale_exit_pending reclaims legacy/timed-out claims.
- GeminiAI FUT paper trades now get a local SL/target safety net.
"""
from datetime import datetime, timedelta, timezone

import pytest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_live(status: str, claimed_at: str | None, **overrides) -> int:
    from src.models.schema import insert_live_trade

    trade = {
        "opened_at": _now(),
        "symbol": "NATURALGAS",
        "expiry": "2026-10-27",
        "verdict_label": "Trend",
        "side": "SELL",
        "option_type": "CE",
        "strike": 300.0,
        "entry_underlying": 274.5,
        "entry_premium": 1.1,
        "sl_underlying": 280.0,
        "sl_premium": 1.32,
        "target_underlying": 260.0,
        "target_premium": 0.72,
        "lots": 1,
        "status": status,
        "reason": "test",
        "digest_id": f"digest-{_now()}",
        "trade_status": "TRIGGERED_CORE",
        "setup_type": "TREND_CONTINUATION",
        "decision_reason": "test",
        "signal_key": f"test-{_now()}",
        "broker_order_id": "sh-ord-test",
        "broker_status": "SHADOW",
        "exit_mode": "GTT",
        "exit_claimed_at": claimed_at,
    }
    trade.update(overrides)
    return insert_live_trade(trade)


def _get_live(trade_id: int) -> dict:
    from src.models.schema import get_conn

    with get_conn(read_only=True) as conn:
        return dict(
            conn.execute("SELECT * FROM live_trades WHERE id=?", (trade_id,)).fetchone()
        )


def test_close_live_trade_accepts_exit_pending_claim():
    """An exit that wins the atomic claim must still be recorded closed."""
    from src.models.schema import close_live_trade

    trade_id = _insert_live("EXIT_PENDING", _now())
    close_live_trade(
        trade_id,
        _now(),
        exit_underlying=265.0,
        exit_premium=0.8,
        status="CLOSED_TARGET",
        reason="cmp poll exit",
    )

    trade = _get_live(trade_id)
    assert trade["status"] == "CLOSED_TARGET"
    assert trade["closed_at"] is not None
    assert trade["pnl_rupees"] != 0.0
    assert trade["exit_claimed_at"] is None


def test_recover_stale_exit_pending():
    from src.models.schema import recover_stale_exit_pending

    from src.models.schema import get_conn

    legacy = _insert_live("EXIT_PENDING", None)
    stale = _insert_live(
        "EXIT_PENDING",
        (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
    )
    fresh = _insert_live("EXIT_PENDING", _now())

    # insert_live_trade() has a fixed column list, so set claim stamps directly.
    with get_conn() as conn:
        conn.execute(
            "UPDATE live_trades SET exit_claimed_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(), stale),
        )
        conn.execute(
            "UPDATE live_trades SET exit_claimed_at=? WHERE id=?",
            (_now(), fresh),
        )

    recover_stale_exit_pending(max_age_minutes=10)
    assert _get_live(legacy)["status"] == "OPEN"
    assert _get_live(stale)["status"] == "OPEN"
    assert _get_live(fresh)["status"] == "EXIT_PENDING"

    recover_stale_exit_pending(max_age_minutes=0)
    assert _get_live(fresh)["status"] == "OPEN"


def test_gemini_fut_target_breach_closes_paper_trade():
    """FUT GeminiAI trades were excluded from monitoring, so breached targets
    never closed. The local safety net must close them."""
    from src.engine.paper_trading import monitor_paper_trades
    from src.models.schema import get_conn, insert_paper_trade

    trade_id = insert_paper_trade(
        {
            "opened_at": _now(),
            "symbol": "NATURALGAS",
            "expiry": "2026-10-27",
            "verdict_label": "GeminiAI",
            "side": "SELL",
            "option_type": "FUT",
            "strike": 0.0,
            "entry_underlying": 271.2,
            "entry_premium": 271.2,
            "sl_underlying": 274.1,
            "target_underlying": 267.5,
            "lots": 1,
            "status": "OPEN",
            "reason": "GeminiAI Setup: NG_TEST_SELL",
            "digest_id": "gemini_test",
            "signal_key": "NG_TEST_SELL",
            "trade_status": "TRIGGERED_CORE",
            "setup_type": "GeminiAI",
            "decision_reason": "test",
            "regime": "GEMINI_SPARK",
            "lot_size": 1250,
        }
    )

    actions = monitor_paper_trades(
        "NATURALGAS", {"underlying": 261.0, "force_monitor": True}
    )
    assert any(a.get("trade_id") == trade_id for a in actions), actions

    with get_conn(read_only=True) as conn:
        row = conn.execute(
            "SELECT status, exit_reason, pnl_rupees FROM paper_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
    assert row["status"] == "CLOSED_TARGET"
    assert row["pnl_rupees"] > 0


def test_gemini_nifty_fut_excluded_from_spot_monitoring():
    """B1 FIX: NIFTY/BANKNIFTY futures have basis spread to spot.
    The local spot monitor must exclude them to prevent false spot stop-outs."""
    from src.engine.paper_trading import monitor_paper_trades
    from src.models.schema import get_conn, insert_paper_trade

    trade_id = insert_paper_trade(
        {
            "opened_at": _now(),
            "symbol": "NIFTY",
            "expiry": "2026-10-29",
            "verdict_label": "GeminiAI",
            "side": "BUY",
            "option_type": "FUT",
            "strike": 0.0,
            "entry_underlying": 25800.0,  # Futures price
            "entry_premium": 25800.0,
            "sl_underlying": 25700.0,
            "target_underlying": 26000.0,
            "lots": 1,
            "status": "OPEN",
            "reason": "GeminiAI Setup: NIFTY_TEST_BUY",
            "digest_id": "gemini_nifty_test",
            "signal_key": "NIFTY_TEST_BUY",
            "trade_status": "TRIGGERED_CORE",
            "setup_type": "GeminiAI",
            "decision_reason": "test",
            "regime": "GEMINI_SPARK",
            "lot_size": 25,
        }
    )

    # Spot is at 25100 (spot basis ~700 pts lower than futures entry 25800).
    # If the monitor evaluated spot against the futures SL (25700), it would falsely trigger SL.
    actions = monitor_paper_trades(
        "NIFTY", {"underlying": 25100.0, "force_monitor": True}
    )
    assert not any(a.get("trade_id") == trade_id for a in actions), actions

    with get_conn(read_only=True) as conn:
        row = conn.execute(
            "SELECT status FROM paper_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
    assert row["status"] == "OPEN"


def test_dst_aware_mcx_and_eia_times():
    """A18 & B19 FIX: EIA announcement and MCX close times adjust with US DST."""
    import pytz
    from src.engine.time_guards import get_mcx_close_time, get_eia_announcement_time

    IST = pytz.timezone("Asia/Kolkata")
    
    # Summer date (e.g. 2026-10-20, US EDT active)
    summer_dt = IST.localize(datetime(2026, 10, 20, 12, 0, 0))
    assert get_mcx_close_time(summer_dt) == (23, 30)
    assert get_eia_announcement_time(summer_dt) == (20, 0)

    # Winter date (e.g. 2026-11-05, US EST active after Nov 1 DST end)
    winter_dt = IST.localize(datetime(2026, 11, 5, 12, 0, 0))
    assert get_mcx_close_time(winter_dt) == (23, 55)
    assert get_eia_announcement_time(winter_dt) == (21, 0)


def test_c14_single_sided_breakeven_cushion():
    """C14 FIX: Credit spreads have one breakeven None, must not raise TypeError."""
    from src.engine.multileg_strategy import _score_entry_quality

    proposal = {
        "structure": "BULL_PUT_SPREAD",
        "net_credit": 35.0,
        "legs": [
            {"strike": 24000, "option_type": "PE", "side": "SELL", "lots": 1},
            {"strike": 23900, "option_type": "PE", "side": "BUY", "lots": 1},
        ],
    }
    # For a bull put spread, upper breakeven is None, lower breakeven is 23965
    risk_profile = {
        "max_profit": 3500.0,
        "max_loss": 6500.0,
        "breakeven_upper": None,
        "breakeven_lower": 23965.0,
    }
    market_data = {
        "underlying": 24500.0,
        "iv": 14.5,
        "atm_strike": 24500.0,
    }

    # Should succeed without TypeError and compute a positive score
    score, reasons = _score_entry_quality(proposal, market_data, risk_profile)
    assert score > 0
    assert any("breakeven cushion" in r.lower() for r in reasons)




# ── Decision Matrix Exit Priority & AI Authority Tests (Phase 4) ─────────────

class TestExitPriorityAndAIAuthority:
    def test_order_legs_for_exit_sell_first(self):
        """Phase 4: SELL legs MUST be ordered before BUY legs to flatten short liability first."""
        from src.engine.exit_evaluator import order_legs_for_exit

        legs = [
            {"id": 1, "side": "BUY", "strike": 24000, "option_type": "PE"},
            {"id": 2, "side": "SELL", "strike": 24200, "option_type": "PE"},
            {"id": 3, "side": "BUY", "strike": 24800, "option_type": "CE"},
            {"id": 4, "side": "SELL", "strike": 24600, "option_type": "CE"},
        ]
        ordered = order_legs_for_exit(legs)
        assert [l["id"] for l in ordered] == [2, 4, 1, 3]
        assert ordered[0]["side"] == "SELL"
        assert ordered[1]["side"] == "SELL"
        assert ordered[2]["side"] == "BUY"
        assert ordered[3]["side"] == "BUY"

    def test_exit_priority_precedence(self):
        """Phase 4: HARD_RISK takes precedence over TARGET even if target threshold is also reached."""
        from src.engine.exit_evaluator import evaluate_exit_priority

        book = {
            "symbol": "NIFTY",
            "strategy_type": "IRON_CONDOR",
            "net_premium": 50.0,
            "max_loss": 150.0,
            "entry_underlying": 24500.0,
        }
        legs = [
            {"id": 1, "side": "SELL", "strike": 24300, "option_type": "PE", "delta": 0.20, "lots": 1},
            {"id": 2, "side": "SELL", "strike": 24700, "option_type": "CE", "delta": 0.20, "lots": 1},
        ]
        # PnL is deeply negative (-Rs 10,000) breaching stop loss (stop is ~50 pts * 25 = 1250)
        market_state = {
            "underlying": 24500.0,
            "total_pnl": -10000.0,
        }
        dec = evaluate_exit_priority(book, legs, market_state)
        assert dec.should_exit is True
        assert dec.exit_reason == "HARD_RISK"
        assert dec.priority == 1

    def test_tested_side_delta_triggers(self):
        """Phase 4: Defined-risk triggers at delta >= 0.48; Naked triggers at delta >= 0.35."""
        from src.engine.exit_evaluator import evaluate_exit_priority

        # Defined-risk (Iron Condor) with delta = 0.49 on short CE
        book_def = {
            "symbol": "NIFTY",
            "strategy_type": "IRON_CONDOR",
            "net_premium": 50.0,
            "max_loss": 150.0,
            "entry_underlying": 24500.0,
        }
        legs_def = [
            {"id": 1, "side": "SELL", "strike": 24700, "option_type": "CE", "delta": 0.49, "lots": 1},
            {"id": 2, "side": "BUY", "strike": 24900, "option_type": "CE", "delta": 0.25, "lots": 1},
        ]
        dec_def = evaluate_exit_priority(book_def, legs_def, {"underlying": 24650.0, "total_pnl": -500.0})
        assert dec_def.should_exit is True
        assert dec_def.exit_reason == "TESTED_SIDE"
        assert "delta 0.49" in dec_def.details

        # Naked strangle: delta = 0.36 triggers tested side (trigger is 0.35)
        book_naked = {
            "symbol": "NIFTY",
            "strategy_type": "SHORT_STRANGLE",
            "net_premium": 80.0,
            "max_loss": 0.0,
            "entry_underlying": 24500.0,
        }
        legs_naked = [
            {"id": 1, "side": "SELL", "strike": 24700, "option_type": "CE", "delta": 0.36, "lots": 1},
            {"id": 2, "side": "SELL", "strike": 24300, "option_type": "PE", "delta": 0.15, "lots": 1},
        ]
        dec_naked = evaluate_exit_priority(book_naked, legs_naked, {"underlying": 24620.0, "total_pnl": -500.0})
        assert dec_naked.should_exit is True
        assert dec_naked.exit_reason == "TESTED_SIDE"
        assert "0.36" in dec_naked.details

    def test_tested_side_spot_breach(self):
        """Phase 4: Candle close / spot breach past short strike triggers TESTED_SIDE."""
        from src.engine.exit_evaluator import evaluate_exit_priority

        book = {
            "symbol": "NIFTY",
            "strategy_type": "BEAR_CALL_SPREAD",
            "net_premium": 30.0,
            "max_loss": 70.0,
            "entry_underlying": 24500.0,
        }
        legs = [
            {"id": 1, "side": "SELL", "strike": 24600, "option_type": "CE", "delta": 0.40, "lots": 1},
            {"id": 2, "side": "BUY", "strike": 24700, "option_type": "CE", "delta": 0.25, "lots": 1},
        ]
        # Spot at 24610 has breached the short CE strike 24600
        dec = evaluate_exit_priority(book, legs, {"underlying": 24610.0, "total_pnl": -300.0})
        assert dec.should_exit is True
        assert dec.exit_reason == "TESTED_SIDE"
        assert "breached short CE strike 24600" in dec.details

    def test_ai_authority_stop_and_target_tighten_only(self):
        """Phase 4: AI can only tighten stops and targets, never loosen or widen."""
        from src.engine.exit_evaluator import resolve_effective_stop_and_target

        cfg = {
            "ai_authority": {
                "tighten_stop_allowed": True,
                "widen_stop_allowed": False,
                "loosen_target_allowed": False,
            }
        }
        # 1. AI proposes tighter stop (40 < 50): accepted
        sl1, tgt1 = resolve_effective_stop_and_target(50.0, 40.0, ai_sl_pts=40.0, ai_tgt_pts=None, cfg=cfg)
        assert sl1 == 40.0
        assert tgt1 == 40.0

        # 2. AI proposes wider stop (70 > 50): rejected (holds at rule 50)
        sl2, tgt2 = resolve_effective_stop_and_target(50.0, 40.0, ai_sl_pts=70.0, ai_tgt_pts=None, cfg=cfg)
        assert sl2 == 50.0

        # 3. AI proposes tighter target (25 < 40): accepted
        sl3, tgt3 = resolve_effective_stop_and_target(50.0, 40.0, ai_sl_pts=None, ai_tgt_pts=25.0, cfg=cfg)
        assert tgt3 == 25.0

        # 4. AI proposes looser target (60 > 40): rejected (holds at rule 40)
        sl4, tgt4 = resolve_effective_stop_and_target(50.0, 40.0, ai_sl_pts=None, ai_tgt_pts=60.0, cfg=cfg)
        assert tgt4 == 40.0

    def test_ai_early_close_authority_gates(self):
        """Phase 4: AI close allowed only if profit >= 30% credit or loss >= 50% stop."""
        from src.engine.exit_evaluator import evaluate_exit_priority

        book = {
            "symbol": "NIFTY",
            "strategy_type": "IRON_CONDOR",
            "net_premium": 100.0,
            "max_loss": 100.0,
            "entry_underlying": 24500.0,
        }
        legs = [
            {"id": 1, "side": "SELL", "strike": 24300, "option_type": "PE", "delta": 0.20, "lots": 1},
            {"id": 2, "side": "SELL", "strike": 24700, "option_type": "CE", "delta": 0.20, "lots": 1},
        ]
        # Total credit = 100 pts * 25 = Rs 2500. Stop loss = 100 pts * 25 = Rs 2500.
        # Case 1: Trivial profit Rs 200 (8% of credit < 30%): AI close is REJECTED
        dec_premature = evaluate_exit_priority(
            book, legs,
            {"underlying": 24500.0, "total_pnl": 200.0},
            ai_verdict={"action": "CLOSE", "reason": "Feeling unsure"}
        )
        assert dec_premature.should_exit is False

        # Case 2: Profit Rs 800 (32% of credit >= 30%): AI close is AUTHORIZED
        dec_profit_qual = evaluate_exit_priority(
            book, legs,
            {"underlying": 24500.0, "total_pnl": 2000.0},
            ai_verdict={"action": "CLOSE", "reason": "Momentum slowing"}
        )
        assert dec_profit_qual.should_exit is True
        assert dec_profit_qual.exit_reason == "AI_TIGHTEN"

        # Case 3: Loss -Rs 1300 (52% of stop >= 50%): AI close is AUTHORIZED
        dec_loss_qual = evaluate_exit_priority(
            book, legs,
            {"underlying": 24500.0, "total_pnl": -3500.0},
            ai_verdict={"action": "CLOSE", "reason": "Structure deteriorating"}
        )
        assert dec_loss_qual.should_exit is True
        assert dec_loss_qual.exit_reason == "AI_TIGHTEN"

    def test_frozen_parameters_cannot_be_loosened(self):
        """Phase 1/4: Open trade's frozen stop loss cannot be loosened by config edits."""
        from src.engine.multileg_paper_trading import _get_stop_loss_threshold_rupees

        # Book entered with frozen stop_loss_rupees = 2500.0
        book = {
            "symbol": "NIFTY",
            "strategy_type": "IRON_CONDOR",
            "net_premium": 100.0,
            "max_loss": 300.0,
            "entry_underlying": 24500.0,
            "stop_loss_rupees": 2500.0,
        }
        legs = [
            {"id": 1, "side": "SELL", "strike": 24300, "option_type": "PE", "lots": 1},
            {"id": 2, "side": "SELL", "strike": 24700, "option_type": "CE", "lots": 1},
        ]
        # Even if current formula would give higher threshold, frozen cap strictly binds
        threshold = _get_stop_loss_threshold_rupees(book, legs, "NIFTY")
        assert threshold <= 2500.0
