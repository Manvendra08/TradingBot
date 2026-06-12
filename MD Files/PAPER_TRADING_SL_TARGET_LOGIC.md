# Paper Trading: SL & Target Logic

## Overview

The paper trading engine automatically calculates Stop Loss (SL) and Target levels based on the trade verdict and market conditions. This document explains the logic used for each trade type.

---

## Trade Types & Verdicts

### **1. Call Writing (Bearish)**
- **Verdict**: "Call Writing"
- **Option Type**: PE (Put)
- **Market View**: Bearish (expecting price to fall)
- **Strategy**: Short Put (sell put option)

#### SL & Target Calculation:
```
Entry Premium: Current LTP of PE option
SL Premium:    Entry × 0.70  (-30% from entry)
Target Premium: Entry × 1.50 (+50% from entry)
```

**Example:**
- Entry Premium: ₹100
- SL: ₹100 × 0.70 = ₹70 (loss limit)
- Target: ₹100 × 1.50 = ₹150 (profit target)

**Logic:**
- For short PE: Premium should decrease (profit)
- SL at -30% means if premium rises to ₹130, stop loss hits
- Target at +50% means if premium falls to ₹50, target hits

---

### **2. Put Writing (Bullish)**
- **Verdict**: "Put Writing"
- **Option Type**: CE (Call)
- **Market View**: Bullish (expecting price to rise)
- **Strategy**: Short Call (sell call option)

#### SL & Target Calculation:
```
Entry Premium: Current LTP of CE option
SL Premium:    Entry × 1.30  (+30% from entry)
Target Premium: Entry × 0.50 (-50% from entry)
```

**Example:**
- Entry Premium: ₹100
- SL: ₹100 × 1.30 = ₹130 (loss limit)
- Target: ₹100 × 0.50 = ₹50 (profit target)

**Logic:**
- For short CE: Premium should decrease (profit)
- SL at +30% means if premium rises to ₹130, stop loss hits
- Target at -50% means if premium falls to ₹50, target hits

---

### **3. Long Buildup (Bullish)**
- **Verdict**: "Long Buildup"
- **Option Type**: CE (Call)
- **Market View**: Bullish (expecting price to rise)
- **Strategy**: Long Call (buy call option)

#### SL & Target Calculation:
```
Entry Premium: Current LTP of CE option
SL Premium:    Entry × 0.70  (-30% from entry)
Target Premium: Entry × 1.50 (+50% from entry)
```

**Example:**
- Entry Premium: ₹100
- SL: ₹100 × 0.70 = ₹70 (loss limit)
- Target: ₹100 × 1.50 = ₹150 (profit target)

**Logic:**
- For long CE: Premium should increase (profit)
- SL at -30% means if premium falls to ₹70, stop loss hits
- Target at +50% means if premium rises to ₹150, target hits

---

### **4. Short Buildup (Bearish)**
- **Verdict**: "Short Buildup"
- **Option Type**: PE (Put)
- **Market View**: Bearish (expecting price to fall)
- **Strategy**: Long Put (buy put option)

#### SL & Target Calculation:
```
Entry Premium: Current LTP of PE option
SL Premium:    Entry × 1.30  (+30% from entry)
Target Premium: Entry × 0.50 (-50% from entry)
```

**Example:**
- Entry Premium: ₹100
- SL: ₹100 × 1.30 = ₹130 (loss limit)
- Target: ₹100 × 0.50 = ₹50 (profit target)

**Logic:**
- For long PE: Premium should decrease (profit)
- SL at +30% means if premium rises to ₹130, stop loss hits
- Target at -50% means if premium falls to ₹50, target hits

---

### **5. OI Bias Bullish**
- **Verdict**: "OI Bias Bullish"
- **Option Type**: PE (Put)
- **Market View**: Bullish (based on Open Interest bias)
- **Strategy**: Short Put

#### SL & Target Calculation:
Same as **Call Writing** (Bearish verdict)
```
Entry Premium: Current LTP of PE option
SL Premium:    Entry × 0.70  (-30% from entry)
Target Premium: Entry × 1.50 (+50% from entry)
```

---

### **6. OI Bias Bearish**
- **Verdict**: "OI Bias Bearish"
- **Option Type**: CE (Call)
- **Market View**: Bearish (based on Open Interest bias)
- **Strategy**: Short Call

#### SL & Target Calculation:
Same as **Put Writing** (Bullish verdict)
```
Entry Premium: Current LTP of CE option
SL Premium:    Entry × 1.30  (+30% from entry)
Target Premium: Entry × 0.50 (-50% from entry)
```

---

## Underlying-Based SL/Target (Fallback)

When option premium data is unavailable, the system falls back to underlying price-based SL/Target:

```
Entry Underlying: Current spot/futures price
SL Underlying:    Support level (or entry × 0.995 for bullish, entry × 1.005 for bearish)
Target Underlying: Resistance level (or entry × 1.01 for bullish, entry × 0.99 for bearish)
```

**Note:** This is less accurate than premium-based logic but ensures trades can still be managed.

---

## P&L Calculation

