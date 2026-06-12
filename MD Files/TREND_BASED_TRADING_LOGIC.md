# Trend-Based Trading Logic — Multi-Scan Analysis

## Current State Analysis

### What's Already Working
1. **Broader Trend Computation** (`_compute_broader_trend`)
   - Analyzes last 50 alerts
   - Counts: Long Buildup, Short Buildup, OI Spikes, Volume Aggression, ATM moves
   - Outputs: Strong Bullish/Bearish, Mild Bullish/Bearish, Rangebound, Mixed
   - **Issue**: Computed but NOT used in trade decisions — only displayed in message

2. **Single-Scan Trade Triggers** (current)
   - Verdict from current scan only
   - Confidence ≥ 65% → triggers paper trade
   - No memory of previous scans' verdicts

3. **Available Historical Data**
   - `anomaly_alerts`: Last 50+ alerts per symbol with timestamps
   - `underlying_price`: Spot price history (5-min cadence)
   - `option_chain_snapshots`: Full OI/LTP/IV history per strike
   - `paper_trades`: All past trades with entry/exit/P&L

---

## Proposed Trading Logic — Trend Confirmation Framework

### Core Principle
**Don't trade on single-scan noise. Wait for multi-scan trend confirmation.**

---

## Logic 1: Trend Persistence Filter (Conservative)

### Entry Rules
```python
def should_trigger_trade(symbol, current_verdict, current_confidence, ctx):
    """
    Trigger trade ONLY if:
    1. Current scan confidence ≥ 70% (raised from 65)
    2. Broader trend aligns with current verdict
    3. Last 3 scans show consistent directional bias
    4. No conflicting chart signals (1H vs 3H)
    """
    
    # Step 1: Base confidence gate (stricter)
    if current_confidence < 70:
        return False, "Confidence too low"
    
    # Step 2: Broader trend alignment
    trend = get_broader_trend(symbol)  # from last 50 alerts
    if current_verdict in BULLISH_VERDICTS:
        if "Bearish" in trend or "Mixed" in trend:
            return False, "Broader trend not aligned — wait for trend flip confirmation"
    elif current_verdict in BEARISH_VERDICTS:
        if "Bullish" in trend or "Mixed" in trend:
            return False, "Broader trend not aligned — wait for trend flip confirmation"
    
    # Step 3: Last 3 scans consistency check
    last_3_verdicts = get_last_n_scan_verdicts(symbol, n=3)
    if len(last_3_verdicts) < 3:
        return False, "Insufficient scan history — need 3+ scans"
    
    bullish_count = sum(1 for v in last_3_verdicts if v in BULLISH_VERDICTS)
    bearish_count = sum(1 for v in last_3_verdicts if v in BEARISH_VERDICTS)
    
    if current_verdict in BULLISH_VERDICTS:
        if bullish_count < 2:  # at least 2 of last 3 must be bullish
            return False, f"Inconsistent bias — only {bullish_count}/3 scans bullish"
    elif current_verdict in BEARISH_VERDICTS:
        if bearish_count < 2:
            return False, f"Inconsistent bias — only {bearish_count}/3 scans bearish"
    
    # Step 4: Chart conflict check (already exists)
    if ctx.get("chart_conflict"):
        return False, "1H vs 3H chart conflict — wait for alignment"
    
    return True, "All trend filters passed"
```

**Pros:**
- Filters out single-scan noise
- Reduces false entries during choppy/rangebound markets
- Higher win rate (fewer bad trades)

**Cons:**
- Slower to enter (misses early trend moves)
- May enter after best R:R is gone

---

## Logic 2: Trend Momentum Scoring (Balanced)

