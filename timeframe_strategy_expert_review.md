# Expert Review — Timeframe Strategy: Deep Quant Analysis & Reversal-Biased Redesign

## Executive Summary

Your diagnosis is directionally correct: **the current logic is too naïve** because it treats every 3H breakout as equally valid. The strategy lacks market context, so it enters chop, late trends, and whipsaw re-entries.

However, the proposed redesign is **slightly over-correcting** from “blind breakout” to “strict reversal-only”. I would not implement it exactly as written. I’d implement a **reversal-biased breakout model**, not a pure reversal-only system.

---

# 1. High-Level Verdict

## What Is Good

### Correct root cause identified

The current trigger:

```python
c_3h_close > p_3h_high + buffer
```

is too basic. It does not know whether the move is:

- early reversal
- late continuation
- range noise
- exhaustion candle
- re-entry after SL
- genuine momentum expansion

So yes, the strategy needs **context before entry**.

---

### Adding reversal context is the right direction

Requiring the previous candle to be opposite direction is a useful first filter.

For example:

```python
LONG only if previous 3H candle was bearish
SHORT only if previous 3H candle was bullish
```

This will reduce many weak continuation entries and help shift the strategy toward better reversal zones.

---

### Trend exhaustion filter is useful

Blocking entries after too many same-direction 3H candles is sensible.

But the implementation needs correction — especially around counting unique 3H candles rather than raw scan rows.

---

### Breakout quality filter is necessary

A 0.1% buffer is probably too small for BANKNIFTY, CRUDEOIL, and NATURALGAS-style instruments. It allows too many fake breakouts.

You need a volatility/range-aware filter.

---

# 2. Main Problems in the Proposed Plan

## Issue 1 — The historical proof is not strong enough

You only have **25 closed trades**. That is a very small sample.

The table shows something interesting:

| Prior Same-Direction Candles | Avg P&L |
|---|---:|
| 0 | -24,673 |
| 1-2 | -1,146 |
| 3-5 | +5,833 |
| 6+ | +32,500 |

This actually says the opposite of a pure reversal thesis.

Your “fresh breakout / reversal” bucket performed the worst, while deeper-trend buckets performed better.

So the data does **not yet prove** that reversal-only is better.

What it proves is:

> The current definition of “fresh breakout” is poor and includes a lot of noise.

That means the solution should be:

> Improve signal quality and context, not blindly force every trade to be reversal-only.

---

## Issue 2 — Previous candle opposite is not enough

This condition:

```python
prev_3h_direction == "BEARISH" and is_long_trigger
```

is too weak.

A single bearish candle before a bullish breakout may simply be chop.

Example:

```text
Bullish → Bearish → Bullish → Bearish → Bullish
```

That is not a reversal. That is a range.

Better logic:

```text
For LONG:
- Previous candle bearish
- At least 2 of last 3 candles were bearish OR market made lower high/lower low
- Current candle breaks above previous high with meaningful body
```

This keeps the system flexible but avoids noise.

---

## Issue 3 — Trend exhaustion count may be technically wrong

Your helper:

```python
SELECT candle_3h FROM scan_summaries
WHERE symbol=? AND fetched_at < ?
ORDER BY fetched_at DESC LIMIT 10
```

Potential issue: `scan_summaries` may contain **multiple rows for the same 3H candle** if scans happen every few minutes.

So this could count the same 3H candle multiple times.

Example:

```text
09:15 scan → BULLISH
09:30 scan → BULLISH
09:45 scan → BULLISH
10:00 scan → BULLISH
```

Your function may count this as 4 bullish candles, when actually it is still the same 3H candle.

### Fix

You need to count **distinct 3H candle timestamps**, not scan rows.

Ideal structure:

```python
SELECT DISTINCT candle_3h_start, candle_3h
FROM scan_summaries
WHERE symbol = ?
  AND candle_3h_start < ?
ORDER BY candle_3h_start DESC
LIMIT 10
```

If `candle_3h_start` is not available, add it.

