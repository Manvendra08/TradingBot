# User-Friendly Telegram Templates

## Overview

Three message formats designed for different user types:

1. **User-Friendly** (Recommended) - Clear, actionable, easy to understand
2. **Compact** - Quick scanning, mobile-optimized
3. **Detailed** - Power users, full technical analysis

---

## Format 1: USER-FRIENDLY (Recommended)

### Best For
- Most traders
- Mobile users
- Quick decision-making
- Non-technical users

### Example: APPROVED TRADE

```
==================================================
📊 NIFTY — TRADING SIGNAL
==================================================

🟢 BUY SIGNAL
Verdict: Long Buildup
Confidence: 85% 🔥

📝 WHAT'S HAPPENING:
  Buyers are accumulating positions. Price likely to go up. (Very High Confidence)

✅ BOT DECISION:
  Status: ✅ GO AHEAD (High Quality)
  Type: 📈 Trend Trade (Following trend)
  Why: Multiple scans confirm same direction

  Score Breakdown:
    Confidence: 🟢 ████████░░ 85%
    Entry Quality: 🟢 █████████░ 90/100
    Trend Alignment: 🟢 ███████░░░ 78%
    Market Regime: 🟢 ███████░░░ 72%
    Momentum: 🟢 ████████░░ 82%

⚠️ RISK CHECK:
  Open Trades: 1/4
  Daily Loss: ₹2,000/10,000

🎯 WHAT TO DO:
  ✅ Bot approved this trade
  → You can enter if you agree with the setup

  IF YOU TRADE BULLISH:
    • Buy Call (CE) at ATM or slightly OTM
    • Set Stop Loss below support
    • Target: Resistance level

📊 MARKET CONTEXT:
  🟢 Strong Bullish Trend — persistent put writing + long buildup

==================================================
⏰ Check back in 5 minutes for next scan
==================================================
```

### Key Features

**1. Clear Signal** (Top)
- 🟢 BUY / 🔴 SELL / ⚪ WAIT
- Verdict name
- Confidence with emoji (🔥 high, ⚡ medium, ❄️ low)

**2. Simple Explanation**
- Non-technical language
- What's happening in the market
- Why this matters

**3. Bot Decision**
- Status: ✅ GO / 🧪 RISKY / ❌ WAIT
- Setup type with explanation
- Visual score bars (green/yellow/red)

**4. Risk Check**
- Open trades count
- Daily loss tracking
- Warnings if limits approaching

**5. Action Plan**
- What to do next
- Specific trade instructions
- Entry/exit guidance

**6. Market Context**
- Broader trend
- Chart warnings (if any)

---

## Format 2: COMPACT

### Best For
- Quick scanning
- Mobile users with limited data
- Traders who want just the essentials
- Monitoring multiple symbols

### Example

```
🟢 BUY | NIFTY | Long Buildup | Conf: 85%
Decision: ✅ GO | Type: Trend
Scores: Conf:85% EQ:90 TA:78 Reg:72
```

### Interpretation

| Part | Meaning |
|------|---------|
| 🟢 BUY | Signal direction |
| NIFTY | Symbol |
| Long Buildup | Verdict |
| Conf: 85% | Confidence level |
| ✅ GO | Bot decision |
| Type: Trend | Setup type |
| Conf:85% | Confidence score |
| EQ:90 | Entry Quality |
| TA:78 | Trend Alignment |
| Reg:72 | Regime Score |

### Status Codes

| Code | Meaning |
|------|---------|
| ✅ GO | Approved, high quality |
| 🧪 RISKY | Approved, experimental |
| ❌ WAIT | Blocked, not ready |

### Setup Types

| Type | Meaning |
|------|---------|
| Reversal | Counter-trend trade (high R:R) |
| Trend | Following trend (safe) |
| Momentum | Strong confluence (balanced) |
| Experimental | Marginal setup (research) |

---

## Format 3: DETAILED

### Best For
- Power users
- Technical traders
- Backtesting/analysis
- Full transparency

### Example

