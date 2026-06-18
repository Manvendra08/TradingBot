"""Phase 3 smoke test — verify IntelligenceResult fields are set natively."""
import logging
logging.basicConfig(level=logging.WARNING)

from src.engine.intelligence import generate_intelligence, generate_intelligence_structured, IntelligenceResult

ctx = {
    "underlying": 24050.0, "atm_strike": 24050.0, "expiry": "2026-06-26",
    "support": 23900.0, "resistance": 24200.0, "pcr": 0.65,
    "total_ce_oi": 400000, "total_pe_oi": 650000,
    "ce_oi_change": -15000, "pe_oi_change": 45000, "price_change_pct": 0.9,
    "chart_indicators": {
        "1h": {"sentiment": "BULLISH", "indicators": []},
        "3h": {"sentiment": "BULLISH", "indicators": []},
    },
}
alerts = [
    {"alert_type": "BUILDUP_CLASSIFY", "option_type": "PE", "strike": 23900.0,
     "severity": "HIGH", "detail_json": '{"buildup_type": "Long Buildup", "pct_change": 35}'},
]

# Test 1: generate_intelligence returns IntelligenceResult
result = generate_intelligence("NIFTY", alerts, scan_context=ctx)
assert isinstance(result, IntelligenceResult), f"Expected IntelligenceResult, got {type(result)}"
assert result.verdict_label != "", f"verdict_label is empty"
assert result.confidence > 0, f"confidence is 0"
assert result.bias in ("BULLISH", "BEARISH", "NEUTRAL"), f"bad bias: {result.bias}"
assert result.telegram_text != "", f"telegram_text is empty"
assert isinstance(result.chart_conflict, bool), f"chart_conflict not bool"
print(f"T1 PASS | verdict={result.verdict_label} conf={result.confidence} bias={result.bias} conflict={result.chart_conflict}")

# Test 2: dict-like access still works
assert result["verdict_label"] == result.verdict_label
assert result.get("confidence") == result.confidence
assert result.get("missing_key", 42) == 42
print("T2 PASS | dict-like access OK")

# Test 3: 'in' operator works on telegram_text
assert result.verdict_label in result, f"verdict_label not found in result"
assert "Confidence:" in result, f"Confidence not in result"
print("T3 PASS | 'in' operator OK")

# Test 4: str() returns telegram_text
assert str(result) == result.telegram_text
print("T4 PASS | str() returns telegram_text")

# Test 5: generate_intelligence_structured returns same IntelligenceResult
result2 = generate_intelligence_structured("NIFTY", alerts, scan_context=ctx)
assert isinstance(result2, IntelligenceResult)
assert result2.verdict_label == result.verdict_label
assert result2.confidence == result.confidence
print("T5 PASS | generate_intelligence_structured returns IntelligenceResult")

# Test 6: No regex anywhere — verify fields are set directly
assert result.bull_forces is not None
assert result.bear_forces is not None
assert result.action_plan != ""
print(f"T6 PASS | bull_forces={len(result.bull_forces)} bear_forces={len(result.bear_forces)} action={result.action_plan[:40]}")

# Test 7: Low-conviction scan returns IntelligenceResult with correct fields
ctx_flat = {**ctx, "ce_oi_change": 0, "pe_oi_change": 0, "price_change_pct": 0.0,
            "chart_indicators": {}}
result_flat = generate_intelligence("NIFTY", [], scan_context=ctx_flat)
assert isinstance(result_flat, IntelligenceResult)
assert result_flat.verdict_label == "Low Conviction"
assert result_flat.bias == "NEUTRAL"
print(f"T7 PASS | low-conviction: verdict={result_flat.verdict_label} bias={result_flat.bias}")

# Test 8: Verify no regex import needed in generate_intelligence_structured
import inspect
src = inspect.getsource(generate_intelligence_structured)
assert "re.search" not in src, "regex still in generate_intelligence_structured!"
print("T8 PASS | zero regex in generate_intelligence_structured")

print("\n=== ALL PHASE 3 SMOKE TESTS PASSED ===")