This is important. Otherwise the exhaustion filter may incorrectly block good trades.

---

## Issue 4 — Breakout quality filter may be too aggressive

You proposed:

```python
min_breakout_distance = max(breakout_buffer, prev_3h_range * 0.3)
```

This can become too strict.

Example:

```text
Previous 3H range = 300 points
30% = 90 points
```

Now the close must be 90 points above previous high. That may enter too late and damage risk-reward.

Better:

```python
min_breakout_distance = max(
    breakout_buffer,
    min(prev_3h_range * 0.25, underlying * 0.002)
)
```

This means:

- use at least normal buffer
- use up to 25% of previous candle range
- cap the required breakout distance at around 0.2% of underlying

This avoids filtering too aggressively in high-volatility candles.

---

## Issue 5 — Structural SL idea is good, but premium conversion is weak

This part is risky:

```python
premium_at_sl = entry_premium * (1 - (underlying - structural_sl) / underlying * 2)
```

This is a rough approximation and may fail badly because option premium movement depends on:

- delta
- gamma
- IV
- time decay
- moneyness
- liquidity/spread

For paper trading, it is okay as a temporary approximation.  
For live trading, I would not use this.

Better approach:

### Option A — Track underlying SL directly

For long call / bullish trade:

```text
Exit if underlying <= structural_sl
```

For bearish trade:

```text
Exit if underlying >= structural_sl
```

This is cleaner.

Then premium exit happens at market premium when underlying SL is breached.

### Option B — Keep premium SL but adjust position size

If structural SL is too wide, do not force a premium approximation. Instead:

```text
risk_per_trade = fixed amount
position_size = risk_per_trade / expected_premium_loss
```

This is safer.

---

# 3. Recommended Redesign

I would redesign the system as a **scored reversal-breakout model**, not hard reversal-only.

Instead of one strict pass/fail condition, give each setup a quality score or apply flexible conditional gates.

---

## Entry Model

### Base trigger remains

```python
is_long_trigger = c_3h_close > p_3h_high + breakout_buffer
is_short_trigger = c_3h_close < p_3h_low - breakout_buffer
```

But entry only happens if setup quality passes.

---

## Recommended Filters

### Filter 1 — Reversal context

For LONG:

```python
prev_candle_bearish = prev_3h_close < prev_3h_open
two_of_last_three_bearish = bearish_count_last_3 >= 2
```

Allow entry if:

```python
reversal_context_long = prev_candle_bearish or two_of_last_three_bearish
```

For SHORT:

```python
reversal_context_short = prev_candle_bullish or two_of_last_three_bullish
```

This is better than requiring only one opposite candle.

---

### Filter 2 — Avoid deep trend exhaustion

Use a soft block:

```python
if prior_same_dir_streak >= 5:
    block_trade
elif prior_same_dir_streak >= 3:
    require_strong_oi_or_strong_breakout
```

Do not block all 3+ streaks blindly.

Recommended default:

```python
if prior_streak >= 5:
    return BLOCKED

if prior_streak >= 3 and not strong_oi_support:
    return BLOCKED
```

This is less restrictive and avoids killing all valid momentum trades.

---

### Filter 3 — Breakout quality

Replace fixed 30% range rule with capped range-aware logic:

```python
prev_range = p_3h_high - p_3h_low

min_breakout_distance = max(
    breakout_buffer,
    min(prev_range * 0.25, underlying * 0.002)
)
```

Then:

```python
quality_long = c_3h_close - p_3h_high >= min_breakout_distance
quality_short = p_3h_low - c_3h_close >= min_breakout_distance
```

This avoids both:

- tiny fake breakouts
- overly late entries

---

### Filter 4 — Candle body strength

Add this. It will help avoid wick-based fake breakouts.

```python
body = abs(c_3h_close - c_3h_open)
range_ = c_3h_high - c_3h_low

body_ratio = body / range_ if range_ > 0 else 0
```

Recommended minimum:

```python
body_ratio >= 0.45
```

