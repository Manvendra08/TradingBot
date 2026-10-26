"""Regression tests for Phase 2 Entry Correctness fixes.

Covers:
- A1: MCX FUT fallback side based on verdict (bullish->BUY, bearish->SELL, neutral->None).
- A2: PE buildup alerts classified with correct sign (put buying is bearish, put writing is bullish).
- A3: AI veto derived in full mode from NO_TRADE or HIGH risk rating.
- A11: Neutral verdicts enforce the same confidence floor (70% NSE, 72% MCX) as directional verdicts.
- A13: NG momentum keywords direction (Long Unwinding -> SELL, Short Covering -> BUY).
- A14: Decision engine failure or missing trade decision fails closed (BLOCKED_DECISION).
- A15 & A16: Sizing uses notional margin (12% of underlying) and returns 0 when capital is insufficient.
"""
import pytest
from unittest.mock import MagicMock, patch


def test_a1_mcx_fut_direction_matches_verdict():
    """A1 FIX: When MCX options are illiquid, FUT fallback direction must match market verdict."""
    from src.engine.paper_plan import create_paper_trade_plan

    # Mock mcx_option_liquidity_ok to False to trigger FUT fallback
    with patch("src.engine.paper_plan.mcx_option_liquidity_ok", return_value=False):
        # 1. Bullish verdict -> BUY FUT
        ctx_bull = {
            "symbol": "NATURALGAS",
            "underlying": 270.0,
            "atm_strike": 270.0,
            "expiry": "2026-10-27",
            "option_rows": [],
            "verdict_label": "Long Buildup",
            "confidence": 80,
        }
        plan_bull = create_paper_trade_plan("Long Buildup", 80, ctx_bull, symbol="NATURALGAS")
        assert plan_bull is not None
        assert plan_bull["option_type"] == "FUT"
        assert plan_bull["side"] == "BUY"

        # 2. Bearish verdict -> SELL FUT
        ctx_bear = {
            "symbol": "NATURALGAS",
            "underlying": 270.0,
            "atm_strike": 270.0,
            "expiry": "2026-10-27",
            "option_rows": [],
            "verdict_label": "Short Buildup",
            "confidence": 80,
        }
        plan_bear = create_paper_trade_plan("Short Buildup", 80, ctx_bear, symbol="NATURALGAS")
        assert plan_bear is not None
        assert plan_bear["option_type"] == "FUT"
        assert plan_bear["side"] == "SELL"

        # 3. Neutral verdict -> None (cannot execute neutral/strangle in single futures contract)
        ctx_neut = {
            "symbol": "NATURALGAS",
            "underlying": 270.0,
            "atm_strike": 270.0,
            "expiry": "2026-10-27",
            "option_rows": [],
            "verdict_label": "Sideways",
            "confidence": 80,
        }
        plan_neut = create_paper_trade_plan("Sideways", 80, ctx_neut, symbol="NATURALGAS")
        assert plan_neut is None


def test_a2_classify_buildup_bias_pe_vs_ce():
    """A2 FIX: Long Buildup on PE is Put Buying (Bearish), Short Buildup on PE is Put Writing (Bullish)."""
    from src.engine.intelligence import classify_buildup_bias

    # Call options
    assert classify_buildup_bias("Long Buildup", "CE") == "BULLISH"
    assert classify_buildup_bias("Short Covering", "CE") == "BULLISH"
    assert classify_buildup_bias("Short Buildup", "CE") == "BEARISH"
    assert classify_buildup_bias("Long Unwinding", "CE") == "BEARISH"

    # Put options (inverted direction)
    assert classify_buildup_bias("Long Buildup", "PE") == "BEARISH"
    assert classify_buildup_bias("Short Covering", "PE") == "BEARISH"
    assert classify_buildup_bias("Short Buildup", "PE") == "BULLISH"
    assert classify_buildup_bias("Long Unwinding", "PE") == "BULLISH"


