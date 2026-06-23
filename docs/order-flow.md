# NSEBOT Order Flow — Signal to Execution

> **Generated:** June 22, 2026 | **Method:** AGoT Reasoning Graph
> **Purpose:** Trace the complete order flow from market data ingestion to trade execution.
> Read after `architecture.md` to understand the runtime execution path.

---

## 1. Pipeline State Machine

```
┌─────────────────────────────────────────────────────────────────────┐
│                     PIPELINE STATE MACHINE                           │
│                                                                     │
│  IDLE ──▶ FETCH ──▶ DETECT ──▶ DEDUP ──▶ ENRICH ──▶ DECIDE        │
│    │         │         │         │         │         │               │
│    │         ▼         ▼         ▼         ▼         ▼               │
│    │      [FAIL]   [NO_ALERT] [DUP]   [NO_LLM]  [BLOCKED]         │
│    │         │         │         │         │         │               │
│    │         ▼         ▼         ▼         ▼         ▼               │
│    └────────────────────────────────────────────────────────────────│
│                                                                     │
│  DECIDE ──▶ RISK_CHECK ──▶ PLAN ──▶ EXECUTE ──▶ MONITOR ──▶ ALERT │
│    │            │           │         │           │          │      │
│    │            ▼           ▼         ▼           ▼          ▼      │
│    │       [RISK_FAIL]  [NO_PLAN] [SL_HIT]   [CLOSED]  [SENT]    │
│    │            │           │       [TGT_HIT]      │         │      │
│    │            ▼           ▼         ▼           ▼         ▼      │
│    └────────────────────────────────────────────────────────────────│
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Stage 1: Data Fetching

**Entry:** `pipeline.run_pipeline(symbols)` → `router.fetch_option_chain(symbol)`

### Fetcher Router Logic
```python
for source in _priority_for(symbol):
    fetcher = _get_fetcher(source)
    result = fetcher.fetch_option_chain(symbol)
    if result and result.get("strikes"):
        # Validate: not zero-filled, has underlying price
        if total_oi == 0 and total_ltp == 0:
            continue  # try next fetcher
        _filter_atm_strikes(result)  # ATM ± STRIKES_AROUND_ATM
        return result
return None  # ALL fetchers failed
```

### Output Schema
```python
{
    "symbol": "NIFTY",
    "underlying_price": 24850.0,
    "expiry": "2026-06-25",
    "source": "nse_public",
    "strikes": [
        {
            "strike": 24800.0,
            "option_type": "CE",
            "ltp": 120.5,
            "ltp_change_pct": 5.2,
            "oi": 1500000,
            "oi_change_pct": 8.3,
            "volume": 50000,
            "iv": 12.5,
            "bid": 120.0,
            "ask": 121.0,
            "delta": 0.65,
        },
        # ... 20 more strikes (ATM ± 10)
    ]
}
```

### Failure Handling
| Condition | Action |
|-----------|--------|
| All fetchers fail | Send Telegram alert: "ALL data fetchers failed" |
| Underlying price None | Fallback to prev_price, set `is_fallback=True` |
| Zero-filled strikes | Try next fetcher |
| MCX without underlying | Try next fetcher (special handling) |

---

## 3. Stage 2: Anomaly Detection

**Entry:** `anomaly_detector.detect_anomalies(oc_data, fetched_at, chart_indicators)`

### Detection Pipeline
```
Option Chain Data
    │
    ├──▶ OI Analysis
    │    ├── OI Spike Detection (>15% change)
    │    ├── Buildup Detection (Long/Short/Covering/Unwinding)
    │    └── OTM Unusual Activity
    │
    ├──▶ Price Analysis
    │    ├── LTP Spike Detection (>2% change)
    │    └── Volume Aggression (2.5x normal)
    │
    ├──▶ PCR Analysis
    │    ├── PCR Extreme (0.5 or 1.8)
    │    └── PCR Shift (>0.3 change)
    │
    ├──▶ IV Analysis
    │    ├── IV Spike at ATM (>20%)
    │    └── IV Crush (>15% drop)
    │
    ├──▶ Max Pain Analysis
    │    └── Max Pain Shift (>50 rupees)
    │
    └──▶ Straddle Analysis
         └── Straddle Premium Change
