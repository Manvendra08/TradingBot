"""
Tests for multi-leg risk engine integration (Phase 6).
"""
import pytest
from unittest.mock import patch, MagicMock


class TestMultilegRiskChecks:
    """Tests for multi-leg book risk checks in risk_engine."""

    def test_config_constants_importable(self):
        from config.multileg_strategies import (
            MAX_BOOK_MARGIN, MAX_NET_DELTA, MAX_LEGS_PER_BOOK,
            STRATEGY_CONSTRAINTS, CONFLICTING_STRATEGIES, ALLOWED_SYMBOLS,
        )
        assert MAX_BOOK_MARGIN == 500_000.0
        assert MAX_NET_DELTA == 0.60
        assert MAX_LEGS_PER_BOOK == 6
        assert "IRON_CONDOR" in STRATEGY_CONSTRAINTS
        assert "NIFTY" in ALLOWED_SYMBOLS

    def test_strategy_constraints_structure(self):
        from config.multileg_strategies import STRATEGY_CONSTRAINTS
        for strat, constraints in STRATEGY_CONSTRAINTS.items():
            assert "min_legs" in constraints
            assert "max_legs" in constraints
            assert "all_sell" in constraints
            assert constraints["all_sell"] is True
            assert constraints["min_legs"] <= constraints["max_legs"]

    def test_conflicting_strategies(self):
        from config.multileg_strategies import CONFLICTING_STRATEGIES
        assert "BULL_PUT_SPREAD" in CONFLICTING_STRATEGIES
        assert "BEAR_CALL_SPREAD" in CONFLICTING_STRATEGIES["BULL_PUT_SPREAD"]["conflicts_with"]
