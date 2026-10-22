"""
Tests for the multi-leg strategy engine (Phase 3).
"""
import pytest
from unittest.mock import patch, MagicMock


class TestValidateLegs:
    """Tests for validate_legs()."""

    def _make_chain(self, strikes):
        """Build option_chain dict keyed by strike -> {CE: {...}, PE: {...}}."""
        chain = {}
        for s in strikes:
            strike = s["strike"]
            opt = s["option_type"]
            if strike not in chain:
                chain[strike] = {}
            chain[strike][opt] = s
        return chain

    def test_valid_iron_condor(self):
        from src.engine.multileg_strategy import validate_legs
        chain = self._make_chain([
            {"strike": 24800, "option_type": "CE", "ltp": 85},
            {"strike": 24200, "option_type": "PE", "ltp": 40},
            {"strike": 25000, "option_type": "CE", "ltp": 30},
            {"strike": 24000, "option_type": "PE", "ltp": 15},
        ])
        legs = [
            {"side": "SELL", "option_type": "CE", "strike": 24800, "premium": 85},
            {"side": "SELL", "option_type": "PE", "strike": 24200, "premium": 40},
            {"side": "SELL", "option_type": "CE", "strike": 25000, "premium": 30},
            {"side": "SELL", "option_type": "PE", "strike": 24000, "premium": 15},
        ]
        ok, msg = validate_legs("IRON_CONDOR", legs, chain, 24500)
        assert ok is True

    def test_invalid_leg_count(self):
        from src.engine.multileg_strategy import validate_legs
        legs = [
            {"side": "SELL", "option_type": "CE", "strike": 24800, "premium": 85},
        ]
        ok, msg = validate_legs("IRON_CONDOR", legs, {}, 24500)
        assert ok is False
        assert "legs" in msg.lower() or "IRON_CONDOR" in msg

    def test_non_sell_leg_rejected(self):
        from src.engine.multileg_strategy import validate_legs
        chain = self._make_chain([
            {"strike": 24800, "option_type": "CE", "ltp": 85},
            {"strike": 24200, "option_type": "PE", "ltp": 40},
        ])
        legs = [
            {"side": "BUY", "option_type": "CE", "strike": 24800, "premium": 85},
            {"side": "SELL", "option_type": "PE", "strike": 24200, "premium": 40},
        ]
        ok, msg = validate_legs("SHORT_STRANGLE", legs, chain, 24500)
        assert ok is False
        assert "SELL" in msg

    def test_strike_not_in_chain(self):
        from src.engine.multileg_strategy import validate_legs
        chain = self._make_chain([
            {"strike": 24800, "option_type": "CE", "ltp": 85},
        ])
        legs = [
            {"side": "SELL", "option_type": "CE", "strike": 24800, "premium": 85},
            {"side": "SELL", "option_type": "PE", "strike": 99999, "premium": 40},
        ]
        ok, msg = validate_legs("SHORT_STRANGLE", legs, chain, 24500)
        assert ok is False
        assert "99999" in msg

    def test_invalid_strategy_type(self):
        from src.engine.multileg_strategy import validate_legs
        ok, msg = validate_legs("NONEXISTENT", [], {}, 24500)
        assert ok is False

    def test_option_chain_passed_as_list_of_rows(self):
        from src.engine.multileg_strategy import validate_legs
        option_rows = [
            {"strike": 24800, "CE": {"ltp": 85}, "PE": {"ltp": 20}},
            {"strike": 24200, "CE": {"ltp": 200}, "PE": {"ltp": 40}},
        ]
        legs = [
            {"side": "SELL", "option_type": "CE", "strike": 24800, "premium": 85},
            {"side": "SELL", "option_type": "PE", "strike": 24200, "premium": 40},
        ]
        ok, msg = validate_legs("SHORT_STRANGLE", legs, option_rows, 24500)
        assert ok is True
        assert msg == ""

    def test_option_chain_passed_as_list_of_contracts(self):
        from src.engine.multileg_strategy import validate_legs
        option_contracts = [
            {"strike": 24800, "option_type": "CE", "ltp": 85},
            {"strike": 24200, "option_type": "PE", "ltp": 40},
        ]
        legs = [
            {"side": "SELL", "option_type": "CE", "strike": 24800, "premium": 85},
            {"side": "SELL", "option_type": "PE", "strike": 24200, "premium": 40},
        ]
        ok, msg = validate_legs("SHORT_STRANGLE", legs, option_contracts, 24500)
        assert ok is True
        assert msg == ""


class TestComputeBookGreeks:
    """Tests for compute_book_greeks()."""

    def test_basic_greeks_computation(self):
        from src.engine.multileg_strategy import compute_book_greeks
        legs = [
            {"strike": 24800, "option_type": "CE", "premium": 85, "side": "SELL"},
            {"strike": 24200, "option_type": "PE", "premium": 40, "side": "SELL"},
        ]
        chain = [
            {"strike": 24800, "option_type": "CE", "ltp": 85, "oi": 1000, "volume": 500, "iv": 20, "bid": 84, "ask": 86, "delta": 0.22},
            {"strike": 24200, "option_type": "PE", "ltp": 40, "oi": 1200, "volume": 600, "iv": 18, "bid": 39, "ask": 41, "delta": -0.18},
        ]
        result = compute_book_greeks(legs, chain, 24500, "2026-08-14")
        assert "net_delta" in result
        assert "net_theta" in result
        assert "net_vega" in result
        assert "per_leg_greeks" in result
        assert len(result["per_leg_greeks"]) == 2