```
🤖 NSEBOT INTELLIGENCE — NIFTY

🟢 BULLISH SIGNAL
Verdict: Long Buildup
Confidence: 85%

✅ TRADE APPROVED (High Quality)
Setup Type: TREND_CONTINUATION
Reason: All trend persistence filters passed

SCORE ANALYSIS:
  Confidence: 🟢 ████████░░ 85
  Entry Quality: 🟢 █████████░ 90
  Trend Alignment: 🟢 ███████░░░ 78
  Regime Score: 🟢 ███████░░░ 72
  Momentum Score: 🟢 ████████░░ 82

MARKET CONTEXT:
  Spot: 22500
  Support: 22400
  Resistance: 22600
  PCR: 1.30

BROADER TREND: 🟢 Strong Bullish Trend — persistent put writing + long buildup

NEXT STEPS:
  1. Review the setup on your chart
  2. Confirm entry and exit levels
  3. Place trade if you agree
```

### Sections

1. **Signal** - Direction and verdict
2. **Decision** - Status and reason
3. **Score Analysis** - All metrics with bars
4. **Market Context** - Key levels
5. **Broader Trend** - Multi-scan analysis
6. **Next Steps** - Action items

---

## Scenario Examples

### Scenario 1: APPROVED TRADE (High Quality)

**User-Friendly:**
```
🟢 BUY SIGNAL
Confidence: 85% 🔥

✅ BOT DECISION:
  Status: ✅ GO AHEAD (High Quality)
  Type: 📈 Trend Trade (Following trend)

🎯 WHAT TO DO:
  ✅ Bot approved this trade
  → You can enter if you agree with the setup
```

**What it means:**
- ✅ Safe to trade
- High confidence
- Multiple scans confirm
- Good entry quality

**Action:** Enter the trade

---

### Scenario 2: BLOCKED TRADE

**User-Friendly:**
```
🟢 BUY SIGNAL
Confidence: 72% ⚡

✅ BOT DECISION:
  Status: ❌ WAIT (Not Ready)

  Score Breakdown:
    Confidence: 🟢 72%
    Entry Quality: 🟡 65/100
    Trend Alignment: 🔴 45%
    Market Regime: 🔴 30%
    Momentum: 🟡 56%

⚠️ RISK CHECK:
  ❌ BLOCKED: Max open trades per symbol (1/1)

🎯 WHAT TO DO:
  ❌ Bot is not ready to trade
  → Wait for better conditions
```

**What it means:**
- ❌ Not ready to trade
- Trend alignment weak (45%)
- Market regime unfavorable (30%)
- Already at position limit

**Action:** Close existing trade or wait for better setup

---

### Scenario 3: EXPERIMENTAL TRADE

**User-Friendly:**
```
🟢 BUY SIGNAL
Confidence: 72% ⚡

✅ BOT DECISION:
  Status: 🧪 RISKY (Low Quality)
  Type: 🧪 Experimental (Research only)
  Why: Marginal setup, not recommended

🎯 WHAT TO DO:
  🧪 Bot found a marginal setup
  → Only trade if you're comfortable with higher risk
```

**What it means:**
- 🧪 Marginal setup
- Lower win rate expected
- Research mode only
- Higher risk

**Action:** Trade only if you understand the risk

---

### Scenario 4: REVERSAL TRADE

**User-Friendly:**
```
🔴 SELL SIGNAL
Confidence: 78% ⚡

✅ BOT DECISION:
  Status: ✅ GO AHEAD (High Quality)
  Type: 🔄 Reversal Trade (Counter-trend)
  Why: Market reversing from previous trend

  Score Breakdown:
    Confidence: 🟢 78%
    Entry Quality: 🟢 88/100
    Trend Alignment: 🔴 35%
    Market Regime: 🟡 65%

🎯 WHAT TO DO:
  ✅ Bot approved this trade
  → You can enter if you agree with the setup

  IF YOU TRADE BEARISH:
    • Buy Put (PE) at ATM or slightly OTM
    • Set Stop Loss above resistance
    • Target: Support level
```

**What it means:**
- ✅ High quality reversal
- Counter-trend trade (low trend alignment expected)
- High entry quality (88)
- Best R:R potential

**Action:** Enter for reversal trade

---

## Score Interpretation

### Color Coding

| Color | Range | Meaning |
|-------|-------|---------|
| 🟢 Green | ≥70 | Good, proceed |
| 🟡 Yellow | 50-69 | Marginal, caution |
| 🔴 Red | <50 | Poor, avoid |

### Score Types

**Confidence (0-100%)**
- How sure the bot is about this scan
- 🔥 ≥80% = Very high
- ⚡ 65-79% = High
- ❄️ <65% = Low

