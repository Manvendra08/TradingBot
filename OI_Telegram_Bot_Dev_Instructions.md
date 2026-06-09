# OI Telegram Bot v2.0 - Developer Instructions

## 1. Core Objective
Build a Telegram alert engine that:
- Interprets OI + price + candles correctly
- Separates Bias, Trade Status, and Paper Trade
- Avoids premature trades
- Produces clean, decision-ready messages

---

## 2. Output Structure (MANDATORY)
Always include:
- Bias: Bullish / Bearish / Mixed
- Trade Status: ELIGIBLE / WAIT / NO TRADE / WATCH BREAKOUT
- Paper Trade: TRIGGERED / PENDING / BLOCKED / EXITED

---

## 3. OI Interpretation Rules

### Call Side
- Short Buildup → Bearish (call writing)
- Long Buildup → Bullish
- Short Covering → Bullish
- Long Unwinding → Bearish

### Put Side
- Short Buildup → Bullish (put writing)
- Long Buildup → Bearish (put buying)
- Short Covering → Bearish (support weakening)
- Long Unwinding → Bearish

---

## 4. Signal Filtering
Ignore low-quality signals:

if old_oi < threshold and new_oi < threshold:
    mark as LOW CONFIDENCE

---

## 5. Bias Engine

score = 0

if CE_short_buildup: score -= weight
if CE_short_covering: score += weight

if PE_short_buildup: score += weight
if PE_short_covering: score -= weight
if PE_long_buildup: score -= weight

Classification:
- score >= +3 → Bullish
- score <= -3 → Bearish
- else → Mixed

---

## 6. Trade Status Logic

if confidence < 60:
    NO TRADE
elif bias conflicts with candles:
    WAIT
elif strong confluence:
    ELIGIBLE
else:
    WAIT

---

## 7. Paper Trade Logic

Trigger ONLY if:
- Bias is strong
- Trade Status = ELIGIBLE
- Candles align
- Good risk/reward
- Proper entry zone

Paper Trade States:
- TRIGGERED
- PENDING
- BLOCKED
- EXITED

---

## 8. Entry Rules

Bearish: Enter on bounce failure near resistance
Bullish: Enter after breakout/reclaim

---

## 9. Level Calculation

Resistance = CE OI cluster or ATM+1
Support = PE OI cluster or ATM-1

Avoid extreme far levels

---

## 10. Hard Rules

- No trade if confidence < 60
- Candle conflict blocks trade
- No contradictory messaging
- Always define entry condition

---

## 11. Final Principle

Bias ≠ Trade

Trade only when:
Bias + Confirmation + Entry + Risk-Reward align

