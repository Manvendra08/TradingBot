# NSEBOT Options Engine — Strategy & Execution

> **Generated:** June 22, 2026 | **Method:** AGoT Reasoning Graph
> **Purpose:** Deep dive into options-specific trading logic, strike selection, premium management, and timeframe strategies.
> Read after `order-flow.md` to understand options-specific execution.

---

## 1. Options Chain Processing

### Data Normalization
**Entry:** `fetchers/router.py` → `_filter_atm_strikes(result)`

The options engine processes **ATM ± STRIKES_AROUND_ATM** strikes (default: ±10 = 21 strikes total). This window provides:
- Sufficient OTM strikes for writing strategies
- ATM strikes for directional plays
- Limited data volume for fast processing

### Strike Filtering Algorithm
```python
# Find ATM strike
if underlying price available:
    atm_strike = closest strike to underlying
else:
    # Fallback: find strike where CE LTP ≈ PE LTP
    atm_strike = strike with min(|CE_LTP - PE_LTP|)

# Keep ATM ± STRIKES_AROUND_ATM
start_idx = max(0, atm_index - STRIKES_AROUND_ATM)
end_idx = min(len(strikes), atm_index + STRIKES_AROUND_ATM + 1)
result["strikes"] = strikes[start_idx:end_idx]
```

### Option Chain Schema (Post-Processing)
```python
{
    "symbol": "NIFTY",
    "underlying_price": 24850.0,
    "expiry": "2026-06-25",
    "atm_strike": 24850.0,          # Derived
    "total_ce_oi": 15000000,        # Aggregated
    "total_pe_oi": 12000000,        # Aggregated
    "pcr": 0.80,                    # PE OI / CE OI
    "max_pain": 24800.0,            # Calculated
    "support": 24750.0,             # From OI buildup
    "resistance": 24950.0,          # From OI buildup
    "strikes": [
        {
            "strike": 24800.0,
            "option_type": "CE",
            "ltp": 120.5,
            "ltp_change_pct": 5.2,
            "oi": 1500000,
            "oi_change_pct": 8.3,
            "oi_change": 115000,
            "volume": 50000,
            "iv": 12.5,
            "bid": 120.0,
            "ask": 121.0,
            "delta": 0.65,
        },
        # ... PE at same strike, then adjacent strikes
    ]
}
```

---

## 2. Strike Selection Logic

### Verdict → Action Mapping
**File:** `src/engine/paper_plan.py`

```python
VERDICT_ACTION_MAP = {
    # Bullish verdicts
    "Long Buildup":    ("BUY",  "CE"),    # Buy call option
    "Put Writing":     ("SELL", "PE"),    # Sell put (collect premium at support)
    "Short Covering":  ("BUY",  "CE"),    # Buy call (shorts covering)
    "OI Bias Bullish": ("SELL", "PE"),    # Sell put (OI shift to CE)
    "GO_LONG":         ("BUY",  "CE"),    # LLM bullish directive

    # Bearish verdicts
    "Short Buildup":   ("BUY",  "PE"),    # Buy put option
    "Call Writing":    ("SELL", "CE"),    # Sell call (collect premium at resistance)
    "Long Unwinding":  ("BUY",  "PE"),    # Buy put (longs exiting)
    "OI Bias Bearish": ("SELL", "CE"),    # Sell call (OI shift to PE)
    "GO_SHORT":        ("BUY",  "PE"),    # LLM bearish directive
}
```

### Strike Selection Rules

| Side | Option Type | Strike Selection | Rationale |
|------|------------|-----------------|-----------|
| BUY | CE | ATM strike | Maximum delta, lower time decay |
| BUY | PE | ATM strike | Maximum delta, lower time decay |
| SELL | CE | Resistance level (or ATM + 3*step) | Collect premium at resistance |
| SELL | PE | Support level (or ATM - 3*step) | Collect premium at support |
| BUY/SELL | FUT | ATM strike | Direct underlying exposure |