### **For Options (CE/PE):**
```
P&L Points = Exit Premium - Entry Premium
P&L Rupees = P&L Points × Lot Size × Number of Lots
```

**Example:**
- Entry Premium: ₹100
- Exit Premium: ₹120
- Lot Size: 25 (NIFTY)
- Lots: 10
- P&L Points: 120 - 100 = +20
- P&L Rupees: 20 × 25 × 10 = **+₹5,000**

### **For Futures (FUT):**
```
P&L Points = Exit Price - Entry Price
P&L Rupees = P&L Points × Lot Size × Number of Lots
```

**Example:**
- Entry Price: 280.00
- Exit Price: 290.00
- Lot Size: 1250 (NATURALGAS)
- Lots: 10
- P&L Points: 290 - 280 = +10
- P&L Rupees: 10 × 1250 × 10 = **+₹1,25,000**

---

## Lot Sizes

Default lot sizes used for P&L calculation:

| Symbol | Lot Size | Type |
|--------|----------|------|
| NIFTY | 25 | Index Options/Futures |
| BANKNIFTY | 15 | Index Options/Futures |
| FINNIFTY | 25 | Index Options/Futures |
| MIDCPNIFTY | 50 | Index Options/Futures |
| NATURALGAS | 1250 | MCX Commodity Futures |
| CRUDEOIL | 100 | MCX Commodity Futures |
| GOLD | 100 | MCX Commodity Futures |
| SILVER | 30 | MCX Commodity Futures |

**Default Lots Per Trade**: 10 lots

---

## Trade Exit Conditions

### **Premium-Based Exit (Preferred)**
Trades close when premium hits SL or Target:

```
For Long Options (CE/PE):
  - Close at TARGET if premium ≥ target_premium
  - Close at SL if premium ≤ sl_premium

For Short Options (CE/PE):
  - Close at TARGET if premium ≤ target_premium
  - Close at SL if premium ≥ sl_premium
```

### **Underlying-Based Exit (Fallback)**
If premium data unavailable, use underlying price:

```
For Bullish Trades:
  - Close at TARGET if underlying ≥ target_underlying
  - Close at SL if underlying ≤ sl_underlying

For Bearish Trades:
  - Close at TARGET if underlying ≤ target_underlying
  - Close at SL if underlying ≥ sl_underlying
```

---

## Risk/Reward Ratio

### **Typical Risk/Reward for Options:**
```
Risk = Entry Premium × 0.30 (SL at -30%)
Reward = Entry Premium × 0.50 (Target at +50%)
Ratio = Reward / Risk = 0.50 / 0.30 = 1.67:1
```

This means for every ₹1 risked, you can make ₹1.67 in profit.

### **Example with Numbers:**
- Entry Premium: ₹100
- Risk: ₹100 × 0.30 = ₹30
- Reward: ₹100 × 0.50 = ₹50
- Ratio: 50/30 = **1.67:1** (favorable)

---

## Trade Status

### **OPEN**
- Trade is active, waiting for SL or Target to hit

### **CLOSED_TARGET**
- Trade closed at target level (profit)
- Premium/Price reached target_premium/target_underlying

### **CLOSED_SL**
- Trade closed at stop loss (loss)
- Premium/Price reached sl_premium/sl_underlying

### **CLOSED_MANUAL**
- Trade manually closed by user
- May be at better/worse price than SL/Target

---

## Key Takeaways

1. **Premium-based logic is more accurate** than underlying-based for options
2. **SL is always -30% from entry** for long positions, **+30% for short positions**
3. **Target is always +50% from entry** for long positions, **-50% for short positions**
4. **Risk/Reward ratio is typically 1.67:1** (favorable for trading)
5. **P&L is calculated in rupees** using lot size and number of lots
6. **Default 10 lots per trade** means larger P&L swings (both profit and loss)

---

## Example Trade Walkthrough

### **Scenario: NIFTY Call Writing (Bearish)**

**Setup:**
- Verdict: Call Writing (Bearish)
- Symbol: NIFTY
- Strike: 24000 PE
- Entry Premium: ₹100
- Lot Size: 25
- Lots: 10

**Calculations:**
- SL Premium: 100 × 0.70 = ₹70
- Target Premium: 100 × 1.50 = ₹150

**Scenario 1: Target Hit**
- Exit Premium: ₹150
- P&L Points: 150 - 100 = +50
- P&L Rupees: 50 × 25 × 10 = **+₹12,500** ✅

**Scenario 2: SL Hit**
- Exit Premium: ₹70
- P&L Points: 70 - 100 = -30
- P&L Rupees: -30 × 25 × 10 = **-₹7,500** ❌

**Scenario 3: Manual Close at ₹110**
- Exit Premium: ₹110
- P&L Points: 110 - 100 = +10
- P&L Rupees: 10 × 25 × 10 = **+₹2,500** ✓

---

## Questions?

For more details on:
- **Verdict generation**: See `INTELLIGENCE.md`
- **Market context**: See `CHART_SOURCES.md`
- **Dashboard usage**: See `PAPER_TRADING_QUICK_REF.md`