For LONG, also prefer close near high:

```python
close_position = (c_3h_close - c_3h_low) / range_
close_position >= 0.65
```

For SHORT:

```python
close_position <= 0.35
```

This is a very useful practical filter.

---

# 4. Revised Decision Tree

## LONG Entry

```text
3H close > previous 3H high + valid breakout distance?
  └─ NO → SKIP
  └─ YES
      ├─ Reversal context exists?
      │   - previous 3H candle bearish OR
      │   - 2 of last 3 candles bearish
      │   └─ NO → SKIP or classify as continuation setup
      │
      ├─ Same-direction bullish streak too high?
      │   - 5+ bullish candles → SKIP
      │   - 3-4 bullish candles → require strong OI / strong breakout
      │
      ├─ Breakout candle quality good?
      │   - body ratio >= 45%
      │   - close in upper 35% of candle
      │
      ├─ OI supports direction?
      │   └─ NO → SKIP
      │
      └─ ENTER LONG
```

SHORT mirrors the same logic.

---

# 5. Recommended Code-Level Changes

## Add distinct candle streak logic

Do not count raw `scan_summaries` rows.

Use this style:

```python
def _count_prior_same_dir_candles(symbol: str, direction: str, before_candle_start: str) -> int:
    target = "BULLISH" if direction == "LONG" else "BEARISH"

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT candle_3h_start, candle_3h
            FROM (
                SELECT candle_3h_start, candle_3h,
                       ROW_NUMBER() OVER (
                           PARTITION BY candle_3h_start
                           ORDER BY fetched_at DESC
                       ) AS rn
                FROM scan_summaries
                WHERE symbol = ?
                  AND candle_3h_start < ?
                  AND candle_3h IS NOT NULL
            )
            WHERE rn = 1
            ORDER BY candle_3h_start DESC
            LIMIT 10
        """, (symbol, before_candle_start)).fetchall()

    streak = 0
    for r in rows:
        if r["candle_3h"] == target:
            streak += 1
        else:
            break

    return streak
```

If `candle_3h_start` does not exist, add it. This is important.

---

## Add candle quality helper

```python
def _candle_quality(open_, high, low, close, direction):
    candle_range = high - low
    if candle_range <= 0:
        return False, {"reason": "invalid_range"}

    body = abs(close - open_)
    body_ratio = body / candle_range
    close_position = (close - low) / candle_range

    if body_ratio < 0.45:
        return False, {
            "reason": "weak_body",
            "body_ratio": round(body_ratio, 2)
        }

    if direction == "LONG" and close_position < 0.65:
        return False, {
            "reason": "long_close_not_near_high",
            "close_position": round(close_position, 2)
        }

    if direction == "SHORT" and close_position > 0.35:
        return False, {
            "reason": "short_close_not_near_low",
            "close_position": round(close_position, 2)
        }

    return True, {
        "body_ratio": round(body_ratio, 2),
        "close_position": round(close_position, 2)
    }
```

---

## Add breakout quality helper

```python
def _is_quality_breakout(
    direction,
    close,
    prev_high,
    prev_low,
    underlying,
    base_buffer
):
    prev_range = prev_high - prev_low

    min_breakout_distance = max(
        base_buffer,
        min(prev_range * 0.25, underlying * 0.002)
    )

    if direction == "LONG":
        breakout_distance = close - prev_high
    else:
        breakout_distance = prev_low - close

    return breakout_distance >= min_breakout_distance, {
        "breakout_distance": round(breakout_distance, 2),
        "required_distance": round(min_breakout_distance, 2),
        "prev_range": round(prev_range, 2)
    }
```

---

# 6. Suggested Final Rule Set