### Strike Selection Code
```python
if option_type in ("CE", "PE"):
    if side == "SELL":
        if option_type == "CE":
            # Sell CE at resistance (above underlying)
            strike = round_to_step(resistance, step) if resistance > underlying
                   else round_to_step(underlying + step * MAX_LEVEL_DISTANCE_STEPS, step)
        else:
            # Sell PE at support (below underlying)
            strike = round_to_step(support, step) if support < underlying
                   else round_to_step(underlying - step * MAX_LEVEL_DISTANCE_STEPS, step)
    else:
        # BUY: use ATM
        strike = atm
```

**MAX_LEVEL_DISTANCE_STEPS:** Default 3. If support/resistance is more than 3 strike-steps away, use ATM instead to avoid illiquid deep OTM strikes.

---

## 3. Premium Calculation & SL/Target

### Premium Resolution
**File:** `src/engine/trade_plan.py` → `get_option_premium()`

```python
def get_option_premium(symbol, expiry, strike, option_type, option_rows):
    """
    Find the LTP for a specific option contract from the option chain.
    Returns None if not found (triggers BLOCKED_PLAN).
    """
    for row in option_rows:
        if (abs(float(row["strike"]) - strike) < 0.01 and
            row["option_type"].upper() == option_type.upper()):
            ltp = float(row.get("ltp") or 0.0)
            return ltp if ltp > 0 else None
    return None
```

### SL/Target Calculation (Two Methods)

#### Method 1: ATR-Based (Futures and TIMEFRAME trades)
```python
atr = get_atr(ctx)  # 14-period ATR from 3H or 1H chart

if side == "BUY":
    sl_underlying = entry_underlying - 1.5 * atr
    target_underlying = entry_underlying + 2.0 * atr
elif side == "SELL":
    sl_underlying = entry_underlying + 1.5 * atr
    target_underlying = entry_underlying - 2.0 * atr
```

**R:R Ratio:** 1:1.33 (1.5 ATR risk, 2.0 ATR reward)

#### Method 2: Support/Resistance-Based (Options)
```python
if side == "BUY":
    sl_underlying = support if support and support < underlying
                  else underlying - 2 * step
    target_underlying = resistance if resistance and resistance > underlying
                      else underlying + 2 * step
elif side == "SELL":
    sl_underlying = resistance if resistance and resistance > underlying
                  else underlying + 2 * step
    target_underlying = support if support and support < underlying
                      else underlying - 2 * step
```

### Premium SL/Target Conversion
**File:** `src/engine/trade_plan.py` → `convert_underlying_sl_to_premium()`

Since options are traded at premium prices but SL/Target is calculated on the underlying, we convert:

```python
def convert_underlying_sl_to_premium(
    underlying, sl_underlying, target_underlying,
    entry_premium, side, option_type, strike, option_rows
):
    """
    Convert underlying-based SL/Target to premium-based equivalents.
    Uses delta approximation from the option chain.
    """
    # Find delta for the option
    delta = find_option_delta(option_rows, strike, option_type)
    if not delta:
        delta = 0.5  # Default ATM delta approximation

    # Premium change ≈ delta × underlying change
    sl_premium = entry_premium + delta * (sl_underlying - underlying)
    target_premium = entry_premium + delta * (target_underlying - underlying)

    # For SELL side, invert the logic
    if side == "SELL":
        sl_premium = entry_premium + delta * (underlying - sl_underlying)
        target_premium = entry_premium + delta * (underlying - target_underlying)

    return sl_premium, target_premium
```

---

## 4. MCX Commodity Options vs Futures

### Decision Tree
**File:** `src/engine/paper_plan.py` → `mcx_option_liquidity_ok()`

```
Symbol in (NATURALGAS, CRUDEOIL, GOLD, SILVER)?
    │
    ├── NO → Use standard options (CE/PE)
    │
    └── YES → Check ATM liquidity
              │
              ├── Total ATM volume >= 500 AND Total ATM OI >= 2000?
              │   │
              │   ├── YES → Use options (CE/PE)
              │   │         Strike = ATM
              │   │         Side = BUY/SELL based on verdict
              │   │
              │   └── NO → Fall back to FUT
              │            Strike = ATM
              │            Side = BUY (bullish) / SELL (bearish)
              │            SL = 1.5x ATR, Target = 2.0x ATR
```

### Liquidity Thresholds
```python
_MCX_OPTION_MIN_VOLUME = 500    # Minimum total volume (CE + PE) at ATM
_MCX_OPTION_MIN_OI = 2000       # Minimum total open interest (CE + PE) at ATM
```

