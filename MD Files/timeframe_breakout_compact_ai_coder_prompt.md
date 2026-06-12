# TIMEFRAME_BREAKOUT — Compact AI Coder Prompt

Implement a configurable paper-trading strategy module named `TIMEFRAME_BREAKOUT`.

## Core Objective
Enter on completed 3H breakout, confirm with simple OI bias, exit using 1H reversal/SL/trailing/dead-trade logic, allow controlled pyramiding, and log analytics for profitability review.

## Config Defaults
```json
{
  "setup_type": "TIMEFRAME_BREAKOUT",
  "entry_timeframe": "3H",
  "exit_timeframe": "1H",
  "breakout_buffer_percent": 0.10,
  "atr_period": 14,
  "atr_buffer_multiplier": 0.10,
  "trend_ema_period": 20,
  "oi_lookback_minutes": 60,
  "oi_max_scan_lookback": 50,
  "minimum_oi_gap": 5000,
  "minimum_total_oi_change": 10000,
  "allow_neutral_oi": true,
  "block_opposite_oi": true,
  "option_itm_steps": 4,
  "option_sl_percent": 25,
  "max_option_spread_percent": 4,
  "risk_per_trade_percent": 0.75,
  "max_daily_loss_percent": 2.5,
  "max_open_trades_per_symbol": 3,
  "max_total_open_trades": 6,
  "max_consecutive_losses": 4,
  "move_sl_to_breakeven_at_r": 1.0,
  "start_trailing_at_r": 1.5,
  "option_trailing_sl_percent_of_ltp": 85,
  "dead_trade_exit_after_1h_candles": 3,
  "dead_trade_min_favorable_r": 0.5,
  "data_delay_buffer_seconds": 60,
  "allow_pyramiding": true,
  "allow_overnight": false
}
```

## Entry Logic
Use only completed 3H candles. Candle is valid only after `bar_end_utc + 60 seconds`.

Calculate:
```text
percent_buffer = current_3h.close * 0.001
atr_buffer = ATR_14_3H * 0.10
breakout_buffer = max(percent_buffer, atr_buffer)
```

Long entry:
```text
current_3h.close > previous_3h.high + breakout_buffer
AND current_3h.close > EMA_20_3H
AND oi_bias != SHORT
AND risk_engine_allows_new_trade
AND signal_key is unique
```

Short entry:
```text
current_3h.close < previous_3h.low - breakout_buffer
AND current_3h.close < EMA_20_3H
AND oi_bias != LONG
AND risk_engine_allows_new_trade
AND signal_key is unique
```

If ATR or EMA unavailable, skip signal and log reason.

## OI Bias
Compare current OI scan with latest scan from at least 60 minutes ago, searching only last 50 scans.

```text
ce_diff = current_ce_oi - previous_ce_oi
pe_diff = current_pe_oi - previous_pe_oi
oi_gap = abs(pe_diff - ce_diff)
total_oi_change = abs(pe_diff) + abs(ce_diff)
```

Rules:
```text
If no valid old scan: NEUTRAL
If oi_gap < 5000 or total_oi_change < 10000: NEUTRAL
If pe_diff > ce_diff: LONG
If ce_diff > pe_diff: SHORT
Else: NEUTRAL
```

Usage:
```text
Matching OI = allow
Neutral OI = allow
Opposite OI = block
```

## Instrument Selection
Options:
```text
Long = Buy CE strike = ATM - 4 * strike_step
Short = Buy PE strike = ATM + 4 * strike_step
```

Futures:
```text
Use active FUT contract only.
Signal may use spot, but execution and P&L must use FUT LTP, never spot.
```

Option execution:
```text
If bid/ask available: buy at ask, sell at bid.
If spread > 4%, skip.
If bid/ask unavailable: use LTP and mark pricing_quality = LTP_ONLY.
```

## Signal Key / Duplicate Prevention
Create deterministic unique key:
```text
signal_key = "{symbol}:{setup_type}:{timeframe}:{direction}:{bar_end_utc}"
```
Add DB unique constraint on `signal_key`. Do not rely only on `opened_at`.

## Stop Loss
Options:
```text
initial_sl = entry_option_price * 0.75
```

