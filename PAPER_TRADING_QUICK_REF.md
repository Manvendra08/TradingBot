# Paper Trading - Quick Reference Card

## 🚀 Phase 1 New Features (May 25, 2026)

### **6 Enhanced KPI Metrics**
```
┌─────────────┬──────────────────────────────────────────────┐
│ Metric      │ What It Tells You                            │
├─────────────┼──────────────────────────────────────────────┤
│ Total       │ All trades + open count                      │
│ Win Rate    │ Success % + wins/losses breakdown            │
│ Total P&L   │ Closed trades profit/loss                    │
│ Avg P&L     │ Per-trade average + win/loss comparison      │
│ Profit      │ Total wins ÷ total losses (>1.5 = good)      │
│ Factor      │ ∞ means no losses yet                        │
│ Streak      │ Current consecutive wins                     │
└─────────────┴──────────────────────────────────────────────┘
```

### **Symbol Performance Breakdown**
```
Each symbol shows:
  • Total trades
  • Win rate (color-coded: green ≥60%, yellow 40-60%, red <40%)
  • Average P&L per trade
  • Total P&L contribution

Use this to: Identify which symbols are most profitable
```

### **Trade Duration**
```
Format Examples:
  45s      = Less than 1 minute (ultra-fast scalp)
  20m      = 20 minutes (quick trade)
  1h 15m   = 1 hour 15 minutes (swing trade)

Use this to: Optimize holding periods, identify scalping vs swing
```

---

## 🎨 Visual Elements

### **Status Badges**
```
🟡 [OPEN]         = Active position
🟢 [TARGET HIT]   = Successful trade (hit target)
🔴 [STOP LOSS]    = Loss trade (hit SL)
🔵 [MANUAL]       = Manually closed
```

### **Color Coding**
```
🟢 Green  = Positive P&L, high win rate, good metrics
🟡 Yellow = Medium performance, warnings, open trades
🔴 Red    = Negative P&L, losses, stop losses
🔵 Blue   = Neutral metrics, manual actions
```

---

## 📊 Key Metrics Interpretation

### **Profit Factor**
```
Formula: Total Wins ÷ Total Losses

> 2.0   = Excellent (every ₹1 lost makes ₹2)
1.5-2.0 = Good (sustainable strategy)
1.0-1.5 = Marginal (needs improvement)
< 1.0   = Losing (stop trading!)
∞       = No losses yet (all wins)
```

### **Win Rate**
```
Formula: (Wins ÷ Total Closed) × 100

≥ 60%  = Green (strong strategy)
40-60% = Yellow (acceptable if good profit factor)
< 40%  = Red (needs work)

Note: 40% win rate with 3:1 avg win/loss beats 60% with 1:1
```

### **Avg Win vs Avg Loss**
```
Ideal: Avg Win should be ≥ 2× Avg Loss

Example:
  Avg Win:  +15.40
  Avg Loss: -8.20
  Ratio:    1.88 (good, close to 2:1)

This means: Even with 50% win rate, you're profitable
```

---

## 🔍 Quick Workflows

### **30-Second Check**
1. Open http://localhost:8080/paper
2. Glance at KPI cards (win rate, P&L, profit factor)
3. Check symbol breakdown (best performer?)
4. Done!

### **5-Minute Analysis**
1. Review all KPIs
2. Compare symbol performance
3. Check equity curve trend
4. Review recent trades + duration
5. Filter by symbol/status if needed

### **Strategy Optimization**
1. Filter by specific symbol
2. Check win rate + profit factor
3. Analyze trade durations
4. Compare avg win vs avg loss
5. Identify best verdicts
6. Adjust bot parameters

---

## 🎯 What to Look For

### **Healthy Strategy Signs**
✅ Profit factor > 1.5
✅ Win rate > 50% (or high avg win/loss ratio)
✅ Consistent equity curve (upward trend)
✅ Positive P&L across multiple symbols
✅ Reasonable trade durations (not too short/long)

### **Warning Signs**
⚠️ Profit factor < 1.2
⚠️ Win rate < 40% with low avg win/loss
⚠️ Declining equity curve
⚠️ One symbol dragging down performance
⚠️ Extremely short durations (overtrading?)

