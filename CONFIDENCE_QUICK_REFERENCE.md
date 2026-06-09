# Confidence % — Quick Reference

## One-Line Definition
**Confidence % = How strong and confirmed are the signals in THIS scan?**

---

## The 5 Components

| Component | Points | What It Checks |
|-----------|--------|---|
| **OI Strength** | +0 to +40 | How extreme is the OI spike? |
| **Chart Alignment** | +15 to -15 | Do 1H and 3H candles confirm? |
| **PCR Support** | +0 to +5 | Does Put-Call Ratio support direction? |
| **Multiple Alerts** | +0 to +5 | Are there 3+ HIGH severity alerts? |
| **Mixed OI** | -10 | Are both CE and PE building (choppy)? |

**Base:** 50 points  
**Range:** 30-100 (minimum 30, maximum 100)

---

## Quick Scoring

### OI Spike Strength
```
> 200%  → +40 (Extreme)
> 100%  → +30 (Very Strong)
> 50%   → +20 (Strong)
> 30%   → +10 (Moderate)
< 30%   → +0  (Weak)
```

### Chart Alignment
```
Both 1H & 3H agree    → +15 (Perfect)
One agrees            → +10 (Partial)
Conflict              → -15 (Negative)
```

### PCR Support
```
Bullish + PCR > 1.2   → +5
Bearish + PCR < 0.8   → +5
Neutral               → +0
```

### Multiple Alerts
```
3+ HIGH severity      → +5
< 3 HIGH severity     → +0
```

### Mixed OI
```
Both building         → -10 (Choppy)
One-sided            → +0  (Clear)
```

---

## Score Interpretation

| Score | Signal | Action |
|-------|--------|--------|
| 90-100 | 🔥 Extreme | Strong buy/sell |
| 75-89 | ⚡ Very Strong | Good entry |
| 60-74 | 📈 Strong | Reasonable entry |
| 45-59 | ⚠️ Moderate | Wait for confirmation |
| 30-44 | ❄️ Weak | Avoid or reduce size |

---

## What It IS

✅ Signal strength of THIS scan  
✅ How confirmed are the signals?  
✅ How extreme is the OI spike?  
✅ Do charts align with verdict?  
✅ Is there multiple confirmation?  

---

## What It IS NOT

❌ Probability of winning trade  
❌ Win rate  
❌ Risk/Reward ratio  
❌ Market direction (that's verdict)  
❌ Trend strength (that's trend alignment)  

---

## Real Examples

### NATURALGAS 100%
- OI spike: 235% (extreme)
- Charts: 1H BULLISH + 3H BULLISH (perfect)
- PCR: 2.68 (very bullish)
- Alerts: 5 HIGH (multiple)
- **Result:** 100% = Extreme signal

### NIFTY 72%
- OI spike: 85% (moderate)
- Charts: 1H BULLISH + 3H NEUTRAL (partial)
- PCR: 1.25 (mildly bullish)
- Alerts: 2 HIGH (not enough)
- **Result:** 72% = Strong signal

### BANKNIFTY 45%
- OI spike: 35% (weak)
- Charts: 1H BULLISH + 3H BEARISH (conflict)
- PCR: 1.05 (neutral)
- Alerts: 1 HIGH (not enough)
- **Result:** 45% = Moderate signal

---

## How It's Used

### In Telegram
```
Confidence: ██████████ 100%
```

### In Trade Decision
```
if confidence >= 70:
    # Strong signal, consider trading
elif confidence >= 50:
    # Moderate signal, be cautious
else:
    # Weak signal, wait
```

### In Paper Trading
```
if confidence >= 70 AND entry_quality >= 70:
    # Approve trade
elif confidence >= 50 AND entry_quality >= 50:
    # Experimental (research mode)
else:
    # Block trade
```

---

## Code Location

**File:** `src/alerts/digest.py`  
**Function:** `_calculate_confidence_score()` (line 552)  
**Called from:** `build_digest_enhanced()` (line 967)

---

## Key Takeaway

**Confidence % tells you how strong THIS scan's signals are.**

It does NOT tell you if the trade will win.

That depends on:
- **Trend Alignment** (multi-scan confirmation)
- **Entry Quality** (strike selection)
- **Regime** (market condition)
- **Risk Management** (SL/target)

---

## Formula

```
Score = 50 (base)
      + OI_strength (0-40)
      + chart_alignment (15 to -15)
      + pcr_support (0-5)
      + multiple_alerts (0-5)
      - mixed_oi_penalty (0-10)

Confidence = max(30, min(100, Score))
```

---

## Remember

🎯 **Confidence % = Signal Strength**

Not win probability, not trend strength, not entry quality.

Just: **How strong and confirmed are the signals in THIS scan?**
