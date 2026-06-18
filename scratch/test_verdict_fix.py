"""
Simulate the exact NATURALGAS alert scenario:
  price_pct = -0.22%, ce_oi = +185, pe_oi = +1500, PCR = 2.27
  Key signal: 315 CE OI SPIKE HIGH
"""
import sys
sys.path.insert(0, ".")

from src.engine.intelligence import _price_oi_verdict

# Simulate a HIGH severity CE spike alert (315 CE OI SPIKE +18.6%)
mock_alerts = [{
    "alert_type": "OI_SPIKE",
    "option_type": "CE",
    "strike": 315,
    "severity": "HIGH",
    "detail_json": '{"pct_change": 18.6, "prev_oi": 1700, "curr_oi": 2017}'
}]

label, emoji, desc = _price_oi_verdict(
    price_pct=-0.22,
    net_oi_change=185 + 1500,
    ce_oi_change=185,
    pe_oi_change=1500,
    pcr=2.27,
    alerts=mock_alerts,
)
print(f"Verdict: {emoji} {label}")
print(f"Desc:    {desc}")
print()
print("Expected: Put Writing / OI Bias Bullish (NOT Short Buildup)")
print()

# Also test old scenario that must still work:
# Genuine Short Buildup: price down, CE >>PE
label2, emoji2, _ = _price_oi_verdict(-0.5, 800, 800, -200, pcr=0.7)
print(f"Genuine Short Buildup: {emoji2} {label2}  (expected Short Buildup 🔴)")

# Long Buildup: price up, both building, PE heavy
label3, emoji3, _ = _price_oi_verdict(0.3, 2000, 200, 1800, pcr=1.8)
print(f"Long Buildup:          {emoji3} {label3}  (expected Long Buildup 🟢)")
