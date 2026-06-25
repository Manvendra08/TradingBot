"""
Tests for LLM enrichment v2.0 action-oriented schema and prompt changes.

Covers:
- New LLMTradeVerdict schema (action, instrument, entry_trigger, etc.)
- Historical OI formatting with price impact analysis
- Action→bias mapping in trade_decision (_extract_ai_bias)
- Digest formatting with new schema fields
- OpenRouter array unwrapping fix
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.engine.llm_enrichment import (
    LLMExitAdvice,
    LLMTradeVerdict,
    _format_historical_oi,
)

# ── Schema Validation ─────────────────────────────────────────────────────


class TestLLMTradeVerdictSchema:
    """Verify the new action-oriented schema accepts valid data."""

    def test_valid_go_long(self):
        data = {
            "action": "GO_LONG",
            "confidence": 78,
            "instrument": "NIFTY 24500 CE 27Jun",
            "entry_trigger": "Underlying breaks above 24520 with volume",
            "entry_premium_range": "180-195",
            "stop_loss": "Premium 140",
            "target_1": "Premium 230",
            "target_2": "Premium 280",
            "risk_reward": "1:1.8",
            "thesis": "Short covering at support with PCR rising above 1.2",
            "invalidation": "If underlying closes below 24400 on 1H",
            "risk_rating": "LOW",
            "catalyst": "EIA report Thursday 8:30PM",
            "signal_chain": "OI: Put Writing — CE Δ +2.3K vs PE Δ +186 → BULLISH\nPrice: +0.92% vs MP 24500 → upside momentum\nChart: 1H BULL + 3H BULL → entry timing aligned",
        }
        verdict = LLMTradeVerdict(**data)
        assert verdict.action == "GO_LONG"
        assert verdict.confidence == 78
        assert verdict.instrument == "NIFTY 24500 CE 27Jun"
        assert verdict.risk_rating == "LOW"

    def test_valid_no_trade(self):
        data = {
            "action": "NO_TRADE",
            "confidence": 40,
            "instrument": "N/A",
            "entry_trigger": "N/A",
            "entry_premium_range": "N/A",
            "stop_loss": "N/A",
            "target_1": "N/A",
            "target_2": "N/A",
            "risk_reward": "N/A",
            "thesis": "Conflicting signals — wait for clarity",
            "invalidation": "N/A",
            "risk_rating": "HIGH",
            "catalyst": "No major catalyst",
            "signal_chain": "OI: Both unwinding — CE Δ −43 vs PE Δ −224 → squaring\nPrice: 306.4 within 300–310 range → no breakout\nChart: 1H BEAR vs 3H BULL → 1H pullback",
        }
        verdict = LLMTradeVerdict(**data)
        assert verdict.action == "NO_TRADE"

    def test_missing_required_field_raises(self):
        with pytest.raises(Exception):
            LLMTradeVerdict(action="GO_LONG")  # Missing all other required fields


class TestLLMExitAdviceSchema:
    def test_valid_exit_advice(self):
        data = {
            "action": "TRAIL_SL",
            "new_sl_premium": 160.0,
            "new_target_premium": None,
            "reasoning": "Price moved 1.5R in favor; trail to lock profits",
            "urgency": "MEDIUM",
        }
        advice = LLMExitAdvice(**data)
        assert advice.action == "TRAIL_SL"
        assert advice.new_sl_premium == 160.0
        assert advice.new_target_premium is None


# ── Historical OI Formatting ──────────────────────────────────────────────


class TestFormatHistoricalOi:
    def test_insufficient_data(self):
        # Patch get_conn in models.schema since it's imported dynamically inside the function
        with patch("src.models.schema.get_conn") as mock_get_conn:
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = []

            mock_conn_ctx = MagicMock()
            mock_conn_ctx.__enter__ = MagicMock(
                return_value=MagicMock(execute=MagicMock(return_value=mock_cursor))
            )
            mock_conn_ctx.__exit__ = MagicMock(return_value=False)
            mock_get_conn.return_value = mock_conn_ctx

            result = _format_historical_oi("NIFTY")
        assert "Insufficient" in result or "unavailable" in result

    def test_formats_trend_correctly(self):
        """Verify historical OI includes PCR trend, OI trend, and price impact."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        rows = []
        for i in range(5):
            ts = (now - timedelta(minutes=i * 60)).isoformat()
            rows.append(
                {
                    "fetched_at": ts,
                    "underlying": 22000.0 + i * 10,
                    "pcr": 1.0 + i * 0.05,
                    "max_pain": 22000.0,
                    "ce_oi_change": 5000 + i * 1000,
                    "pe_oi_change": 3000 + i * 500,
                    "verdict_label": "Long Buildup",
                    "confidence": 80,
                }
            )

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = rows

        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(
            return_value=MagicMock(execute=MagicMock(return_value=mock_cursor))
        )
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)

        with patch("src.models.schema.get_conn", return_value=mock_conn_ctx):
            result = _format_historical_oi("NIFTY")

        assert "Last 5 scans" in result
        assert "PCR Trend:" in result
        assert "OI Trend:" in result
        assert "Price Impact:" in result

    def test_db_exception_returns_graceful_message(self):
        with patch("src.models.schema.get_conn", side_effect=Exception("DB error")):
            result = _format_historical_oi("NIFTY")
        assert "unavailable" in result


# ── Action → Bias Mapping (trade_decision) ────────────────────────────────


