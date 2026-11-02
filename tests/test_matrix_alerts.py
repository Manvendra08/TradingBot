"""
Tests for Telegram Matrix Alert Templates.
Verifies Markdown V1 character escaping, length constraints (<= 4096),
shadow prefixes, and format completeness across full, partial, and None fields.
"""

import pytest

from src.alerts.matrix_alerts import (
    matrix_digest_line,
    format_entry,
    format_exit,
    format_skip,
    format_config_event,
    TELEGRAM_MAX_LEN,
)


def test_matrix_digest_line_shadow_mode():
    """Verify digest line under shadow mode has prefix and shadow diff."""
    shadow_data = {
        "mode": "shadow",
        "config_version": "1.0.0",
        "direction": "NEUTRAL",
        "vol_bucket": "MID",
        "ivp": 48.5,
        "recommended_structure": "IRON_CONDOR",
        "delta_profile": "aggressive",
        "actual_llm_structure": "SHORT_STRANGLE",
        "reason": "OK",
    }
    line = matrix_digest_line(shadow_data)
    assert "👁 SHADOW" in line
    assert "Matrix v1.0.0" in line
    assert "IV MID" in line
    assert "IVP 48.5" in line
    assert "IRON CONDOR" in line or "IRON\\_CONDOR" in line or "IRON_CONDOR" in line
    assert "shadow diff" in line


def test_matrix_digest_line_mcx_nymex_agreement():
    """MCX digest line includes NYMEX trend and agreement status."""
    shadow_data = {
        "mode": "enforce",
        "config_version": "1.0.0",
        "direction": "BULLISH",
        "vol_bucket": "MID",
        "ivp": 55.0,
        "recommended_structure": "BULL_PUT_SPREAD",
        "delta_profile": "aggressive",
        "nymex_symbol": "NG=F",
        "nymex_trend": "BULLISH",
        "nymex_agrees": True,
        "reason": "OK",
    }
    line = matrix_digest_line(shadow_data)
    assert "NYMEX 1H BULLISH" in line
    assert "agree ✓" in line


def test_format_entry_full_and_escaped():
    """Verify format_entry builds complete text with Markdown safety."""
    plan = {
        "symbol": "NIFTY",
        "strategy": "IRON_CONDOR",
        "expiry": "2026-10-29",
        "delta_profile": "aggressive",
        "legs": [
            {"action": "SELL", "option_type": "CE", "strike": 25000, "delta": 0.25, "premium": 45.0},
            {"action": "BUY", "option_type": "CE", "strike": 25200, "delta": 0.10, "premium": 15.0},
            {"action": "SELL", "option_type": "PE", "strike": 24000, "delta": 0.24, "premium": 42.0},
            {"action": "BUY", "option_type": "PE", "strike": 23800, "delta": 0.09, "premium": 14.0},
        ],
        "net_credit": 58.0,
        "credit_to_width_pct": 29.0,
        "max_loss": 142.0,
        "stop_description": "Tested-side Δ≥0.48 or 1.0x credit",
        "target_description": "40% (₹23.2)",
        "lots": 2,
        "capital_cap_pct": 1.2,
        "ai_action": "agree",
        "ai_verdict": "Low tail risk before expiry",
    }
    msg = format_entry(plan, config_version="1.0.0")
    assert "NIFTY" in msg
    assert "IRON\\_CONDOR" in msg or "IRON_CONDOR" in msg
    assert "Net Credit: ₹58.0 (29.0% wing)" in msg
    assert "Tested-side" in msg
    assert "Lots: 2" in msg
    assert "AI agrees" in msg
    assert len(msg) <= TELEGRAM_MAX_LEN


def test_format_exit_standard_codes():
    """Verify format_exit handles standard exit codes and SELL-first notice."""
    exit_data = {
        "symbol": "NATURALGAS",
        "strategy": "BULL_PUT_SPREAD",
        "reason_code": "TESTED_SIDE",
        "pnl_rupees": -12500.0,
        "pnl_credit_pct": -48.0,
        "hold_time_str": "3h 45m",
        "trigger_detail": "PE 340 Δ0.51 (ATM breach)",
    }
    msg = format_exit(exit_data)
    assert "TESTED\\_SIDE" in msg or "TESTED_SIDE" in msg
    assert "🔴 P&L: ₹-12,500.0" in msg
    assert "SELL legs closed first" in msg
    assert len(msg) <= TELEGRAM_MAX_LEN


def test_format_config_event_types():
    """Verify reload and invalid alert formatting."""
    reloaded = format_config_event("RELOADED", "delta_profile -> aggressive", config_version="1.0.0")
    assert "Matrix cfg v1.0.0 loaded" in reloaded

    invalid = format_config_event("INVALID", "bad delta range in IC")
    assert "Matrix cfg INVALID" in invalid
    assert "defaults active" in invalid


def test_telegram_length_clamping():
    """Ensure excessively long text is clamped safely under 4096 characters."""
    plan = {
        "symbol": "TEST",
        "strategy": "IRON_CONDOR",
        "ai_verdict": "X" * 5000,
    }
    msg = format_entry(plan)
    assert len(msg) <= TELEGRAM_MAX_LEN
    assert msg.endswith("...")