### Entry Rules
```python
def calculate_trend_momentum_score(symbol, current_verdict, current_confidence):
    """
    Score 0-100 based on:
    - Current scan confidence (40% weight)
    - Broader trend alignment (30% weight)
    - Recent scan consistency (20% weight)
    - Chart confluence (10% weight)
    
    Trigger trade if score ≥ 75
    """
    score = 0
    
    # 1. Current scan confidence (max 40 pts)
    score += min(current_confidence * 0.4, 40)
    
    # 2. Broader trend alignment (max 30 pts)
    trend = get_broader_trend(symbol)
    if current_verdict in BULLISH_VERDICTS:
        if "Strong Bullish" in trend:
            score += 30
        elif "Mild Bullish" in trend:
            score += 20
        elif "Mixed" in trend or "Rangebound" in trend:
            score += 10
        else:  # Bearish trend
            score += 0
    elif current_verdict in BEARISH_VERDICTS:
        if "Strong Bearish" in trend:
            score += 30
        elif "Mild Bearish" in trend:
            score += 20
        elif "Mixed" in trend or "Rangebound" in trend:
            score += 10
        else:  # Bullish trend
            score += 0
    
    # 3. Recent scan consistency (max 20 pts)
    last_5_verdicts = get_last_n_scan_verdicts(symbol, n=5)
    if len(last_5_verdicts) >= 3:
        if current_verdict in BULLISH_VERDICTS:
            bullish_pct = sum(1 for v in last_5_verdicts if v in BULLISH_VERDICTS) / len(last_5_verdicts)
            score += bullish_pct * 20
        elif current_verdict in BEARISH_VERDICTS:
            bearish_pct = sum(1 for v in last_5_verdicts if v in BEARISH_VERDICTS) / len(last_5_verdicts)
            score += bearish_pct * 20
    
    # 4. Chart confluence (max 10 pts)
    chart_1h = ctx.get("chart_indicators", {}).get("1h", {}).get("sentiment")
    chart_3h = ctx.get("chart_indicators", {}).get("3h", {}).get("sentiment")
    if current_verdict in BULLISH_VERDICTS:
        if chart_1h == "BULLISH" and chart_3h == "BULLISH":
            score += 10
        elif chart_1h == "BULLISH" or chart_3h == "BULLISH":
            score += 5
    elif current_verdict in BEARISH_VERDICTS:
        if chart_1h == "BEARISH" and chart_3h == "BEARISH":
            score += 10
        elif chart_1h == "BEARISH" or chart_3h == "BEARISH":
            score += 5
    
    return score

# Trigger logic
if calculate_trend_momentum_score(symbol, verdict, confidence) >= 75:
    trigger_paper_trade()
```

**Pros:**
- Balanced approach (not too conservative, not too aggressive)
- Weighted scoring allows partial credit for weak signals
- Can enter earlier than Logic 1 if all factors align

**Cons:**
- More complex to tune thresholds
- Still may miss very early trend reversals

---

## Logic 3: Trend Reversal Detection (Aggressive)

### Entry Rules
```python
def detect_trend_reversal(symbol, current_verdict, current_confidence):
    """
    Trigger trade on EARLY trend reversal signals:
    1. Broader trend is opposite to current verdict (reversal setup)
    2. Current scan confidence ≥ 75% (high conviction)
    3. Last 2 scans show same new direction (confirmation)
    4. HIGH severity OI spike in reversal direction
    
    Example: Market in "Strong Bearish" trend, but last 2 scans show
    "Long Buildup" with HIGH PE OI spikes → early bullish reversal
    """
    
    if current_confidence < 75:
        return False, "Need high confidence for reversal trade"
    
    trend = get_broader_trend(symbol)
    last_2_verdicts = get_last_n_scan_verdicts(symbol, n=2)
    
    # Bullish reversal setup
    if current_verdict in BULLISH_VERDICTS:
        # Check if we're reversing from bearish trend
        if "Bearish" not in trend:
            return False, "Not a reversal — already in bullish/neutral trend"
        
        # Last 2 scans must agree
        if len(last_2_verdicts) < 2:
            return False, "Need 2+ scans for reversal confirmation"
        if not all(v in BULLISH_VERDICTS for v in last_2_verdicts):
            return False, "Last 2 scans not consistently bullish"
        
        # Must have HIGH severity bullish signal in current scan
        current_alerts = get_current_scan_alerts(symbol)
        high_bullish = any(
            a["severity"] == "HIGH" and 
            a["alert_type"] in ("OI_SPIKE", "BUILDUP_CLASSIFY") and
            a["option_type"] == "PE"  # PE OI spike = bullish
            for a in current_alerts
        )
        if not high_bullish:
            return False, "No HIGH severity bullish signal for reversal"
        
        return True, "Bullish reversal detected"
    
    # Bearish reversal setup (mirror logic)
    elif current_verdict in BEARISH_VERDICTS:
        if "Bullish" not in trend:
            return False, "Not a reversal — already in bearish/neutral trend"
        if len(last_2_verdicts) < 2:
            return False, "Need 2+ scans for reversal confirmation"
        if not all(v in BEARISH_VERDICTS for v in last_2_verdicts):
            return False, "Last 2 scans not consistently bearish"
        
        current_alerts = get_current_scan_alerts(symbol)
        high_bearish = any(
            a["severity"] == "HIGH" and 
            a["alert_type"] in ("OI_SPIKE", "BUILDUP_CLASSIFY") and
            a["option_type"] == "CE"  # CE OI spike = bearish
            for a in current_alerts
        )
        if not high_bearish:
            return False, "No HIGH severity bearish signal for reversal"
        
        return True, "Bearish reversal detected"
    
    return False, "Not a reversal setup"
```

**Pros:**
- Catches early trend reversals (best R:R)
- Higher profit potential per trade