class TestComputeBookRiskProfile:
    """Tests for compute_book_risk_profile()."""

    def test_short_strangle_profile(self):
        from src.engine.multileg_strategy import compute_book_risk_profile
        legs = [
            {"strike": 24800, "option_type": "CE", "premium": 85},
            {"strike": 24200, "option_type": "PE", "premium": 40},
        ]
        result = compute_book_risk_profile("SHORT_STRANGLE", legs, 125, 24500)
        assert result["max_profit"] == 125
        assert result["max_loss"] > 0  # unlimited capped
        # SHORT STRANGLE: Short CE at 24800, Short PE at 24200, Net Premium = 125
        # Upper breakeven = Short CE strike + net premium = 24800 + 125 = 24925
        # Lower breakeven = Short PE strike - net premium = 24200 - 125 = 24075
        assert result["breakeven_upper"] == 24800 + 125
        assert result["breakeven_lower"] == 24200 - 125

    def test_iron_condor_profile(self):
        from src.engine.multileg_strategy import compute_book_risk_profile
        legs = [
            {"strike": 24800, "option_type": "CE", "premium": 85},
            {"strike": 24200, "option_type": "PE", "premium": 40},
            {"strike": 25000, "option_type": "CE", "premium": 30},
            {"strike": 24000, "option_type": "PE", "premium": 15},
        ]
        result = compute_book_risk_profile("IRON_CONDOR", legs, 170, 24500)
        assert result["max_profit"] == 170
        assert result["max_loss"] > 0

    def test_bull_put_spread_profile(self):
        from src.engine.multileg_strategy import compute_book_risk_profile
        legs = [
            {"strike": 73500, "option_type": "PE", "side": "SELL", "premium": 120.8},
            {"strike": 73000, "option_type": "PE", "side": "BUY", "premium": 48.2},
        ]
        result = compute_book_risk_profile("BULL_PUT_SPREAD", legs, 72.6, 73756)
        assert result["max_profit"] == 72.6
        assert result["breakeven_lower"] == round(73500 - 72.6, 2)
        assert result["breakeven_upper"] is None

    def test_bear_call_spread_profile(self):
        from src.engine.multileg_strategy import compute_book_risk_profile
        legs = [
            {"strike": 74500, "option_type": "CE", "side": "SELL", "premium": 110.0},
            {"strike": 75000, "option_type": "CE", "side": "BUY", "premium": 40.0},
        ]
        result = compute_book_risk_profile("BEAR_CALL_SPREAD", legs, 70.0, 74000)
        assert result["max_profit"] == 70.0
        assert result["breakeven_upper"] == round(74500 + 70.0, 2)
        assert result["breakeven_lower"] is None


class TestCheckBookConflicts:
    """Tests for check_book_conflicts()."""

    def test_no_conflict(self):
        from src.engine.multileg_strategy import check_book_conflicts
        conflict, msg = check_book_conflicts("NIFTY", "IRON_CONDOR", [])
        assert conflict is False

    def test_conflict_detected(self):
        from src.engine.multileg_strategy import check_book_conflicts
        existing = [{"strategy_type": "BULL_PUT_SPREAD", "symbol": "NIFTY", "status": "OPEN"}]
        conflict, msg = check_book_conflicts("NIFTY", "BEAR_CALL_SPREAD", existing)
        assert conflict is True
        assert "conflict" in msg.lower() or "BULL_PUT_SPREAD" in msg

    def test_same_structure_type_conflict(self):
        """Only one position of a particular structure type can be open at any time."""
        from src.engine.multileg_strategy import check_book_conflicts
        existing = [{"strategy_type": "IRON_CONDOR", "symbol": "NIFTY", "status": "OPEN", "book_id": "B1"}]
        conflict, msg = check_book_conflicts("NIFTY", "IRON_CONDOR", existing)
        assert conflict is True
        assert "Structure exclusivity" in msg or "already open" in msg

    def test_different_structure_type_allowed(self):
        """Different structure types can be open at the same time."""
        from src.engine.multileg_strategy import check_book_conflicts
        existing = [{"strategy_type": "IRON_CONDOR", "symbol": "NIFTY", "status": "OPEN", "book_id": "B1"}]
        conflict, msg = check_book_conflicts("NIFTY", "SHORT_STRANGLE", existing)
        assert conflict is False
        assert msg == ""

    def test_closed_same_structure_type_allowed(self):
        """A new position of the same structure type is allowed if prior position is CLOSED."""
        from src.engine.multileg_strategy import check_book_conflicts
        existing = [{"strategy_type": "IRON_CONDOR", "symbol": "NIFTY", "status": "CLOSED", "book_id": "B1"}]
        conflict, msg = check_book_conflicts("NIFTY", "IRON_CONDOR", existing)
        assert conflict is False
        assert msg == ""

    def test_different_symbol_same_structure_allowed(self):
        """Structure exclusivity is scoped per symbol."""
        from src.engine.multileg_strategy import check_book_conflicts
        existing = [{"strategy_type": "IRON_CONDOR", "symbol": "BANKNIFTY", "status": "OPEN", "book_id": "B1"}]
        conflict, msg = check_book_conflicts("NIFTY", "IRON_CONDOR", existing)
        assert conflict is False
        assert msg == ""
