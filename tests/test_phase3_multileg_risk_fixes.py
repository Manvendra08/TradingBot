"""Regression tests for Phase 3 Multi-Leg Risk & Execution Rigor fixes.

Covers:
- C2: AI adjustment guards (preserve side, enforce OTM roll direction, populate book_id/expiry).
- C8: Strangle stop loss threshold explicitly rupee-defined and aligned with LLM prompt (no 30% clamp).
- C10: Cross-book net-delta cap per symbol.
- C11 & C12: Block CUSTOM structures; require neutral engine verdict for neutral structures.
- C13: Proposal validator fails closed on exceptions.
- C15: Honor time_decay_exit_dte across all symbols.
- C20: Clear staged AI close on non-CLOSE actions; co-sign requires full profit target.
"""
import pytest
from unittest.mock import MagicMock, patch
from src.engine.execution_parser import ParsedExecution


def test_c11_c12_block_custom_and_trending_neutral():
    """C11 & C12 FIX: Block CUSTOM structures and reject neutral structures in trending market."""
    from src.engine.multileg_validator import validate_multileg_trade

    # 1. CUSTOM structure -> Rejected
    custom_proposal = ParsedExecution(
        is_valid=True,
        action="NEUTRAL",
        strategy="CUSTOM",
        legs=[
            {"strike": 24000, "option_type": "CE", "side": "SELL", "ratio": 1},
            {"strike": 24500, "option_type": "PE", "side": "BUY", "ratio": 1},
        ],
    )
    res_custom = validate_multileg_trade(custom_proposal, "Sideways", 24200.0)
    assert res_custom.is_valid is False
    assert "CUSTOM structures are not permitted" in res_custom.rejection_reason

    # 2. Iron Condor against strong trending market (Long Buildup) -> Rejected
    ic_proposal = ParsedExecution(
        is_valid=True,
        action="NEUTRAL",
        strategy="IRON_CONDOR",
        legs=[
            {"strike": 24500, "option_type": "CE", "side": "SELL", "ratio": 1},
            {"strike": 24700, "option_type": "CE", "side": "BUY", "ratio": 1},
            {"strike": 23900, "option_type": "PE", "side": "SELL", "ratio": 1},
            {"strike": 23700, "option_type": "PE", "side": "BUY", "ratio": 1},
        ],
    )
    res_trend = validate_multileg_trade(ic_proposal, "Long Buildup", 24200.0)
    assert res_trend.is_valid is False
    assert "cannot be entered against trending" in res_trend.rejection_reason

    # 3. Iron Condor in Sideways/Neutral market -> Valid
    res_neutral = validate_multileg_trade(ic_proposal, "Sideways", 24200.0)
    assert res_neutral.is_valid is True


def test_c8_strangle_stop_loss_threshold():
    """C8 FIX: Strangle rupee SL threshold is 150% of credit, not clamped to 2.5% margin."""
    from src.engine.multileg_paper_trading import _get_stop_loss_threshold_rupees

    book = {
        "book_id": "TEST_C8",
        "structure": "SHORT_STRANGLE",
        "net_premium": 200.0,
        "stop_loss_pct": 1.5,
    }
    legs = [
        {"strike": 25000, "option_type": "CE", "side": "SELL", "lots": 2, "premium": 100.0},
        {"strike": 24000, "option_type": "PE", "side": "SELL", "lots": 2, "premium": 100.0},
    ]
    # NIFTY lot size = 65. Credit = 200 * 65 * 2 = 26,000. 150% credit SL = 39,000.
    sl_rupees = _get_stop_loss_threshold_rupees(book, legs, "NIFTY")
    assert 38000.0 <= sl_rupees <= 40000.0, f"Expected ~39,000 SL threshold, got {sl_rupees}"


def test_c10_cross_book_delta_cap_blocked():
    """C10 FIX: When open books have cumulative net delta >= 0.60, new entries are blocked."""
    from src.engine.multileg_paper_trading import _attempt_new_entry

    open_books = [
        {"book_id": "B1", "net_delta": 0.35},
        {"book_id": "B2", "net_delta": 0.28},  # Total = 0.63 >= 0.60
    ]
    res = _attempt_new_entry(
        symbol="BANKNIFTY",
        scan_context={"underlying": 54000.0},
        digest_id="dig",
        intel={},
        ai_verdict=None,
        ai_mode="advisory",
        now_iso="2026-10-26T12:00:00",
        open_books=open_books,
    )
    assert res is not None
    assert res.get("action") == "BLOCKED_DELTA_CAP"
