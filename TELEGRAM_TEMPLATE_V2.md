# Telegram Template V2 — Phase 2-4 Enhanced

## Overview

The Telegram intelligence message has been redesigned to include Phase 2-4 enhancements:
- **Phase 2**: Decision engine scores (confidence, entry quality, trend alignment, regime)
- **Phase 3**: Structured intelligence (no regex parsing)
- **Phase 4**: Trend-based trading logic (mode indicator, momentum score)

---

## New Template Structure

### 1. Header with Mode Indicator (NEW)
```
🤖 *Bot Intelligence | NIFTY*
_🎯 Mode: Hybrid [RESEARCH]_
```

**Mode Emojis:**
- 🛡️ Conservative (highest win rate)
- ⚖️ Balanced (steady growth)
- ⚡ Aggressive (reversal hunting)
- 🎯 Hybrid (recommended)

**Research Tag:** Shows `[RESEARCH]` when `PAPER_RESEARCH_MODE = True`

---

### 2. Verdict & Confidence (Enhanced)
```
🟢 *Verdict: Long Buildup*
_Bullish — fresh longs / heavy put writing_
Confidence: 95%
⚠️ _Chart conflict: 1H vs 3H signals disagree — reduce size, wait alignment_
```

**Chart Conflict Warning:** Only shown when 1H and 3H disagree

---

### 3. OI Analysis (Unchanged)
```
📊 *OI Analysis*
CE OI: `50.00L` ↓ (-2.00L)
PE OI: `65.00L` ↑ (+8.00L)
_Writers adding puts — supporting downside_
```

---

### 4. Key Levels (Unchanged)
```
📍 *Key Levels*
Spot: `22515` | PCR: `1.30`
Support: `22400` | Resistance: `22600` | MaxPain: `22450`
ATM Straddle: `180` pts
```

---

### 5. Chart Status (Enhanced)
```
📉 *Chart Status*
*1H*: 🟢 BULLISH | 🕯️ O:22480.0 H:22520.0 L:22470.0 C:22515.0 🔥 | (ST)
*3H*: 🟢 BULLISH | 🕯️ O:22450.0 H:22530.0 L:22440.0 C:22510.0 🔥 | (ST)
```

**Momentum Tags:**
- 🔥 Price near high (bullish momentum)
- ❄️ Price near low (bearish momentum)

**Indicator Tags:**
- (ST) SuperTrend active
- RSI `67.2` OB (overbought)
- RSI `28.5` OS (oversold)

---

### 6. Bull/Bear Forces (Unchanged)
```
*BULL FORCES (Criticality Order)*
- P1 [88] Price x OI verdict: Long Buildup
- P1 [85] PCR supportive (1.30)
- P2 [80] Spot momentum +0.35%
- P2 [78] 3H chart bullish
- P2 [75] Put writing visible

*BEAR FORCES (Criticality Order)*
- P3 [40] No strong bearish factor
```

**Priority Levels:**
- P1: Critical (score ≥ 90)
- P2: Important (score 70-89)
- P3: Minor (score < 70)

---

### 7. Trade Strategy (Unchanged)
```
*TRADE STRATEGY*
- Bias: Bullish — fresh longs / heavy put writing
- Action Plan: Trail SL on longs. Avoid blind chase.
- Critical Warning: Thesis invalid if spot breaks below 22400
```

---

### 8. Paper Trade Idea (Unchanged)
```
*PAPER TRADE (Specific)*
- Buy 22500 CE at current scan | SL spot 22400 | Target spot 22600
```

---

### 9. Trade Decision Engine (NEW - Phase 2-4)
```
🎯 *TRADE DECISION ENGINE*
Status: ✅ *APPROVED (Core Setup)*
Setup: 📈 Trend Continuation
Scores: Conf:95% | EQ:🟢100 | TA:🟢75 | Reg:🟢70 | Mom:🟢82
_All trend persistence filters passed_
```

**Status Types:**
- ✅ APPROVED (Core Setup) - High-quality trade
- 🧪 APPROVED (Experimental) - Research mode trade
- ❌ BLOCKED - Trade rejected

**Setup Types:**
- 🔄 Confirmed Reversal (aggressive, high R:R)
- 📈 Trend Continuation (safe, high win rate)
- ⚡ Momentum Trade (balanced)
- 🧪 Experimental Setup (research only)

**Score Indicators:**
- 🟢 Good (≥70 for most, ≥75 for momentum)
- 🟡 Marginal (50-69, 60-74 for momentum)
- 🔴 Poor (<50, <60 for momentum)

**Score Types:**
- **Conf**: Confidence (0-100%)
- **EQ**: Entry Quality (0-100)
- **TA**: Trend Alignment (0-100)
- **Reg**: Regime Score (0-100)
- **Mom**: Momentum Score (0-100, hybrid mode only)

**Additional Indicators:**
- ⚠️ Risk Block: Shows risk engine rejection reason
- ⚠️ Conflicts: Shows soft conflicts (e.g., CHART_CONFLICT_1H_3H)

---

### 10. Broader Trend (Unchanged)
```
🌊 *Broader Trend:* 🟢 Strong Bullish Trend — persistent put writing + long buildup
```

**Trend Labels:**
- 🟢 Strong Bullish Trend
- 🟡 Mild Bullish
- 🔴 Strong Bearish Trend
- 🟠 Mild Bearish
- ⚪ Rangebound
- ⚪ Mixed
- ⚪ High Activity

---

### 11. Footer (Unchanged)
```
_Based on 3 signals this scan_
```

---

## Example Messages