| Rule | Recommended Setting |
|---|---|
| Base timeframe | 3H |
| Entry type | Reversal-biased breakout |
| Prior opposite context | Previous candle opposite OR 2 of last 3 opposite |
| Same-direction streak block | Hard block at 5+ |
| Same-direction caution | 3-4 allowed only with strong OI / strong breakout |
| Breakout distance | `max(buffer, min(25% prev range, 0.2% CMP))` |
| Candle body ratio | Minimum 45% |
| Long close location | Close in top 35% of candle |
| Short close location | Close in bottom 35% of candle |
| SL | Prefer underlying structural SL |
| Re-entry after SL | Block same-direction re-entry for 1 closed 3H candle |

---

# 7. Additional Important Filter: Re-Entry Cooldown

Your plan correctly identifies whipsaw re-entries.

Add this rule:

```text
If a trade hits SL, do not allow another trade in the same symbol and same direction until at least one full 3H candle has closed.
```

Optional stronger version:

```text
Allow re-entry only if price breaks the failed breakout level again with stronger OI confirmation.
```

This will likely reduce unnecessary repeated losses.

---

# 8. Backtest / Paper Trade Plan

Do not replace the old logic immediately.

Run both in parallel for 1 week:

| Mode | Purpose |
|---|---|
| Current strategy | Baseline |
| New reversal-biased model | Candidate |
| Log-only rejected trades | Understand missed winners/losses |

For every skipped trade, log:

```text
symbol
direction
entry_price
reason_blocked
prior_streak
prev_candle_direction
breakout_distance
required_breakout_distance
body_ratio
close_position
OI status
hypothetical P&L
```

This is critical. Otherwise you will only know losses avoided, not profits missed.

---

# 9. Final Recommendation

## Do Implement

- Reversal context filter
- Proper distinct 3H candle streak counting
- Breakout quality filter
- Candle body quality filter
- Same-direction re-entry cooldown
- Better logging for blocked trades

## Modify Before Implementation

- Do not use hard `prior_streak >= 3` block.
- Do not use fixed `30% of previous candle range` without cap.
- Do not rely on rough premium SL conversion for live trading.
- Do not call it pure reversal-only yet.

## Best Strategy Name

Use:

> **3H Reversal-Biased Quality Breakout Strategy**

Instead of:

> Reversal-Only Strategy

Because the data does not yet prove reversal-only is superior.

---

# 10. Final Rating of the Plan

| Area | Rating |
|---|---:|
| Diagnosis | 8.5 / 10 |
| Proposed direction | 8 / 10 |
| Historical evidence strength | 5 / 10 |
| Implementation readiness | 6.5 / 10 |
| Risk control improvement | 8 / 10 |
| Risk of over-filtering | Medium-High |

Final view:

> Good plan, but make it less restrictive, fix the streak counting, add candle-quality scoring, and test in shadow mode before replacing the current engine.

---

# 11. Implementation Priority

## Phase 1 — Must-Have Fixes

1. Add unique 3H candle identification.
2. Fix streak counting using distinct candle timestamps.
3. Add breakout quality helper.
4. Add candle quality helper.
5. Add re-entry cooldown after SL.
6. Improve entry/blocked-trade logging.

## Phase 2 — Strategy Refinement

1. Run current and new logic in parallel.
2. Track skipped trades and hypothetical P&L.
3. Compare profit factor, win rate, average loss, average win, and drawdown.
4. Tune thresholds only after at least 75–100 trade observations.

## Phase 3 — Risk Model Improvement

1. Move from flat premium SL to underlying structural SL.
2. Adjust position size based on actual structural risk.
3. Avoid rough delta-based premium SL conversion for live trading.

---

# 12. Developer Notes

The developer should avoid making the strategy too restrictive in the first implementation. The purpose of the redesign is not to eliminate all losing trades; it is to remove obvious low-quality entries while preserving enough trade flow to validate the model.

Recommended first version should include:

```text
- Reversal-biased context, not strict reversal-only
- Hard exhaustion block only at 5+ same-direction 3H candles
- Conditional caution at 3-4 candles
- Breakout distance capped by CMP percentage
- Candle quality validation
- SL re-entry cooldown
- Full logging of blocked trades
```

This makes the implementation practical, testable, and less likely to overfit the current 25-trade dataset.
