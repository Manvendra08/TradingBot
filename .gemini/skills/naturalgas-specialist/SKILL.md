---
name: naturalgas-specialist
description: Use when analyzing, executing, debugging, or configuring Natural Gas (NATURALGAS) strategies, MCX commodity session regimes, EIA storage reports, parity arbitrage, or risk controls in NSEBOT
---

# Natural Gas Specialist

## Overview
Authoritative engineering and domain operating standard for the **NATURALGAS** MCX commodity module in NSEBOT. Governs multi-regime execution across Parity Mean-Reversion, NYMEX Momentum, and EIA Event strategies while enforcing strict physical contract invariants, DST-aware schedules, and margin risk caps.

## When to Use
- Managing, inspecting, or debugging the `NATURALGAS` pipeline or scan cycles.
- Evaluating regime transitions (`PARITY`, `MOMENTUM`, `EVENT`, `BLOCKED`) in `ng_session_router.py`.
- Auditing MCX option chain fetching (`dhan_commodity`, `moneycontrol`, `shoonya`) and pricing feeds.
- Investigating trade entry/exit rejections, stop-loss calculations, or position sizing anomalies.
- Monitoring Thursday EIA storage reports, consensus surprise calculations, or pre-print force closes.
- Reviewing multi-leg option strategies (`TFSS`, `MULTILEG`) applied to Natural Gas.

## When NOT to Use
- NSE/BSE Equity or Index F&O tasks (use `agency-senior-developer`, `agency-backend-architect`, or standard index pipelines).
- General dashboard UI changes unrelated to commodity reporting or charts.

---

## Contract & Risk Parameters

| Parameter | Value | Enforcement Location |
|---|---|---|
| **Exchange / Symbol** | MCX: `NATURALGAS` | `config/symbol_classes.py` |
| **Contract Lot Size** | 1,250 mmBtu | `config/settings.py` (`LOT_SIZES["NATURALGAS"]`) |
| **Tick Size / Point Value** | ₹0.10 / ₹125.00 per point | MCX contract specification |
| **Strike Interval** | 5.0 points | `config/multileg_strategies.py` |
| **Instrument Type** | `FUT` (Directional) / `OPT` (TFSS Multi-Leg) | `NG_FUT_ONLY = True` in `settings.py` |
| **Max Open Positions** | 1 directional trade | `ng_risk_manager.py` (`NG_MAX_POSITIONS = 1`) |
| **Risk Cap per Trade** | 2.0% of portfolio equity | `NG_RISK_PCT_PER_TRADE = 2` |
| **Max Auto Lots** | 5 lots (Clamped) | `ng_risk_manager.py:calculate_ng_lot_size` |
| **Stop-Loss Bounds** | Min: 1.5 pts, Max: 5.0 pts | `MIN_NG_PARITY_SL_POINTS`, `MAX_NG_PARITY_SL_POINTS` |
| **LLM Confidence Floor** | 72% (vs 70% for NSE indices) | `multileg_paper_trading.py`, `live_trading.py` |
| **Hedge Drag Ceiling** | 0.55 (Net credit $\ge$ 25% wing width) | `MAX_HEDGE_COST_RATIO_BY_SYMBOL["NATURALGAS"]` |
| **Friday Mandatory Exit** | 23:25 – 23:30 IST | `time_guards.py`, `NG_WEEKEND_FLAT = True` |

---

## Session Regimes & Routing State Machine

Regimes are evaluated dynamically in `src/engine/ng_session_router.py`:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        NOW_IST TIMESTAMP                               │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
           ┌────────────────────────┴────────────────────────┐
           ▼                                                 ▼
   Weekend / CME Holiday?                        EIA Storage Window?
   (Sat/Sun or is_cme_closed)                     (get_eia_schedule)
           │                                                 │
          YES                                               YES
           ▼                                                 ▼
      [ BLOCKED ]                                       [ EVENT ]
                                                  (Thu 19:45-20:15 EDT /
                                                   20:45-21:15 EST)
                                                             │
           ┌─────────────────────────────────────────────────┘
           │ NO
           ▼
    Time of Day (IST)?
    ├── 09:00 - 17:30 IST ──► DTE <= 3 (Expiry Week)?
    │                         ├── YES ──► [ BLOCKED ] (Basis uncoupled)
    │                         └── NO  ──► [ PARITY ]  (Mean reversion vs NYMEX)
    ├── 17:30 - 18:00 IST ──────────────► [ BLOCKED ] (Handoff Dead Zone)
    ├── 18:00 - 23:00 IST ──────────────► [ MOMENTUM ] (NYMEX Live Trading)
    └── 23:00 - 09:00 IST ──────────────► [ BLOCKED ] (Late night / Pre-open)
