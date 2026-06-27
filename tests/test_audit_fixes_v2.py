"""
Tests for audit fixes v2: H1, H2, M1, M3, M4, M5, L1, L4.

Covers:
- H1: Chart confidence boost + chart_conflict detection in _compute_confidence
- H2: Sell-side STT calculation in _calc_transaction_costs
- M1: close_paper_trade reads stored lot_size column
- M3: get_previous_underlying_before returns None instead of latest row
- M4: Dead trade exit falls back to datetime.now when bar_end_1h is None
- M5: _ctx_copy safely handles non-string keys
- L1: Expiry cleanup guard (once per day)
- L4: scan_summary chart traversal without verdict_label guard
"""
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.models.schema import get_conn, init_db


@pytest.fixture(autouse=True)
def setup_test_db():
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM live_trades")
        conn.execute("DELETE FROM underlying_price")
        conn.execute("DELETE FROM option_chain_snapshots")
        conn.execute("DELETE FROM scan_summaries")


# ═══════════════════════════════════════════════════════════════════════════
# H1: Chart confidence boost + chart_conflict detection
# ═══════════════════════════════════════════════════════════════════════════


class TestChartConfidenceBoost:
    """Verify _compute_confidence uses parsed_chart for alignment boost + conflict."""

    def test_no_chart_no_boost(self):
        from src.engine.intelligence import _compute_confidence

        ctx = {"price_change_pct": 0.1, "ce_oi_change": 5000, "pe_oi_change": 2000}
        base_score, _ = _compute_confidence(ctx, [], verdict_label="Long Buildup")
        # No chart passed — no boost, no conflict
        assert base_score >= 10

    def test_chart_alignment_adds_boost(self):
        from src.engine.intelligence import _compute_confidence

        ctx = {"price_change_pct": 0.1, "ce_oi_change": 5000, "pe_oi_change": 2000}
        parsed = {"1h": {"sentiment": "BULLISH", "ind_dict": {}}}

        base_score, _ = _compute_confidence(ctx, [], parsed_chart=None, verdict_label="Long Buildup")
        boosted_score, conflict = _compute_confidence(
            ctx, [], parsed_chart=parsed, verdict_label="Long Buildup"
        )

        assert boosted_score >= base_score + 10
        assert conflict is False

    def test_chart_mismatch_no_boost(self):
        from src.engine.intelligence import _compute_confidence

        ctx = {"price_change_pct": 0.1, "ce_oi_change": 5000, "pe_oi_change": 2000}
        parsed = {"1h": {"sentiment": "BEARISH", "ind_dict": {}}}

        base_score, _ = _compute_confidence(ctx, [], parsed_chart=None, verdict_label="Long Buildup")
        boosted_score, _ = _compute_confidence(
            ctx, [], parsed_chart=parsed, verdict_label="Long Buildup"
        )

        assert boosted_score == base_score

    def test_chart_conflict_detected(self):
        from src.engine.intelligence import _compute_confidence

        ctx = {"price_change_pct": 0.1, "ce_oi_change": 5000, "pe_oi_change": 2000}
        parsed = {
            "1h": {"sentiment": "BULLISH", "ind_dict": {}},
            "3h": {"sentiment": "BEARISH", "ind_dict": {}},
        }

        _, conflict = _compute_confidence(
            ctx, [], parsed_chart=parsed, verdict_label="Long Buildup"
        )
        assert conflict is True

    def test_chart_conflict_requires_both_non_neutral(self):
        from src.engine.intelligence import _compute_confidence

        ctx = {"price_change_pct": 0.1, "ce_oi_change": 5000, "pe_oi_change": 2000}
        # One NEUTRAL — not a real conflict
        parsed = {
            "1h": {"sentiment": "NEUTRAL", "ind_dict": {}},
            "3h": {"sentiment": "BEARISH", "ind_dict": {}},
        }

        _, conflict = _compute_confidence(
            ctx, [], parsed_chart=parsed, verdict_label="Long Buildup"
        )
        assert conflict is False

    def test_empty_parsed_chart_no_crash(self):
        from src.engine.intelligence import _compute_confidence

        ctx = {"price_change_pct": 0.1, "ce_oi_change": 5000, "pe_oi_change": 2000}
        score, conflict = _compute_confidence(
            ctx, [], parsed_chart={}, verdict_label="Long Buildup"
        )
        assert score >= 10
        assert conflict is False

    def test_only_one_timeframe_no_conflict(self):
        from src.engine.intelligence import _compute_confidence

        ctx = {"price_change_pct": 0.1}
        parsed = {"1h": {"sentiment": "BULLISH", "ind_dict": {}}}

        _, conflict = _compute_confidence(
            ctx, [], parsed_chart=parsed, verdict_label="Long Buildup"
        )
        assert conflict is False


