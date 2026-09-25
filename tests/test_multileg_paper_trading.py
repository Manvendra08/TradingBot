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


class TestMultilegPnlAndSnapshotFixes:
    """Tests for multi-leg snapshot lookups and PnL calculation fixes."""

    def test_get_latest_option_snapshot_exists(self):
        from src.models.schema import get_latest_option_snapshot
        assert callable(get_latest_option_snapshot)

    def test_multileg_live_trading_update_pnl_exists(self):
        from src.engine.multileg_live_trading import _update_live_book_pnl
        assert callable(_update_live_book_pnl)

    def test_calc_multileg_pnl_expiry_fallback(self):
        from src.engine.multileg_paper_trading import _calc_multileg_pnl
        book = {
            "symbol": "BANKNIFTY",
            "expiry": "2026-08-27",
            "entry_underlying": 51000.0,
            "scan_context": {"underlying": 51000.0, "option_rows": []},
        }
        legs = [
            {"strike": 52000, "option_type": "CE", "entry_premium": 100.0, "side": "SELL", "lots": 1},
            {"strike": 50000, "option_type": "PE", "entry_premium": 100.0, "side": "SELL", "lots": 1},
        ]
        pnl = _calc_multileg_pnl(book, legs)
        assert isinstance(pnl, float)

    def test_close_book_prefers_leg_math_over_synthetic_total_pnl(self):
        from config.settings import LOT_SIZES
        from src.models.schema import close_book, init_db, insert_multileg_trade_atomically

        symbol = "NIFTY"
        lot_size = LOT_SIZES[symbol]
        trade = {
            "trade_ref": "T1",
            "symbol": symbol,
            "structure": "IRON_CONDOR",
            "net_premium": 102.0,
            "margin_req": 120000.0,
            "total_pnl": 0.0,
            "opened_at": "2026-01-01T00:00:00+00:00",
            "closed_at": None,
            "status": "OPEN",
            "reason": "open",
            "entry_reason": "open",
            "exit_reason": None,
            "profit_factor": 0.0,
            "book_id": "BOOK-REGRESSION-1",
            "strategy_type": "IRON_CONDOR",
            "expiry": "2026-01-07",
            "entry_underlying": 24500.0,
            "exit_underlying": None,
            "net_delta": 0.05,
            "net_theta": -15.0,
            "net_vega": -20.0,
            "max_profit": 160.0,
            "max_loss": 840.0,
            "breakeven_upper": 24660.0,
            "breakeven_lower": 24340.0,
            "profit_target_pct": 0.5,
            "stop_loss_pct": 1.0,
            "time_decay_exit_dte": 2,
            "adjustment_count": 0,
            "confidence_score": 80,
            "entry_quality_score": 85,
            "digest_id": "digest-1",
            "ai_model_name": "test-model",
            "snapshot_id": "snap-1",
        }
        legs = [
            {"side": "SELL", "lots": 1, "strike": 24400.0, "option_type": "CE", "entry_premium": 65.0, "exit_premium": 45.0, "delta": 0.28, "theta": -8.0, "vega": 12.0, "iv": 18.0, "rationale": "sell ce", "status": "OPEN", "closed_at": None, "exit_reason": None, "broker_order_id": None},
            {"side": "BUY", "lots": 1, "strike": 24500.0, "option_type": "CE", "entry_premium": 28.0, "exit_premium": 18.0, "delta": 0.22, "theta": 2.0, "vega": 8.0, "iv": 18.0, "rationale": "buy ce", "status": "OPEN", "closed_at": None, "exit_reason": None, "broker_order_id": None},
            {"side": "SELL", "lots": 1, "strike": 24600.0, "option_type": "PE", "entry_premium": 95.0, "exit_premium": 70.0, "delta": -0.26, "theta": -7.0, "vega": 11.0, "iv": 19.0, "rationale": "sell pe", "status": "OPEN", "closed_at": None, "exit_reason": None, "broker_order_id": None},
            {"side": "BUY", "lots": 1, "strike": 24500.0, "option_type": "PE", "entry_premium": 30.0, "exit_premium": 20.0, "delta": -0.24, "theta": 1.5, "vega": 7.0, "iv": 19.0, "rationale": "buy pe", "status": "OPEN", "closed_at": None, "exit_reason": None, "broker_order_id": None},
        ]
        trade_id = insert_multileg_trade_atomically(trade, legs)
        assert trade_id > 0

        synthetic_total_pnl = 12345.67
        leg_exits = [
            {"id": 1, "exit_premium": 45.0},
            {"id": 2, "exit_premium": 18.0},
            {"id": 3, "exit_premium": 70.0},
            {"id": 4, "exit_premium": 20.0},
        ]
        with patch("src.models.schema._calc_transaction_costs", return_value=0.0):
            close_book(
                "BOOK-REGRESSION-1",
                "2026-01-07T15:35:00+00:00",
                "CLOSED",
                "REGRESSION_TEST",
                total_pnl=synthetic_total_pnl,
                exit_underlying=24550.0,
                leg_exits=leg_exits,
            )

        import sqlite3
        from src.models.schema import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        trade_row = conn.execute("SELECT * FROM multi_leg_trades WHERE id=?", (trade_id,)).fetchone()
        assert trade_row is not None
        assert trade_row["status"] == "CLOSED"
        assert float(trade_row["total_pnl"]) != synthetic_total_pnl

        leg_rows = conn.execute("SELECT * FROM multi_leg_legs WHERE trade_id=? ORDER BY id", (trade_id,)).fetchall()
        assert len(leg_rows) == 4
        for leg_row, expected_exit in zip(leg_rows, [45.0, 18.0, 70.0, 20.0]):
            assert float(leg_row["exit_premium"]) == expected_exit
            assert leg_row["status"] == "CLOSED"
            assert leg_row["exit_reason"] == "REGRESSION_TEST"

        expected_leg_pnl = 0.0
        for leg, expected_exit in zip(legs, [45.0, 18.0, 70.0, 20.0]):
            side = (leg.get("side") or "SELL").upper()
            entry = float(leg.get("entry_premium") or 0.0)
            leg_pnl = (entry - expected_exit) if side == "SELL" else (expected_exit - entry)
            expected_leg_pnl += leg_pnl * lot_size * int(leg.get("lots") or 1)
        assert float(trade_row["total_pnl"]) == round(expected_leg_pnl, 2)

    def test_close_book_without_leg_exits_does_not_invent_exit_premium(self):
        from src.models.schema import close_book, insert_multileg_trade_atomically

        trade = {
            "trade_ref": "T2",
            "symbol": "BANKNIFTY",
            "structure": "SHORT_STRANGLE",
            "net_premium": 180.0,
            "margin_req": 150000.0,
            "total_pnl": 0.0,
            "opened_at": "2026-01-01T00:00:00+00:00",
            "closed_at": None,
            "status": "OPEN",
            "reason": "open",
            "entry_reason": "open",
            "exit_reason": None,
            "profit_factor": 0.0,
            "book_id": "BOOK-REGRESSION-2",
            "strategy_type": "SHORT_STRANGLE",
            "expiry": "2026-01-07",
            "entry_underlying": 51000.0,
            "exit_underlying": None,
            "net_delta": 0.04,
            "net_theta": -10.0,
            "net_vega": -15.0,
            "max_profit": 180.0,
            "max_loss": 820.0,
            "breakeven_upper": 51180.0,
            "breakeven_lower": 50820.0,
            "profit_target_pct": 0.5,
            "stop_loss_pct": 1.0,
            "time_decay_exit_dte": 2,
            "adjustment_count": 0,
            "confidence_score": 75,
            "entry_quality_score": 78,
            "digest_id": "digest-2",
            "ai_model_name": "test-model",
            "snapshot_id": "snap-2",
        }
        legs = [
            {"side": "SELL", "lots": 1, "strike": 50900.0, "option_type": "CE", "entry_premium": 90.0, "exit_premium": 0.0, "delta": 0.28, "theta": -8.0, "vega": 12.0, "iv": 18.0, "rationale": "sell ce", "status": "OPEN", "closed_at": None, "exit_reason": None, "broker_order_id": None},
            {"side": "SELL", "lots": 1, "strike": 51100.0, "option_type": "PE", "entry_premium": 90.0, "exit_premium": 0.0, "delta": -0.26, "theta": -7.0, "vega": 11.0, "iv": 19.0, "rationale": "sell pe", "status": "OPEN", "closed_at": None, "exit_reason": None, "broker_order_id": None},
        ]
        trade_id = insert_multileg_trade_atomically(trade, legs)
        assert trade_id > 0

        close_book(
            "BOOK-REGRESSION-2",
            "2026-01-07T15:35:00+00:00",
            "CLOSED",
            "NO_LEG_EXITS_TEST",
            total_pnl=None,
            exit_underlying=None,
            leg_exits=None,
        )

        import sqlite3
        from src.models.schema import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        leg_rows = conn.execute("SELECT * FROM multi_leg_legs WHERE trade_id=? ORDER BY id", (trade_id,)).fetchall()
        assert len(leg_rows) == 2
        for leg_row in leg_rows:
            assert leg_row["status"] == "CLOSED"
            assert leg_row["exit_premium"] in (None, 0.0)