**Entry Quality (0-100)**
- How good the entry point is
- Strike selection, premium level
- 🟢 ≥70 = Good entry
- 🟡 50-69 = Acceptable entry
- 🔴 <50 = Poor entry

**Trend Alignment (0-100%)**
- How many recent scans agree
- 🟢 ≥70% = Strong trend
- 🟡 50-69% = Weak trend
- 🔴 <50% = No trend

**Regime Score (0-100)**
- Market condition favorability
- 🟢 ≥70 = Trending market
- 🟡 50-69 = Mixed market
- 🔴 <50 = Choppy market

**Momentum Score (0-100)**
- Multi-factor confluence
- 🟢 ≥75 = Strong momentum
- 🟡 60-74 = Moderate momentum
- 🔴 <60 = Weak momentum

---

## Decision Rules

### APPROVED (Core Setup) ✅
**Conditions:**
- Confidence ≥ 70%
- Entry Quality ≥ 70
- Trend Alignment ≥ 70%
- Regime Score ≥ 70
- No risk blocks

**Action:** Safe to trade

---

### APPROVED (Experimental) 🧪
**Conditions:**
- Confidence ≥ 50%
- Entry Quality ≥ 40
- Research mode enabled
- No hard risk blocks

**Action:** Trade only if comfortable with risk

---

### BLOCKED ❌
**Conditions:**
- Any score too low
- Risk limit exceeded
- Position limit reached
- Market hours violation

**Action:** Wait for better conditions

---

## Configuration by User Type

### Conservative Trader
```
TREND_FILTER_MODE = "conservative"
MOMENTUM_SCORE_THRESHOLD = 80
```
- Fewer approvals
- Higher quality trades
- Longer wait times

### Balanced Trader
```
TREND_FILTER_MODE = "balanced"
MOMENTUM_SCORE_THRESHOLD = 75
```
- Moderate approvals
- Good win rate
- Steady trades

### Aggressive Trader
```
TREND_FILTER_MODE = "aggressive"
REVERSAL_MIN_CONFIDENCE = 70
```
- More approvals
- Reversal hunting
- Higher risk

### Hybrid Trader (Recommended)
```
TREND_FILTER_MODE = "hybrid"
MOMENTUM_SCORE_THRESHOLD = 75
```
- Best balance
- Multiple setup types
- Flexible approach

---

## Tips for Using Templates

### 1. Read the Signal First
- 🟢 BUY / 🔴 SELL / ⚪ WAIT
- Confidence level
- That's all you need for quick decision

### 2. Check the Decision
- ✅ GO = Safe to trade
- 🧪 RISKY = Research only
- ❌ WAIT = Not ready

### 3. Review the Scores
- Green bars = Good
- Yellow bars = Caution
- Red bars = Problem area

### 4. Check Risk Status
- Open trades count
- Daily loss tracking
- Position limits

### 5. Follow the Action Plan
- Specific entry/exit levels
- Stop loss placement
- Target levels

---

## Switching Between Formats

### In Code
```python
from src.engine.telegram_formatter import (
    format_user_friendly_message,
    format_compact_message,
    format_detailed_message,
)

# Use based on user preference
if user_preference == "friendly":
    msg = format_user_friendly_message(intel, decision, risk_info)
elif user_preference == "compact":
    msg = format_compact_message(intel, decision)
else:  # detailed
    msg = format_detailed_message(intel, decision, scan_context)
```

### In Config
```python
# Add to settings.py
TELEGRAM_MESSAGE_FORMAT = "friendly"  # friendly | compact | detailed
```

---

## Benefits

### User-Friendly Format
✅ Easy to understand  
✅ Actionable guidance  
✅ Visual score bars  
✅ Risk warnings  
✅ Recommended for most users  

### Compact Format
✅ Quick scanning  
✅ Mobile-friendly  
✅ Low data usage  
✅ Multiple symbols at once  

### Detailed Format
✅ Full transparency  
✅ Technical analysis  
✅ Backtesting data  
✅ Power user control  

---

## Conclusion

Choose the format that matches your trading style:

- **New traders** → User-Friendly
- **Mobile traders** → Compact
- **Technical traders** → Detailed

All formats provide the same decision logic, just presented differently.

**Recommendation:** Start with User-Friendly, switch to Compact for quick scanning, use Detailed for analysis.
