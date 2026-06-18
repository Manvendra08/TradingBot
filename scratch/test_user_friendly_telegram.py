"""
Test user-friendly Telegram templates
Shows 3 formats: Friendly, Compact, Detailed
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.engine.telegram_formatter import (
    format_user_friendly_message,
    format_compact_message,
    format_detailed_message,
)


def test_all_formats():
    """Test all three message formats."""
    
    # Mock data
    intel = {
        "symbol": "NIFTY",
        "verdict_label": "Long Buildup",
        "verdict_emoji": "🟢",
        "confidence": 85,
        "bias": "BULLISH",
        "chart_conflict": False,
        "trend": "🟢 Strong Bullish Trend — persistent put writing + long buildup",
    }
    
    decision = {
        "status": "TRIGGERED_CORE",
        "setup_type": "TREND_CONTINUATION",
        "reason": "All trend persistence filters passed",
        "scores": {
            "confidence": 85,
            "entry_quality": 90,
            "trend_alignment": 78,
            "regime_score": 72,
            "momentum_score": 82,
        },
    }
    
    risk_info = {
        "blocked": False,
        "open_trades": 1,
        "max_trades": 4,
        "daily_loss": 2000,
        "max_loss": 10000,
    }
    
    scan_context = {
        "underlying": 22500,
        "support": 22400,
        "resistance": 22600,
        "pcr": 1.30,
    }
    
    # ── FORMAT 1: USER-FRIENDLY ───────────────────────────────────────────
    print("\n" + "=" * 80)
    print("FORMAT 1: USER-FRIENDLY (Recommended for most users)")
    print("=" * 80)
    print()
    print(format_user_friendly_message(intel, decision, risk_info))
    
    # ── FORMAT 2: COMPACT ──────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("FORMAT 2: COMPACT (Quick scanning, mobile-friendly)")
    print("=" * 80)
    print()
    print(format_compact_message(intel, decision))
    
    # ── FORMAT 3: DETAILED ─────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("FORMAT 3: DETAILED (Power users, full analysis)")
    print("=" * 80)
    print()
    print(format_detailed_message(intel, decision, scan_context))
    
    # ── TEST BLOCKED SCENARIO ──────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("SCENARIO: BLOCKED TRADE (User-Friendly Format)")
    print("=" * 80)
    print()
    
    blocked_decision = {
        "status": "BLOCKED",
        "setup_type": None,
        "reason": "Momentum score too low (56 < 75)",
        "scores": {
            "confidence": 72,
            "entry_quality": 65,
            "trend_alignment": 45,
            "regime_score": 30,
            "momentum_score": 56,
        },
    }
    
    blocked_risk = {
        "blocked": True,
        "reason": "Max open trades per symbol (1/1)",
    }
    
    print(format_user_friendly_message(intel, blocked_decision, blocked_risk))
    
    # ── TEST EXPERIMENTAL SCENARIO ─────────────────────────────────────────
    print("\n" + "=" * 80)
    print("SCENARIO: EXPERIMENTAL TRADE (User-Friendly Format)")
    print("=" * 80)
    print()
    
    experimental_decision = {
        "status": "TRIGGERED_EXPERIMENTAL",
        "setup_type": "EXPERIMENTAL_SETUP",
        "reason": "Marginal setup — conf=72 eq=65 ta=45 regime=RANGE momentum=56",
        "scores": {
            "confidence": 72,
            "entry_quality": 65,
            "trend_alignment": 45,
            "regime_score": 30,
            "momentum_score": 56,
        },
    }
    
    print(format_user_friendly_message(intel, experimental_decision, risk_info))
    
    # ── TEST REVERSAL SCENARIO ─────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("SCENARIO: REVERSAL TRADE (User-Friendly Format)")
    print("=" * 80)
    print()
    
    reversal_intel = {
        "symbol": "BANKNIFTY",
        "verdict_label": "Short Buildup",
        "verdict_emoji": "🔴",
        "confidence": 78,
        "bias": "BEARISH",
        "chart_conflict": False,
        "trend": "🟠 Mild Bearish — resistance building, sellers active",
    }
    
    reversal_decision = {
        "status": "TRIGGERED_CORE",
        "setup_type": "CONFIRMED_REVERSAL",
        "reason": "Reversal confirmed: BULLISH → Short Buildup",
        "scores": {
            "confidence": 78,
            "entry_quality": 88,
            "trend_alignment": 35,
            "regime_score": 65,
            "momentum_score": 0,
        },
    }
    
    print(format_user_friendly_message(reversal_intel, reversal_decision, risk_info))


if __name__ == "__main__":
    test_all_formats()
    print("\n✓ All formats tested successfully\n")