def test_a3_ai_veto_derived_in_full_mode():
    """A3 FIX: In full mode, AI recommendation NO_TRADE or HIGH risk rating derives a veto."""
    from src.engine.decision_pipeline import step_ai_alignment, PipelineContext

    # Mock runtime config to full mode
    with patch("config.runtime_config.load_runtime_config", return_value={"live_ai_decision_mode": "full"}):
        # 1. AI recommends NO_TRADE -> VETO
        ctx_no_trade = PipelineContext(
            engine="CORE_OI",
            symbol="NIFTY",
            direction="LONG",
            underlying=24500.0,
            scan_context={"intel": {"confidence": 80}},
            ai_verdict={"recommended_action": "NO_TRADE", "confidence": 40, "risk_rating": "MEDIUM", "bias": "BEARISH"},
            steps=[],
        )
        res1 = step_ai_alignment(ctx_no_trade)
        assert res1.passed is False
        assert "AI VETO" in res1.reason

        # 2. AI flags HIGH risk -> VETO
        ctx_high_risk = PipelineContext(
            engine="CORE_OI",
            symbol="NIFTY",
            direction="LONG",
            underlying=24500.0,
            scan_context={"intel": {"confidence": 80}},
            ai_verdict={"recommended_action": "GO_LONG", "confidence": 75, "risk_rating": "HIGH", "bias": "BULLISH"},
            steps=[],
        )
        res2 = step_ai_alignment(ctx_high_risk)
        assert res2.passed is False
        assert "AI VETO" in res2.reason


def test_a11_neutral_verdict_confidence_floor():
    """A11 FIX: Neutral verdicts must enforce confidence floor (70% NSE)."""
    from src.engine.decision_pipeline import step_signal_core_oi, PipelineContext

    # 1. Below floor (50% < 70%) -> Blocked
    ctx_low = PipelineContext(
        engine="CORE_OI",
        symbol="NIFTY",
        direction=None,
        underlying=24000.0,
        scan_context={"intel": {"verdict_label": "Sideways", "confidence": 50}},
        ai_verdict=None,
        steps=[],
    )
    res_low = step_signal_core_oi(ctx_low)
    assert res_low.passed is False
    assert "below minimum 70%" in res_low.reason

    # 2. Above floor (75% >= 70%) -> Passed
    ctx_high = PipelineContext(
        engine="CORE_OI",
        symbol="NIFTY",
        direction=None,
        underlying=24000.0,
        scan_context={"intel": {"verdict_label": "Sideways", "confidence": 75}},
        ai_verdict=None,
        steps=[],
    )
    res_high = step_signal_core_oi(ctx_high)
    assert res_high.passed is True
    assert ctx_high.direction == "NEUTRAL"


def test_a13_ng_momentum_keyword_direction():
    """A13 FIX: 'LONG UNWINDING' must map to SELL, 'SHORT COVERING' must map to BUY."""
    from src.engine.strategy_registry import get_runner

    runner = get_runner("NG_MOMENTUM")
    assert runner is not None

    with patch("src.engine.ng_momentum_strategy.check_ng_momentum_entry") as mock_check, \
         patch("src.engine.paper_trading.run_paper_trading", return_value={"action": "ENTERED"}):
        
        mock_check.return_value = (True, "OK")
        
        # 1. Long Unwinding -> must evaluate SELL side
        runner("NATURALGAS", {}, "dig1", {"verdict_label": "Long Unwinding"})
        assert mock_check.call_args[0][0] == "SELL"

        # 2. Short Covering -> must evaluate BUY side
        runner("NATURALGAS", {}, "dig2", {"verdict_label": "Short Covering"})
        assert mock_check.call_args[0][0] == "BUY"


def test_a14_fail_closed_on_missing_trade_decision():
    """A14 FIX: When trade_decision is missing or None, trade must NOT open."""
    from src.engine.paper_trading import run_paper_trading

    # Intel with None trade_decision
    intel = {
        "verdict_label": "Long Buildup",
        "confidence": 85,
        "trade_decision": None,
    }
    with patch("src.engine.paper_trading._is_market_open", return_value=True), \
         patch("src.engine.trade_decision.make_trade_decision", return_value=None):
        res = run_paper_trading("NIFTY", {"underlying": 24500.0, "expiry": "2026-10-29"}, "dig", intel)
        assert res.get("action") == "BLOCKED_DECISION"


def test_a15_a16_sizing_notional_margin():
    """A15 & A16 FIX: SELL options margin is based on 12% of underlying notional, not 12x premium."""
    from src.engine.capital_allocator import _calculate_live_lots, estimate_short_option_margin

    # 1. Notional margin helper
    # NIFTY spot 24,000, lot size 25: Notional = 600,000. 12% margin = 72,000.
    margin = estimate_short_option_margin(underlying=24000.0, lot_size=25, lots=1, premium=10.0)
    assert 70000.0 <= margin <= 75000.0

    # 2. Sizing with insufficient capital returns 0, does not force 1
    config = {
        "live_capital_per_trade_inr": 20000.0,  # ₹20k is not enough for ₹72k margin requirement
        "underlying": 24000.0,
    }
    lots = _calculate_live_lots(
        symbol="NIFTY",
        entry_premium=10.0,
        side="SELL",
        config=config,
        strike=24000.0,
    )
    assert lots == 0