### MCX-Specific Considerations
1. **Contract Rollover:** MCX contracts expire monthly. `DHAN_SECURITY_IDS` must be updated.
2. **Lot Sizes:** Larger than NSE (NATURALGAS: 1250, CRUDEOIL: 100)
3. **Market Hours:** 9:00 AM - 11:30 PM IST (includes Saturday session)
4. **Lower OI Volumes:** MCX has lower absolute OI than NSE indices
5. **Per-Symbol Thresholds:** Tighter anomaly thresholds for MCX:
   - NATURALGAS: OI threshold 10%, LTP threshold 4%
   - CRUDEOIL: OI threshold 15%, LTP threshold 5%
   - GOLD: OI threshold 20%, LTP threshold 5%

---

## 5. Timeframe Strategy (3H Entry / 1H Exit)

### Strategy Overview
A secondary strategy that uses **completed candle crossovers** on higher timeframes:
- **Entry:** 3-hour candle breakout
- **Exit:** 1-hour candle reversal
- **Confirmation:** OI bias must support the direction

### Entry Logic
**File:** `src/engine/paper_trading.py` → `run_timeframe_strategy()`

```python
# Prerequisites
pay_3h = chart_indicators.get("3h")  # 3H candle data
pay_1h = chart_indicators.get("1h")  # 1H candle data
older_scan = get_scan_summary_at_least_1h_old(symbol)  # OI history

# Calculate breakout buffer (ATR-based)
atr_val = get_atr(ctx)
breakout_buffer = max(atr_val * 0.5, underlying * 0.003)

# OI bias confirmation
current_ce = ctx.get("total_ce_oi")
current_pe = ctx.get("total_pe_oi")
prev_ce = older_scan["total_ce_oi"]
prev_pe = older_scan["total_pe_oi"]
ce_diff = current_ce - prev_ce
pe_diff = current_pe - prev_pe
min_diff_pct = TIMEFRAME_OI_MIN_DIFF_PCT  # 0.5%

long_oi_support = (pe_diff - ce_diff) > (prev_pe * min_diff_pct)
short_oi_support = (ce_diff - pe_diff) > (prev_ce * min_diff_pct)

# Entry triggers
c_3h_close = float(pay_3h["ohlc"]["close"])
p_3h_high = float(pay_3h["prev_ohlc"]["high"])
p_3h_low = float(pay_3h["prev_ohlc"]["low"])

is_long_trigger = (c_3h_close > p_3h_high + breakout_buffer) and long_oi_support
is_short_trigger = (c_3h_close < p_3h_low - breakout_buffer) and short_oi_support
```

### Exit Logic

#### 1. 1H Candle Crossover Exit
```python
c_1h_close = float(pay_1h["ohlc"]["close"])
p_1h_high = float(pay_1h["prev_ohlc"]["high"])
p_1h_low = float(pay_1h["prev_ohlc"]["low"])

# LONG exit: 1H close below previous 1H low
if trade["verdict_label"] == "LONG":
    if c_1h_close < p_1h_low:
        crossover_size = p_1h_low - c_1h_close
        if crossover_size > 2 * breakout_buffer:
            # Large reversal — exit immediately
            close_trade("TF-1H-Cross", "Large reversal move")
        elif short_oi_support:
            # OI confirms reversal — exit
            close_trade("TF-1H-Cross", "1H close below prev low + Short OI bias")

# SHORT exit: 1H close above previous 1H high
elif trade["verdict_label"] == "SHORT":
    if c_1h_close > p_1h_high:
        crossover_size = c_1h_close - p_1h_high
        if crossover_size > 2 * breakout_buffer:
            close_trade("TF-1H-Cross", "Large reversal move")
        elif long_oi_support:
            close_trade("TF-1H-Cross", "1H close above prev high + Long OI bias")
```

#### 2. Dead Trade Exit
```python
# If 3 hours pass and max favorable R < 0.5, exit
time_elapsed = (current_bar_end - trade_opened_at).total_seconds()
if time_elapsed >= 3.0 * 3600 - 60:  # ~3 hours
    if max_favorable_R < 0.5:
        close_trade("Dead Trade", f"3 hours passed, max R {max_fav:.2f} < 0.5")
```

