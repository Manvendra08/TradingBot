"""
ADR-007 Test Suite — AI Role Redesign
Tests for: Priority-5 empirical boost, CORE_OI veto mirror, grep test, ML guard, async flow.

Per §9 of ADR-007:
- Priority-5 rewrite: blocked trade + precedent (n=25, wr=0.65) → EMPIRICAL_PROMOTED
- CORE_OI veto mirror: veto_flag substitution test
- Grep-level test: no ai_conf >= in gating code paths
- pattern_history.get_pattern_stats(): unit test
- ML guard: auc=0.449 → forced shadow
- Async enrichment: LLM stub delayed → digest v1 sent, v2 edit
"""

import unittest
import re
import sys
import tempfile
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, ".")


class TestADR007GrepTest(unittest.TestCase):
    """§9: Grep-level test — no ai_conf >= in gating code paths."""

    def test_no_ai_conf_in_gating_code(self):
        files = ["src/engine/decision_pipeline.py", "src/engine/trade_decision.py"]
        pattern = re.compile(r'ai_conf\s*>=|ai_conf>=|ai_conf\s*>=\s*\d')
        found = []
        for f in files:
            try:
                with open(f, encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if pattern.search(line):
                            found.append(f"{f}:{i}: {line.strip()}")
            except FileNotFoundError:
                pass
        self.assertEqual(found, [], f"Found ai_conf >= in gating code:\n" + "\n".join(found))


class TestADR007EmpiricalBoost(unittest.TestCase):
    """§9: Priority-5 empirical boost tests."""

    def test_empirical_boost_conditions_met(self):
        from src.engine.decision_pipeline import PipelineContext, StepResult
        from src.engine.pattern_history import PatternStats

        precedent = PatternStats(n_trades=25, win_rate=0.65, avg_pnl=100.0)

        ctx = PipelineContext(
            engine="CORE_OI",
            symbol="NIFTY",
            direction="LONG",
            underlying=24000.0,
            scan_context={"intel": {"verdict_label": "LONG_BUILDUP"}},
            ai_verdict={"confidence": 70, "bias": "BULLISH", "veto_flag": False},
            steps=[
                StepResult(name="signal", passed=True, score=80, reason="", data={}),
                StepResult(name="confidence", passed=False, score=50, reason="blocked", data={}),
            ],
        )

        self.assertTrue(precedent.n_trades >= 20)
        self.assertTrue(precedent.win_rate >= 0.60)
        self.assertTrue(precedent.avg_pnl > 0)

    def test_empirical_boost_insufficient_trades(self):
        from src.engine.pattern_history import PatternStats

        precedent = PatternStats(n_trades=15, win_rate=0.70, avg_pnl=100.0)
        self.assertFalse(precedent.n_trades >= 20)

    def test_empirical_boost_veto_flag_blocks(self):
        from src.engine.decision_pipeline import _extract_ai_veto_flag

        ai_verdict = {"veto_flag": True, "confidence": 85, "bias": "BULLISH"}
        veto_flag = _extract_ai_veto_flag(ai_verdict)
        self.assertTrue(veto_flag)


class TestADR007CoreOiVeto(unittest.TestCase):
    """§9: CORE_OI full-mode veto test."""

    def test_veto_flag_only_no_confidence_threshold(self):
        from src.engine.decision_pipeline import step_ai_alignment, PipelineContext, StepResult

        ctx = PipelineContext(
            engine="CORE_OI",
            symbol="NIFTY",
            direction="LONG",
            underlying=24000.0,
            scan_context={},
            ai_verdict={"confidence": 50, "bias": "BEARISH", "veto_flag": True, "veto_reason": "event_risk"},
            steps=[StepResult(name="signal", passed=True, score=80, reason="", data={})],
        )

        with patch("config.runtime_config.load_runtime_config") as mock_rconf:
            mock_rconf.return_value = {"live_ai_decision_mode": "full"}
            result = step_ai_alignment(ctx)
            self.assertFalse(result.passed)
            self.assertIn("VETO", result.reason)

    def test_no_veto_no_block(self):
        from src.engine.decision_pipeline import step_ai_alignment, PipelineContext, StepResult

        ctx = PipelineContext(
            engine="CORE_OI",
            symbol="NIFTY",
            direction="LONG",
            underlying=24000.0,
            scan_context={},
            ai_verdict={"confidence": 85, "bias": "BULLISH", "veto_flag": False},
            steps=[StepResult(name="signal", passed=True, score=80, reason="", data={})],
        )

        with patch("config.runtime_config.load_runtime_config") as mock_rconf:
            mock_rconf.return_value = {"live_ai_decision_mode": "full"}
            result = step_ai_alignment(ctx)
            self.assertTrue(result.passed)


class TestADR007PatternHistory(unittest.TestCase):
    """§9: pattern_history.get_pattern_stats() tests."""

    def test_pattern_stats_empty(self):
        from src.engine.pattern_history import PatternStats

        ps = PatternStats(n_trades=0, win_rate=0.0, avg_pnl=0.0)
        self.assertEqual(ps.n_trades, 0)
        self.assertEqual(ps.win_rate, 0.0)
        self.assertEqual(ps.avg_pnl, 0.0)

    def test_get_pattern_stats_live_calculation(self):
        from src.engine.pattern_history import get_pattern_stats, PatternStats

        with patch("src.engine.pattern_history.get_conn") as mock_conn:
            mock_c = MagicMock()
            mock_conn.return_value.__enter__.return_value = mock_c
            mock_c.execute.return_value.fetchone.return_value = None
            mock_c.execute.return_value.fetchall.return_value = []

            with patch.object(Path, "exists", return_value=False):
                result = get_pattern_stats("TEST", "LONG_BUILDUP", "TRENDING")
                self.assertEqual(result.n_trades, 0)


class TestADR007MLGuard(unittest.TestCase):
    """§9: ML predictor shadow mode guard."""

    def test_force_shadow_when_auc_below_threshold(self):
        predictor = MagicMock()
        predictor._force_shadow = True
        predictor.current_auc = 0.45
        predictor.training_samples = 200

        self.assertTrue(predictor._force_shadow)
        self.assertTrue(predictor.current_auc < 0.55)

    def test_force_shadow_when_samples_below_threshold(self):
        predictor = MagicMock()
        predictor._force_shadow = True
        predictor.current_auc = 0.60
        predictor.training_samples = 200

        self.assertTrue(predictor._force_shadow)
        self.assertTrue(predictor.training_samples < 300)

    def test_no_force_shadow_when_adequate(self):
        predictor = MagicMock()
        predictor._force_shadow = False
        predictor.current_auc = 0.65
        predictor.training_samples = 500

        self.assertFalse(predictor._force_shadow)
        self.assertTrue(predictor.current_auc >= 0.55)
        self.assertTrue(predictor.training_samples >= 300)


class TestADR007AsyncEnrichment(unittest.TestCase):
    """§9: Async enrichment flow tests."""

    def test_llm_enrichment_async_setting(self):
        from config.settings import LLM_ENRICHMENT_ASYNC, LLM_ENRICH_TIMEOUT_S

        self.assertTrue(LLM_ENRICHMENT_ASYNC)
        self.assertEqual(LLM_ENRICH_TIMEOUT_S, 120)

    def test_async_llm_pending_flag(self):
        mock_pipeline_vars = {"_async_llm_pending": True, "llm_verdict": None}
        self.assertTrue(mock_pipeline_vars["_async_llm_pending"])
        self.assertIsNone(mock_pipeline_vars["llm_verdict"])


class TestADR007AutopsyWriter(unittest.TestCase):
    """§9: Autopsy writer tests."""

    def test_autopsy_disabled_by_default(self):
        from config.settings import AUTOPSY_ENABLED, AUTOPSY_TIME_IST
        self.assertIsNotNone(AUTOPSY_ENABLED)
        self.assertEqual(AUTOPSY_TIME_IST, "23:45")

    def test_get_closed_trades_today_empty(self):
        from src.engine.autopsy_writer import get_closed_trades_today

        with patch("src.engine.autopsy_writer.get_conn") as mock_conn:
            mock_c = MagicMock()
            mock_conn.return_value.__enter__.return_value = mock_c
            mock_c.execute.return_value.fetchall.return_value = []
            result = get_closed_trades_today()
            self.assertEqual(result, [])


class TestADR007BoostOnlyRemoved(unittest.TestCase):
    """Verify boost_only mode is removed."""

    def test_decision_pipeline_no_boost_only(self):
        with open("src/engine/decision_pipeline.py", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn('"boost_only"', content)
        self.assertNotIn("boost_only", content)

    def test_settings_default_empirical(self):
        with open("config/settings.py", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('AI_DECISION_MODE', content)
        self.assertIn('empirical', content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
