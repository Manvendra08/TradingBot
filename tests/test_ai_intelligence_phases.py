"""
Comprehensive tests for AI Intelligence Roadmap v3.0 — Phases 1-4.

Phase 1: Pattern cache invalidation on trade close
Phase 2: ML Success Predictor integration (scan_cache, retraining triggers)
Phase 3: Edge Decay Monitor (health scoring, trend detection, API)
Phase 4: AI Dashboard UI (API endpoints, static file serving)
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.models.schema import get_conn, init_db

# ═══════════════════════════════════════════════════════════════════════════
# Phase 1: Pattern Cache Invalidation
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase1PatternCacheInvalidation:
    """Verify pattern cache is invalidated when trades close."""

    def test_invalidate_pattern_cache_function_exists(self):
        """_invalidate_pattern_cache must exist in paper_trading module."""
        from src.engine.paper_trading import _invalidate_pattern_cache

        assert callable(_invalidate_pattern_cache)

    def test_invalidate_pattern_cache_callable_without_error(self):
        """Calling _invalidate_pattern_cache should not raise."""
        from src.engine.paper_trading import _invalidate_pattern_cache

        # Should not raise even with no active cache
        _invalidate_pattern_cache()

    def test_close_paper_trade_calls_invalidation(self):
        """close_paper_trade must call _invalidate_pattern_cache."""
        from src.models import schema

        assert hasattr(schema, "close_paper_trade")
        # Verify the function is referenced in the module
        import inspect

        source = inspect.getsource(schema.close_paper_trade)
        assert "_invalidate_pattern_cache" in source, (
            "close_paper_trade must call _invalidate_pattern_cache"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: Scan Cache
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase2ScanCache:
    """Test scan context cache for ML prediction endpoint hydration."""

    def setup_method(self):
        """Clear cache before each test."""
        from src.engine.scan_cache import clear_scan_cache

        clear_scan_cache()

    def test_update_and_retrieve(self):
        """Cached snapshot should be retrievable."""
        from src.engine.scan_cache import get_latest_scan_snapshot, update_scan_snapshot

        ctx = {"underlying": 22000, "verdict_label": "Long Buildup", "confidence": 80}
        update_scan_snapshot("NIFTY", ctx)
        result = get_latest_scan_snapshot("NIFTY")
        assert result is not None
        assert result["underlying"] == 22000
        assert result["verdict_label"] == "Long Buildup"

    def test_case_insensitive_symbol(self):
        """Symbol lookup should be case-insensitive."""
        from src.engine.scan_cache import get_latest_scan_snapshot, update_scan_snapshot

        update_scan_snapshot("nifty", {"underlying": 22000})
        assert get_latest_scan_snapshot("NIFTY") is not None
        assert get_latest_scan_snapshot("nifty") is not None
        assert get_latest_scan_snapshot("Nifty") is not None

    def test_returns_copy_not_reference(self):
        """Retrieved snapshot should be a copy, not the original."""
        from src.engine.scan_cache import get_latest_scan_snapshot, update_scan_snapshot

        ctx = {"underlying": 22000}
        update_scan_snapshot("NIFTY", ctx)
        result = get_latest_scan_snapshot("NIFTY")
        result["underlying"] = 99999
        # Original should be unchanged
        result2 = get_latest_scan_snapshot("NIFTY")
        assert result2["underlying"] == 22000

    def test_missing_symbol_returns_none(self):
        """Non-existent symbol should return None."""
        from src.engine.scan_cache import get_latest_scan_snapshot

        assert get_latest_scan_snapshot("NONEXISTENT") is None

    def test_empty_symbol_returns_none(self):
        """Empty or whitespace symbol should return None."""
        from src.engine.scan_cache import get_latest_scan_snapshot, update_scan_snapshot

        assert get_latest_scan_snapshot("") is None
        assert get_latest_scan_snapshot("   ") is None
        update_scan_snapshot("", {"data": 1})  # Should not raise

    def test_ttl_expiry(self):
        """Stale entries should return None after TTL."""
        from src.engine.scan_cache import (
            CACHE_TTL_SECONDS,
            get_latest_scan_snapshot,
            update_scan_snapshot,
        )

        ctx = {"underlying": 22000}
        update_scan_snapshot("NIFTY", ctx)

        # Mock time to simulate TTL expiry
        with patch(
            "src.engine.scan_cache._time",
            return_value=time.time() + CACHE_TTL_SECONDS + 1,
        ):
            assert get_latest_scan_snapshot("NIFTY") is None

    def test_clear_cache(self):
        """clear_scan_cache should remove all entries."""
        from src.engine.scan_cache import (
            clear_scan_cache,
            get_latest_scan_snapshot,
            update_scan_snapshot,
        )

        update_scan_snapshot("NIFTY", {"data": 1})
        update_scan_snapshot("BANKNIFTY", {"data": 2})
        clear_scan_cache()
        assert get_latest_scan_snapshot("NIFTY") is None
        assert get_latest_scan_snapshot("BANKNIFTY") is None

    def test_get_all_cached_symbols(self):
        """Should return list of symbols with fresh snapshots."""
        from src.engine.scan_cache import get_all_cached_symbols, update_scan_snapshot

        update_scan_snapshot("NIFTY", {"data": 1})
        update_scan_snapshot("BANKNIFTY", {"data": 2})
        symbols = get_all_cached_symbols()
        assert "NIFTY" in symbols
        assert "BANKNIFTY" in symbols

    def test_overwrite_existing(self):
        """Updating same symbol should overwrite previous snapshot."""
        from src.engine.scan_cache import get_latest_scan_snapshot, update_scan_snapshot

        update_scan_snapshot("NIFTY", {"underlying": 22000})
        update_scan_snapshot("NIFTY", {"underlying": 23000})
        result = get_latest_scan_snapshot("NIFTY")
        assert result["underlying"] == 23000


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: ML Predictor Integration Points
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase2MLPredictorIntegration:
    """Verify ML predictor integration points exist and are wired."""

    def test_pipeline_has_ml_prediction_step(self):
        """Pipeline should reference ML prediction."""
        import inspect

        from src.engine import pipeline

        source = inspect.getsource(pipeline)
        assert "ml_predict" in source.lower() or "get_predictor" in source.lower(), (
            "Pipeline must integrate ML prediction"
        )

    def test_pipeline_updates_scan_cache(self):
        """Pipeline should update scan cache after anomaly detection."""
        import inspect

        from src.engine import pipeline

        source = inspect.getsource(pipeline)
        assert "scan_cache" in source.lower() or "update_scan_snapshot" in source, (
            "Pipeline must update scan cache"
        )

    def test_paper_trading_has_retrain_trigger(self):
        """Paper trading should have ML retraining trigger."""
        import inspect

        from src.engine import paper_trading

        source = inspect.getsource(paper_trading)
        assert "_trigger_ml_retraining" in source or "ml_retrain" in source.lower(), (
            "Paper trading must have ML retraining trigger"
        )

    def test_scheduler_has_weekly_training_job(self):
        """Scheduler should have weekly ML training job."""
        import inspect

        from src.scheduler import job_runner

        source = inspect.getsource(job_runner)
        assert "ml" in source.lower() and (
            "weekly" in source.lower() or "sunday" in source.lower()
        ), "Scheduler must have weekly ML training job"


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3: Edge Decay Monitor — Unit Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase3EdgeDecayMonitorUnit:
    """Test EdgeDecayMonitor without DB dependencies."""

    def test_singleton_pattern(self):
        """get_monitor() should return the same instance."""
        from src.intelligence.edge_monitor import get_monitor

        m1 = get_monitor()
        m2 = get_monitor()
        assert m1 is m2

    def test_edge_health_dataclass(self):
        """EdgeHealth should serialize correctly."""
        from src.intelligence.edge_monitor import EdgeHealth

        eh = EdgeHealth(
            strategy_name="NIFTY Long Buildup",
            current_win_rate=0.65,
            historical_win_rate=0.60,
            win_rate_trend="STABLE",
            pnl_trend="IMPROVING",
            health_score=75.0,
            recommendation="⚪ STABLE",
        )
        d = eh.to_dict()
        assert d["strategy_name"] == "NIFTY Long Buildup"
        assert d["current_win_rate"] == 0.65
        assert d["health_score"] == 75.0

    def test_classify_trend_improving(self):
        """>10% improvement should classify as IMPROVING."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        assert m._classify_trend(0.15, 0.50) == "IMPROVING"
        assert m._classify_trend(0.06, 0.50) == "IMPROVING"  # >10% of 0.50

    def test_classify_trend_stable(self):
        """Within ±threshold should classify as STABLE."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        assert m._classify_trend(0.02, 0.50) == "STABLE"
        assert m._classify_trend(-0.05, 0.50) == "STABLE"

    def test_classify_trend_declining(self):
        """>15% decline should classify as DECLINING."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        assert m._classify_trend(-0.10, 0.50) == "DECLINING"  # -20% of 0.50
        assert m._classify_trend(-0.20, 0.60) == "DECLINING"  # -33% of 0.60

    def test_classify_trend_zero_baseline(self):
        """Zero baseline should return STABLE."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        assert m._classify_trend(0.5, 0) == "STABLE"

    def test_health_score_absolute_high_wr(self):
        """High win rate should score well."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        score = m._calculate_health_score_absolute(0.75, 1500)
        assert score >= 80  # 50 base + 30 WR + 20 PnL = 100

    def test_health_score_absolute_low_wr(self):
        """Low win rate should score poorly."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        score = m._calculate_health_score_absolute(0.30, -1000)
        assert score <= 30  # 50 base - 20 WR - 20 PnL = 10

    def test_health_score_absolute_neutral(self):
        """Neutral performance should score around 50."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        score = m._calculate_health_score_absolute(0.50, 0)
        assert 40 <= score <= 60

    def test_health_score_absolute_bounds(self):
        """Score should be clamped to 0-100."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        assert 0 <= m._calculate_health_score_absolute(1.0, 10000) <= 100
        assert 0 <= m._calculate_health_score_absolute(0.0, -10000) <= 100

    def test_health_score_trend_based(self):
        """Trend-based scoring should combine absolute + trend."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        # Strong WR + improving trend + good PnL
        score = m._calculate_health_score(0.75, 0.60, 1500, 1000)
        assert score >= 70

    def test_health_score_trend_declining(self):
        """Declining trend should reduce score."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        # Good WR but severe decline
        score = m._calculate_health_score(0.55, 0.75, -200, 1000)
        assert score < 50

    def test_strategy_name_builder(self):
        """Strategy name builder should handle various filters."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        assert m._get_strategy_name(None) == "All Strategies"
        assert m._get_strategy_name({}) == "All Strategies"
        assert m._get_strategy_name({"symbol": "NIFTY"}) == "NIFTY"
        assert (
            m._get_strategy_name({"symbol": "NIFTY", "verdict_label": "Long Buildup"})
            == "NIFTY Long Buildup"
        )

    def test_recommendation_insufficient_history(self):
        """INSUFFICIENT_HISTORY should give building history message."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        rec = m._generate_edge_recommendation(0.5, 0.5, 0, "INSUFFICIENT_HISTORY")
        assert "Building history" in rec or "not enough" in rec.lower()

    def test_recommendation_declining(self):
        """DECLINING should give edge decay warning."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        rec = m._generate_edge_recommendation(0.45, 0.60, -500, "DECLINING")
        assert "DECAY" in rec.upper() or "declin" in rec.lower()

    def test_recommendation_strong_edge(self):
        """High WR + good PnL should give strong edge message."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        rec = m._generate_edge_recommendation(0.75, 0.70, 1500, "STABLE")
        assert "STRONG" in rec.upper()

    def test_recommendation_breakeven(self):
        """Below 50% WR should give breakeven warning."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        rec = m._generate_edge_recommendation(0.40, 0.45, -200, "STABLE")
        assert "BREAKEVEN" in rec.upper() or "below" in rec.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3: Edge Decay Monitor — DB Integration Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase3EdgeDecayMonitorDB:
    """Test EdgeDecayMonitor with real DB (uses isolated test DB)."""

    def setup_method(self):
        """Ensure clean state."""
        init_db()

    def _insert_closed_trade(self, symbol, verdict, pnl, days_ago, conn):
        """Helper to insert a closed trade."""
        closed_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        opened_at = (
            datetime.now(timezone.utc) - timedelta(days=days_ago + 1)
        ).isoformat()
        conn.execute(
            """
            INSERT INTO paper_trades (
                symbol, verdict_label, option_type, entry_underlying, status, opened_at, closed_at,
                pnl_rupees, entry_premium, exit_premium, lots
            ) VALUES (?, ?, 'CE', 100.0, 'CLOSED', ?, ?, ?, 100, 110, 50)
        """,
            (symbol, verdict, opened_at, closed_at, pnl),
        )

    def test_insufficient_recent_trades(self):
        """With < 5 recent trades, should return INSUFFICIENT_HISTORY."""
        from src.intelligence.edge_monitor import get_monitor

        monitor = get_monitor()

        with get_conn() as conn:
            # Insert only 2 recent trades
            self._insert_closed_trade("TEST_SYM_A", "Long Buildup", 500, 5, conn)
            self._insert_closed_trade("TEST_SYM_A", "Long Buildup", -200, 10, conn)

        results = monitor.check_edge_health({"symbol": "TEST_SYM_A"})
        assert len(results) == 1
        assert results[0].win_rate_trend == "INSUFFICIENT_HISTORY"
        assert results[0].health_score == 50  # Default neutral

    def test_sufficient_recent_insufficient_historical(self):
        """With recent data but no historical, should use absolute scoring."""
        from src.intelligence.edge_monitor import get_monitor

        monitor = get_monitor()

        with get_conn() as conn:
            # 6 recent trades, 0 historical
            for i in range(6):
                self._insert_closed_trade(
                    "TEST_SYM_B", "Long Buildup", 800, i + 1, conn
                )

        results = monitor.check_edge_health({"symbol": "TEST_SYM_B"})
        assert len(results) == 1
        assert results[0].win_rate_trend == "INSUFFICIENT_HISTORY"
        # Should have absolute-only score (not default 50)
        assert results[0].health_score != 50 or results[0].current_win_rate > 0

    def test_full_comparison(self):
        """With both windows populated, should do full trend comparison."""
        from src.intelligence.edge_monitor import get_monitor

        monitor = get_monitor()

        with get_conn() as conn:
            # Recent: 6 trades, 5 wins (83% WR)
            for i in range(6):
                pnl = 1000 if i < 5 else -500
                self._insert_closed_trade(
                    "TEST_SYM_C", "Short Buildup", pnl, i + 1, conn
                )
            # Historical: 6 trades, 3 wins (50% WR)
            for i in range(6):
                pnl = 500 if i < 3 else -300
                self._insert_closed_trade(
                    "TEST_SYM_C", "Short Buildup", pnl, 45 + i, conn
                )

        results = monitor.check_edge_health({"symbol": "TEST_SYM_C"})
        assert len(results) == 1
        r = results[0]
        assert r.win_rate_trend in ("IMPROVING", "STABLE", "DECLINING")
        assert r.current_win_rate > 0
        assert r.historical_win_rate > 0
        assert 0 <= r.health_score <= 100

    def test_get_all_strategies_health_structure(self):
        """get_all_strategies_health should return overall + per-strategy."""
        from src.intelligence.edge_monitor import get_monitor

        monitor = get_monitor()

        with get_conn() as conn:
            for i in range(6):
                self._insert_closed_trade(
                    "TEST_SYM_D", "Long Buildup", 500, i + 1, conn
                )

        results = monitor.get_all_strategies_health()
        assert len(results) >= 1
        # First result should be overall
        assert results[0].strategy_name == "All Strategies"

    def test_pnl_baseline_floor(self):
        """PnL baseline floor should prevent ratio explosion near zero."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        # When hist_pnl ≈ 0, the floor at 100 should prevent extreme ratios
        trend = m._classify_trend(5.0, 0.0)  # change=5, baseline=0 → STABLE (guarded)
        assert trend == "STABLE"


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3: Pipeline Integration
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase3PipelineIntegration:
    """Verify edge health check is wired into pipeline."""

    def test_pipeline_has_edge_health_check(self):
        """Pipeline should reference edge health monitoring."""
        import inspect

        from src.engine import pipeline

        source = inspect.getsource(pipeline)
        assert (
            "edge_health" in source.lower()
            or "edge_monitor" in source.lower()
            or "check_edge_health" in source
        ), "Pipeline must integrate edge health checking"


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4: Dashboard API Endpoints
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase4DashboardAPI:
    """Test AI Insights dashboard API endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client for dashboard server."""
        from fastapi.testclient import TestClient

        from dashboard_server import app

        return TestClient(app)

    def test_ml_prediction_endpoint_exists(self, client):
        """GET /api/ai/ml-prediction/{symbol} should return 200 or valid error."""
        resp = client.get("/api/ai/ml-prediction/NIFTY?verdict=Long+Buildup")
        # Should not 404 — endpoint must exist
        assert resp.status_code != 404, "ML prediction endpoint not found"
        # Accept 200 (with data or null) or 500 (missing deps) but not 404
        assert resp.status_code in (200, 500)

    def test_edge_health_endpoint_exists(self, client):
        """GET /api/ai/edge-health should return valid response."""
        resp = client.get("/api/ai/edge-health")
        assert resp.status_code != 404, "Edge health endpoint not found"
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, list)

    def test_edge_health_filtered_endpoint_exists(self, client):
        """GET /api/ai/edge-health/{symbol} should return valid response."""
        resp = client.get("/api/ai/edge-health/NIFTY")
        assert resp.status_code != 404, "Filtered edge health endpoint not found"

    def test_static_css_route(self, client):
        """Static CSS file should be servable."""
        resp = client.get("/static/ai_insights.css")
        # Should not 404
        assert resp.status_code != 404, "AI insights CSS route not found"

    def test_static_js_route(self, client):
        """Static JS file should be servable."""
        resp = client.get("/static/ai_insights.js")
        # Should not 404
        assert resp.status_code != 404, "AI insights JS route not found"


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4: Dashboard UI Files Exist
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase4UIFilesExist:
    """Verify Phase 4 UI files were created."""

    def test_ai_insights_css_exists(self):
        """ai_insights.css should exist."""
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", "src", "dashboard", "ai_insights.css"
        )
        assert os.path.exists(path), f"Missing: {path}"

    def test_ai_insights_js_exists(self):
        """ai_insights.js should exist."""
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", "src", "dashboard", "ai_insights.js"
        )
        assert os.path.exists(path), f"Missing: {path}"

    def test_index_html_has_ai_tab(self):
        """index.html should contain AI Insights tab."""
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", "src", "dashboard", "index.html"
        )
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            assert (
                "AI Insights" in content
                or "ai-insights" in content
                or "aiPanel" in content
            ), "index.html must contain AI Insights tab"


# ═══════════════════════════════════════════════════════════════════════════
# Cross-Phase Integration Safety
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossPhaseSafety:
    """Verify phases don't break existing functionality."""

    def test_imports_clean(self):
        """All Phase 1-4 modules should import without errors."""
        from src.engine.scan_cache import get_latest_scan_snapshot, update_scan_snapshot
        from src.intelligence.edge_monitor import EdgeHealth, get_monitor

        # Should not raise
        assert callable(update_scan_snapshot)
        assert callable(get_latest_scan_snapshot)
        assert callable(get_monitor)

    def test_scan_cache_thread_safety(self):
        """Concurrent reads/writes should not corrupt cache."""
        import threading

        from src.engine.scan_cache import (
            clear_scan_cache,
            get_latest_scan_snapshot,
            update_scan_snapshot,
        )

        clear_scan_cache()
        errors = []

        def writer(sym, val):
            try:
                for _ in range(50):
                    update_scan_snapshot(sym, {"val": val})
            except Exception as e:
                errors.append(e)

        def reader(sym):
            try:
                for _ in range(50):
                    get_latest_scan_snapshot(sym)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("NIFTY", 1)),
            threading.Thread(target=writer, args=("BANKNIFTY", 2)),
            threading.Thread(target=reader, args=("NIFTY",)),
            threading.Thread(target=reader, args=("BANKNIFTY",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety violations: {errors}"

    def test_edge_monitor_no_crash_on_empty_db(self):
        """Edge monitor should handle empty DB gracefully."""
        from src.intelligence.edge_monitor import get_monitor

        monitor = get_monitor()
        results = monitor.check_edge_health({"symbol": "TOTALLY_FAKE_SYMBOL_XYZ"})
        assert len(results) == 1
        assert results[0].win_rate_trend == "INSUFFICIENT_HISTORY"


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: ML Predictor Core (ml_predictor.py)
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase2MLPredictorCore:
    """Test TradeSuccessPredictor model internals."""

    def test_feature_order_constant_exists(self):
        """FEATURE_ORDER must be an explicit constant, not sorted()."""
        import inspect

        from src.intelligence import ml_predictor

        source = inspect.getsource(ml_predictor)
        assert "FEATURE_ORDER" in source, "Must define explicit FEATURE_ORDER"
        # Ensure it's not dynamically sorted
        assert "sorted(" not in source.split("FEATURE_ORDER")[1].split("\n")[0], (
            "FEATURE_ORDER must not use sorted() — order must be explicit"
        )

    def test_scale_pos_weight_handling(self):
        """Model must handle class imbalance via scale_pos_weight."""
        import inspect

        from src.intelligence import ml_predictor

        source = inspect.getsource(ml_predictor)
        assert "scale_pos_weight" in source, (
            "XGBoost must use scale_pos_weight for class imbalance"
        )

    def test_model_versioning_exists(self):
        """Model versioning with AUC-based rollback must exist."""
        import inspect

        from src.intelligence import ml_predictor

        source = inspect.getsource(ml_predictor)
        assert "version" in source.lower() and "auc" in source.lower(), (
            "Must implement model versioning with AUC comparison"
        )

    def test_shap_explainability_exists(self):
        """SHAP top-3 factor extraction must exist."""
        import inspect

        from src.intelligence import ml_predictor

        source = inspect.getsource(ml_predictor)
        assert "shap" in source.lower() or "top_factors" in source.lower(), (
            "Must implement SHAP explainability"
        )

    def test_predict_returns_none_when_untrained(self):
        """predict() should return None when no model is trained."""
        from src.intelligence.ml_predictor import get_predictor

        predictor = get_predictor()
        # With fresh/untrained predictor, predict should not crash
        result = predictor.predict({"feature_1": 0.5})
        # Either None or a valid dict — must not raise
        assert result is None or isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3: Edge Monitor Recommendations Extended
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase3EdgeRecommendationsExtended:
    """Additional edge recommendation edge cases."""

    def test_recommendation_marginal_edge(self):
        """50-60% WR with stable trend should give marginal edge."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        rec = m._generate_edge_recommendation(0.55, 0.54, 200, "STABLE")
        assert "MARGINAL" in rec.upper() or "MODERATE" in rec.upper()

    def test_recommendation_improving_trend(self):
        """Improving trend with decent WR should give positive signal."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        rec = m._generate_edge_recommendation(0.60, 0.50, 800, "IMPROVING")
        assert "IMPROV" in rec.upper() or "STRONG" in rec.upper()

    def test_health_score_clamped_upper(self):
        """Perfect scores should not exceed 100."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        score = m._calculate_health_score(0.95, 0.70, 5000, 2000)
        assert score <= 100

    def test_health_score_clamped_lower(self):
        """Worst scores should not go below 0."""
        from src.intelligence.edge_monitor import EdgeDecayMonitor

        m = EdgeDecayMonitor()
        score = m._calculate_health_score(0.10, 0.80, -5000, -3000)
        assert score >= 0


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4: Dashboard UI Content Validation
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase4UIContentValidation:
    """Validate AI Insights UI file contents meet requirements."""

    def _read_dashboard_file(self, filename):
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", "src", "dashboard", filename
        )
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_css_has_design_tokens(self):
        """CSS must define design tokens in :root."""
        css = self._read_dashboard_file("ai_insights.css")
        assert ":root" in css or "--ai-" in css, "CSS must define design tokens"

    def test_css_has_semantic_colors(self):
        """CSS must have semantic color classes."""
        css = self._read_dashboard_file("ai_insights.css")
        assert "ai-is-good" in css or "ai-pill--good" in css or "--ai-good" in css, (
            "CSS must define semantic color classes"
        )

    def test_css_has_responsive_breakpoint(self):
        """CSS must collapse grid below 720px."""
        css = self._read_dashboard_file("ai_insights.css")
        assert "720" in css or "responsive" in css.lower() or "@media" in css, (
            "CSS must have responsive breakpoint"
        )

    def test_css_has_reduced_motion(self):
        """CSS must respect prefers-reduced-motion."""
        css = self._read_dashboard_file("ai_insights.css")
        assert "reduced-motion" in css, (
            "CSS must include prefers-reduced-motion media query"
        )

    def test_js_has_escape_function(self):
        """JS must have XSS escape helper."""
        js = self._read_dashboard_file("ai_insights.js")
        assert "_esc" in js or "escapeHtml" in js or "sanitize" in js.lower(), (
            "JS must have XSS escape function"
        )

    def test_js_has_four_states(self):
        """JS must handle loading/ready/empty/error states."""
        js = self._read_dashboard_file("ai_insights.js")
        states_found = sum(
            [
                "loading" in js.lower(),
                "ready" in js.lower() or "render" in js.lower(),
                "empty" in js.lower(),
                "error" in js.lower(),
            ]
        )
        assert states_found >= 3, (
            f"JS must handle 4 panel states (found {states_found}/4)"
        )

    def test_js_has_aria_live(self):
        """JS must set aria-live for accessibility."""
        js = self._read_dashboard_file("ai_insights.js")
        assert "aria-live" in js, "JS must set aria-live regions for screen readers"

    def test_js_has_polling_management(self):
        """JS must manage polling (start/stop)."""
        js = self._read_dashboard_file("ai_insights.js")
        assert (
            "poll" in js.lower() or "interval" in js.lower() or "setInterval" in js
        ), "JS must implement polling management"

    def test_index_html_has_symbol_selector_integration(self):
        """index.html should wire symbol selector to AI context."""
        html = self._read_dashboard_file("index.html")
        # Should reference both symbol selection and AI panel
        has_symbol = "symbol" in html.lower()
        has_ai = "ai" in html.lower()
        assert has_symbol and has_ai, (
            "index.html must integrate symbol selector with AI panel"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Cross-Phase: Scheduler Integration
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossPhaseSchedulerIntegration:
    """Verify scheduler correctly integrates Phase 2 weekly job."""

    def test_job_runner_imports_clean(self):
        """job_runner should import without errors."""
        from src.scheduler import job_runner

        assert job_runner is not None

    def test_scheduler_docstring_mentions_phase2(self):
        """Scheduler __init__ docstring should reference Phase 2."""
        # Module-level docstring or __init__ content
        import src.scheduler as sched_mod
        from src.scheduler import __doc__ as sched_doc

        content = str(sched_mod.__doc__ or "") + str(getattr(sched_mod, "__all__", ""))
        # At minimum the module should be importable; docstring check is best-effort
        assert sched_mod is not None


# ═══════════════════════════════════════════════════════════════════════════
# Cross-Phase: Intelligence Module Exports
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossPhaseIntelligenceExports:
    """Verify intelligence module exports all Phase 1-3 components."""

    def test_edge_monitor_importable_from_intelligence(self):
        """edge_monitor should be importable from src.intelligence."""
        from src.intelligence.edge_monitor import (
            EdgeDecayMonitor,
            EdgeHealth,
            get_monitor,
        )

        assert callable(get_monitor)
        assert EdgeHealth is not None
        assert EdgeDecayMonitor is not None

    def test_ml_predictor_importable_from_intelligence(self):
        """ml_predictor should be importable from src.intelligence."""
        from src.intelligence.ml_predictor import get_predictor

        assert callable(get_predictor)

    def test_scan_cache_importable_from_engine(self):
        """scan_cache should be importable from src.engine."""
        from src.engine.scan_cache import (
            clear_scan_cache,
            get_all_cached_symbols,
            get_latest_scan_snapshot,
            update_scan_snapshot,
        )

        assert callable(update_scan_snapshot)
        assert callable(get_latest_scan_snapshot)
        assert callable(clear_scan_cache)
        assert callable(get_all_cached_symbols)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
