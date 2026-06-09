# Confidence % — What It Actually Measures

## Location
**File:** `src/alerts/digest.py`  
**Function:** `_calculate_confidence_score()` (line 552)

---

## What It Is

The confidence % is a **composite score (0-100)** that measures how strong and confirmed the current scan's signals are.

**NOT** the probability of a winning trade. It's the **signal strength** of THIS scan.

---

## How It's Calculated

### Base Score: 50 points
```python
score = 50  # Starting point
```

---

### 1. Primary Signal Strength (+0 to +40 points)
**What:** Maximum OI change % across all alerts in this scan

```python
if max_oi_pct > 200:
    score += 40  # Extreme OI spike
elif max_oi_pct > 100:
    score += 30  # Very strong OI spike
elif max_oi_pct > 50:
    score += 20  # Strong OI spike
elif max_oi_pct > 30:
    score += 10  # Moderate OI spike
```

**Example:**
- OI spike of 235% → +40 points
- OI spike of 120% → +30 points
- OI spike of 45% → +20 points
- OI spike of 25% → +0 points

---

### 2. Candle Confirmation (+15 to -15 points)
**What:** Do 1H and 3H charts agree with the verdict?

```python
# BULLISH verdict
if candles_1h == "BULLISH" and candles_3h == "BULLISH":
    score += 15  # Perfect alignment
elif candles_1h == "BULLISH" or candles_3h == "BULLISH":
    score += 10  # Partial alignment
elif candles_1h == "BEARISH" or candles_3h == "BEARISH":
    score -= 15  # Conflict (reduces confidence)

# BEARISH verdict (mirror logic)
```

**Example:**
- Bullish verdict + 1H BULLISH + 3H BULLISH → +15 points
- Bullish verdict + 1H BULLISH + 3H NEUTRAL → +10 points
- Bullish verdict + 1H BEARISH + 3H NEUTRAL → -15 points

---

### 3. PCR Support (+0 to +5 points)
**What:** Does Put-Call Ratio support the verdict?

```python
if is_bullish_verdict and pcr > 1.2:
    score += 5  # High PCR = bullish (puts protective)
elif is_bearish_verdict and pcr < 0.8:
    score += 5  # Low PCR = bearish (calls expensive)
```

**Example:**
- Bullish verdict + PCR 1.30 → +5 points
- Bearish verdict + PCR 0.75 → +5 points
- Neutral PCR → +0 points

---

### 4. Multiple Confirmations (+0 to +5 points)
**What:** How many HIGH severity alerts in this scan?

```python
high_severity_count = sum(1 for a in alerts if a.get("severity") == "HIGH")
if high_severity_count >= 3:
    score += 5  # 3+ HIGH severity signals
```

**Example:**
- 5 HIGH severity alerts → +5 points
- 2 HIGH severity alerts → +0 points

---

### 5. Mixed OI Penalty (-10 points)
**What:** Are both CE and PE building equally? (Uncertainty)

```python
if ce_change > 0 and pe_change > 0 and min(ce_change, pe_change) > max(ce_change, pe_change) * 0.5:
    score -= 10  # Both sides building = choppy market
```

**Example:**
- CE OI +500K, PE OI +450K → -10 points (both building)
- CE OI +500K, PE OI +100K → +0 points (one-sided)

---

## Final Score Range

```python
return max(30, min(100, score))
```

**Minimum:** 30 (even weak signals get 30)  
**Maximum:** 100 (perfect alignment)

---

## Score Breakdown Example

### Scenario: NATURALGAS 100% Confidence

```
Base score:                    50
+ OI spike 235%:              +40  (max_oi_pct > 200)
+ 1H BULLISH + 3H BULLISH:    +15  (perfect candle alignment)
+ PCR 2.68 (bullish):          +5  (PCR > 1.2)
+ 3 HIGH severity alerts:      +5  (multiple confirmations)
- Mixed OI (both building):    -0  (not applicable)
─────────────────────────────────
= 115 → capped at 100 = 100%
```

**Why 100%?** Extreme OI spike + perfect chart alignment + supportive PCR + multiple HIGH alerts

---

### Scenario: Moderate Confidence (65%)

```
Base score:                    50
+ OI spike 75%:               +20  (30 < 75 < 100)
+ 1H BULLISH + 3H NEUTRAL:    +10  (partial alignment)
+ PCR 1.15 (neutral):          +0  (not > 1.2)
+ 2 HIGH severity alerts:      +0  (need 3+)
- Mixed OI (both building):   -10  (both sides active)
─────────────────────────────────
= 60 → 60%
```