#### 3. LLM Reversal Exit
```python
# If AI bias contradicts trade direction with high confidence
if trade["verdict_label"] == "LONG" and ai_bias == "BEARISH":
    if ai_confidence >= 70:
        close_trade("LLM_REVERSAL", f"LLM bias {ai_bias} (conf {ai_conf}%)")
```

#### 4. Premium SL/Target (Options)
```python
if option_type in ("CE", "PE"):
    exit_premium = get_option_premium(...)
    if side == "BUY":
        hit_sl = exit_premium <= sl_premium
        hit_target = exit_premium >= target_premium
    elif side == "SELL":
        hit_sl = exit_premium >= sl_premium
        hit_target = exit_premium <= target_premium
```

#### 5. Underlying SL/Target (Futures)
```python
if option_type == "FUT":
    if verdict == "LONG":
        hit_sl = underlying <= sl_underlying
        hit_target = underlying >= target_underlying
    elif verdict == "SHORT":
        hit_sl = underlying >= sl_underlying
        hit_target = underlying <= target_underlying
```

---

## 6. Pyramid Management

### Pyramid Rules
```
Max levels: 3 per direction
Direction lock: All pyramid trades must be same direction (LONG or SHORT)
Profitability gate: At least one open trade must be profitable to add level

Lot scaling:
    Level 1: 1.00x DEFAULT_LOTS_PER_TRADE
    Level 2: 0.75x DEFAULT_LOTS_PER_TRADE
    Level 3: 0.50x DEFAULT_LOTS_PER_TRADE
```

### Pyramid Entry Validation
```python
open_trades = get_open_timeframe_trades(symbol)

# Max 3 levels
if len(open_trades) >= 3:
    return "BLOCKED_PLAN", "Maximum pyramid level (3) reached"

# Direction consistency
if any(t["verdict_label"] != direction for t in open_trades):
    return "BLOCKED_PLAN", "Cannot pyramid in opposite direction"

# Profitability gate
any_profitable = False
for t in open_trades:
    if option_type in ("CE", "PE"):
        current_premium = get_option_premium(...)
        if side == "BUY":
            is_profitable = current_premium > t["entry_premium"]
        elif side == "SELL":
            is_profitable = current_premium < t["entry_premium"]
    else:  # FUT
        if verdict == "LONG":
            is_profitable = underlying > t["entry_underlying"]
        elif verdict == "SHORT":
            is_profitable = underlying < t["entry_underlying"]
    if is_profitable:
        any_profitable = True
        break

if not any_profitable:
    return "BLOCKED_PLAN", "No profitable open trades to pyramid"
```

### Max Favorable R Tracking
```python
# Track the maximum favorable R-multiple reached during trade life
if option_type in ("CE", "PE"):
    if side == "BUY":
        r_current = (exit_premium - entry_premium) / (entry_premium - sl_premium)
    elif side == "SELL":
        r_current = (entry_premium - exit_premium) / (sl_premium - entry_premium)
else:  # FUT
    if verdict == "LONG":
        r_current = (underlying - entry_underlying) / (entry_underlying - sl_underlying)
    elif verdict == "SHORT":
        r_current = (entry_underlying - underlying) / (sl_underlying - entry_underlying)

max_favorable_R = max(trade.get("max_favorable_r", 0.0), r_current)
# Persist to DB for dead trade exit evaluation
UPDATE paper_trades SET max_favorable_r = ? WHERE id = ?
```

---

## 7. Transaction Cost Model

### Cost Structure
**File:** `config/settings.py` → `TRANSACTION_COSTS`

```python
TRANSACTION_COSTS = {
    "OPTIONS": {
        "flat_brokerage": 20.0,              # ₹20 per order (Zerodha/Dhan)
        "stt_pct_turnover": 0.000625,        # 0.0625% of sell-side premium
    },
    "FUTURES": {
        "flat_brokerage": 20.0,              # ₹20 per order
        "stt_pct_turnover": 0.0001,          # 0.01% of turnover
    },
}
```

