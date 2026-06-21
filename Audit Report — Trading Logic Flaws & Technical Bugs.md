x# 🚨 NSEBOT Audit Report — Trading Logic Flaws & Technical Bugs

**Date:** 2026-06-19  
**Auditor:** Automated Code Review  
**Scope:** Full engine audit of `src/engine/`, `src/risk/`, `src/config/`  
**Status:** Action Required

---

## Executive Summary

A deep-dive audit of the NSEBOT codebase revealed **5 critical flaws**, **5 high-severity issues**, and **5 medium-severity concerns**. The most urgent finding is a **severe paper-to-live divergence**: paper trading enforces strict risk guards, ATR-based exits, and premium SL monitoring that are either missing, weakened, or fundamentally different in live trading. **Paper P&L is not a reliable indicator of live performance** until C1–C5 are resolved.

---

## 🔴 CRITICAL — Directly Affects Live Money / P&L Correctness

### C1. Live Trading Reversal Guard is 3× Weaker Than Paper Trading

| Check | Paper (`paper_trading.py`) | Live (`live_trading.py`) |
|---|---|---|
| Confidence threshold | `>= REVERSAL_MIN_CONFIDENCE` (75) | `< 70` only |
| Entry quality check | `>= MIN_ENTRY_QUALITY_CORE` (60) | ❌ Missing |
| Trend alignment check | `<= 40` (trend must have shifted) | ❌ Missing |

**Impact:** Live trading will flip/reverse open positions on weak noise that paper trading would correctly ignore. A 72-confidence counter-signal during a strong trend day will close a profitable live position — but the same signal would be blocked in paper. This makes paper results look artificially good vs live.

**Files:** `src/engine/paper_trading.py` (~line 180), `src/engine/live_trading.py` (~line 170)

**Fix:** Mirror paper's reversal guard logic exactly in live trading. Extract shared reversal validation into `risk/guardrails.py`.

---

### C2. Live Trading `check_live_risk_limits` Shadows `risk_engine.py` — 4 of 6 Safeguards Bypassed

`live_trading.py` defines its own `check_live_risk_limits()` that only checks:
- Max concurrent positions
- Max 1 open per symbol

But `risk_engine.py`'s `check_live_risk_limits()` checks **all 6 safeguards:**

| # | Safeguard | Paper | Live |
|---|---|---|---|
| 1 | Max open per symbol | ✅ | ✅ |
| 2 | Max total open | ✅ | ✅ |
| 3 | Max trades per symbol per day | ✅ | ❌ Missing |
| 4 | Daily loss cap (₹200k) | ✅ | ❌ Missing |
| 5 | Loss cooldown (30 min) | ✅ | ❌ Missing |
| 6 | Consecutive-loss circuit breaker (3 losses / 30 min) | ✅ | ❌ Missing |

**Impact:** If the bot enters a losing streak in live mode, there is no circuit breaker. It will keep trading until the account is blown. Paper trading has all 6 safeguards, so paper results don't reflect this live risk.

**File:** `src/engine/live_trading.py` (~lines 170-185)

**Fix:** Remove the local `check_live_risk_limits()` in `live_trading.py` and import/use `risk_engine.check_live_risk_limits()` instead.

---

### C3. `run_live_timeframe_strategy` is a Stub — Timeframe Strategy NEVER Executes Live

```python
def run_live_timeframe_strategy(symbol, scan_context, digest_id, intel) -> dict | None:
    # For simplicity, Phase 1 details option chain execution.
    return None  # ← ALWAYS returns None
```

**Impact:** All 3H/1H crossover trades only exist in paper. Paper P&L includes timeframe strategy profits/losses, but live trading never executes them. This creates a massive paper-to-live divergence in strategy performance.

**File:** `src/engine/live_trading.py` (last function)

**Fix:** Implement live timeframe strategy mirroring `run_timeframe_strategy()` in `paper_trading.py`, or explicitly disable timeframe strategy in paper until live implementation exists.

---

### C4. SL/Target Systems Are Completely Different Between Paper and Live

