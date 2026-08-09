"""
Tests for multi-leg paper trading (Phase 4).
"""
import pytest
from unittest.mock import patch, MagicMock


class TestMultilegPaperTrading:
    """Tests for run_multileg_paper_strategy()."""

    def test_import(self):
        from src.engine.multileg_paper_trading import run_multileg_paper_strategy
        assert callable(run_multileg_paper_strategy)

    @patch("src.engine.paper_trading._is_market_open", return_value=False)
    def test_skips_when_market_closed(self, mock_market):
        from src.engine.multileg_paper_trading import run_multileg_paper_strategy
        result = run_multileg_paper_strategy("NIFTY", {}, "test_digest", {})
        assert result is None or (
            isinstance(result, dict) and result.get("action") == "SKIPPED_MARKET_CLOSED"
        )

    @patch("src.engine.paper_trading._is_market_open", return_value=True)
    @patch("src.engine.strategy_registry.get_ai_mode", return_value="advisory")
    @patch("src.models.schema.get_open_books_for_symbol", return_value=[])
    @patch("src.engine.llm_enrichment.get_multileg_verdict", return_value=None)
    def test_returns_none_when_llm_fails(self, mock_verdict, mock_books, mock_ai, mock_market):
        from src.engine.multileg_paper_trading import run_multileg_paper_strategy
        ctx = {"underlying": 24500, "expiry": "2026-08-14", "option_rows": []}
        result = run_multileg_paper_strategy("NIFTY", ctx, "test_digest", {"verdict_label": "Sideways", "confidence": 65})
        assert result is None


class TestMultilegEntryQuality:
    """Tests for calculate_multileg_entry_quality()."""

    def test_basic_scoring(self):
        from src.engine.entry_quality import calculate_multileg_entry_quality
        legs = [{"strike": 24800, "option_type": "CE"}, {"strike": 24200, "option_type": "PE"}]
        greeks = {"net_delta": 0.04, "net_theta": -12.5, "net_vega": -45.0}
        risk = {"max_profit": 125, "max_loss": 5000, "breakeven_upper": 24675, "breakeven_lower": 24325}
        ctx = {
            "underlying": 24500,
            "option_rows": [
                {"strike": 24500, "option_type": "CE", "iv": 22, "ltp": 150},
                {"strike": 24500, "option_type": "PE", "iv": 20, "ltp": 120},
            ],
            "net_premium": 125,
            "margin_req": 50000,
        }
        score, reasons = calculate_multileg_entry_quality("NIFTY", "SHORT_STRANGLE", legs, greeks, risk, ctx)
        assert 0 <= score <= 100
        assert len(reasons) > 0

    def test_zero_underlying_returns_zero(self):
        from src.engine.entry_quality import calculate_multileg_entry_quality
        score, reasons = calculate_multileg_entry_quality("NIFTY", "SHORT_STRANGLE", [], {}, {}, {"underlying": 0})
        assert score == 0