**Cons:**
- Higher risk (reversal may fail)
- Needs very high confidence to avoid false reversals

---

## Logic 4: Hybrid Approach (Recommended)

### Combine all three logics with priority:

```python
def should_trigger_paper_trade(symbol, verdict, confidence, ctx):
    """
    Priority-based hybrid logic:
    1. Try reversal detection first (highest R:R if valid)
    2. Fall back to trend persistence (safest)
    3. Use momentum scoring as tiebreaker
    """
    
    # Priority 1: Reversal trade (aggressive but high R:R)
    is_reversal, reversal_reason = detect_trend_reversal(symbol, verdict, confidence)
    if is_reversal:
        return True, f"REVERSAL TRADE: {reversal_reason}"
    
    # Priority 2: Trend persistence (conservative, high win rate)
    is_persistent, persist_reason = should_trigger_trade(symbol, verdict, confidence, ctx)
    if is_persistent:
        return True, f"TREND CONTINUATION: {persist_reason}"
    
    # Priority 3: Momentum scoring (balanced fallback)
    momentum_score = calculate_trend_momentum_score(symbol, verdict, confidence)
    if momentum_score >= 80:  # higher threshold for fallback
        return True, f"MOMENTUM TRADE: score={momentum_score}"
    
    # No trade
    return False, f"No trend confirmation — momentum score={momentum_score}, {persist_reason}"
```

---

## Implementation Requirements

### 1. New Database Helper Functions
```python
def get_last_n_scan_verdicts(symbol: str, n: int = 5) -> list[str]:
    """
    Query anomaly_alerts to extract verdict from last N scans.
    Use digest_id to group alerts by scan, then parse verdict from detail_json.
    """
    pass

def get_current_scan_alerts(symbol: str) -> list[dict]:
    """Return all alerts from the current scan (not yet persisted)."""
    pass

def get_broader_trend(symbol: str) -> str:
    """Wrapper around _compute_broader_trend (already exists)."""
    pass
```

### 2. Modify `run_paper_trading()` in `paper_trading.py`
Replace:
```python
plan = _trade_plan_from_verdict(verdict, confidence, ctx)
if not plan:
    return
```

With:
```python
# Apply trend-based filter
should_trade, reason = should_trigger_paper_trade(symbol, verdict, confidence, ctx)
if not should_trade:
    log.info(f"{symbol}: paper trade blocked — {reason}")
    return

plan = _trade_plan_from_verdict(verdict, confidence, ctx)
if not plan:
    return
```

### 3. Add Trend Context to Intelligence Message
In `generate_intelligence()`, after computing broader trend:
```python
trend = _compute_broader_trend(symbol, current_alerts)
trend_aligned = _is_trend_aligned(verdict_label, trend)
msg.append(f"🌊 *Broader Trend:* {trend}")
if not trend_aligned:
    msg.append("⚠️ _Current verdict conflicts with broader trend — paper trade may be blocked_")
```

---

## Configuration Tuning

Add to `config/settings.py`:
```python
# Trend-based trading config
TREND_FILTER_MODE = "hybrid"  # "conservative" | "balanced" | "aggressive" | "hybrid"
TREND_MIN_SCANS = 3  # minimum scans needed before trend-based trades
TREND_CONSISTENCY_THRESHOLD = 0.6  # 60% of last N scans must agree
MOMENTUM_SCORE_THRESHOLD = 75  # 0-100 score to trigger trade
REVERSAL_MIN_CONFIDENCE = 75  # higher bar for reversal trades
```

---

## Expected Impact

### Before (Current)
- Trades on every scan with confidence ≥ 65%
- High trade frequency
- Win rate: ~50-60% (estimated)
- Many false entries during choppy markets

### After (Hybrid Logic)
- Trades only when multi-scan trend confirms
- Lower trade frequency (30-50% fewer trades)
- Win rate: ~65-75% (estimated)
- Better R:R on reversal trades
- Fewer losses during rangebound periods

---

## Testing Strategy

1. **Backtest on historical data** (last 30 days)
   - Compare P&L: current logic vs. each proposed logic
   - Measure: win rate, avg P&L per trade, max drawdown

2. **Paper trade in parallel** (2 weeks)
   - Run both old and new logic side-by-side
   - Track which logic would have triggered on each scan
   - Compare outcomes

3. **Gradual rollout**
   - Start with "conservative" mode for 1 week
   - Switch to "hybrid" if results improve
   - Monitor daily P&L and trade count

---

## Recommendation

**Start with Logic 4 (Hybrid)** in conservative mode:
- Catches high-confidence reversals (best trades)
- Falls back to trend persistence (safe trades)
- Blocks noisy single-scan entries
- Configurable via settings (easy to tune)

This gives the best balance of safety and opportunity.