class TestDualModeDecisionEngineAndConfidenceDecoupling:
    """Tests for Dual-Mode Decision Engine & Multi-Leg Confidence Decoupling."""

    def test_rangebound_verdict_sets_membership(self):
        """Verify Rangebound is registered in NEUTRAL_VERDICTS and NOT in directional sets."""
        from src.engine.verdict_sets import is_neutral, is_bullish, is_bearish
        assert is_neutral("Rangebound") is True
        assert is_bullish("Rangebound") is False
        assert is_bearish("Rangebound") is False

    def test_rangebound_detected_on_balanced_dual_writing(self):
        """Verify _price_oi_verdict returns Rangebound when both CE and PE OI are building in balance."""
        from src.engine.intelligence import _price_oi_verdict
        verdict_label, emoji, desc = _price_oi_verdict(
            price_pct=0.01,
            net_oi_change=4000,
            ce_oi_change=2000,
            pe_oi_change=2000,
            pcr=1.02,
            alerts=[],
        )
        assert verdict_label == "Rangebound"
        assert emoji == "🟣"
        assert "strangle/condor" in desc.lower()

    def test_rangebound_confidence_scoring_and_cap_exemption(self):
        """Verify Rangebound scores high on consolidation and is not capped to 50% or 65%."""
        from src.engine.intelligence import _compute_confidence
        scan_ctx = {
            "underlying": 24500.0,
            "max_pain": 24500.0,
            "price_change_pct": 0.02,
            "pcr": 1.05,
            "ce_oi_change": 2500,
            "pe_oi_change": 2200,
            "support": 24300.0,
            "resistance": 24700.0,
        }
        score, conflict = _compute_confidence(scan_ctx, [], parsed_chart=None, verdict_label="Rangebound")
        # Base 15 + flat price 20 + balanced PCR 15 + balanced OI 20 + max pain 10 = 80
        assert score >= 75
        assert score > 65  # Must not be capped by the 65% flat price cap or 50% sideways cap

    def test_llm_enrichment_decouples_nondirectional_strategy_confidence(self):
        """Verify get_multileg_verdict preserves LLM confidence for non-directional strategies even when engine_conf is low."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from src.engine.llm_enrichment import get_multileg_verdict

        mock_result = SimpleNamespace(
            strategy_type="SHORT_STRANGLE",
            legs=[
                SimpleNamespace(strike=330.0, option_type="CE", side="SELL", ratio=1, premium=10.0),
                SimpleNamespace(strike=290.0, option_type="PE", side="SELL", ratio=1, premium=8.0),
            ],
            confidence=85,
            net_premium=18.0,
            model_name="test-llm",
            thesis="Dual-sided decay edge",
            entry_rationale="Rangebound harvest",
        )

        with patch("src.engine.llm_enrichment._call_llm_api", return_value=mock_result):
            res = get_multileg_verdict(
                symbol="NATURALGAS",
                intel={"verdict_label": "Low Conviction", "confidence": 10},
                scan_context={"underlying": 308.0, "option_rows": []},
            )
            assert res is not None
            assert res.strategy_type == "SHORT_STRANGLE"
            # Crucial assertion: Must remain 85, NOT clamped to engine_conf=10
            assert res.confidence == 85

    def test_llm_enrichment_clamps_directional_strategy_confidence(self):
        """Verify get_multileg_verdict still clamps directional strategies to engine_conf."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from src.engine.llm_enrichment import get_multileg_verdict

        mock_result = SimpleNamespace(
            strategy_type="BEAR_CALL_SPREAD",
            legs=[
                SimpleNamespace(strike=320.0, option_type="CE", side="SELL", ratio=1, premium=12.0),
                SimpleNamespace(strike=330.0, option_type="CE", side="BUY", ratio=1, premium=6.0),
            ],
            confidence=85,
            net_premium=6.0,
            model_name="test-llm",
            thesis="Directional call spread",
            entry_rationale="Bearish drift",
        )

        with patch("src.engine.llm_enrichment._call_llm_api", return_value=mock_result):
            res = get_multileg_verdict(
                symbol="NATURALGAS",
                intel={"verdict_label": "Short Buildup", "confidence": 45},
                scan_context={"underlying": 308.0, "option_rows": []},
            )
            assert res is not None
            assert res.strategy_type == "BEAR_CALL_SPREAD"
            # Directional spread must clamp to min(85, 45) = 45
            assert res.confidence == 45

    def test_multileg_paper_trading_decouples_nondirectional_effective_confidence(self):
        """Verify _attempt_new_entry accepts non-directional trades when LLM conf >= floor, ignoring low engine conf."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from src.engine.multileg_paper_trading import _attempt_new_entry

        mock_verdict = SimpleNamespace(
            strategy_type="SHORT_STRANGLE",
            legs=[
                {"strike": 24800.0, "option_type": "CE", "side": "SELL", "premium": 90.0, "lots": 1, "delta": 0.20},
                {"strike": 24200.0, "option_type": "PE", "side": "SELL", "premium": 80.0, "lots": 1, "delta": -0.20},
            ],
            confidence=78,
            entry_rationale="Safe strikes outside expected move",
            thesis="Consolidation play",
            model_name="test-llm",
            net_delta=0.0,
            net_premium=170.0,
            profit_target_pct=0.5,
            stop_loss_pct=1.0,
            time_decay_exit_dte=2,
            adjustment_plan="Roll tested side when delta exceeds 0.35",
        )

        ctx = {
            "underlying": 24500.0,
            "expiry": "2026-10-23",
            "option_rows": [
                {"strike": 24800.0, "option_type": "CE", "ltp": 90.0, "oi": 5000, "bid": 89.0, "ask": 91.0},
                {"strike": 24200.0, "option_type": "PE", "ltp": 80.0, "oi": 5000, "bid": 79.0, "ask": 81.0},
            ],
            "engine_confidence": 10,
        }
        intel = {"verdict_label": "Low Conviction", "confidence": 10}

        # Mock downstream validation and DB calls to isolate confidence gate check
        with patch("src.models.schema.get_open_books_for_symbol", return_value=[]), \
             patch("src.engine.multileg_validator.validate_multileg_trade") as mock_val, \
             patch("src.engine.multileg_strategy.validate_legs", return_value=(True, "")), \
             patch("src.engine.multileg_strategy.score_entry_quality", return_value=(80, ["Good setup"])), \
             patch("src.models.schema.insert_multileg_trade_atomically", return_value=999):
            res = _attempt_new_entry(
                "NIFTY",
                ctx,
                "test-digest",
                intel,
                ai_verdict=mock_verdict,
                ai_mode="advisory",
                now_iso="2026-10-23T10:00:00+00:00",
                open_books=[],
            )
            assert res is not None
            # Trade must NOT be rejected by LLM_CONFIDENCE_GATE
            assert res.get("decision_stage") != "LLM_CONFIDENCE_GATE"
            assert res.get("confidence") == 78