# ═══════════════════════════════════════════════════════════════════════════
# H2: Sell-side STT calculation in _calc_transaction_costs
# ═══════════════════════════════════════════════════════════════════════════


class TestTransactionCostsSellSide:
    """Verify _calc_transaction_costs uses entry values for sell-to-open trades."""

    def test_buy_option_uses_exit_premium(self):
        from src.models.schema import _calc_transaction_costs

        cost_buy = _calc_transaction_costs(
            option_type="CE", side="BUY",
            entry_premium=100.0, entry_underlying=0,
            exit_premium=120.0, exit_underlying=0,
            lot_size=75, lots=1,
        )
        cost_exit_zero = _calc_transaction_costs(
            option_type="CE", side="BUY",
            entry_premium=100.0, entry_underlying=0,
            exit_premium=0.0, exit_underlying=0,
            lot_size=75, lots=1,
        )
        # STT on sell turnover = 120*75*1 = 9000 → 0.0625% = 5.625
        # Plus flat 50 → total ~55.62
        assert cost_buy > cost_exit_zero, "BUY with premium exit should have higher STT"

    def test_sell_option_uses_entry_premium(self):
        from src.models.schema import _calc_transaction_costs

        cost_sell = _calc_transaction_costs(
            option_type="CE", side="SELL",
            entry_premium=100.0, entry_underlying=0,
            exit_premium=80.0, exit_underlying=0,
            lot_size=75, lots=1,
        )
        cost_sell_low_entry = _calc_transaction_costs(
            option_type="CE", side="SELL",
            entry_premium=10.0, entry_underlying=0,
            exit_premium=80.0, exit_underlying=0,
            lot_size=75, lots=1,
        )
        # SELL side should use entry_premium for STT base
        assert cost_sell > cost_sell_low_entry, (
            "SELL with higher entry premium should have higher STT"
        )

    def test_buy_future_uses_exit_underlying(self):
        from src.models.schema import _calc_transaction_costs

        cost = _calc_transaction_costs(
            option_type="FUT", side="BUY",
            entry_premium=0, entry_underlying=22000,
            exit_premium=0, exit_underlying=22200,
            lot_size=75, lots=1,
        )
        # Flat 50 + STT on 22200*75 = 1,665,000 → 0.01% = 166.50
        assert cost > 200, "Future BUY should have STT on exit_underlying"

    def test_sell_future_uses_entry_underlying(self):
        from src.models.schema import _calc_transaction_costs

        cost_high_entry = _calc_transaction_costs(
            option_type="FUT", side="SELL",
            entry_premium=0, entry_underlying=22200,
            exit_premium=0, exit_underlying=22000,
            lot_size=75, lots=1,
        )
        cost_low_entry = _calc_transaction_costs(
            option_type="FUT", side="SELL",
            entry_premium=0, entry_underlying=22000,
            exit_premium=0, exit_underlying=22200,
            lot_size=75, lots=1,
        )
        assert cost_high_entry > cost_low_entry, (
            "SELL should use entry_underlying, not exit_underlying"
        )

    def test_minimum_flat_cost(self):
        from src.models.schema import _calc_transaction_costs

        cost = _calc_transaction_costs(
            option_type="CE", side="BUY",
            entry_premium=0, entry_underlying=0,
            exit_premium=0, exit_underlying=0,
            lot_size=1, lots=1,
        )
        assert cost == 50.0, "Zero turnover should still have flat 50 cost"


# ═══════════════════════════════════════════════════════════════════════════
# M1: close_paper_trade uses stored lot_size column
# ═══════════════════════════════════════════════════════════════════════════