### Cost Calculation Example
```
Trade: BUY 1 lot NIFTY 24850 CE @ ₹120
Lot size: 50
Premium turnover: 120 × 50 = ₹6,000

Round-trip costs:
  Brokerage: ₹20 × 2 (buy + sell) = ₹40
  STT (sell-side): 0.0625% × 6000 = ₹3.75
  Exchange charges: ~₹5
  GST: ~₹3
  Total: ~₹52 per trade

Breakeven premium move: ₹52 / 50 = ₹1.04 per share
```

---

## 8. Options-Specific Risk Checks

### Premium-Based SL Monitoring
Options trades have **dual SL/Target monitoring**:
1. **Underlying-based:** SL hit when underlying crosses `sl_underlying`
2. **Premium-based:** SL hit when premium crosses `sl_premium`

This dual monitoring catches scenarios where:
- Underlying barely misses SL but premium decays significantly (theta decay)
- Premium spikes on IV expansion even though underlying hasn't moved

### Expiry Handling
```python
# Options expire on last Thursday of month (NSE) or month-end (MCX)
# The system uses the nearest expiry from the option chain
expiry = oc_data["expiry"]  # e.g., "2026-06-25"

# Near expiry, the engine should:
# 1. Reduce position sizing (gamma risk)
# 2. Tighten SL (theta acceleration)
# 3. Prefer ITM strikes (higher delta, lower theta)
```

### IV Monitoring
```python
# IV spike at ATM (>20% change) triggers anomaly alert
# IV crush (>15% drop) triggers anomaly alert
# High IV environment → prefer SELL strategies (collect premium)
# Low IV environment → prefer BUY strategies (cheap options)
```

---

## 9. Options Strategy Matrix

| Market Condition | Preferred Strategy | Strike | Side | Rationale |
|-----------------|-------------------|--------|------|-----------|
| Strong trend up | Long Buildup → BUY CE | ATM | BUY | Maximum delta capture |
| Strong trend down | Short Buildup → BUY PE | ATM | BUY | Maximum delta capture |
| Range-bound (support) | Put Writing → SELL PE | Support | SELL | Collect premium at support |
| Range-bound (resistance) | Call Writing → SELL CE | Resistance | SELL | Collect premium at resistance |
| Short covering rally | Short Covering → BUY CE | ATM | BUY | Momentum play |
| Long unwinding fall | Long Unwinding → BUY PE | ATM | BUY | Momentum play |
| OI shift bullish | OI Bias Bullish → SELL PE | ATM-1 | SELL | OI confirms direction |
| OI shift bearish | OI Bias Bearish → SELL CE | ATM+1 | SELL | OI confirms direction |
| High IV + no trend | NO_TRADE | — | — | Wait for clarity |
| Low IV + breakout | BUY CE/PE | ATM | BUY | Cheap options, breakout potential |

---

## 10. Debugging Options-Specific Issues

### Premium Not Available
```
Symptom: "BLOCKED_PLAN: Option premium unavailable for CE strike 24850"
Cause: Option chain doesn't include the selected strike (filtered out)
Fix: Increase STRIKES_AROUND_ATM or check if strike is valid
```

### SL Hit Immediately
```
Symptom: Trade opens and closes within same scan
Cause: SL_underlying is too close to entry (e.g., support = entry)
Fix: Check support/resistance calculation in anomaly_detector
      Verify MAX_LEVEL_DISTANCE_STEPS fallback is working
```

### Pyramid Not Firing
```
Symptom: Second 3H breakout but no pyramid entry
Cause: No open trade is profitable yet
Fix: Check max_favorable_R tracking
      Verify profitability gate logic
      Consider lowering profitability threshold
```

### Dead Trade Exit Too Early
```
Symptom: Trade closed after 3 hours with small profit
Cause: max_favorable_R threshold (0.5) too aggressive
Fix: Increase threshold in paper_trading.py
      Or increase time window from 3 hours to 4 hours
```

### MCX Options Falling Back to FUT
```
Symptom: NATURALGAS trading FUT instead of options
Cause: ATM volume < 500 or OI < 2000
Fix: Check MCX liquidity thresholds
      Consider lowering thresholds if liquidity has improved
      Verify option chain includes ATM strikes
```

---

**Last Updated:** June 22, 2026
**Analysis Method:** Adaptive Graph of Thoughts (AGoT)