### Example 1: Core Setup Approved (Conservative Mode)
```
🤖 *Bot Intelligence | NIFTY*
_🛡️ Mode: Conservative_

🟢 *Verdict: Long Buildup*
_Bullish — fresh longs / heavy put writing_
Confidence: 85%

[... OI, Levels, Chart sections ...]

🎯 *TRADE DECISION ENGINE*
Status: ✅ *APPROVED (Core Setup)*
Setup: 📈 Trend Continuation
Scores: Conf:85% | EQ:🟢85 | TA:🟢80 | Reg:🟢75
_All trend persistence filters passed_

🌊 *Broader Trend:* 🟢 Strong Bullish Trend — persistent put writing + long buildup
```

---

### Example 2: Experimental Setup (Hybrid Mode)
```
🤖 *Bot Intelligence | BANKNIFTY*
_🎯 Mode: Hybrid [RESEARCH]_

🔴 *Verdict: Short Buildup*
_Bearish — fresh shorts / heavy call writing_
Confidence: 68%

[... OI, Levels, Chart sections ...]

🎯 *TRADE DECISION ENGINE*
Status: 🧪 *APPROVED (Experimental)*
Setup: 🧪 Experimental Setup
Scores: Conf:68% | EQ:🟡65 | TA:🟡55 | Reg:🟡60 | Mom:🟡62
_Marginal setup — conf=68 eq=65 ta=55 regime=RANGE momentum=62_

🌊 *Broader Trend:* ⚪ Mixed — no dominant trend yet
```

---

### Example 3: Blocked Trade (Balanced Mode)
```
🤖 *Bot Intelligence | NIFTY*
_⚖️ Mode: Balanced_

🟢 *Verdict: Long Buildup*
_Bullish — fresh longs / heavy put writing_
Confidence: 72%
⚠️ _Chart conflict: 1H vs 3H signals disagree — reduce size, wait alignment_

[... OI, Levels, Chart sections ...]

🎯 *TRADE DECISION ENGINE*
Status: ❌ *BLOCKED*
Scores: Conf:72% | EQ:🟢75 | TA:🔴45 | Reg:🟡65 | Mom:🔴58
_Momentum score too low (58 < 75)_
⚠️ Conflicts: CHART_CONFLICT_1H_3H

🌊 *Broader Trend:* ⚪ Rangebound — balanced OI activity on both sides
```

---

### Example 4: Reversal Trade (Aggressive Mode)
```
🤖 *Bot Intelligence | NIFTY*
_⚡ Mode: Aggressive_

🟢 *Verdict: Long Buildup*
_Bullish — fresh longs / heavy put writing_
Confidence: 78%

[... OI, Levels, Chart sections ...]

🎯 *TRADE DECISION ENGINE*
Status: ✅ *APPROVED (Core Setup)*
Setup: 🔄 Confirmed Reversal
Scores: Conf:78% | EQ:🟢90 | TA:🔴35 | Reg:🟡65
_Reversal confirmed: BEARISH → Long Buildup_

🌊 *Broader Trend:* 🟠 Mild Bearish — resistance building, sellers active
```

---

## Benefits of New Template

### 1. Transparency
- Users see exactly why a trade was approved/blocked
- Score breakdown shows which factors are strong/weak
- Mode indicator shows current trading strategy

### 2. Education
- Users learn what makes a good setup
- Score thresholds are visible (green/yellow/red)
- Setup types explain the trade rationale

### 3. Confidence
- Decision engine validation adds credibility
- Risk blocks prevent overtrading
- Soft conflicts warn without blocking

### 4. Actionability
- Clear status (approved/blocked)
- Setup type guides trade management
- Scores help size positions (higher scores = larger size)

---

## Configuration Impact

### Conservative Mode
```
TREND_FILTER_MODE = "conservative"
```
- Fewer "APPROVED" messages
- Higher average scores when approved
- More "BLOCKED" due to trend persistence filter

### Balanced Mode
```
TREND_FILTER_MODE = "balanced"
```
- Moderate approval rate
- Momentum score shown
- Blocks when momentum < 75

### Aggressive Mode
```
TREND_FILTER_MODE = "aggressive"
```
- More "APPROVED" on reversals
- Lower trend alignment scores (counter-trend)
- Reversal setup type common

### Hybrid Mode (Recommended)
```
TREND_FILTER_MODE = "hybrid"
```
- Best balance of approvals
- Multiple setup types (reversal, continuation, momentum)
- Experimental trades in research mode

---

## Implementation Details

### Files Modified
- `src/engine/intelligence.py` - Added decision engine section

### Backward Compatibility
- ✅ All existing fields preserved
- ✅ New section only added if decision engine available
- ✅ Graceful fallback on errors

### Performance
- Minimal overhead (~10ms per message)
- Decision engine already runs for paper trading
- No additional database queries

---

## Testing

Run the template test:
```bash
python scratch/test_telegram_template.py
```

Expected output: Full Telegram message with decision engine section

---

## Future Enhancements

1. **Interactive Buttons** (Telegram Bot API)
   - "Override Block" button for manual trades
   - "Adjust Size" button based on scores
   - "View History" button for past decisions

2. **Score Trends** (requires history tracking)
   - "EQ improving: 65 → 75 → 85"
   - "TA declining: 80 → 70 → 60"

3. **Personalized Thresholds** (per user)
   - Conservative users: higher thresholds
   - Aggressive users: lower thresholds

---

## Conclusion

The redesigned Telegram template provides full transparency into the Phase 2-4 decision engine while maintaining backward compatibility. Users now see:

1. ✅ Trading mode (conservative/balanced/aggressive/hybrid)
2. ✅ Trade decision status (approved/blocked)
3. ✅ Setup type (reversal/continuation/momentum)
4. ✅ Score breakdown (confidence, entry quality, trend, regime, momentum)
5. ✅ Risk blocks and soft conflicts

This empowers users to understand and trust the bot's decisions.