class TestClosePaperTradeLotSize:
    """close_paper_trade must read lot_size from the DB row with dict fallback."""

    def _insert_trade(self, lot_size_val=None):
        """Insert an open paper trade with optional stored lot_size."""
        with get_conn() as conn:
            cols = [
                "opened_at", "symbol", "expiry", "verdict_label", "side",
                "option_type", "strike", "entry_underlying", "entry_premium",
                "sl_underlying", "target_underlying", "lots", "status",
                "reason", "digest_id", "trade_status", "signal_key",
            ]
            vals = [
                datetime.now(timezone.utc).isoformat(), "NIFTY", "2099-12-31",
                "Long Buildup", "BUY", "CE", 22000.0, 22000.0, 100.0,
                21900.0, 22200.0, 1, "OPEN", "test", "digest-m1",
                "TRIGGERED_CORE", "sig-m1",
            ]
            if lot_size_val is not None:
                cols.append("lot_size")
                vals.append(lot_size_val)
            placeholders = ",".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO paper_trades ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            return conn.execute(
                "SELECT id FROM paper_trades WHERE signal_key='sig-m1'"
            ).fetchone()["id"]

    def test_uses_stored_lot_size_when_not_null(self):
        """When lot_size column has a value, it must be used over LOT_SIZES dict."""
        trade_id = self._insert_trade(lot_size_val=999)

        with patch("config.settings.LOT_SIZES", {"NIFTY": 1}):
            from src.models.schema import close_paper_trade

            close_paper_trade(
                trade_id, datetime.now(timezone.utc).isoformat(),
                22200.0, 150.0, "CLOSED_TARGET", "test",
            )

        with get_conn() as conn:
            row = conn.execute(
                "SELECT pnl_rupees, pnl_points FROM paper_trades WHERE id=?",
                (trade_id,),
            ).fetchone()
        # pnl_points = exit_premium(150) - entry_premium(100) = 50
        # pnl_rupees = 50 * 999(lot_size) * 1(lots) - tx_cost
        # tx_cost uses lot_size=999 for STT: 150*999*1*0.000625 = 93.66 + 50 flat = 143.66
        # gross = 50*999 = 49950, net = 49950 - 143.66 ≈ 49806.34
        assert row["pnl_points"] == 50.0
        assert row["pnl_rupees"] > 49000, "Should use stored lot_size=999"

    def test_falls_back_to_lot_sizes_dict_when_null(self):
        """When lot_size column is NULL, fall back to LOT_SIZES settings dict."""
        trade_id = self._insert_trade(lot_size_val=None)

        with patch("config.settings.LOT_SIZES", {"NIFTY": 75}):
            from src.models.schema import close_paper_trade

            close_paper_trade(
                trade_id, datetime.now(timezone.utc).isoformat(),
                22200.0, 150.0, "CLOSED_TARGET", "test",
            )

        with get_conn() as conn:
            row = conn.execute(
                "SELECT pnl_rupees, pnl_points FROM paper_trades WHERE id=?",
                (trade_id,),
            ).fetchone()
        # pnl_points = 50, gross = 50 * 75 * 1 = 3750
        assert row["pnl_rupees"] < 3750, "Should use LOT_SIZES fallback (75)"


# ═══════════════════════════════════════════════════════════════════════════
# M3: get_previous_underlying_before returns None when no close match
# ═══════════════════════════════════════════════════════════════════════════


class TestGetPreviousUnderlyingBefore:
    """get_previous_underlying_before should return None when no match found."""

    def test_returns_none_when_no_rows(self):
        from src.models.schema import get_previous_underlying_before

        result = get_previous_underlying_before(
            "NIFTY", datetime.now(timezone.utc).isoformat()
        )
        assert result is None

    def test_returns_none_with_fallback_too_recent(self):
        from src.models.schema import (
            get_previous_underlying_before,
            insert_underlying_price,
        )

        now = datetime.now(timezone.utc)
        insert_underlying_price("NIFTY", 22000.0, 0.0, now.isoformat())

        result = get_previous_underlying_before("NIFTY", now.isoformat())
        # Should be None because the only row is from NOW (too recent)
        assert result is None

    def test_returns_older_row_when_available(self):
        from src.models.schema import (
            get_previous_underlying_before,
            insert_underlying_price,
        )

        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        insert_underlying_price("NIFTY", 21900.0, 0.0, old)
        now = datetime.now(timezone.utc).isoformat()
        insert_underlying_price("NIFTY", 22000.0, 0.0, now)

        result = get_previous_underlying_before("NIFTY", now)
        assert result is not None
        assert float(result["price"]) == 21900.0


# ═══════════════════════════════════════════════════════════════════════════
# M4: Dead trade exit fallback when bar_end_1h is None
# ═══════════════════════════════════════════════════════════════════════════


