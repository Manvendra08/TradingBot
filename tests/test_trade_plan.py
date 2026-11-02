"""
Tests for unified trade_plan module (C4 fix).

Covers:
- ATR extraction from chart_indicators
- BUY/SELL SL/Target calculation (ATR-based + fallback)
- Option premium resolution (live rows + DB staleness check L2)
- Verdict parsing
- Underlying-to-premium conversion
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from src.engine.trade_plan import (
    calculate_breakeven_level,
    calculate_trailing_sl,
    calculate_option_book_stop_target,
    get_atr,
    calculate_buy_sl_target,
    calculate_sell_sl_target,
    get_option_premium,
    parse_verdict_and_confidence,
    convert_underlying_sl_to_premium,
    _DB_PREMIUM_MAX_AGE_SECONDS,
)


# ── ATR Extraction ────────────────────────────────────────────────────────

class TestGetAtr:
    def test_atr_from_3h(self):
        ctx = {"chart_indicators": {"3h": {"atr_14": 150.0}, "1h": {"atr_14": 80.0}}}
        assert get_atr(ctx) == 150.0

    def test_atr_from_1h_fallback(self):
        ctx = {"chart_indicators": {"3h": {}, "1h": {"atr_14": 80.0}}}
        assert get_atr(ctx) == 80.0

    def test_atr_from_any_tf_fallback(self):
        # The implementation iterates over dict items, so we need to ensure 
        # it finds the '5m' key which is not '3h' or '1h'.
        ctx = {"chart_indicators": {"5m": {"atr_14": 25.0}}}
        result = get_atr(ctx)
        assert result == 25.0

    def test_atr_none_when_missing(self):
        ctx = {"chart_indicators": {"3h": {}, "1h": {}}}
        assert get_atr(ctx) is None

    def test_atr_none_empty_ctx(self):
        assert get_atr({}) is None
        assert get_atr({"chart_indicators": None}) is None


# ── BUY SL/Target ─────────────────────────────────────────────────────────

class TestCalculateBuySlTarget:
    def test_atr_based(self):
        ctx = {"chart_indicators": {"3h": {"atr_14": 100.0}}}
        sl, tgt = calculate_buy_sl_target(200.0, 22000.0, ctx)
        # SL = 22000 - 1.5*100 = 21850; Target = 22000 + 2.0*100 = 22200
        assert sl == 21850.0
        assert tgt == 22200.0

    def test_fallback_no_atr(self):
        sl, tgt = calculate_buy_sl_target(200.0, 22000.0, {}, step=50.0)
        # SL = 22000 - 2*50 = 21900; Target = 22000 + 2*50 = 22100
        assert sl == 21900.0
        assert tgt == 22100.0

    def test_custom_step(self):
        sl, tgt = calculate_buy_sl_target(200.0, 1000.0, {}, step=100.0)
        assert sl == 800.0
        assert tgt == 1200.0


# ── SELL SL/Target ────────────────────────────────────────────────────────

class TestCalculateSellSlTarget:
    def test_atr_based(self):
        ctx = {"chart_indicators": {"3h": {"atr_14": 100.0}}}
        sl, tgt = calculate_sell_sl_target(200.0, 22000.0, ctx)
        # SL = 22000 + 1.5*100 = 22150; Target = 22000 - 2.0*100 = 21800
        assert sl == 22150.0
        assert tgt == 21800.0

    def test_fallback_no_atr(self):
        sl, tgt = calculate_sell_sl_target(200.0, 22000.0, {}, step=50.0)
        assert sl == 22100.0
        assert tgt == 21900.0


# ── Option Premium Resolution ─────────────────────────────────────────────

class TestGetOptionPremium:
    def test_live_rows_match(self):
        rows = [
            {"strike": 22000.0, "option_type": "CE", "ltp": 150.0},
            {"strike": 22000.0, "option_type": "PE", "ltp": 80.0},
        ]
        assert get_option_premium("NIFTY", "2026-06-25", 22000.0, "CE", rows) == 150.0
        assert get_option_premium("NIFTY", "2026-06-25", 22000.0, "PE", rows) == 80.0

    def test_live_rows_zero_ltp_returns_zero(self):
        rows = [{"strike": 22000.0, "option_type": "CE", "ltp": 0.0}]
        assert get_option_premium("NIFTY", "2026-06-25", 22000.0, "CE", rows) == 0.0

    def test_live_rows_no_match(self):
        rows = [{"strike": 22100.0, "option_type": "CE", "ltp": 150.0}]
        assert get_option_premium("NIFTY", "2026-06-25", 22000.0, "CE", rows) is None

    def test_db_fallback_fresh_snapshot(self):
        fresh_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        snapshots = [
            {"strike": 22000.0, "option_type": "CE", "ltp": 145.0, "fetched_at": fresh_time}
        ]
        with patch("src.engine.trade_plan.get_latest_snapshots_for_symbol", return_value=snapshots):
            result = get_option_premium("NIFTY", "2026-06-25", 22000.0, "CE", [])
        assert result == 145.0

    def test_db_fallback_stale_snapshot_rejected(self):
        """L2 fix: Snapshots older than 15 minutes must be rejected."""
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        snapshots = [
            {"strike": 22000.0, "option_type": "CE", "ltp": 145.0, "fetched_at": stale_time}
        ]
        with patch("src.engine.trade_plan.get_latest_snapshots_for_symbol", return_value=snapshots):
            result = get_option_premium("NIFTY", "2026-06-25", 22000.0, "CE", [])
        assert result is None

    def test_db_fallback_unparseable_timestamp_rejected(self):
        """L2 fix: Unparseable timestamps should return None (err on caution)."""
        snapshots = [
            {"strike": 22000.0, "option_type": "CE", "ltp": 145.0, "fetched_at": "not-a-date"}
        ]
        with patch("src.engine.trade_plan.get_latest_snapshots_for_symbol", return_value=snapshots):
            result = get_option_premium("NIFTY", "2026-06-25", 22000.0, "CE", [])
        assert result is None

    def test_db_exception_returns_none(self):
        with patch("src.engine.trade_plan.get_latest_snapshots_for_symbol", side_effect=Exception("DB down")):
            result = get_option_premium("NIFTY", "2026-06-25", 22000.0, "CE", [])
        assert result is None


# ── Verdict Parsing ───────────────────────────────────────────────────────

class TestParseVerdictAndConfidence:
    def test_standard_format(self):
        text = "*Verdict: Long Buildup*\nConfidence: 85%"
        v, c = parse_verdict_and_confidence(text)
        assert v == "Long Buildup"
        assert c == 85

    def test_missing_confidence(self):
        text = "*Verdict: Put Writing*"
        v, c = parse_verdict_and_confidence(text)
        assert v == "Put Writing"
        assert c == 0

    def test_empty_text(self):
        v, c = parse_verdict_and_confidence("")
        assert v == ""
        assert c == 0

    def test_none_text(self):
        v, c = parse_verdict_and_confidence(None)
        assert v == ""
        assert c == 0


# ── Premium Conversion ────────────────────────────────────────────────────

class TestConvertUnderlyingSlToPremium:
    def test_fut_passthrough(self):
        sl, tgt = convert_underlying_sl_to_premium(
            22000.0, 21800.0, 22200.0, 22000.0, "BUY", "FUT"
        )
        assert sl == 21800.0
        assert tgt == 22200.0

    def test_buy_option_delta_based(self):
        sl, tgt = convert_underlying_sl_to_premium(
            22000.0, 21800.0, 22200.0, 200.0, "BUY", "CE"
        )
        # default delta = 0.25 (BUG-022)
        # sl = 200.0 - 0.25 * 200 = 150.0
        # tgt = 200.0 + 0.25 * 200 = 250.0
        assert sl == 150.0
        assert tgt == 250.0

    def test_sell_option_delta_based(self):
        sl, tgt = convert_underlying_sl_to_premium(
            22000.0, 22200.0, 21800.0, 200.0, "SELL", "PE"
        )
        # default delta = 0.20 (BUG-022)
        # sl = 200.0 + 0.20 * 200 = 240.0
        # tgt = min(200.0 - 0.20 * 200, 200 * 0.65) = 130.0 (35% time decay target)
        assert sl == 240.0
        assert tgt == 130.0

    def test_buy_option_with_custom_delta(self):
        rows = [{"strike": 22000.0, "option_type": "CE", "delta": 0.6}]
        sl, tgt = convert_underlying_sl_to_premium(
            22000.0, 21800.0, 22200.0, 200.0, "BUY", "CE", strike=22000.0, option_rows=rows
        )
        # custom delta = 0.6
        # sl = 200.0 - 0.6 * 200 = 80.0
        # tgt = 200.0 + 0.6 * 200 = 320.0
        assert sl == 80.0
        assert tgt == 320.0

    def test_zero_underlying_fallback_buy(self):
        sl, tgt = convert_underlying_sl_to_premium(
            0.0, 0.0, 0.0, 200.0, "BUY", "CE"
        )
        assert sl == 140.0   # 200 * 0.70
        assert tgt == 300.0  # 200 * 1.50

    def test_zero_underlying_fallback_sell(self):
        sl, tgt = convert_underlying_sl_to_premium(
            0.0, 0.0, 0.0, 200.0, "SELL", "CE"
        )
        assert sl == 270.0   # 200 * 1.35 (capped at max 1.35x entry premium)
        assert tgt == 130.0  # 200 * 0.65 (35% time decay)



# ── Decision Matrix Config-Driven Stops & Targets (Phase 3) ───────────────────

class TestDecisionMatrixStopsTargets:
    def test_atr_multipliers_from_config(self, monkeypatch):
        import copy
        from config import decision_matrix_loader
        custom_cfg = copy.deepcopy(decision_matrix_loader.DEFAULTS)
        custom_cfg["stops_targets"]["futures"]["sl_atr_mult"] = 2.5
        custom_cfg["stops_targets"]["futures"]["target_atr_mult"] = 4.0
        monkeypatch.setattr("src.engine.trade_plan.load_decision_matrix", lambda sym=None: custom_cfg)

        ctx = {"chart_indicators": {"3h": {"atr_14": 100.0}}}
        sl_buy, tgt_buy = calculate_buy_sl_target(200.0, 22000.0, ctx)
        # SL = 22000 - 2.5 * 100 = 21750
        # Target = 22000 + 4.0 * 100 = 22400
        assert sl_buy == 21750.0
        assert tgt_buy == 22400.0

        sl_sell, tgt_sell = calculate_sell_sl_target(200.0, 22000.0, ctx)
        # SL = 22000 + 2.5 * 100 = 22250
        # Target = 22000 - 4.0 * 100 = 21600
        assert sl_sell == 22250.0
        assert tgt_sell == 21600.0

    def test_breakeven_level(self):
        be_buy = calculate_breakeven_level(22000.0, "BUY", 100.0)
        assert be_buy == 22100.0  # entry + 1.0 * ATR

        be_sell = calculate_breakeven_level(22000.0, "SELL", 100.0)
        assert be_sell == 21900.0  # entry - 1.0 * ATR

    def test_trailing_sl_ratchet(self):
        # Long position: entry 22000, initial SL 21850, ATR 100, BE trigger 22100
        # 1. Price below BE trigger (22050): SL remains 21850
        sl1 = calculate_trailing_sl(22050.0, 22000.0, 21850.0, "BUY", 100.0)
        assert sl1 == 21850.0

        # 2. Price hits BE trigger (22100): SL moves to at least entry 22000
        sl2 = calculate_trailing_sl(22100.0, 22000.0, 21850.0, "BUY", 100.0)
        assert sl2 == 22000.0

        # 3. Price runs to 22300: SL trails to 22300 - 100 = 22200
        sl3 = calculate_trailing_sl(22300.0, 22000.0, sl2, "BUY", 100.0)
        assert sl3 == 22200.0

        # 4. Price pulls back to 22150: SL ratchet maintains highest level (22200)
        sl4 = calculate_trailing_sl(22150.0, 22000.0, sl3, "BUY", 100.0)
        assert sl4 == 22200.0

    def test_option_book_credit_spread_stops_targets(self):
        # Bull Put Spread: 200 pt wing, net credit 50 pts, lot_size 25, 2 lots
        # Max loss points = 200 - 50 = 150 pts
        # Stop loss ratio = 0.50 -> 75 pts (Rs 75 * 25 * 2 = 3750)
        # Normal target = 40% of credit -> 20 pts (Rs 20 * 25 * 2 = 1000)
        res = calculate_option_book_stop_target(
            strategy_type="BULL_PUT_SPREAD",
            net_credit=50.0,
            wing_width=200.0,
            dte=5,
            is_expiry_day=False,
            lot_size=25,
            total_lots=2,
        )
        assert res["stop_loss_points"] == 75.0
        assert res["stop_loss_rupees"] == 3750.0
        assert res["target_profit_pct"] == 0.40
        assert res["target_profit_points"] == 20.0
        assert res["target_profit_rupees"] == 1000.0

    def test_option_book_strangle_stops_targets(self):
        # Weekly Strangle (DTE 4): net credit 100 pts -> 1.0x credit SL (100 pts)
        res_weekly = calculate_option_book_stop_target(
            strategy_type="SHORT_STRANGLE",
            net_credit=100.0,
            dte=4,
            is_expiry_day=False,
            lot_size=50,
            total_lots=1,
        )
        assert res_weekly["stop_loss_points"] == 100.0
        assert res_weekly["stop_loss_rupees"] == 5000.0
        assert res_weekly["target_profit_pct"] == 0.40

        # Monthly Strangle (DTE 25): net credit 100 pts -> 1.5x credit SL (150 pts)
        res_monthly = calculate_option_book_stop_target(
            strategy_type="SHORT_STRANGLE",
            net_credit=100.0,
            dte=25,
            is_expiry_day=False,
            lot_size=50,
            total_lots=1,
        )
        assert res_monthly["stop_loss_points"] == 150.0
        assert res_monthly["stop_loss_rupees"] == 7500.0

    def test_option_book_expiry_day_target(self):
        # Expiry day target is 60% of credit
        res = calculate_option_book_stop_target(
            strategy_type="IRON_CONDOR",
            net_credit=100.0,
            dte=0,
            is_expiry_day=True,
            lot_size=50,
            total_lots=1,
        )
        assert res["target_profit_pct"] == 0.60
        assert res["target_profit_points"] == 60.0
        assert res["target_profit_rupees"] == 3000.0
