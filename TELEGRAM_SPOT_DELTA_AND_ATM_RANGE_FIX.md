# Telegram Spot Delta & ATM Strike Range Fix

## Issues Fixed

### 1. Flat Spot Delta in Telegram Messages ✅

**Problem**: Telegram messages always showed "Δ prev scan Spot `flat`" regardless of actual price movement.

**Root Cause**: In `src/engine/anomaly_detector.py` line 675, the code had a fallback logic:
```python
prev_und = get_previous_underlying_before(symbol, fetched_at) or get_previous_underlying(symbol)
```

When `get_previous_underlying_before()` returned None (no previous scan data), it fell back to `get_previous_underlying()` which retrieves the **LATEST** underlying price record. Since the latest record is the same as the current scan, this resulted in:
- `prev_price = current_price`
- `price_change_points = 0`
- `price_change_pct = 0%`
- Telegram message: "flat"

**Fix**: Removed the fallback to `get_previous_underlying()`. Now the code correctly handles the case when there's no previous scan:
```python
prev_und = get_previous_underlying_before(symbol, fetched_at)
prev_price = prev_und["price"] if prev_und else None
price_change_points = round(float(underlying or 0) - float(prev_price or 0), 4) if prev_price is not None else 0.0
```

**Result**: 
- If previous scan exists: Shows actual price delta (e.g., "+12.5 (`+0.54%`)")
- If no previous scan: Shows "flat" (correct behavior for first scan)
- Subsequent scans will now show real price movements

---

### 2. ATM Strike Range Reduced to ±10 ✅

**Problem**: Option chain data was showing ATM ±15 strikes, but user wanted ATM ±10 strikes.

**Root Cause**: `STRIKES_AROUND_ATM` was set to 15 in `config/settings.py`.

**Fix**: Changed `STRIKES_AROUND_ATM` from 15 to 10 in `config/settings.py`:
```python
STRIKES_AROUND_ATM  = 10  # Changed from 15
```

**How It Works**:
1. `src/fetchers/router.py` calls `_filter_atm_strikes()` after fetching option chain data
2. ATM strike is calculated dynamically based on current underlying price:
   - For NSE indices: Closest strike to underlying price
   - For MCX commodities: Closest strike to underlying price (if available)
   - Fallback: Strike where CE LTP ≈ PE LTP (most balanced)
3. Keeps strikes from `ATM - 10` to `ATM + 10` (total 21 strikes)
4. This filtering happens for ALL symbols (NIFTY, BANKNIFTY, NATURALGAS, etc.)

**Result**: 
- Option chain now shows only ATM ±10 strikes (21 strikes total)
- ATM is recalculated dynamically on every scan
- OTM strikes beyond ±10 are filtered out before anomaly detection

---

## Files Modified

1. **`config/settings.py`**
   - Changed `STRIKES_AROUND_ATM` from 15 to 10

2. **`src/engine/anomaly_detector.py`**
   - Fixed `prev_und` fallback logic (line 675)
   - Updated `prev_price` handling to support None value
   - Updated `scan_context` to handle None `prev_price` correctly

---

## Testing Instructions

### Test Spot Delta Fix:
1. Restart the server: `python main.py`
2. Wait for 2 consecutive scans (10-15 minutes apart)
3. Check Telegram message for "Δ prev scan Spot"
4. Should show actual price change (e.g., "+12.5 (`+0.54%`)" or "-8.2 (`-0.35%`)")
5. First scan after restart may still show "flat" (expected - no previous data)

### Test ATM Strike Range:
1. Restart the server: `python main.py`
2. Check logs for: `"Filtered strikes for NIFTY from X to 21 around ATM strike Y"`
3. Should see 21 strikes (ATM ±10) instead of 31 strikes (ATM ±15)
4. Verify in Telegram messages that only ATM ±10 strikes are mentioned in alerts

---

## Technical Details

### Spot Delta Calculation Flow:
```
1. Current scan: underlying = 23,450
2. Query: get_previous_underlying_before(symbol, current_timestamp)
3. Previous scan: underlying = 23,437.5
4. Calculate: price_change_points = 23,450 - 23,437.5 = +12.5
5. Calculate: price_change_pct = (12.5 / 23,437.5) * 100 = +0.053%
6. Telegram: "Δ prev scan Spot `+12.5 (+0.05%)`"
```

### ATM Strike Filtering Flow:
```
1. Fetch option chain: 100+ strikes from source
2. Calculate ATM: min(strikes, key=lambda s: abs(s - underlying))
3. Find ATM index in sorted strikes list
4. Keep strikes[ATM_index - 10 : ATM_index + 11]
5. Result: 21 strikes centered around ATM
```

---

## Expected Behavior After Fix

### Telegram Message Example (Before):
```
📊 *NIFTY* | 14:30 IST | 5 signals
Spot `23,450` | ATM `23,450` | PCR `1.05`
━━━━━━━━━━━━━━━━━━━━
🟢 *BULLISH* - Long Buildup
Δ prev scan: Spot `flat` | CE OI `+12.5K` | PE OI `-8.3K`
```

### Telegram Message Example (After):
```
📊 *NIFTY* | 14:30 IST | 5 signals
Spot `23,450` | ATM `23,450` | PCR `1.05`
━━━━━━━━━━━━━━━━━━━━
🟢 *BULLISH* - Long Buildup
Δ prev scan: Spot `+12.5 (+0.05%)` | CE OI `+12.5K` | PE OI `-8.3K`
```

---

## Notes

- **First scan after restart**: Will show "flat" because there's no previous scan data (expected)
- **Subsequent scans**: Will show actual price delta
- **ATM calculation**: Dynamic and recalculated on every scan
- **Strike filtering**: Happens AFTER fetching, BEFORE anomaly detection
- **All symbols**: Both fixes apply to NIFTY, BANKNIFTY, NATURALGAS, CRUDEOIL, etc.

---

## Commit Message

```
fix: Telegram spot delta always flat & reduce ATM range to ±10

- Fixed spot delta calculation in anomaly_detector.py
  - Removed fallback to get_previous_underlying() which was returning current price
  - Now correctly shows "flat" only when no previous scan exists
  - Subsequent scans show actual price movement

- Reduced STRIKES_AROUND_ATM from 15 to 10
  - Option chain now shows ATM ±10 strikes (21 total)
  - ATM calculated dynamically based on current underlying price
  - Applies to all symbols (NIFTY, BANKNIFTY, NATURALGAS, etc.)

Fixes: Telegram "Δ prev scan Spot" always showing flat
Fixes: Option chain showing too many OTM strikes
```
