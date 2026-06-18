"""
Test the redesigned Telegram template with Phase 2-4 context
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.engine.intelligence import generate_intelligence


def test_telegram_template():
    """Generate a sample intelligence message to see the new format."""
    print("=" * 80)
    print("TELEGRAM TEMPLATE TEST - Phase 2-4 Enhanced Format")
    print("=" * 80)
    
    # Mock scan context
    symbol = "NIFTY"
    scan_context = {
        "underlying": 22500.0,
        "price_change_pct": 0.35,
        "total_ce_oi": 5000000,
        "total_pe_oi": 6500000,
        "ce_oi_change": -200000,
        "pe_oi_change": 800000,
        "pcr": 1.30,
        "max_pain": 22450,
        "support": 22400,
        "resistance": 22600,
        "atm_strike": 22500,
        "straddle_premium": 180,
        "expiry": "30MAY2026",
        "chart_indicators": {
            "1h": {
                "sentiment": "BULLISH",
                "indicators": [
                    {"name": "SuperTrend 10,3", "sentiment": "BULLISH"},
                ],
                "ohlc": {
                    "open": 22480,
                    "high": 22520,
                    "low": 22470,
                    "close": 22515,
                }
            },
            "3h": {
                "sentiment": "BULLISH",
                "indicators": [
                    {"name": "SuperTrend 10,3", "sentiment": "BULLISH"},
                ],
                "ohlc": {
                    "open": 22450,
                    "high": 22530,
                    "low": 22440,
                    "close": 22510,
                }
            }
        }
    }
    
    # Mock alerts
    current_alerts = [
        {
            "alert_type": "BUILDUP_CLASSIFY",
            "option_type": "PE",
            "strike": 22500,
            "severity": "HIGH",
            "detail_json": '{"buildup_type": "Long Buildup"}',
        },
        {
            "alert_type": "OI_SPIKE",
            "option_type": "PE",
            "strike": 22500,
            "severity": "HIGH",
        },
        {
            "alert_type": "VOLUME_AGGRESSION",
            "option_type": "PE",
            "strike": 22500,
            "severity": "MEDIUM",
        },
    ]
    
    print("\nGenerating intelligence with new template...\n")
    
    try:
        intel = generate_intelligence(symbol, current_alerts, scan_context)
        
        print(intel.telegram_text)
        
        print("\n" + "=" * 80)
        print("STRUCTURED FIELDS (for API/DB)")
        print("=" * 80)
        print(f"Verdict Label: {intel.verdict_label}")
        print(f"Verdict Emoji: {intel.verdict_emoji}")
        print(f"Bias: {intel.bias}")
        print(f"Confidence: {intel.confidence}%")
        print(f"Chart Conflict: {intel.chart_conflict}")
        print(f"Trend: {intel.trend}")
        print(f"Bull Forces: {len(intel.bull_forces)} factors")
        print(f"Bear Forces: {len(intel.bear_forces)} factors")
        
        print("\n✓ Template generation successful")
        
    except Exception as e:
        print(f"✗ Error generating template: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(test_telegram_template())