```

### Verdict Generation
| Verdict | Condition | Direction |
|---------|-----------|-----------|
| Long Buildup | OI↑ + LTP↑ | Bullish |
| Short Buildup | OI↑ + LTP↓ | Bearish |
| Short Covering | OI↓ + LTP↑ | Bullish |
| Long Unwinding | OI↓ + LTP↓ | Bearish |
| Put Writing | OI↑ at PE + LTP↓ | Bullish |
| Call Writing | OI↑ at CE + LTP↓ | Bearish |
| OI Bias Bullish | Net OI shift to CE | Bullish |
| OI Bias Bearish | Net OI shift to PE | Bearish |

---

## 4. Stage 3: Intelligence & LLM Enrichment

### Intelligence Generation
**Entry:** `intelligence.generate_intelligence_structured(symbol, alerts, scan_context)`

**Output:**
```python
{
    "verdict_label": "Long Buildup",
    "confidence": 75,
    "telegram_text": "🟢 **NIFTY** | Long Buildup | Conf: 75% | ...",
}
```

### LLM Enrichment
**Entry:** `llm_enrichment.get_llm_verdict(symbol, intel, scan_context, alerts, news_data, open_trade)`

**Output (LLMVerdict schema):**
```python
{
    "action": "GO_LONG",           # GO_LONG / GO_SHORT / NO_TRADE
    "confidence": 78,              # 0-100
    "instrument": "24850 CE 25JUN",
    "entry_trigger": "Break above 24870",
    "entry_premium_range": "₹110-120",
    "stop_loss": "₹85",
    "target_1": "₹145",
    "target_2": "₹175",
    "risk_reward": "1:2.5",
    "risk_rating": "MEDIUM",       # LOW / MEDIUM / HIGH
    "thesis": "Strong OI buildup at 24850 CE...",
    "invalidation": "Close below 24800",
    "catalyst": "RBI policy announcement on Friday",
    "exit_advice": "...",
}
```

### AI Exit Advisor
**Entry:** `llm_enrichment.get_exit_advice(symbol, open_trade, scan_context, news_data)`

**Output:**
```python
{
    "action": "TRAIL_SL",          # TRAIL_SL / CLOSE_EARLY / HOLD
    "urgency": "MEDIUM",           # LOW / MEDIUM / HIGH
    "new_sl_premium": 95.0,
    "reasoning": "Price moved 1.5R in favor, trail SL to breakeven",
}
```

**Execution:**
- `TRAIL_SL`: Updates `paper_trades.sl_premium` in DB
- `CLOSE_EARLY` + `urgency=HIGH`: Closes trade at current LTP
  - ⚠️ FIX #9: Skips close if LTP unavailable (prevents forced zero-P&L)

---

## 5. Stage 4: Trade Decision

**Entry:** `trade_decision.make_trade_decision(symbol, intel, ctx, ai_verdict)`

### Decision Flow
```
┌─────────────────────────────────────────────────────────────┐
│                    HARD BLOCKS                               │
│  ├── underlying <= 0 → BLOCKED ("Missing underlying price") │
│  ├── verdict not directional → BLOCKED                      │
│  └── scan_count < TREND_MIN_SCANS → BLOCKED (unless research│
│      mode bypasses with soft_conflict)                       │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│                  PLAN VALIDATION                             │
│  build_paper_trade_plan(verdict, confidence, ctx)            │
│  → Returns {side, option_type, strike, SL, target} or None  │
│  → None → BLOCKED ("No valid trade plan")                    │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│                   SCORING LAYERS                             │
│  entry_quality = calculate_entry_quality(symbol, opt, stk)   │
│  trend_alignment = get_trend_alignment_score(symbol, verdict)│
│  regime = detect_market_regime(symbol)                       │
│  regime_sc = regime_score_for_trade(regime, option_type)     │
│                                                             │
│  Chart conflict? → entry_quality -= 20 (soft penalty)       │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│              MODE-BASED DECISION (hybrid)                    │
│                                                             │
│  Priority 1: Trend Persistence                              │
│    is_persistent + entry_quality >= 60 + regime >= 60        │
│    → TRIGGERED_CORE "TREND_CONTINUATION"                    │
│                                                             │
│  Priority 2: Reversal Detection                             │
│    is_reversal + confidence >= 75 + entry_quality >= 60      │
│    → TRIGGERED_CORE "CONFIRMED_REVERSAL"                    │
│                                                             │
│  Priority 3: Momentum Scoring                               │
│    momentum_score >= 75 + entry_quality >= 60               │
│    → TRIGGERED_CORE "MOMENTUM_TRADE"                        │
│                                                             │
│  Priority 4: Experimental (research mode only)              │
│    confidence >= 50 + entry_quality >= 40                   │
│    → TRIGGERED_EXPERIMENTAL "EXPERIMENTAL_SETUP"            │
│                                                             │
│  Priority 5: AI Boost (if mode = boost_only or full)        │
│    AI agrees + AI confidence >= 80                          │
│    → TRIGGERED_EXPERIMENTAL "AI_PROMOTED"                   │
│                                                             │
│  None matched → BLOCKED                                     │
└─────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────┐
│                    AI VETO CHECK                             │
│  if mode == "full" and AI disagrees + AI conf >= 85:         │
│    → BLOCKED "AI VETO"                                      │
│  if mode == "full" and no AI verdict provided:              │
│    → Demote CORE → EXPERIMENTAL                             │
└─────────────────────────────────────────────────────────────┘
```

### Output Schema
```python
{
    "status": "TRIGGERED_CORE",
    "setup_type": "TREND_CONTINUATION",
    "reason": "Conservative: 4/5 scans agree",
    "soft_conflicts": [],
    "scores": {
        "confidence": 75,
        "entry_quality": 72,
        "trend_alignment": 80,
        "regime_score": 65,
        "ai_confidence": 78,
        "ai_bias": "BULLISH",
        "ai_agrees": True,
        "ai_risk_rating": "MEDIUM",
    }
}
```

---

## 6. Stage 5: Risk Check

**Entry:** `risk_engine.check_risk_limits(symbol)` (paper) or `check_live_risk_limits(symbol)` (live)

### Check Sequence
```
1. Max open trades per symbol
   SELECT COUNT(*) FROM paper_trades WHERE symbol=? AND status='OPEN'
   → >= MAX_OPEN_TRADES_PER_SYMBOL (2) → BLOCKED

