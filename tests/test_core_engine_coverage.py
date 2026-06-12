"""
Comprehensive test suite targeting 100% coverage of core engine layers (Layers 2-6):
- regime_detector.py
- entry_quality.py
- risk_engine.py
- trend_analysis.py
- trade_decision.py
"""
import pytest
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from src.models.schema import get_conn, init_db, insert_paper_trade, insert_alert
from src.engine.regime_detector import (
    detect_market_regime,
    regime_score_for_trade,
    REGIME_TRENDING_UP,
    REGIME_TRENDING_DOWN,
    REGIME_RANGE,
    REGIME_VOLATILE,
    REGIME_NO_TRADE,
)
from src.engine.entry_quality import calculate_entry_quality
from src.engine.risk_engine import check_risk_limits
from src.engine.trend_analysis import (
    get_trend_alignment_score,
    detect_reversal_from_scans,
    get_broader_trend_from_alerts,
    check_trend_persistence,
    calculate_momentum_score,
)
from src.engine.trade_decision import make_trade_decision

# Helper to populate scan_summaries
def _insert_scan_summaries(symbol: str, data: list[dict]):
    with get_conn() as conn:
        for i, row in enumerate(data):
            # Ensure fetched_at is sequential to preserve ordering
            fetched_at = (datetime.now(timezone.utc) - timedelta(minutes=10 - i)).isoformat()
            conn.execute(
                """
                INSERT INTO scan_summaries (
                    symbol, fetched_at, underlying, verdict_label, confidence,
                    candle_1h, candle_3h
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    row.get("fetched_at", fetched_at),
                    row.get("underlying", row.get("underlying", 100.0)),
                    row.get("verdict_label", "Sideways"),
                    row.get("confidence", 50),
                    row.get("candle_1h", "NEUTRAL"),
                    row.get("candle_3h", "NEUTRAL"),
                )
            )

# Helper to populate alert history using insert_alert helper
def _insert_alerts(symbol: str, alerts: list[dict]):
    for a in alerts:
        insert_alert({
            "fired_at": a.get("fired_at", datetime.now(timezone.utc).isoformat()),
            "symbol": symbol,
            "alert_type": a.get("alert_type", "BUILDUP_CLASSIFY"),
            "strike": a.get("strike", 100.0),
            "option_type": a.get("option_type", "CE"),
            "expiry": a.get("expiry", "2025-06-26"),
            "detail_json": json.dumps(a.get("detail", {})),
            "telegram_sent": 0,
            "severity": a.get("severity", "MEDIUM"),
            "digest_id": a.get("digest_id"),
        })

# Helper to clear DB tables
def _clear_db():
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_summaries")
        conn.execute("DELETE FROM anomaly_alerts")
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM alert_dedup")

# ─── REGIME DETECTOR TESTS ───

class TestRegimeDetectorDetailed:
    def test_detect_market_regime_insufficient_prices(self):
        _clear_db()
        symbol = "INS_PRICES"
        _insert_scan_summaries(symbol, [
            {"underlying": 100.0, "verdict_label": "Long Buildup"},
            {"underlying": 0.0, "verdict_label": "Long Buildup"}, # filtered out
            {"underlying": None, "verdict_label": "Long Buildup"}, # filtered out
            {"underlying": -5.0, "verdict_label": "Long Buildup"}, # filtered out
        ])
        regime = detect_market_regime(symbol)
        assert regime == REGIME_NO_TRADE

    def test_detect_market_regime_trending_up(self):
        _clear_db()
        symbol = "TREND_UP_SYM"
        data = []
        for i in range(10):
            data.append({
                "underlying": 100.0 if i < 5 else 105.0,
                "verdict_label": "Long Buildup",
            })
        _insert_scan_summaries(symbol, data)
        regime = detect_market_regime(symbol)
        assert regime == REGIME_TRENDING_UP

    def test_detect_market_regime_trending_down(self):
        _clear_db()
        symbol = "TREND_DN_SYM"
        data = []
        for i in range(10):
            data.append({
                "underlying": 100.0 if i < 5 else 94.0,
                "verdict_label": "Short Buildup",
            })
        _insert_scan_summaries(symbol, data)
        regime = detect_market_regime(symbol)
        assert regime == REGIME_TRENDING_DOWN

    def test_detect_market_regime_volatile(self):
        _clear_db()
        symbol = "VOL_SYM"
        data = [
            {"underlying": 100.0, "verdict_label": "Long Buildup"},
            {"underlying": 104.0, "verdict_label": "Short Buildup"},
            {"underlying": 102.0, "verdict_label": "Sideways"},
            {"underlying": 101.0, "verdict_label": "Sideways"},
            {"underlying": 103.0, "verdict_label": "Sideways"},
            {"underlying": 102.0, "verdict_label": "Sideways"},
        ]
        _insert_scan_summaries(symbol, data)
        regime = detect_market_regime(symbol)
        assert regime == REGIME_VOLATILE

    def test_detect_market_regime_range(self):
        _clear_db()
        symbol = "RANGE_SYM"
        data = [
            {"underlying": 100.0, "verdict_label": "Long Buildup"}, # bull
            {"underlying": 100.1, "verdict_label": "Short Buildup"}, # bear
            {"underlying": 100.0, "verdict_label": "Sideways"},
            {"underlying": 100.0, "verdict_label": "Sideways"},
            {"underlying": 100.0, "verdict_label": "Sideways"},
        ]
        _insert_scan_summaries(symbol, data)
        regime = detect_market_regime(symbol)
        assert regime == REGIME_RANGE

    def test_regime_score_for_trade_branches(self):
        assert regime_score_for_trade("UNKNOWN_REGIME", "CE") == 50
        assert regime_score_for_trade(REGIME_TRENDING_UP, "PE") == 70
        assert regime_score_for_trade(REGIME_TRENDING_DOWN, "PE") == 100
        assert regime_score_for_trade(REGIME_RANGE, "CE") == 30
        assert regime_score_for_trade(REGIME_VOLATILE, "CE") == 40


# ─── ENTRY QUALITY TESTS ───

class TestEntryQualityDetailed:
    def test_entry_quality_near_resistance_ce(self):
        score, reasons = calculate_entry_quality(
            "TEST", "CE", 100.0,
            {
                "underlying": 104.5,
                "support": 90.0,
                "resistance": 105.0,
                "price_change_pct": 0.0,
            }
        )
        assert score == 75
        assert any("resistance" in r for r in reasons)

    def test_entry_quality_poor_rr(self):
        score, reasons = calculate_entry_quality(
            "TEST", "PE", 100.0,
            {
                "underlying": 100.0,
                "support": 90.0,
                "resistance": 110.0,
                "sl_underlying": 105.0,
                "target_underlying": 99.0,
                "price_change_pct": 0.0,
            }
        )
        assert score == 75
        assert any("Poor R:R" in r for r in reasons)

    def test_entry_quality_bid_ask_spread(self):
        score, reasons = calculate_entry_quality(
            "TEST", "PE", 100.0,
            {
                "underlying": 100.0,
                "support": 80.0,
                "resistance": 120.0,
                "price_change_pct": 0.0,
                "option_rows": [
                    {"strike": 100.0, "option_type": "PE", "bid": 8.0, "ask": 10.0, "ltp": 9.0}
                ]
            }
        )
        assert score == 80
        assert any("Wide spread" in r for r in reasons)

        score, reasons = calculate_entry_quality(
            "TEST", "PE", 100.0,
            {
                "underlying": 100.0,
                "support": 80.0,
                "resistance": 120.0,
                "price_change_pct": 0.0,
                "option_rows": [
                    {"strike": "invalid", "option_type": "PE", "bid": 8.0, "ask": 10.0, "ltp": 9.0}
                ]
            }
        )
        assert score == 100

    def test_entry_quality_pe_chasing(self):
        score, reasons = calculate_entry_quality(
            "TEST", "PE", 100.0,
            {
                "underlying": 100.0,
                "support": 80.0,
                "resistance": 120.0,
                "price_change_pct": -2.0,
            }
        )
        assert score == 85
        assert any("Chasing" in r for r in reasons)

    def test_entry_quality_low_score_logging(self):
        score, reasons = calculate_entry_quality(
            "TEST", "PE", 100.0,
            {
                "underlying": 91.0,
                "support": 90.0,
                "resistance": 100.0,
                "sl_underlying": 105.0,
                "target_underlying": 90.5,
                "price_change_pct": -2.0,
                "option_rows": [
                    {"strike": 100.0, "option_type": "PE", "bid": 8.0, "ask": 10.0, "ltp": 9.0}
                ]
            }
        )
        assert score == 15
        assert len(reasons) >= 4


# ─── RISK ENGINE TESTS ───

class TestRiskEngineDetailed:
    def test_risk_limit_max_open_symbol(self):
        _clear_db()
        symbol = "TEST_SYM"
        insert_paper_trade({
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "option_type": "CE",
            "entry_underlying": 100.0,
            "status": "OPEN",
        })
        allowed, reason = check_risk_limits(symbol)
        assert not allowed
        assert "Max open trades per symbol" in reason

    def test_risk_limit_max_open_total(self):
        _clear_db()
        for i in range(4):
            insert_paper_trade({
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "symbol": f"SYM_{i}",
                "option_type": "CE",
                "entry_underlying": 100.0,
                "status": "OPEN",
            })
        allowed, reason = check_risk_limits("NEW_SYM")
        assert not allowed
        assert "Max total open trades" in reason

    def test_risk_limit_max_trades_per_day(self):
        _clear_db()
        symbol = "TEST_SYM"
        today_str = datetime.now(timezone.utc).isoformat()
        insert_paper_trade({
            "opened_at": today_str,
            "symbol": symbol,
            "option_type": "CE",
            "entry_underlying": 100.0,
            "status": "CLOSED_SL",
        })
        insert_paper_trade({
            "opened_at": today_str,
            "symbol": symbol,
            "option_type": "CE",
            "entry_underlying": 100.0,
            "status": "CLOSED_TP",
        })
        allowed, reason = check_risk_limits(symbol)
        assert not allowed
        assert "Max trades per day" in reason

    def test_risk_limit_daily_loss(self):
        _clear_db()
        today_str = datetime.now(timezone.utc).isoformat()
        # Insert closed trade with large loss directly using raw SQL
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades (
                    opened_at, closed_at, symbol, option_type, entry_underlying,
                    pnl_rupees, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (today_str, today_str, "TEST_SYM", "CE", 100.0, -250000.0, "CLOSED_TARGET")
            )
        allowed, reason = check_risk_limits("TEST_SYM")
        assert allowed
        assert "Risk checks passed" in reason

    def test_risk_limit_cooldown_active(self):
        _clear_db()
        symbol = "TEST_SYM"
        closed_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        # Insert trade closed 10 mins ago with loss using raw SQL
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades (
                    opened_at, closed_at, symbol, option_type, entry_underlying,
                    pnl_rupees, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (closed_at, closed_at, symbol, "CE", 100.0, -500.0, "CLOSED_SL")
            )
        allowed, reason = check_risk_limits(symbol)
        assert not allowed
        assert "Cooldown active after loss" in reason

        # Test naive datetime string to execute replacement line 80
        _clear_db()
        naive_closed_at = (datetime.now() - timedelta(minutes=10)).isoformat()
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades (
                    opened_at, closed_at, symbol, option_type, entry_underlying,
                    pnl_rupees, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (naive_closed_at, naive_closed_at, symbol, "CE", 100.0, -500.0, "CLOSED_SL")
            )
        allowed, reason = check_risk_limits(symbol)
        assert not allowed
        assert "Cooldown active after loss" in reason

        # Check invalid closed_at handles exception gracefully
        _clear_db()
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades (
                    opened_at, closed_at, symbol, option_type, entry_underlying,
                    pnl_rupees, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("2026-05-20T12:00:00", "invalid-date", symbol, "CE", 100.0, -500.0, "CLOSED_SL")
            )
        allowed, reason = check_risk_limits(symbol)
        assert allowed # Cooldown exception caught, allowed = True


# ─── TREND ANALYSIS TESTS ───

class TestTrendAnalysisDetailed:
    def test_trend_alignment_score_bearish_and_neutral(self):
        _clear_db()
        symbol = "ALIGN_TEST"
        _insert_scan_summaries(symbol, [
            {"verdict_label": "Short Buildup"},
            {"verdict_label": "Call Writing"},
            {"verdict_label": "Sideways"},
        ])
        score = get_trend_alignment_score(symbol, "Short Buildup")
        assert score == 66 # 2/3 bearish

        score = get_trend_alignment_score(symbol, "Sideways")
        assert score == 50

    def test_reversal_broader_trend_scenarios(self):
        _clear_db()
        symbol = "REV_TEST"
        
        # Scenario 1: Broader trend is neutral (4 bull, 4 bear)
        data = [
            {"verdict_label": "Long Buildup"}, # data[0] (bull)
            {"verdict_label": "Long Buildup"}, # data[1] (bull)
            {"verdict_label": "Long Buildup"}, # data[2] (bull)
            {"verdict_label": "Long Buildup"}, # data[3] (bull)
            {"verdict_label": "Short Buildup"}, # data[4] (bear)
            {"verdict_label": "Short Buildup"}, # data[5] (bear)
            {"verdict_label": "Short Buildup"}, # data[6] (bear)
            {"verdict_label": "Short Buildup"}, # data[7] (bear)
            {"verdict_label": "Long Buildup"}, # data[8]
            {"verdict_label": "Long Buildup"}, # data[9]
        ]
        _insert_scan_summaries(symbol, data)
        is_rev, reason = detect_reversal_from_scans(symbol, "Long Buildup", 80)
        assert not is_rev
        assert "neutral" in reason

        # Scenario 2: Broader trend is BEARISH, current verdict is BEARISH
        _clear_db()
        data = [
            {"verdict_label": "Long Buildup"}, # scan 1 (data[0]) (bull)
            {"verdict_label": "Short Buildup"}, # scan 2 (data[1]) (bear)
            {"verdict_label": "Short Buildup"}, # scan 3 (data[2]) (bear)
            {"verdict_label": "Short Buildup"}, # scan 4 (data[3]) (bear)
            {"verdict_label": "Short Buildup"}, # scan 5 (data[4]) (bear)
            {"verdict_label": "Short Buildup"}, # scan 6 (data[5]) (bear)
            {"verdict_label": "Short Buildup"}, # scan 7 (data[6]) (bear)
            {"verdict_label": "Short Buildup"}, # scan 8
            {"verdict_label": "Short Buildup"}, # scan 9
        ]
        _insert_scan_summaries(symbol, data)
        is_rev, reason = detect_reversal_from_scans(symbol, "Short Buildup", 80)
        assert not is_rev
        assert "broader trend is BEARISH" in reason

        # Scenario 3: Non-directional current verdict opposite check
        is_rev, reason = detect_reversal_from_scans(symbol, "Sideways", 80)
        assert not is_rev
        assert "not directional" in reason

        # Scenario 4: Broader trend BEARISH, current verdict BULLISH, but last 2 scans not consistently bullish
        _clear_db()
        data = [
            {"verdict_label": "Short Buildup"}, # data[0] (bear)
            {"verdict_label": "Short Buildup"}, # data[1] (bear)
            {"verdict_label": "Short Buildup"}, # data[2] (bear)
            {"verdict_label": "Short Buildup"}, # data[3] (last 2 - bear)
            {"verdict_label": "Long Buildup"}, # data[4] (last 1 - bull)
        ]
        _insert_scan_summaries(symbol, data)
        is_rev, reason = detect_reversal_from_scans(symbol, "Long Buildup", 80)
        assert not is_rev
        assert "not consistently bullish" in reason

        # Scenario 5: Broader trend BULLISH, current verdict BEARISH, but last 2 scans not consistently bearish
        _clear_db()
        data = [
            {"verdict_label": "Long Buildup"}, # data[0] (bull)
            {"verdict_label": "Long Buildup"}, # data[1] (bull)
            {"verdict_label": "Long Buildup"}, # data[2] (bull)
            {"verdict_label": "Long Buildup"}, # data[3] (last 2 - bull)
            {"verdict_label": "Short Buildup"}, # data[4] (last 1 - bear)
        ]
        _insert_scan_summaries(symbol, data)
        is_rev, reason = detect_reversal_from_scans(symbol, "Short Buildup", 80)
        assert not is_rev
        assert "not consistently bearish" in reason

        # Scenario 6: Broader trend BEARISH, current verdict BULLISH, reversal confirmed!
        _clear_db()
        data = [
            {"verdict_label": "Short Buildup"}, # data[0] (bear)
            {"verdict_label": "Short Buildup"}, # data[1] (bear)
            {"verdict_label": "Short Buildup"}, # data[2] (bear)
            {"verdict_label": "Long Buildup"}, # data[3] (last 2 - bull)
            {"verdict_label": "Long Buildup"}, # data[4] (last 1 - bull)
        ]
        _insert_scan_summaries(symbol, data)
        is_rev, reason = detect_reversal_from_scans(symbol, "Long Buildup", 80)
        assert is_rev
        assert "Reversal confirmed" in reason

    def test_get_broader_trend_from_alerts_branches(self):
        _clear_db()
        symbol = "ALERTS_TREND"

        # 1. No alerts
        assert get_broader_trend_from_alerts(symbol) == "Insufficient history - first scan"

        # 2. Strong Bearish Trend
        alerts = []
        for _ in range(3):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}})
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Unwinding"}})
        for _ in range(2):
            alerts.append({"alert_type": "OI_SPIKE", "option_type": "CE"})
        _insert_alerts(symbol, alerts)
        assert "Strong Bearish" in get_broader_trend_from_alerts(symbol)

        # 3. Mild Bearish
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}})
        alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}})
        _insert_alerts(symbol, alerts)
        assert "Mild Bearish" in get_broader_trend_from_alerts(symbol)

        # 4. Strong Bullish
        _clear_db()
        alerts = []
        for _ in range(3):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}})
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Covering"}})
        for _ in range(2):
            alerts.append({"alert_type": "OI_SPIKE", "option_type": "PE"})
        _insert_alerts(symbol, alerts)
        assert "Strong Bullish" in get_broader_trend_from_alerts(symbol)

        # 5. Mild Bullish
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}})
        alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}})
        _insert_alerts(symbol, alerts)
        assert "Mild Bullish" in get_broader_trend_from_alerts(symbol)

        # 6. High Activity
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "VOLUME_AGGRESSION", "option_type": "CE"})
            alerts.append({"alert_type": "VOLUME_AGGRESSION", "option_type": "PE"})
        _insert_alerts(symbol, alerts)
        assert "High Activity" in get_broader_trend_from_alerts(symbol)

        # 7. Rangebound
        _clear_db()
        alerts = []
        for _ in range(4):
            alerts.append({"alert_type": "OI_SPIKE", "option_type": "CE"})
            alerts.append({"alert_type": "OI_SPIKE", "option_type": "PE"})
        _insert_alerts(symbol, alerts)
        assert "Rangebound" in get_broader_trend_from_alerts(symbol)

        # 8. Low Activity
        _clear_db()
        alerts = [
            {"alert_type": "OI_SPIKE", "option_type": "CE"},
        ]
        _insert_alerts(symbol, alerts)
        assert "Low Activity" in get_broader_trend_from_alerts(symbol)

        # 9. Mixed
        _clear_db()
        alerts = [
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}},
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}},
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}},
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}},
        ]
        _insert_alerts(symbol, alerts)
        assert "Mixed" in get_broader_trend_from_alerts(symbol)

    def test_check_trend_persistence_branches(self):
        _clear_db()
        symbol = "PERSIST_TEST"

        # 1. Non-directional current_verdict
        alerts = [{"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}}]
        _insert_alerts(symbol, alerts)
        ok, reason = check_trend_persistence(symbol, "Sideways", 80, {})
        assert not ok
        assert "not directional" in reason

        # 2. Bullish current_verdict, broader trend BEARISH
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}})
        _insert_alerts(symbol, alerts)
        ok, reason = check_trend_persistence(symbol, "Long Buildup", 80, {})
        assert not ok
        assert "Broader trend not aligned" in reason

        # 3. Bearish current_verdict, broader trend BULLISH
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}})
        _insert_alerts(symbol, alerts)
        ok, reason = check_trend_persistence(symbol, "Short Buildup", 80, {})
        assert not ok
        assert "Broader trend not aligned" in reason

        # 4. Insufficient scan history
        _clear_db()
        alerts = [{"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}}]
        _insert_alerts(symbol, alerts)
        ok, reason = check_trend_persistence(symbol, "Long Buildup", 80, {})
        assert not ok
        assert "Insufficient scan history" in reason

        # 5. Inconsistent bias (only 0/3 scans bullish)
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}})
        _insert_alerts(symbol, alerts)
        _insert_scan_summaries(symbol, [
            {"verdict_label": "Short Buildup"},
            {"verdict_label": "Short Buildup"},
            {"verdict_label": "Sideways"},
        ])
        ok, reason = check_trend_persistence(symbol, "Long Buildup", 80, {})
        assert not ok
        assert "Inconsistent bias" in reason

        # 6. Inconsistent bias for Bearish verdict (0/3 scans bearish)
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}})
        _insert_alerts(symbol, alerts)
        _insert_scan_summaries(symbol, [
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Sideways"},
        ])
        ok, reason = check_trend_persistence(symbol, "Short Buildup", 80, {})
        assert not ok
        assert "Inconsistent bias" in reason

        # 7. Chart conflict check
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}})
        _insert_alerts(symbol, alerts)
        _insert_scan_summaries(symbol, [
            {"verdict_label": "Short Buildup"},
            {"verdict_label": "Short Buildup"},
            {"verdict_label": "Short Buildup"},
        ])
        ok, reason = check_trend_persistence(symbol, "Short Buildup", 80, {"chart_conflict": True})
        assert not ok
        assert "chart conflict" in reason

    def test_calculate_momentum_score_branches(self):
        _clear_db()
        symbol = "MOMENTUM_TEST"

        # 1. Bearish trend & check broader trend weights
        alerts = []
        for _ in range(10):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}})
        _insert_alerts(symbol, alerts)
        _insert_scan_summaries(symbol, [
            {"verdict_label": "Short Buildup"},
            {"verdict_label": "Short Buildup"},
            {"verdict_label": "Short Buildup"},
        ])

        ctx = {"chart_indicators": {"1h": {"sentiment": "BEARISH"}, "3h": {"sentiment": "BEARISH"}}}
        score = calculate_momentum_score(symbol, "Short Buildup", 90, ctx)
        assert score == 96

        # 2. Bullish current verdict + Strong Bullish trend + CE 1h chart only
        _clear_db()
        alerts = []
        for _ in range(10):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}})
        _insert_alerts(symbol, alerts)
        _insert_scan_summaries(symbol, [
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
        ])
        ctx = {"chart_indicators": {"1h": {"sentiment": "BULLISH"}, "3h": {"sentiment": "BEARISH"}}}
        score = calculate_momentum_score(symbol, "Long Buildup", 80, ctx)
        assert score == 87

        # 3. Mild Bullish trend & Mix/Rangebound
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}})
        alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}})
        _insert_alerts(symbol, alerts)
        score = calculate_momentum_score(symbol, "Long Buildup", 80, {})
        assert score == 52

        # Mixed trend
        _clear_db()
        alerts = [
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}},
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}},
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}},
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}},
        ]
        _insert_alerts(symbol, alerts)
        score = calculate_momentum_score(symbol, "Long Buildup", 80, {})
        assert score == 42


# ─── TRADE DECISION TESTS ───

class TestTradeDecisionDetailed:
    def test_decision_no_valid_plan(self):
        _clear_db()
        intel = {"verdict_label": "Long Buildup", "confidence": 80}
        ctx = {"underlying": 100.0} # no option strikes matching plan etc.
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=None):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "BLOCKED"
        assert "No valid trade plan" in decision["reason"]

    def test_decision_no_trade_regime_insufficient_history(self):
        _clear_db()
        intel = {"verdict_label": "Long Buildup", "confidence": 60}
        ctx = {"underlying": 100.0, "support": 90.0, "resistance": 110.0}
        plan = {"option_type": "CE", "strike": 100.0, "sl_underlying": 90.0, "target_underlying": 110.0}
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_NO_TRADE), \
             patch("src.engine.trade_decision.PAPER_RESEARCH_MODE", True):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "BLOCKED"
        assert "Insufficient scan history" in decision["reason"]

    def test_decision_mode_conservative_success_and_fail(self):
        _clear_db()
        intel = {"verdict_label": "Long Buildup", "confidence": 80}
        ctx = {"underlying": 100.0, "support": 90.0, "resistance": 110.0}
        plan = {"option_type": "CE", "strike": 100.0, "sl_underlying": 90.0, "target_underlying": 110.0}
        
        # 1. Conservative filter fails (trend persistence = False)
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "conservative"), \
             patch("src.engine.trade_decision.check_trend_persistence", return_value=(False, "Failed persistence")):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "BLOCKED"
        assert "Conservative filter" in decision["reason"]

        # 2. Conservative filter passes, but entry quality low
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "conservative"), \
             patch("src.engine.trade_decision.check_trend_persistence", return_value=(True, "Passed persistence")), \
             patch("src.engine.trade_decision.calculate_entry_quality", return_value=(30, ["bad EQ"])):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "BLOCKED"
        assert "Entry quality" in decision["reason"]

        # 3. Conservative success
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "conservative"), \
             patch("src.engine.trade_decision.check_trend_persistence", return_value=(True, "Passed persistence")), \
             patch("src.engine.trade_decision.calculate_entry_quality", return_value=(80, [])):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "TRIGGERED_CORE"
        assert decision["setup_type"] == "TREND_CONTINUATION"

    def test_decision_mode_balanced(self):
        _clear_db()
        intel = {"verdict_label": "Long Buildup", "confidence": 80}
        ctx = {"underlying": 100.0, "support": 90.0, "resistance": 110.0}
        plan = {"option_type": "CE", "strike": 100.0, "sl_underlying": 90.0, "target_underlying": 110.0}

        # 1. Momentum score low
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "balanced"), \
             patch("src.engine.trade_decision.calculate_momentum_score", return_value=50):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "BLOCKED"
        assert "Momentum score too low" in decision["reason"]

        # 2. Momentum high, but entry quality low
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "balanced"), \
             patch("src.engine.trade_decision.calculate_momentum_score", return_value=85), \
             patch("src.engine.trade_decision.calculate_entry_quality", return_value=(30, ["bad EQ"])):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "BLOCKED"
        assert "entry quality low" in decision["reason"]

        # 3. Balanced success
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "balanced"), \
             patch("src.engine.trade_decision.calculate_momentum_score", return_value=85), \
             patch("src.engine.trade_decision.calculate_entry_quality", return_value=(80, [])):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "TRIGGERED_CORE"
        assert decision["setup_type"] == "MOMENTUM_TRADE"

    def test_decision_mode_aggressive(self):
        _clear_db()
        intel = {"verdict_label": "Long Buildup", "confidence": 80}
        ctx = {"underlying": 100.0, "support": 90.0, "resistance": 110.0}
        plan = {"option_type": "CE", "strike": 100.0, "sl_underlying": 90.0, "target_underlying": 110.0}

        # 1. No reversal
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "aggressive"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(False, "No reversal")):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "BLOCKED"
        assert "No reversal detected" in decision["reason"]

        # 2. Aggressive success
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "aggressive"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(True, "Reversal!")), \
             patch("src.engine.trade_decision.calculate_entry_quality", return_value=(80, [])):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "TRIGGERED_CORE"
        assert decision["setup_type"] == "CONFIRMED_REVERSAL"

    def test_decision_mode_hybrid_all_branches(self):
        _clear_db()
        intel = {"verdict_label": "Long Buildup", "confidence": 80}
        ctx = {"underlying": 100.0, "support": 90.0, "resistance": 110.0}
        plan = {"option_type": "CE", "strike": 100.0, "sl_underlying": 90.0, "target_underlying": 110.0}

        # Hybrid Priority 1: Reversal
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "hybrid"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(True, "Reversal Confirmed")):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "TRIGGERED_CORE"
        assert decision["setup_type"] == "CONFIRMED_REVERSAL"

        # Hybrid Priority 2: Persistence
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "hybrid"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(False, "No reversal")), \
             patch("src.engine.trade_decision.check_trend_persistence", return_value=(True, "Persistent")):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "TRIGGERED_CORE"
        assert decision["setup_type"] == "TREND_CONTINUATION"

        # Hybrid Priority 3: Momentum
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "hybrid"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(False, "No reversal")), \
             patch("src.engine.trade_decision.check_trend_persistence", return_value=(False, "No persistence")), \
             patch("src.engine.trade_decision.calculate_momentum_score", return_value=85):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "TRIGGERED_CORE"
        assert decision["setup_type"] == "MOMENTUM_TRADE"

        # Hybrid Priority 4: Experimental (research mode only)
        intel_exp = {"verdict_label": "Long Buildup", "confidence": 55}
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "hybrid"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(False, "No reversal")), \
             patch("src.engine.trade_decision.check_trend_persistence", return_value=(False, "No persistence")), \
             patch("src.engine.trade_decision.calculate_momentum_score", return_value=50), \
             patch("src.engine.trade_decision.calculate_entry_quality", return_value=(45, ["minor warning"])):
            decision = make_trade_decision("TEST", intel_exp, ctx)
        assert decision["status"] == "TRIGGERED_EXPERIMENTAL"
        assert decision["setup_type"] == "EXPERIMENTAL_SETUP"

        # Hybrid Priority 5: Blocked
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "hybrid"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(False, "No reversal")), \
             patch("src.engine.trade_decision.check_trend_persistence", return_value=(False, "No persistence")), \
             patch("src.engine.trade_decision.calculate_momentum_score", return_value=50), \
             patch("src.engine.trade_decision.calculate_entry_quality", return_value=(30, ["major issue"])):
            decision = make_trade_decision("TEST", intel_exp, ctx)
        assert decision["status"] == "BLOCKED"

    def test_decision_mode_legacy(self):
        _clear_db()
        intel = {"verdict_label": "Long Buildup", "confidence": 80}
        ctx = {"underlying": 100.0, "support": 90.0, "resistance": 110.0}
        plan = {"option_type": "CE", "strike": 100.0, "sl_underlying": 90.0, "target_underlying": 110.0}

        # Legacy logic, trend filter mode = unknown/legacy
        # 1. Reversal trigger
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "legacy_fallback"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(True, "Reversal")):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "TRIGGERED_CORE"
        assert decision["setup_type"] == "CONFIRMED_REVERSAL"

        # 2. Trend continuation trigger
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "legacy_fallback"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(False, "No reversal")), \
             patch("src.engine.trade_decision.get_trend_alignment_score", return_value=80):
            decision = make_trade_decision("TEST", intel, ctx)
        assert decision["status"] == "TRIGGERED_CORE"
        assert decision["setup_type"] == "TREND_CONTINUATION"

        # 3. Blocked
        intel_low = {"verdict_label": "Long Buildup", "confidence": 40}
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "legacy_fallback"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(False, "No reversal")), \
             patch("src.engine.trade_decision.get_trend_alignment_score", return_value=40):
            decision = make_trade_decision("TEST", intel_low, ctx)
        assert decision["status"] == "BLOCKED"
        assert "Low confidence" in decision["reason"]


class TestCoreEngineUltraCoverage:
    def test_ultra_coverage_entry_quality(self):
        # Line 35: underlying price <= 0
        score, reasons = calculate_entry_quality("TEST", "CE", 100.0, {"underlying": 0.0})
        assert score == 0
        assert "Missing underlying" in reasons[0]

        # Line 89-90: CE option chasing after +1.5% rally
        score, reasons = calculate_entry_quality("TEST", "CE", 100.0, {
            "underlying": 100.0,
            "support": 80.0,
            "resistance": 120.0,
            "price_change_pct": 2.0
        })
        assert score == 85
        assert any("Chasing" in r for r in reasons)

    def test_ultra_coverage_regime_detector(self):
        # Line 47: len(prices) < 5 returns REGIME_NO_TRADE
        _clear_db()
        _insert_scan_summaries("ULTRA_REG", [
            {"underlying": 100.0, "verdict_label": "Long Buildup"},
            {"underlying": 0.0, "verdict_label": "Long Buildup"},
            {"underlying": 0.0, "verdict_label": "Long Buildup"},
            {"underlying": 0.0, "verdict_label": "Long Buildup"},
            {"underlying": 0.0, "verdict_label": "Long Buildup"},
        ])
        assert detect_market_regime("ULTRA_REG") == REGIME_NO_TRADE

        # Line 71: detect_market_regime returns REGIME_NO_TRADE in default fallback case (prices >= 5, but no condition matches)
        _clear_db()
        data = [
            {"underlying": 100.0, "verdict_label": "Long Buildup"},
            {"underlying": 100.0, "verdict_label": "Long Buildup"},
            {"underlying": 100.0, "verdict_label": "Long Buildup"},
            {"underlying": 100.0, "verdict_label": "Sideways"},
            {"underlying": 100.0, "verdict_label": "Sideways"},
        ]
        _insert_scan_summaries("ULTRA_REG", data)
        assert detect_market_regime("ULTRA_REG") == REGIME_NO_TRADE

    def test_ultra_coverage_trade_decision(self):
        # Line 58: make_trade_decision underlying <= 0
        decision = make_trade_decision("TEST", {"verdict_label": "Long Buildup"}, {"underlying": 0.0})
        assert decision["status"] == "BLOCKED"

        # Line 61: make_trade_decision non-directional verdict
        decision = make_trade_decision("TEST", {"verdict_label": "Sideways"}, {"underlying": 100.0})
        assert decision["status"] == "BLOCKED"

        # Line 81-82: make_trade_decision REGIME_NO_TRADE tags INSUFFICIENT_REGIME_HISTORY
        plan = {"option_type": "CE", "strike": 100.0, "sl_underlying": 90.0, "target_underlying": 110.0}
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_NO_TRADE), \
             patch("src.engine.trade_decision.PAPER_RESEARCH_MODE", True):
            decision = make_trade_decision("TEST", {"verdict_label": "Long Buildup", "confidence": 75}, {"underlying": 100.0})
            assert "INSUFFICIENT_REGIME_HISTORY" in decision["soft_conflicts"]

        # Line 97: make_trade_decision hard block on chart_conflict
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP):
            decision = make_trade_decision("TEST", {"verdict_label": "Long Buildup", "confidence": 75, "chart_conflict": True}, {"underlying": 100.0})
            assert decision["status"] == "BLOCKED"
            assert "Timeframe conflict" in decision["reason"]

        # Line 198-204, 210, 214: legacy trade_decision branches
        with patch("src.engine.paper_plan.build_paper_trade_plan", return_value=plan), \
             patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_TRENDING_UP), \
             patch("src.engine.trade_decision.TREND_FILTER_MODE", "legacy_fallback"), \
             patch("src.engine.trade_decision.detect_reversal_from_scans", return_value=(False, "No reversal")), \
             patch("src.engine.trade_decision.calculate_entry_quality", return_value=(80, ["some entry reason"])), \
             patch("src.engine.trade_decision.get_trend_alignment_score", return_value=40):
            decision = make_trade_decision("TEST", {"verdict_label": "Long Buildup", "confidence": 55}, {"underlying": 100.0})
            assert decision["status"] == "TRIGGERED_EXPERIMENTAL"
            assert "some entry reason" in decision["reason"]

            # Low confidence + Poor entry quality (score 30) + Low trend alignment (40) + Unfavorable regime (score 30) -> blocked
            with patch("src.engine.trade_decision.detect_market_regime", return_value=REGIME_RANGE), \
                 patch("src.engine.trade_decision.calculate_entry_quality", return_value=(30, ["bad EQ"])):
                decision = make_trade_decision("TEST", {"verdict_label": "Long Buildup", "confidence": 40}, {"underlying": 100.0})
                assert decision["status"] == "BLOCKED"
                assert "Low confidence" in decision["reason"]
                assert "Poor entry quality" in decision["reason"]
                assert "Trend not aligned" in decision["reason"]
                assert "Unfavorable regime" in decision["reason"]

    def test_ultra_coverage_trend_analysis(self):
        # Line 39: get_trend_alignment_score bullish verdict aligned sum
        _clear_db()
        _insert_scan_summaries("ULTRA_TREND", [
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Put Writing"},
            {"verdict_label": "Sideways"},
        ])
        assert get_trend_alignment_score("ULTRA_TREND", "Long Buildup") == 66

        # Line 67: detect_reversal_from_scans confidence < 75
        is_rev, reason = detect_reversal_from_scans("ULTRA_TREND", "Long Buildup", 70)
        assert not is_rev
        assert "Confidence too low" in reason

        # Line 82: detect_reversal_from_scans len(rows) < 3
        _clear_db()
        _insert_scan_summaries("ULTRA_TREND", [
            {"verdict_label": "Long Buildup"},
        ])
        is_rev, reason = detect_reversal_from_scans("ULTRA_TREND", "Long Buildup", 80)
        assert not is_rev
        assert "Insufficient" in reason

        # Line 98: detect_reversal_from_scans is_bullish but broader_trend is not BEARISH
        _clear_db()
        _insert_scan_summaries("ULTRA_TREND", [
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
        ])
        is_rev, reason = detect_reversal_from_scans("ULTRA_TREND", "Long Buildup", 80)
        assert not is_rev
        assert "broader trend is BULLISH" in reason

        # Line 152-153: get_broader_trend_from_alerts exception block (invalid JSON)
        _clear_db()
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO anomaly_alerts (
                    fired_at, symbol, alert_type, strike, option_type, expiry,
                    detail_json, severity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("2026-05-20T12:00:00", "ULTRA_TREND", "BUILDUP_CLASSIFY", 100.0, "CE", "2025-06-26", "{invalid-json}", "MEDIUM")
            )
        assert get_broader_trend_from_alerts("ULTRA_TREND") == "⚪ Low Activity — insufficient signals for trend"

        # Line 177-181: ATM_LEG_MOVE processing in get_broader_trend_from_alerts
        _clear_db()
        alerts = [
            {"alert_type": "ATM_LEG_MOVE", "detail": {"bias": "Bullish"}},
            {"alert_type": "ATM_LEG_MOVE", "detail": {"bias": "Bullish"}},
            {"alert_type": "ATM_LEG_MOVE", "detail": {"bias": "Bearish"}},
        ]
        _insert_alerts("ULTRA_TREND", alerts)
        assert get_broader_trend_from_alerts("ULTRA_TREND") == "⚪ Mixed — no dominant trend yet"

        # Line 228: check_trend_persistence confidence < 70
        ok, reason = check_trend_persistence("ULTRA_TREND", "Long Buildup", 65, {})
        assert not ok
        assert "Confidence too low" in reason

        # Line 272: check_trend_persistence success
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}})
        _insert_alerts("ULTRA_TREND", alerts)
        _insert_scan_summaries("ULTRA_TREND", [
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
        ])
        ok, reason = check_trend_persistence("ULTRA_TREND", "Long Buildup", 80, {"chart_conflict": False})
        assert ok
        assert "persistence filters passed" in reason

        # Line 311-314: calculate_momentum_score mild bearish/mixed/rangebound in trend
        _clear_db()
        alerts = []
        for _ in range(5):
            alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}})
        alerts.append({"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}})
        _insert_alerts("ULTRA_TREND", alerts)
        score = calculate_momentum_score("ULTRA_TREND", "Short Buildup", 80, {})
        assert score == 52

        # Mixed broader trend
        _clear_db()
        alerts = [
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}},
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Long Buildup"}},
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}},
            {"alert_type": "BUILDUP_CLASSIFY", "detail": {"buildup_type": "Short Buildup"}},
        ]
        _insert_alerts("ULTRA_TREND", alerts)
        score = calculate_momentum_score("ULTRA_TREND", "Short Buildup", 80, {})
        assert score == 42

        # Line 353: chart confluence BULLISH / BULLISH
        _clear_db()
        _insert_scan_summaries("ULTRA_TREND", [
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
            {"verdict_label": "Long Buildup"},
        ])
        ctx = {"chart_indicators": {"1h": {"sentiment": "BULLISH"}, "3h": {"sentiment": "BULLISH"}}}
        score = calculate_momentum_score("ULTRA_TREND", "Long Buildup", 80, ctx)
        assert score == 62

        # Line 359-360: chart confluence BEARISH / BEARISH
        _clear_db()
        _insert_scan_summaries("ULTRA_TREND", [
            {"verdict_label": "Short Buildup"},
            {"verdict_label": "Short Buildup"},
            {"verdict_label": "Short Buildup"},
        ])
        ctx = {"chart_indicators": {"1h": {"sentiment": "BEARISH"}, "3h": {"sentiment": "BULLISH"}}}
        score = calculate_momentum_score("ULTRA_TREND", "Short Buildup", 80, ctx)
        assert score == 57


class TestScanSummaryDetailed:
    def test_save_scan_summary_success(self):
        from src.engine.scan_summary import save_scan_summary
        _clear_db()
        ctx = {
            "expiry": "2026-06-04",
            "underlying": 100.0,
            "atm_strike": 100.0,
            "total_ce_oi": 1000,
            "total_pe_oi": 1200,
            "ce_oi_change": 50,
            "pe_oi_change": 80,
            "pcr": 1.2,
            "max_pain": 100.0,
            "support": 95.0,
            "resistance": 105.0,
            "chart_indicators": {
                "TEST_SYM": {
                    "1h": {"sentiment": "BULLISH"},
                    "3h": {"sentiment": "BULLISH"}
                }
            }
        }
        alerts = [
            {"alert_type": "OI_SPIKE", "strike": 100.0, "option_type": "CE", "severity": "HIGH", "detail_json": '{"pct_change": 25.0}'}
        ]
        intel = {"verdict_label": "Long Buildup", "confidence": 85}
        save_scan_summary("TEST_SYM", ctx, alerts, intel, "digest-123", "2026-05-20T12:00:00")
        
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM scan_summaries WHERE symbol='TEST_SYM'").fetchone()
            assert row is not None
            assert row["verdict_label"] == "Long Buildup"
            assert row["candle_1h"] == "BULLISH"
            assert row["candle_3h"] == "BULLISH"
            assert row["top_signal_oi_pct"] == 25.0

    def test_save_scan_summary_db_exception(self):
        from src.engine.scan_summary import save_scan_summary
        ctx = {"underlying": 100.0}
        intel = {"verdict_label": "Long Buildup"}
        with patch("src.engine.scan_summary._db_insert_scan_summary") as mock_insert:
            mock_insert.side_effect = Exception("Simulated DB Error")
            save_scan_summary("TEST_SYM", ctx, [], intel, "digest-123", "2026-05-20T12:00:00")

    def test_save_scan_summary_json_exceptions(self):
        from src.engine.scan_summary import save_scan_summary
        _clear_db()
        ctx = {"underlying": 100.0}
        intel = {"verdict_label": "Long Buildup"}
        alerts = [
            {"alert_type": "OI_SPIKE", "strike": 100.0, "option_type": "CE", "severity": "HIGH", "detail_json": "{invalid json}"}
        ]
        save_scan_summary("TEST_SYM", ctx, alerts, intel, "digest-123", "2026-05-20T12:00:00")
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM scan_summaries WHERE symbol='TEST_SYM'").fetchone()
            assert row is not None
            assert row["top_signal_oi_pct"] == 0.0


class TestPaperTradesFUTPNL:
    def test_close_paper_trade_futures_bearish(self):
        from src.models.schema import close_paper_trade
        _clear_db()
        trade_id = insert_paper_trade({
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "symbol": "NATURALGAS",
            "option_type": "FUT",
            "verdict_label": "Long Unwinding",
            "entry_underlying": 317.7,
            "entry_premium": 317.7,
            "lots": 10,
            "strike": 320.0,
            "status": "OPEN",
        })
        close_paper_trade(
            trade_id=trade_id,
            closed_at=datetime.now(timezone.utc).isoformat(),
            exit_underlying=299.9,
            exit_premium=299.9,
            status="CLOSED_TARGET",
            reason="target hit",
        )
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            assert row["pnl_points"] == pytest.approx(17.8)
            assert row["pnl_rupees"] == pytest.approx(222500.0)

    def test_close_paper_trade_futures_bullish(self):
        from src.models.schema import close_paper_trade
        _clear_db()
        trade_id = insert_paper_trade({
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "symbol": "NATURALGAS",
            "option_type": "FUT",
            "verdict_label": "Long Buildup",
            "entry_underlying": 312.1,
            "entry_premium": 312.1,
            "lots": 10,
            "strike": 310.0,
            "status": "OPEN",
        })
        close_paper_trade(
            trade_id=trade_id,
            closed_at=datetime.now(timezone.utc).isoformat(),
            exit_underlying=321.6,
            exit_premium=321.6,
            status="CLOSED_TARGET",
            reason="target hit",
        )
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            assert row["pnl_points"] == pytest.approx(9.5)
            assert row["pnl_rupees"] == pytest.approx(118750.0)


class TestSellOptionTrades:
    def test_close_paper_trade_sell_option_pnl(self):
        from src.models.schema import close_paper_trade
        _clear_db()
        trade_id = insert_paper_trade({
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "symbol": "NIFTY",
            "option_type": "CE",
            "side": "SELL",
            "verdict_label": "Call Writing",
            "entry_underlying": 22000.0,
            "entry_premium": 100.0,
            "lots": 1,
            "strike": 22100.0,
            "status": "OPEN",
        })
        close_paper_trade(
            trade_id=trade_id,
            closed_at=datetime.now(timezone.utc).isoformat(),
            exit_underlying=22050.0,
            exit_premium=60.0,
            status="CLOSED_TARGET",
            reason="target hit",
        )
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            # P&L should be entry_premium (100) - exit_premium (60) = 40 points
            assert row["pnl_points"] == pytest.approx(40.0)
            from config.settings import LOT_SIZES
            lot_size = LOT_SIZES.get("NIFTY", 25)
            assert row["pnl_rupees"] == pytest.approx(40.0 * lot_size)

    def test_paper_plan_sell_verdicts(self):
        from src.engine.paper_plan import build_paper_trade_plan
        # 1. Put Writing (BULLISH, SELL PE)
        ctx = {
            "symbol": "NIFTY",
            "underlying": 22000.0,
            "atm_strike": 22000.0,
            "support": 21900.0,
            "resistance": 22100.0,
        }
        plan = build_paper_trade_plan("Put Writing", 80, ctx)
        assert plan is not None
        assert plan["side"] == "SELL"
        assert plan["option_type"] == "PE"
        assert plan["strike"] == 21900.0   # Support strike for Put Writing OTM

        # 2. Call Writing (BEARISH, SELL CE)
        plan = build_paper_trade_plan("Call Writing", 80, ctx)
        assert plan is not None
        assert plan["side"] == "SELL"
        assert plan["option_type"] == "CE"
        assert plan["strike"] == 22100.0   # Resistance strike for Call Writing OTM

    def test_sell_option_sl_target_logic(self):
        from src.engine.paper_trading import _calculate_sell_sl_target, _calculate_buy_sl_target
        buy_sl, buy_tgt = _calculate_buy_sl_target(100.0)
        assert buy_sl == 70.0
        assert buy_tgt == 150.0

        sell_sl, sell_tgt = _calculate_sell_sl_target(100.0)
        assert sell_sl == 150.0
        assert sell_tgt == 60.0