| Strategy | Paper Core | Paper Timeframe | Live |
|---|---|---|---|
| SL basis | Underlying ATR (1.5×) | Premium (75% of entry) | Premium (70% of entry for BUY) |
| Target basis | Underlying ATR (2.0×) | 1H candle crossover | Premium (150% of entry for BUY) |
| Exit monitor | Underlying price only | Premium + 1H crossover | Premium polling |

**Impact:** A trade that hits target in paper might still be open in live (or vice versa). Paper backtest results are not transferable to live trading because the exit criteria are fundamentally different.

**Files:** `src/engine/paper_trading.py` (`_calculate_buy_sl_target`), `src/engine/live_trading.py` (`_trade_plan_from_verdict`)

**Fix:** Unify SL/Target calculation into a single shared module (`risk/trade_plan.py`). Both paper and live should call the same function with identical parameters.

---

### C5. `monitor_paper_trades` Only Checks Underlying-Based SL — Ignores Premium SL

```python
# Current logic in monitor_paper_trades():
hit_sl = (side == "BUY" and sl_ul > 0 and underlying <= sl_ul)
hit_target = (side == "BUY" and tgt_ul > 0 and underlying >= tgt_ul)
```

Option trades have `sl_premium` and `target_premium` set in the trade plan. The underlying could be within SL range while the premium has already dropped 30%+ (hitting the premium SL). This monitor completely ignores premium-based exits for the core strategy.

**Impact:** Paper trades stay open past their premium SL, creating artificially better P&L on winners and worse P&L on losers.

**File:** `src/engine/paper_trading.py` — `monitor_paper_trades()`

**Fix:** Add premium-based SL/target checks alongside underlying checks. Use current option LTP from `_get_option_premium()`.

---

## 🟠 HIGH — Significant Logic Errors

### H1. Live Trading Signal Key Includes Verdict Text — Dedup Divergence

```python
# Live: includes verdict text → more permissive dedup
signal_key = f"{symbol}:{option_type}:{strike}:{verdict}:{today_date}:live"

# Paper: NO verdict text → stricter dedup
signal_key = f"{symbol}:{option_type}:{int(strike)}:{today_date}:paper"
```

**Impact:** If the verdict changes from "Long Buildup" to "Put Writing" (both bullish) between scans, live will create a duplicate trade while paper correctly deduplicates.

**Fix:** Remove verdict from live signal key, or add verdict to paper signal key. Use consistent format across both.

---

### H2. Two Independent Paper Strategies Run Simultaneously — Can Double-Trade

`run_paper_trading()` and `run_timeframe_strategy()` both run every scan and both call `execute_paper_trade()` / `insert_paper_trade()`. They have different entry criteria and different SL/Target systems.

**Impact:** If both trigger on the same scan, you get two trades with conflicting exit criteria on the same symbol. Unpredictable behavior.

**Fix:** Add cross-strategy dedup check before executing any paper trade. Or merge into a single strategy dispatcher.

---

### H3. No Transaction Costs in Paper P&L

`config/settings.py` defines `TRANSACTION_COSTS` (STT, brokerage), but they are never applied in `close_paper_trade()` or any P&L calculation.

**Impact:** Paper P&L overstates real profitability by ₹40-200 per trade (brokerage + STT). For NIFTY options at 1 lot, STT alone is ~₹15-30 per round trip.

**Fix:** Apply `TRANSACTION_COSTS` in `close_paper_trade()` when computing net P&L.

---

### H4. `_update_live_cmps` Doesn't Check SL/Target Hits Between Scans

The scheduler's live CMP refresh loop runs every 120 seconds but only writes prices to the DB. It never calls `monitor_paper_trades()` or any exit logic.

**Impact:** If a 5-minute scan frequency is used, SL/Target hits are only checked every 5 minutes. A sharp adverse move could blow past the SL by a wide margin before the next scan detects it.

**Fix:** Call exit-check logic inside `_update_live_cmps()` after updating prices, or reduce scan interval for active trades.

---

### H5. Confidence Scoring Has Order-Dependent Caps

In `intelligence.py` `_compute_confidence()`:
```python
if volume_dominant: score = min(score, 88)
if chart_conflict: score = min(score, 85)
if flat_price_and_balanced_oi: score = min(score, 65)
```

**Impact:** Final score depends on execution order, not just market conditions. If chart conflict fires before volume dominance, the cap sequence differs.

