import os
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)

# Load .env
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    k, v = parts
                    v = v.strip("\"'")
                    os.environ[k] = v

from src.engine.llm_enrichment import get_llm_verdict

# Mock data
intel = {
    "verdict_label": "OI Bias Bullish",
    "verdict_desc": "Strong PE writing at support",
    "bias": "BULLISH",
    "confidence": 75,
    "trend": "UPTREND",
    "chart_conflict": False,
    "days_to_expiry": 3,
    "bull_forces": [(2, "PCR is rising"), (3, "OI wall support built at ATM-1")],
    "bear_forces": [(1, "Resistance at ATM+2")]
}

scan_context = {
    "underlying": 23500.0,
    "prev_underlying": 23450.0,
    "price_change_points": 50.0,
    "price_change_pct": 0.21,
    "atm_strike": 23500.0,
    "support": 23400.0,
    "resistance": 23600.0,
    "max_pain": 23500.0,
    "pcr": 1.15,
    "total_ce_oi": 1500000,
    "total_pe_oi": 1725000,
    "ce_oi_change": -50000,
    "pe_oi_change": 120000,
    "chart_indicators": {
        "1h": {
            "ohlc": {"open": 23480.0, "high": 23520.0, "low": 23470.0, "close": 23500.0},
            "sentiment": "BULLISH"
        },
        "3h": {
            "ohlc": {"open": 23440.0, "high": 23520.0, "low": 23430.0, "close": 23500.0},
            "sentiment": "BULLISH"
        }
    }
}

print("Running LLM enrichment test...")
verdict = get_llm_verdict("NIFTY", intel, scan_context)
if verdict:
    print("\nSUCCESS! Generated verdict:")
    print(verdict.model_dump_json(indent=2))
else:
    print("\nFAILED to generate verdict")