2. Max total open trades
   SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'
   → >= MAX_OPEN_TRADES_TOTAL (5) → BLOCKED

3. Max trades per symbol per day
   SELECT COUNT(*) FROM paper_trades WHERE symbol=? AND opened_at >= today_start
   → >= MAX_TRADES_PER_SYMBOL_PER_DAY (4) → BLOCKED

4. Daily loss cap (FIX #3: sums ONLY negative P&L)
   SELECT SUM(pnl_rupees) FROM paper_trades
   WHERE closed_at >= today_start AND pnl_rupees < 0
   → < -MAX_DAILY_LOSS_RUPEES (-200000) → BLOCKED

5. Loss cooldown (per-symbol)
   SELECT closed_at FROM paper_trades
   WHERE symbol=? AND status IN ('CLOSED_SL',...) AND pnl_rupees < 0
   → within LOSS_COOLDOWN_MINUTES (30) → BLOCKED

6. Consecutive-loss circuit breaker (FIX #11)
   SELECT COUNT(*) FROM paper_trades
   WHERE pnl_rupees < 0 AND closed_at >= now - 30min
   → >= CONSECUTIVE_LOSS_LIMIT (3) → BLOCKED (ALL symbols halted)
```

---

## 7. Stage 6: Trade Planning

**Entry:** `paper_plan.build_paper_trade_plan(verdict, confidence, ctx)`

### Strike Selection Logic
```
VERDICT_ACTION_MAP = {
    "Long Buildup":    ("BUY",  "CE"),
    "Put Writing":     ("SELL", "PE"),
    "Short Covering":  ("BUY",  "CE"),
    "OI Bias Bullish": ("SELL", "PE"),
    "Short Buildup":   ("BUY",  "PE"),
    "Call Writing":    ("SELL", "CE"),
    "Long Unwinding":  ("BUY",  "PE"),
    "OI Bias Bearish": ("SELL", "CE"),
    "GO_LONG":         ("BUY",  "CE"),
    "GO_SHORT":        ("BUY",  "PE"),
}
```

### SL/Target Calculation
```
IF FUT and not TIMEFRAME:
    Use ATR-based:
        Bullish: SL = underlying - 1.5*ATR, Target = underlying + 2.0*ATR
        Bearish: SL = underlying + 1.5*ATR, Target = underlying - 2.0*ATR
ELSE:
    Use support/resistance levels:
        Bullish: SL = support (or underlying - 2*step), Target = resistance (or +2*step)
        Bearish: SL = resistance (or underlying + 2*step), Target = support (or -2*step)
```

### MCX Commodity Options Decision
```
IF symbol in (NATURALGAS, CRUDEOIL):
    ATM volume >= 500 AND ATM OI >= 2000?
        YES → Use options (CE/PE)
        NO  → Fall back to FUT
```

---

## 8. Stage 7: Execution

### Paper Trading Execution
**Entry:** `paper_trading.execute_paper_trade(symbol, verdict, confidence, ctx, plan, ai_verdict)`

```
1. Check reversal against open trade
   IF open_trade exists:
       is_reversal = _is_reversal_against_open_trade(...)
       Guards: conf >= 75, entry_quality >= 60, trend_alignment <= 40
       IF reversal:
           close_paper_trade(open_trade, "CLOSED_REVERSAL")
           → fall through to open new trade
       ELSE:
           → HOLD (open trade exists, no valid reversal)

2. Risk limits check
   check_risk_limits(symbol) → BLOCKED_RISK if fails

3. Lot sizing
   calculate_trade_lots(symbol, premium, side, is_paper=True)

4. Insert trade
   insert_paper_trade({...trade_data...})
   → Returns trade_id
```

### Live Trading Execution
**Entry:** `live_trading.run_live_trading(symbol, scan_context, digest_id, intel, ai_verdict)`

```
1. Gate: live_trading_enabled in runtime_config
2. Gate: market is open for symbol
3. Gate: broker configured (Zerodha/Dhan)
4. Same decision/risk/plan logic as paper
5. Place real order via KiteConnect:
   kite.place_order(
       variety="regular",
       exchange="NFO" or "MCX",
       tradingsymbol=resolved_symbol,
       transaction_type="BUY" or "SELL",
       quantity=lots * lot_size,
       order_type="MARKET",
       product="MIS",
   )
6. Store order_id in live_trades table
7. Sync Kite positions to SQLite every 5 minutes
```

---

## 9. Stage 8: Monitoring

### Paper Trade Monitoring
**Entry:** `paper_trading.monitor_paper_trades(symbol, current_ctx)`

```
For each open trade:
    1. Underlying SL/Target check
       BUY:  hit_sl if underlying <= sl_underlying
             hit_target if underlying >= target_underlying
       SELL: hit_sl if underlying >= sl_underlying
             hit_target if underlying <= target_underlying

    2. Premium SL/Target check (options only)
       BUY CE:  hit_sl if premium <= sl_premium
                hit_target if premium >= target_premium
       SELL PE: hit_sl if premium >= sl_premium
                hit_target if premium <= target_premium

    3. Close trade
       close_paper_trade(id, closed_at, underlying, exit_premium, status, reason)
       status: "CLOSED_SL" or "CLOSED_TARGET"
```

### Timeframe Strategy Monitoring
**Entry:** `paper_trading.run_timeframe_strategy(symbol, scan_context, ...)`

**Entry Logic (3H candle):**
```
LONG trigger:  3H close > prev_3H_high + breakout_buffer AND long_OI_support
SHORT trigger: 3H close < prev_3H_low - breakout_buffer AND short_OI_support

breakout_buffer = max(ATR * 0.5, underlying * 0.003)
```

**Exit Logic (1H candle):**
```
LONG exit:  1H close < prev_1H_low + OI bias confirmation
SHORT exit: 1H close > prev_1H_high + OI bias confirmation

Additional exits:
- Dead Trade: 3 hours passed, max_favorable_R < 0.5
- LLM Reversal: AI bias contradicts trade direction, conf >= 70
- Premium SL/Target hit
```

**Pyramid Management:**
```
Max 3 levels per direction
Level 1: 1.0x DEFAULT_LOTS_PER_TRADE
Level 2: 0.75x DEFAULT_LOTS_PER_TRADE
Level 3: 0.50x DEFAULT_LOTS_PER_TRADE

Pyramid conditions:
- All open trades in same direction
- At least one trade is profitable
```

---

## 10. Stage 9: Alerting

### Digest Building
**Entry:** `digest.build_digest_wrapper(...)`

**Content:**
```
🟢 **NSEBOT SCAN** | NIFTY | 24850.0 | +0.5%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 **Anomalies:** 3 detected (1 dedup suppressed)
  • Long Buildup at 24800 CE (OI +8.3%)
  • OI Bias Bullish (PCR shift +0.35)
  • Volume Aggression at 24850 CE

🤖 **AI Trade Plan** (GO_LONG, 78%)
📋 Contract: 24850 CE 25JUN
🎯 Entry: Break above 24870
💰 Premium: ₹110-120
🛑 SL: ₹85
🎯 T1: ₹145 | T2: ₹175
📊 R:R: 1:2.5 | 🟡 Risk: MEDIUM
💡 Thesis: Strong OI buildup...

📝 **Paper Trade:** OPENED #42 | BUY 24850 CE | SL 24800 | Tgt 24920
```

### Dispatch
- **Telegram:** `telegram_dispatcher.send_text(digest_msg)`
- **Discord:** `discord_dispatcher.send_webhook(digest_msg)` (if configured)
- **Dedup Recording:** `dedup.record_alert(alert)` for cooldown tracking

---

## 11. Error Handling Paths

### Fetch Failure
```
All fetchers fail → Telegram alert: "ALL data fetchers failed for symbol X"
                     → Pipeline skips symbol, continues with others
```

### Underlying Price Missing
```
underlying = None → Fallback to prev_price
                   → Set is_fallback = True
                   → regime_detector excludes this row from trend analysis
```

### LLM Failure
```
get_llm_verdict raises → intel = None
                        → Use fallback digest text
                        → Skip AI enrichment
                        → Continue with rule-based decision
```

### Risk Check Failure
```
check_risk_limits returns (False, reason) → action = "BLOCKED_RISK"
                                           → Trade not placed
                                           → Reason logged and included in digest
```

### Broker API Failure (Live)
```
kite.place_order raises → Log error
                        → action = "EXECUTION_FAILED"
                        → Telegram alert with error details
                        → Trade remains in OPEN state for retry
```

### Premium Unavailable
```
_get_option_premium returns None → Paper plan aborted
                                  → action = "BLOCKED_PLAN"
                                  → "Option premium unavailable"
```

---

## 12. Paper vs Live Divergence Points

| Stage | Paper Trading | Live Trading |
|-------|--------------|--------------|
| **Order Placement** | Insert to `paper_trades` table | `kite.place_order()` via API |
| **Lot Sizing** | `calculate_trade_lots(is_paper=True)` | `calculate_trade_lots(is_paper=False)` |
| **Premium Resolution** | From option chain snapshots | From broker real-time quotes |
| **Position Sync** | N/A | `sync_direct_kite_positions()` every 5 min |
| **Risk Table** | `paper_trades` | `live_trades` |
| **Reversal Guard** | Same 3 guards | Same 3 guards (Fix C1 alignment) |
| **Gate** | Market hours only | Market hours + `live_trading_enabled` + broker configured |

---

## 13. Quick Reference: Order Flow Debugging

### Trade Not Placed — Debug Checklist
1. **Check logs:** `grep "BLOCKED" logs/main.log | tail -20`
2. **Check scan count:** `SELECT COUNT(*) FROM scan_summaries WHERE symbol='NIFTY' AND (is_fallback IS NULL OR is_fallback=0)`
3. **Check risk limits:** `SELECT * FROM paper_trades WHERE status='OPEN'`
4. **Check regime:** `SELECT * FROM scan_summaries WHERE symbol='NIFTY' ORDER BY fetched_at DESC LIMIT 5`
5. **Check AI mode:** `cat data/runtime_config.json | grep ai_decision_mode`

### Trade Closed Unexpectedly — Debug Checklist
1. **Check exit reason:** `SELECT exit_reason, reason FROM paper_trades WHERE id=?`
2. **Check reversal guards:** Were all 3 guards satisfied?
3. **Check timeframe exit:** Was 1H crossover triggered?
4. **Check dead trade:** Did 3 hours pass with max_favorable_R < 0.5?
5. **Check AI exit:** Was AI_CLOSE_EARLY triggered?

---

**Last Updated:** June 22, 2026
**Analysis Method:** Adaptive Graph of Thoughts (AGoT)