**Fix:** Collect all applicable caps first, then apply `min(score, min(all_caps))` in a single step.

---

## 🟡 MEDIUM — Moderate Logic Issues

### M1. Timeframe Breakout Buffer is Too Small

```python
breakout_buffer = underlying * 0.001  # 0.1% of underlying
```

| Symbol | Price | Buffer |
|---|---|---|
| NIFTY | 24,000 | 24 points |
| NATURALGAS | 300 | 0.3 points |
| CRUDEOIL | 7,000 | 7 points |

This is noise-level. False breakouts will trigger entries constantly.

**Fix:** Increase to 0.3-0.5% or use ATR-based buffer.

---

### M2. Paper Plan SL/Target Is Overwritten at Execution

`build_paper_trade_plan()` computes ATR-based SL/Target. But `execute_paper_trade()` calls `_calculate_buy_sl_target()` which recalculates and overwrites the plan's values.

**Impact:** The plan's SL/Target (shown in Telegram digest) doesn't match what's actually stored in the trade.

**Fix:** Either use the plan's values directly, or update the digest after execution with actual values.

---

### M3. Regime Detector Uses Last 10 Scans — Time-Blind

The regime detector queries `LIMIT 10` from `scan_summaries`. If scans fail or are skipped, those 10 scans could span 30 minutes or 3 hours.

**Fix:** Add timestamp-weighted decay or filter scans by recency (e.g., last 60 minutes only).

---

### M4. AI CLOSE_EARLY Falls Back to Entry Premium — P&L = ₹0

When the AI exit advisor fires CLOSE_EARLY but current option LTP is unavailable:
```python
exit_premium = float(open_trade.get("entry_premium") or 0.0)
```

**Impact:** Trade closes at entry price regardless of actual profit/loss.

**Fix:** Skip CLOSE_EARLY if current premium is unavailable. Log warning instead of forcing zero-P&L exit.

---

### M5. SELL Leg Capital Allocator Uses 10× Margin Multiplier — May Under-Size

`capital_allocator.py` uses `_SELL_MARGIN_PREMIUM_MULTIPLIER = 10.0`. Actual SPAN+exposure margin for index options can be 12-15× premium.

**Impact:** Allocator may size 2 lots but broker needs margin for only 1 lot → order rejected.

**Fix:** Increase multiplier to 15× or fetch actual margin requirements from broker API.

---

## 🔵 LOW — Minor Issues

| # | Issue | File |
|---|---|---|
| L1 | `run_live_timeframe_strategy` is a stub with TODO comment | `live_trading.py` |
| L2 | `_get_option_premium` fallback to DB snapshots may return stale premiums | `live_trading.py` |
| L3 | `sync_direct_kite_positions()` is commented out in pipeline | `pipeline.py` |
| L4 | Multiple lazy imports inside function bodies — minor per-scan cost | Various |
| L5 | `paper_plan.py` forces FUT for all MCX commodities regardless of option liquidity | `paper_plan.py` |

---

## Priority Fix Roadmap

| Phase | Items | Effort | Risk if Deferred |
|---|---|---|---|
| **Phase 1 (Immediate)** | C1, C2, C4, C5 | 2-3 days | Live trading operates without proper risk controls |
| **Phase 2 (This Week)** | C3, H1, H2, H3 | 2-3 days | Paper results remain unreliable; double-trading risk |
| **Phase 3 (Next Week)** | H4, H5, M1, M2 | 1-2 days | Missed exits; false breakout entries |
| **Phase 4 (Backlog)** | M3, M4, M5, L1-L5 | Ongoing | Gradual degradation of signal quality |

---

## Recommendations

1. **Extract shared logic:** Create `risk/trade_plan.py` and `risk/reversal_guard.py` to eliminate paper/live divergence at the source.
2. **Add integration tests:** Write tests that run the same signal through both paper and live paths and assert identical decisions.
3. **Disable timeframe strategy in paper** until live implementation exists (C3), to avoid misleading paper P&L.
4. **Add transaction costs immediately** (H3) — this is a one-line fix that improves paper accuracy.
5. **Add alerting on risk safeguard bypasses** — log WARNING whenever live trading skips a check that paper enforces.

---

*End of Audit Report*