class TestExtractAiBias:
    """Verify the action→bias mapping works for both old and new schemas."""

    def test_new_schema_go_long(self):
        from src.engine.trade_decision import _extract_ai_bias

        ai_verdict = MagicMock()
        ai_verdict.action = "GO_LONG"
        ai_verdict.bias = None  # New schema doesn't have bias
        assert _extract_ai_bias(ai_verdict) == "BULLISH"

    def test_new_schema_go_short(self):
        from src.engine.trade_decision import _extract_ai_bias

        ai_verdict = MagicMock()
        ai_verdict.action = "GO_SHORT"
        ai_verdict.bias = None
        assert _extract_ai_bias(ai_verdict) == "BEARISH"

    def test_new_schema_no_trade(self):
        from src.engine.trade_decision import _extract_ai_bias

        ai_verdict = MagicMock()
        ai_verdict.action = "NO_TRADE"
        ai_verdict.bias = None
        assert _extract_ai_bias(ai_verdict) == "NEUTRAL"

    def test_legacy_schema_passthrough(self):
        from src.engine.trade_decision import _extract_ai_bias

        ai_verdict = MagicMock()
        ai_verdict.action = None
        ai_verdict.bias = "BULLISH"
        assert _extract_ai_bias(ai_verdict) == "BULLISH"

    def test_none_verdict(self):
        from src.engine.trade_decision import _extract_ai_bias

        assert _extract_ai_bias(None) is None


# ── OpenRouter Array Unwrapping ───────────────────────────────────────────


class TestOpenRouterArrayUnwrap:
    """Verify that single-element array responses are unwrapped correctly."""

    def test_array_wrapped_response_unwrapped(self):
        """Simulate openrouter/free returning [{...}] instead of {...}."""
        from src.engine.llm_enrichment import _call_llm_api

        wrapped_json = json.dumps(
            [
                {
                    "action": "GO_LONG",
                    "confidence": 75,
                    "signal_chain": "OI: test\\nPrice: test\\nChart: test",
                    "instrument": "NIFTY 24500 CE 27Jun",
                    "entry_trigger": "Break above 24520",
                    "entry_premium_range": "180-195",
                    "stop_loss": "Premium 140",
                    "target_1": "Premium 230",
                    "target_2": "Premium 280",
                    "risk_reward": "1:1.8",
                    "thesis": "Test thesis",
                    "invalidation": "Below 24400",
                    "risk_rating": "LOW",
                    "catalyst": "None",
                }
            ]
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": wrapped_json}}]
        }

        import os

        from src.engine import llm_enrichment as llm_mod

        os.environ["OPENROUTER_API_KEY"] = "fake-key"
        old_opencode = os.environ.pop("OPENCODE_API_KEY", None)
        old_gemini = os.environ.pop("GEMINI_API_KEY", None)
        old_groq = os.environ.pop("GROQ_API_KEY", None)
        llm_mod._CONSECUTIVE_FAILURES = 0
        llm_mod._CIRCUIT_OPEN_UNTIL = 0.0
        llm_mod._PROVIDER_COOLDOWN_UNTIL.clear()

        try:
            with patch("requests.Session.post", return_value=mock_resp):
                result = _call_llm_api("NIFTY", "test prompt", LLMTradeVerdict)
            assert result is not None
            assert result.action == "GO_LONG"
            assert result.confidence == 75
        finally:
            if old_opencode:
                os.environ["OPENCODE_API_KEY"] = old_opencode
            if old_gemini:
                os.environ["GEMINI_API_KEY"] = old_gemini
            if old_groq:
                os.environ["GROQ_API_KEY"] = old_groq
            os.environ.pop("OPENROUTER_API_KEY", None)


# ── Digest Formatting with New Schema ─────────────────────────────────────


class TestDigestNewSchema:
    """Verify digest renders correctly with new action-oriented fields."""

    def test_build_digest_wrapper_with_new_schema(self):
        from src.alerts.digest import build_digest_wrapper

        llm_verdict = {
            "action": "GO_LONG",
            "confidence": 78,
            "instrument": "NIFTY 24500 CE 27Jun",
            "entry_trigger": "Underlying breaks above 24520",
            "entry_premium_range": "180-195",
            "stop_loss": "Premium 140",
            "target_1": "Premium 230",
            "target_2": "Premium 280",
            "risk_reward": "1:1.8",
            "thesis": "Short covering at support",
            "invalidation": "Below 24400 on 1H",
            "risk_rating": "LOW",
            "catalyst": "EIA report Thursday",
        }

        scan_context = {
            "expiry": "2026-06-27",
            "underlying": 24500.0,
            "atm_strike": 24500,
            "pcr": 1.25,
            "support": 24400,
            "resistance": 24600,
            "price_change_pct": 0.3,
            "price_change_points": 73.5,
            "chart_indicators": {
                "1h": {"sentiment": "BULLISH"},
                "3h": {"sentiment": "BULLISH"},
            },
        }

        alerts = [
            {
                "alert_type": "OI_SPIKE",
                "severity": "HIGH",
                "strike": 24500,
                "option_type": "CE",
                "detail_json": '{"pct_change": 30.0}',
            }
        ]

        digest_id, msg = build_digest_wrapper(
            symbol="NIFTY",
            alerts=alerts,
            fetched_at="2026-06-20T06:00:00Z",
            scan_context=scan_context,
            intelligence_text="rule verdict",
            detected_count=1,
            dedup_suppressed_count=0,
            llm_verdict=llm_verdict,
        )

        # Verify new schema fields appear in digest
        assert "GO\\_LONG" in msg or "GO_LONG" in msg
        assert "78%" in msg
        assert "LOW" in msg