```

### 1. PARITY Regime (`09:00 – 17:30 IST`)
- **Premise:** NYMEX pit is closed/thin; MCX prices oscillate around landed fair value (NYMEX prompt + USD/INR conversion + import duties/taxes).
- **Strategy:** `NG_PARITY` (`src/engine/ng_parity_strategy.py`).
- **Entry Rules:** 
  - Deviation between MCX futures and NYMEX prompt must exceed `PARITY_DEV_ENTRY_PCT` (0.45%), but remain below `PARITY_DEV_MAX_ENTRY_PCT` (2.0%). (Deviations > 2% signal structural roll divergence or shock events, not mean reversion).
  - Deviation must be stable or shrinking (`check_deviation_stable_or_shrinking`).
  - DTE must be $> 3$ days. If $\le 3$, near-month basis distorts due to tender margin / rollover contango; entries are strictly blocked.
- **Exit Rules:** 
  - Mean-reversion to zero parity basis.
  - Hard time stop: Force flat at **17:30 IST** before US cash open.
  - Trailing stop clamped between 1.5 and 5.0 points.

### 2. MOMENTUM Regime (`18:00 – 23:00 IST`)
- **Premise:** US regular market session is live; high liquidity and trend formation driven by Henry Hub volume.
- **Strategy:** `NG_MOMENTUM` (`src/engine/ng_momentum_strategy.py`).
- **Entry Rules:**
  - Alignment with NYMEX 1H 20-EMA trend (`check_nymex_1h_trend()` using `NG=F`).
  - Must not blindly oppose weekly EIA macro stance (`get_active_ng_macro_context()`) unless overridden by real-time OI conviction (heavy Call/Put writing) and clean technical breakdown/breakout.
  - Entry cutoff: No new entries after **23:00 IST**.

### 3. EVENT Regime (EIA Storage Print Window)
- **Premise:** US Energy Information Administration (EIA) Weekly Natural Gas Storage Report.
- **Timing (DST-Aware):**
  - US Daylight Saving Time (Summer/EDT): **20:00 IST** (10:30 AM EDT).
  - US Standard Time (Winter/EST): **21:00 IST** (10:30 AM EST).
  - Window: Announcement time $\pm 15$ minutes.
- **Safeguards & Actions:**
  - `force_close_eia_pre_print()`: All open NG trades are forcefully squared off before the print to prevent catastrophic gamma / slippage.
  - Surprise Threshold: Minimum absolute surprise of **15 Bcf** (`EIA_MIN_SURPRISE_BCF`) vs consensus to trigger trade. Neutral deadband: $\le 5$ Bcf (`EIA_NO_TRADE_BAND_BCF`).
  - Hard time stop: Position exit at **21:30 IST** (EDT) / **22:30 IST** (EST).

### 4. TFSS Standalone Volatility Harvesting (`trend_following_short_strangle.py`)
- Independent multi-tranche options execution (`sid == "TFSS"`) using 5.0 pt strike increments.
- Exempt from `NG_MAX_POSITIONS = 1` directional limit.
- Net delta cap: `0.60`. Selective delta-stop exit at `HARD_STOP_DELTA = 0.50` closes tested side while decaying opposite side runs.
- Paired with $0.03$-delta disaster wings.

---

## Data Pipeline & Fetcher Invariants

1. **MCX Permissions & Priority Order (`src/fetchers/router.py`)**:
   - Zerodha Kite API provides full real-time quotes, Greeks, depth, and historical candles for MCX commodities.
   - `_priority_for` routes `NATURALGAS` strictly through:
     1. **`kite`** (Primary broker feed: 60+ strikes, Black-Scholes Greeks, 5-level market depth).
     2. **`shoonya`** (Fallback 1: Shoonya Finvasia broker API).
     3. **`dhan_commodity`** (Fallback 2: Dhan ScanX API).
     4. **`moneycontrol`** (Fallback 3: Web-scraped commodity fallback).
   - `live_trading.py` uses `kite.ltp` directly for `MCX:` keys with automatic failover to the router.
2. **Weekend & Holiday Invariants**:
   - No open directional positions over weekends (`NG_WEEKEND_FLAT = True`). Friday exits executed at 23:25–23:30 IST.
   - CME holidays completely block both Parity and Momentum trading.

---

## Common Rationalizations & Anti-Patterns

| Rationalization / Excuse | Reality & Core Invariant |
|---|---|
| *"Deviation is 2.8%, huge mean-reversion opportunity!"* | **BLOCKED.** Deviation $> 2.0\%$ indicates cash-futures rollover divergence or pipeline crisis, not mean reversion. |
| *"It's expiry week but DTE=2 has good premium."* | **BLOCKED.** Rollover contango permanently breaks the landed parity equation near tender period. |
| *"Hold open trade through EIA print for big breakout."* | **FORBIDDEN.** Pre-print force close is non-negotiable. 50-point slippage spikes destroy portfolios. |
| *"MCX trades until 23:30, enter momentum at 23:15."* | **BLOCKED.** `MOMENTUM_NO_ENTRY_AFTER_IST = "23:00"`. Late entries suffer illiquidity and sudden square-off. |
| *"Let trade run over the weekend; NYMEX will open in our favor."* | **FORBIDDEN.** Weather models update on weekends. Weekend gap risk exceeds 10%. Must be flat by Friday 23:30. |

---

## Troubleshooting & Verification Checklist

When diagnosing Natural Gas issues in NSEBOT:
1. **Check Session & Regime:** Run `python -c "from src.engine.ng_session_router import get_ng_regime; from datetime import datetime; import pytz; print(get_ng_regime(datetime.now(pytz.timezone('Asia/Kolkata'))))"`.
2. **Check MCX Underlying Price:** Verify Dhan commodity fetcher returns valid CMP (not `None` or 0.0) without falling back to stale DB prices.
3. **Verify DTE Calculation:** Ensure `get_futures_expiry("NATURALGAS", today)` resolves active near-month contract and DTE $> 3$.
4. **Audit Open Books:** Confirm open directional trades $\le 1$ in `paper_trades` and `live_trades`.
5. **Inspect EIA Schedule:** Verify `get_eia_schedule()` correctly flags holiday shifts (e.g. Thanksgiving, Juneteenth) and current DST offset.
