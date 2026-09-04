"""
Unit tests for src/engine/skill_loader.py and prompt integration.
"""

import pathlib
import tempfile
import pytest

from src.engine.skill_loader import (
    _clean_markdown_for_prompt,
    get_skill_guidelines,
    get_reality_check_guardrails,
    get_investment_research_guidance,
    get_autopsy_analyst_guidance,
)
from src.engine.llm_enrichment import _build_deep_prompt, _build_exit_prompt


def test_clean_markdown_for_prompt():
    sample_text = """---
name: test-skill
description: test
---
# Header
This is a [test link](https://example.com) for testing.

Multiple newlines.
"""
    cleaned = _clean_markdown_for_prompt(sample_text, max_chars=500)
    assert "name: test-skill" not in cleaned
    assert "test link" in cleaned
    assert "https://example.com" not in cleaned
    assert "Multiple newlines." in cleaned


def test_clean_markdown_truncation():
    long_text = "Word " * 200
    cleaned = _clean_markdown_for_prompt(long_text, max_chars=50)
    assert len(cleaned) <= 60
    assert cleaned.endswith("...")


def test_get_skill_guidelines_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        res = get_skill_guidelines("non-existent-skill", skills_dir=tmpdir)
        assert res == ""


def test_get_skill_guidelines_from_temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = pathlib.Path(tmpdir) / "custom-trader"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: custom-trader\n---\n# Trader\nAlways maintain 1:2 risk reward.",
            encoding="utf-8",
        )
        res = get_skill_guidelines("custom-trader", skills_dir=tmpdir)
        assert "Always maintain 1:2 risk reward." in res


def test_guardrails_return_valid_text():
    rc = get_reality_check_guardrails()
    assert isinstance(rc, str)
    assert len(rc) > 50
    assert "REALITY CHECK" in rc

    ir = get_investment_research_guidance()
    assert isinstance(ir, str)
    assert len(ir) > 50
    assert "VARIANT PERCEPTION" in ir

    aa = get_autopsy_analyst_guidance()
    assert isinstance(aa, str)
    assert len(aa) > 50
    assert "POST-MORTEM" in aa


def test_build_deep_prompt_incorporates_skills(monkeypatch):
    monkeypatch.setattr(
        "src.engine.llm_enrichment._format_historical_oi",
        lambda sym: "Mock OI History: 5 scans analyzed",
    )
    mock_intel = {
        "verdict_label": "BULLISH",
        "confidence": 80,
        "trend": "STRONG_UP",
        "verdict_desc": "Breakout above resistance",
    }
    mock_context = {
        "underlying": 24500.0,
        "atm_strike": 24500.0,
        "days_to_expiry": 2,
        "support": 24400.0,
        "resistance": 24600.0,
        "max_pain": 24500.0,
        "pcr": 1.25,
        "ce_oi_change": -50000,
        "pe_oi_change": 120000,
        "price_change_pct": 0.45,
        "price_change_points": 110.0,
        "chart_indicators": {
            "1h": {"open": 24450.0, "high": 24520.0, "low": 24430.0, "close": 24500.0, "sentiment": "BULLISH"},
            "3h": {"open": 24400.0, "high": 24550.0, "low": 24380.0, "close": 24500.0, "sentiment": "BULLISH"},
        },
        "option_rows": [
            {"strike": 24500, "option_type": "CE", "ltp": 125.0, "iv": 14.5},
            {"strike": 24500, "option_type": "PE", "ltp": 110.0, "iv": 15.0},
        ],
    }

    prompt = _build_deep_prompt(
        symbol="NIFTY",
        intel=mock_intel,
        scan_context=mock_context,
    )

    assert "REALITY CHECK" in prompt
    assert "VARIANT PERCEPTION" in prompt
    assert "NIFTY" in prompt
    assert "24500" in prompt


def test_build_exit_prompt_incorporates_capital_preservation():
    mock_open_trade = {
        "symbol": "BANKNIFTY",
        "option_type": "CE",
        "strike": 52000,
        "entry_premium": 250.0,
        "stop_loss_premium": 180.0,
        "target_premium": 390.0,
        "entry_underlying": 51950.0,
        "stop_loss": 51800.0,
        "target": 52300.0,
        "expiry": "2026-09-10",
        "opened_at": "2026-09-05 10:00:00",
    }
    mock_context = {
        "underlying": 52100.0,
        "price_change_points": 150.0,
        "price_change_pct": 0.29,
        "pcr": 1.15,
        "support": 51800.0,
        "resistance": 52400.0,
        "current_bid": 310.0,
        "current_ask": 315.0,
        "current_oi": 50000,
        "atm_iv": 16.0,
    }

    exit_prompt = _build_exit_prompt(
        symbol="BANKNIFTY",
        open_trade=mock_open_trade,
        scan_context=mock_context,
    )

    assert "CAPITAL PRESERVATION" in exit_prompt
    assert "TRAIL_SL" in exit_prompt


def test_autopsy_incorporates_analyst_guidance():
    from unittest.mock import patch
    from src.engine.autopsy_writer import _call_llm_autopsy, _call_llm_autopsy_batch

    with patch("src.engine.llm_enrichment._call_llm_api") as mock_call:
        mock_call.return_value = (None, 0.0)
        mock_trade = {
            "symbol": "NIFTY",
            "verdict_label": "LONG",
            "entry_price": 100,
            "exit_price": 120,
            "pnl": 20,
            "pnl_pct": 20.0,
            "exit_reason": "TARGET_HIT",
            "bars_held": 5,
            "active_reasons": '["Breakout"]',
            "raw_features": "{}",
        }
        _call_llm_autopsy(mock_trade, None)
        assert mock_call.called
        single_prompt = mock_call.call_args[0][1]
        assert "ANALYSIS FRAMEWORK:" in single_prompt
        assert "POST-MORTEM" in single_prompt

    with patch("src.engine.llm_enrichment._call_llm_api") as mock_call_batch:
        mock_call_batch.return_value = (None, 0.0)
        _call_llm_autopsy_batch([(mock_trade, None)])
        assert mock_call_batch.called
        batch_prompt = mock_call_batch.call_args[0][1]
        assert "ANALYSIS FRAMEWORK:" in batch_prompt
        assert "POST-MORTEM" in batch_prompt