**Why 60%?** Moderate OI spike + partial chart alignment + mixed OI uncertainty

---

### Scenario: Low Confidence (35%)

```
Base score:                    50
+ OI spike 25%:                +0  (< 30)
+ 1H BEARISH vs BULLISH:     -15  (conflict)
+ PCR 1.05 (neutral):          +0  (not > 1.2)
+ 1 HIGH severity alert:       +0  (need 3+)
- Mixed OI (both building):    +0  (not applicable)
─────────────────────────────────
= 35 → 35%
```

**Why 35%?** Weak OI spike + chart conflict + no multiple confirmations

---

## What It DOES NOT Measure

❌ **Probability of winning trade** - That's determined by trend alignment, regime, entry quality  
❌ **Win rate** - That's historical performance  
❌ **Risk/Reward ratio** - That's determined by SL and target levels  
❌ **Market direction** - That's the verdict (bullish/bearish)  
❌ **Trend strength** - That's trend alignment score (different metric)  

---

## What It DOES Measure

✅ **Signal strength** - How strong are the OI/price signals?  
✅ **Chart alignment** - Do candles confirm the verdict?  
✅ **Signal confirmation** - Multiple HIGH severity alerts?  
✅ **Market structure** - Is PCR supportive?  
✅ **Uncertainty** - Are both sides building (choppy)?  

---

## Interpretation Guide

| Score | Meaning | Signal Strength |
|-------|---------|-----------------|
| 90-100 | Extreme | Perfect alignment, extreme OI spike |
| 75-89 | Very Strong | Strong OI spike + chart confirmation |
| 60-74 | Strong | Moderate OI spike + partial confirmation |
| 45-59 | Moderate | Weak OI spike + mixed signals |
| 30-44 | Weak | Very weak signals, conflicts |

---

## Real-World Examples

### Example 1: NATURALGAS 100%
```
OI spike: 235% (PE)
Charts: 1H BULLISH, 3H BULLISH
PCR: 2.68 (very bullish)
Alerts: 5 HIGH severity
Result: 100% confidence
Interpretation: Extreme bullish signal, perfect alignment
```

### Example 2: NIFTY 72%
```
OI spike: 85% (PE)
Charts: 1H BULLISH, 3H NEUTRAL
PCR: 1.25 (mildly bullish)
Alerts: 2 HIGH severity
Result: 72% confidence
Interpretation: Strong signal, partial confirmation
```

### Example 3: BANKNIFTY 45%
```
OI spike: 35% (mixed)
Charts: 1H BULLISH, 3H BEARISH
PCR: 1.05 (neutral)
Alerts: 1 HIGH severity
Result: 45% confidence
Interpretation: Weak signal, chart conflict
```

---

## How It's Used

### In Telegram Message
```
Confidence: ██████████ 100%
```
Shows the signal strength visually

### In Trade Decision
```python
if confidence >= 70:
    # Strong signal, consider trading
elif confidence >= 50:
    # Moderate signal, be cautious
else:
    # Weak signal, wait for better
```

### In Paper Trading
```python
if confidence >= 70 and entry_quality >= 70:
    # Approve trade
```

---

## Key Takeaway

**Confidence % = Signal Strength of THIS Scan**

It answers: *"How strong and confirmed are the signals in this scan?"*

**NOT:** *"What's the probability this trade will win?"*

That's determined by:
- Trend alignment (multi-scan confirmation)
- Entry quality (strike selection)
- Regime (market condition)
- Risk management (SL/target)

---

## Code Location

**File:** `src/alerts/digest.py`  
**Function:** `_calculate_confidence_score()` (line 552-615)  
**Called from:** `build_digest_enhanced()` (line 967)

---

## Summary

| Component | Points | What It Measures |
|-----------|--------|-----------------|
| Base | 50 | Starting point |
| OI Strength | +0 to +40 | How extreme is the OI spike? |
| Chart Alignment | +15 to -15 | Do candles confirm verdict? |
| PCR Support | +0 to +5 | Does PCR support direction? |
| Multiple Alerts | +0 to +5 | Are there 3+ HIGH severity alerts? |
| Mixed OI | -10 | Are both sides building (uncertainty)? |
| **Total** | **30-100** | **Signal strength of this scan** |

---

**Bottom Line:** Confidence % tells you how strong THIS scan's signals are, not whether the trade will win.