Futures long:
```text
initial_sl = current_3h.low
minimum distance = 0.30% of entry_price
if too close: initial_sl = entry_price - entry_price * 0.003
```

Futures short:
```text
initial_sl = current_3h.high
minimum distance = 0.30% of entry_price
if too close: initial_sl = entry_price + entry_price * 0.003
```

## R Logic
```text
Futures long R = entry_price - initial_sl
Futures short R = initial_sl - entry_price
Options R = entry_option_price - option_sl_price
```

At +1R:
```text
move SL to entry price
```

At +1.5R:
```text
Futures long trail = max(current_sl, previous_completed_1h.low)
Futures short trail = min(current_sl, previous_completed_1h.high)
Options trail = max(current_sl, option_ltp * 0.85)
```

## Exit Rules
Exit on any of these:
```text
STOP_LOSS: LTP breaches current_sl
1H_REVERSAL: after at least one completed 1H candle post-entry
DEAD_TRADE_EXIT: after 3 completed 1H candles if max_favorable_r < 0.5
EOD_EXIT: 15 minutes before market close if allow_overnight=false
```

1H reversal:
```text
Long exit: current_completed_1h.close < previous_completed_1h.low
Short exit: current_completed_1h.close > previous_completed_1h.high
```

## Pyramiding
Allowed only for `TIMEFRAME_BREAKOUT`.
```text
max_open_trades_per_symbol = 3
Only same direction
Only if at least one existing trade for symbol is profitable
Daily loss not breached
New signal_key unique
```

Sizing:
```text
1st entry = 100%
2nd entry = 75%
3rd entry = 50%
```
If variable sizing not supported, use same lot size but keep max 3 trades.

## Risk Rules
```text
risk_per_trade = 0.75% capital
max_daily_loss = 2.5% capital
max_open_trades_per_symbol = 3
max_total_open_trades = 6
max_consecutive_losses = 4
```
After 4 consecutive closed losses, pause new entries for day. Exits must still work.

## Required Analytics Fields
Log:
```text
symbol, instrument, setup_type, direction, signal_key,
entry_time, entry_bar_end_utc, exit_time,
entry_price, exit_price, quantity,
initial_sl, current_sl, final_sl, exit_reason,
oi_bias, ce_diff, pe_diff, oi_gap, total_oi_change,
ema_20, atr_14, breakout_buffer,
risk_amount, max_favorable_r, max_adverse_r,
pnl_amount, pnl_percent, r_multiple,
pyramid_level, pricing_quality, is_pyramid_trade, parent_trade_id
```

## Skip Reasons
Log explicit skip reason:
```text
NO_COMPLETED_3H_CANDLE, NO_PREVIOUS_3H_CANDLE,
DATA_DELAY_BUFFER_NOT_PASSED, ATR_NOT_AVAILABLE, EMA_NOT_AVAILABLE,
NO_BREAKOUT, TREND_FILTER_FAILED, OI_OPPOSITE_DIRECTION,
DUPLICATE_SIGNAL_KEY, RISK_ENGINE_BLOCKED, INSTRUMENT_NOT_FOUND,
PRICE_NOT_AVAILABLE, WIDE_SPREAD, MAX_DAILY_LOSS_REACHED,
MAX_OPEN_TRADES_REACHED, MAX_CONSECUTIVE_LOSSES_REACHED
```

## Acceptance Criteria
Done when:
```text
1. Uses completed 3H candles only.
2. Entry = breakout + EMA20 + OI not opposite + risk allowed + unique signal_key.
3. Options select 4-step ITM CE/PE.
4. Futures use FUT LTP for execution/P&L.
5. Every trade has initial SL.
6. 1H reversal exit works only after one completed 1H candle post-entry.
7. Breakeven at +1R and trailing at +1.5R works.
8. Dead-trade exit works after 3 completed 1H candles if <0.5R favorable move.
9. Pyramiding capped at 3 trades per symbol.
10. Daily loss/consecutive loss blocks entries but allows exits.
11. Analytics and skip reasons are stored.
```

Final instruction: Do not add extra filters. Keep implementation simple, configurable, traceable, and optimized for paper-trade analytics.
