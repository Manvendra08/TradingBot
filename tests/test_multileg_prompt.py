"""
Tests for the multi-leg LLM prompt builder (Phase 2).
"""
import pytest
from src.engine.multileg_llm_prompt import (
    build_multileg_prompt,
    build_multileg_exit_prompt,
    _format_full_option_chain,
    _format_iv_summary,
)


class TestFormatOptionChain:
    """Tests for _format_full_option_chain()."""

    def test_empty_chain(self):
        result = _format_full_option_chain([], 24500, 24500)
        assert "No option chain data" in result

    def test_formats_strikes(self):
        chain = [
            {"strike": 24500, "option_type": "CE", "ltp": 150, "oi": 5000, "volume": 1000, "iv": 18, "bid": 149, "ask": 151, "delta": 0.50},
            {"strike": 24500, "option_type": "PE", "ltp": 120, "oi": 6000, "volume": 1200, "iv": 19, "bid": 119, "ask": 121, "delta": -0.48},
            {"strike": 24800, "option_type": "CE", "ltp": 85, "oi": 3000, "volume": 800, "iv": 20, "bid": 84, "ask": 86, "delta": 0.22},
            {"strike": 24200, "option_type": "PE", "ltp": 40, "oi": 4000, "volume": 900, "iv": 18, "bid": 39, "ask": 41, "delta": -0.18},
        ]
        result = _format_full_option_chain(chain, 24500, 24500)
        assert "24500" in result
        assert "24800" in result
        assert "24200" in result
        assert "ATM" in result


class TestFormatIVSummary:
    """Tests for _format_iv_summary()."""

    def test_empty_chain(self):
        result = _format_iv_summary([], 24500)
        assert "No IV data" in result

    def test_formats_iv(self):
        chain = [
            {"strike": 24500, "option_type": "CE", "iv": 18.5, "ltp": 150},
            {"strike": 24500, "option_type": "PE", "iv": 19.2, "ltp": 120},
            {"strike": 24800, "option_type": "CE", "iv": 20.1, "ltp": 85},
        ]
        result = _format_iv_summary(chain, 24500)
        assert "ATM IV" in result
        assert "18.5" in result or "19.2" in result


class TestBuildMultilegPrompt:
    """Tests for build_multileg_prompt()."""

    def test_basic_prompt(self):
        intel = {"verdict_label": "Sideways", "confidence": 65}
        ctx = {
            "underlying": 24500,
            "atm_strike": 24500,
            "expiry": "2026-08-14",
            "dte": 7,
            "option_rows": [
                {"strike": 24500, "option_type": "CE", "ltp": 150, "oi": 5000, "volume": 1000, "iv": 18, "delta": 0.50},
                {"strike": 24500, "option_type": "PE", "ltp": 120, "oi": 6000, "volume": 1200, "iv": 19, "delta": -0.48},
            ],
            "support": 24200,
            "resistance": 24800,
            "max_pain": 24500,
            "pcr": 1.1,
            "chart_indicators": {"1h": {"ohlc": {"open": 24480, "high": 24520, "low": 24450, "close": 24500}},
                                  "3h": {"ohlc": {"open": 24400, "high": 24550, "low": 24380, "close": 24500}}},
        }
        prompt = build_multileg_prompt("NIFTY", intel, ctx)
        assert "NIFTY" in prompt
        assert "24500" in prompt
        assert "SELL" in prompt
        assert "options seller" in prompt.lower() or "premium" in prompt.lower()


class TestBuildMultilegExitPrompt:
    """Tests for build_multileg_exit_prompt()."""

    def test_basic_exit_prompt(self):
        book = {"book_id": "NIFTY:20260807:SHORT_STRANGLE:1", "strategy_type": "SHORT_STRANGLE",
                "net_premium": 125.0, "total_pnl": 45.0, "adjustment_count": 0}
        legs = [
            {"side": "SELL", "option_type": "CE", "strike": 24800, "entry_premium": 85.0, "lots": 1, "delta": 0.22},
            {"side": "SELL", "option_type": "PE", "strike": 24200, "entry_premium": 40.0, "lots": 1, "delta": -0.18},
        ]
        ctx = {"underlying": 24500, "dte": 5, "option_rows": [
            {"strike": 24800, "option_type": "CE", "ltp": 70},
            {"strike": 24200, "option_type": "PE", "ltp": 35},
        ]}
        intel = {"verdict_label": "Sideways", "confidence": 65}
        prompt = build_multileg_exit_prompt("NIFTY", book, legs, ctx, intel)
        assert "NIFTY" in prompt
        assert "HOLD" in prompt
        assert "ADJUST" in prompt
        assert "CLOSE" in prompt