### **Red Flags**
🚨 Profit factor < 1.0 (losing money!)
🚨 Win rate < 30%
🚨 Negative P&L across all symbols
🚨 Equity curve in freefall
🚨 Consecutive losses (no wins)

---

## 🛠️ Filters & Controls

### **Symbol Filter**
```
ALL         = Show all symbols
NIFTY       = NIFTY trades only
BANKNIFTY   = BANKNIFTY trades only
NATURALGAS  = NATURALGAS trades only
```

### **Status Filter**
```
ALL           = All trades
OPEN          = Active positions only
TARGET HIT    = Successful trades only
STOP LOSS     = Loss trades only
MANUAL        = Manually closed only
```

### **Auto-Refresh**
```
Automatic: Every 30 seconds
Manual:    Click [⟳ Refresh] button
```

---

## 📱 Device Support

### **Desktop** (>1200px)
- Full 6-column KPI grid
- 3-column symbol breakdown
- Side-by-side equity curve + open trades
- All table columns visible

### **Tablet** (768px-1200px)
- 3-column KPI grid
- 2-column symbol breakdown
- Stacked equity curve + open trades
- Horizontal scroll for table

### **Mobile** (<768px)
- Stacked KPI cards
- Single-column symbol breakdown
- Full-width sections
- Horizontal scroll for table

---

## 🔗 Navigation

```
Main Dashboard  →  http://localhost:8080/
Paper Trading   →  http://localhost:8080/paper

From paper page:
  [← Main Dashboard] button = Return to main dashboard
  [⟳ Refresh] button        = Manual refresh
```

---

## 💡 Pro Tips

1. **Focus on profit factor first** - More important than win rate
2. **Compare symbols** - Trade what works best
3. **Monitor duration** - Optimize holding periods
4. **Check streak** - Builds confidence, but don't overtrade
5. **Use filters** - Focus analysis on specific symbols/statuses
6. **Review regularly** - Check after each trading session
7. **Look for patterns** - Which verdicts work best?
8. **Don't chase losses** - If profit factor < 1.0, stop and analyze

---

## 🐛 Troubleshooting

### **Page not loading?**
```
1. Check if dashboard_server.py is running
2. Verify URL: http://localhost:8080/paper
3. Check browser console for errors
4. Try hard refresh (Ctrl+F5)
```

### **Data not updating?**
```
1. Click [⟳ Refresh] button
2. Check if main bot is running (generating trades)
3. Verify database connection
4. Check browser console for API errors
```

### **Filters not working?**
```
1. Clear filters (select "ALL" for both)
2. Refresh page
3. Check if trades exist for selected filters
```

---

## 📚 Related Documentation

- `PAPER_TRADING_REVIEW.md` - Original problem analysis
- `PAPER_TRADING_PHASE1_COMPLETE.md` - Complete implementation guide
- `PAPER_TRADING_VISUAL_GUIDE.md` - Visual transformation guide
- `AGENTS.md` - Bot configuration and rules

---

## 🎓 Key Takeaways

1. **Profit Factor > Win Rate** - You can be profitable with 40% win rate if avg win is 3× avg loss
2. **Symbol Comparison** - Not all symbols perform equally, focus on winners
3. **Duration Matters** - Helps identify if you're scalping or swing trading
4. **Visual Clarity** - Color coding and badges make analysis instant
5. **Regular Review** - Check after each session to optimize strategy

---

## ✅ Quick Checklist

Before each trading session:
- [ ] Check profit factor (should be > 1.5)
- [ ] Review symbol breakdown (which to focus on?)
- [ ] Check equity curve trend (upward?)
- [ ] Review recent trades (any patterns?)
- [ ] Verify open positions (if any)

After each trading session:
- [ ] Review new trades
- [ ] Check if win rate maintained
- [ ] Verify profit factor still healthy
- [ ] Analyze trade durations
- [ ] Identify improvement areas

---

*Phase 1 Complete - Professional Trading Dashboard Live!*
*Next: Phase 2 (Market Context, Greeks Tracking, Advanced Analytics)*