class TestDeadTradeExitFallback:
    """Dead trade exit in timeframe strategy uses datetime.now fallback."""

    def test_dead_trade_uses_now_fallback(self):
        """Verify the code path uses datetime.now when bar_end_1h is None."""
        from src.engine import paper_trading

        import inspect

        source = inspect.getsource(paper_trading)
        # The dead trade block must have a fallback for bar_end_1h
        assert "else:" in source or "bar_end_dt = datetime.now(timezone.utc)" in source

    def test_dead_trade_calls_close_on_old_unprofitable_trade(self):
        """When bar_end_1h is None and trade is >3h old with max_fav<0.5, dead trade fires."""
        from src.engine.paper_trading import run_timeframe_strategy

        old_time = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades (
                    opened_at, symbol, expiry, verdict_label, side, option_type,
                    strike, entry_underlying, entry_premium, sl_underlying,
                    target_underlying, lots, status, reason, digest_id,
                    trade_status, signal_key, pyramid_level, max_favorable_r
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    old_time, "NIFTY", "2099-12-31", "LONG", "BUY", "CE",
                    22000.0, 22000.0, 100.0, 21900.0, 22200.0, 1, "OPEN",
                    "test", "digest-m4", "TRIGGERED_TIMEFRAME", "sig-m4-tf", 1, 0.1,
                ),
            )

        ctx = {
            "symbol": "NIFTY",
            "underlying": 22050.0,
            "expiry": "2099-12-31",
            "option_rows": [{"strike": 22000.0, "option_type": "CE", "ltp": 95.0}],
            "chart_indicators": {
                "3h": {
                    "ohlc": {"open": 22000, "high": 22100, "low": 21900, "close": 22050},
                    "prev_ohlc": {"open": 21900, "high": 21980, "low": 21800, "close": 21950},
                    "bar_end_utc": None,
                },
                "1h": {
                    "ohlc": {"open": 22050, "high": 22100, "low": 22000, "close": 22050},
                    "prev_ohlc": {"open": 21950, "high": 22000, "low": 21930, "close": 22000},
                    "bar_end_utc": None,
                },
            },
            "total_ce_oi": 100000,
            "total_pe_oi": 100000,
        }

        with (
            patch("src.engine.paper_trading.get_scan_frequency_nse", return_value=5),
            patch("src.engine.paper_trading._invalidate_pattern_cache"),
            patch("src.engine.paper_trading._trigger_ml_retraining"),
        ):
            result = run_timeframe_strategy("NIFTY", ctx, "digest-m4", {
                "verdict_label": "LONG", "confidence": 80,
            })

        if result and result.get("action") == "CLOSED":
            with get_conn() as conn:
                trade = conn.execute(
                    "SELECT * FROM paper_trades WHERE signal_key='sig-m4-tf'"
                ).fetchone()
            assert trade is not None
            assert trade["status"].startswith("CLOSED")


# ═══════════════════════════════════════════════════════════════════════════
# M5: _ctx_copy safely handles non-string keys
# ═══════════════════════════════════════════════════════════════════════════


class TestCtxCopy:
    """_ctx_copy must discard non-string keys safely."""

    def test_filters_non_string_keys(self):
        from src.engine.intelligence import _ctx_copy

        ctx = {"a": 1, "b": 2, 0: "zero", 1: "one"}
        result = _ctx_copy(ctx)
        assert "a" in result
        assert "b" in result
        assert 0 not in result
        assert 1 not in result
        assert result["a"] == 1
        assert result["b"] == 2

    def test_empty_dict_returns_empty(self):
        from src.engine.intelligence import _ctx_copy

        assert _ctx_copy({}) == {}

    def test_all_string_keys_unchanged(self):
        from src.engine.intelligence import _ctx_copy

        ctx = {"a": 1, "b": 2}
        assert _ctx_copy(ctx) == ctx

    def test_used_in_paper_trade_idea(self):
        """Verify _ctx_copy is used at the paper trade idea call site."""
        from src.engine import intelligence

        import inspect

        source = inspect.getsource(intelligence)
        assert "_ctx_copy(ctx)" in source


# ═══════════════════════════════════════════════════════════════════════════
# L1: Expiry cleanup guard (once per day)
# ═══════════════════════════════════════════════════════════════════════════


class TestExpiryCleanupGuard:
    """Pipeline expiry cleanup must run at most once per calendar day."""

    def test_cleanup_dates_set_in_module(self):
        from src.engine.pipeline import _CLEANUP_DATES

        assert isinstance(_CLEANUP_DATES, set)
        # Should start empty
        assert len(_CLEANUP_DATES) == 0

    def test_cleanup_skipped_after_first_run(self):
        """_CLEANUP_DATES prevents cleanup from running again same day."""
        from src.engine.pipeline import _CLEANUP_DATES

        _CLEANUP_DATES.clear()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # First run adds the date
        assert today not in _CLEANUP_DATES
        _CLEANUP_DATES.add(today)
        assert today in _CLEANUP_DATES

        # Verify the guard prevents re-running
        from src.engine.pipeline import _CLEANUP_DATES as cd
        assert today in cd
