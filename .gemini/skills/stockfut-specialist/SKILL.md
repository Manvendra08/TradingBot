---
name: stockfut-specialist
description: Use when analyzing, executing, configuring, scanning, or debugging the Stock Futures (stockfut) module, 216 F&O stock universe, EOD swing setups, or futures risk controls in NSEBOT
---

# Stock Futures Specialist

## Overview
Authoritative engineering and domain operating standard for the **Stock Futures (`stockfut`)** swing module in NSEBOT. Governs the universe of 216 NSE F&O underlying single-stock futures, automated End-of-Day (EOD) scanning, quantitative feature engineering, multi-factor scoring, next-morning limit execution, trailing risk management, and mandatory physical delivery avoidance.

---

## When to Use
- Analyzing, executing, debugging, or configuring the Stock Futures swing trading pipeline (`src/stockfut/`).
- Inspecting eligible contract selection across the 216 NSE F&O universe (`src/stockfut/universe.py`).
- Auditing EOD feature computation (`src/stockfut/features.py`), relative strength, or scoring weights (`src/stockfut/scoring.py`).
- Investigating setup filtering, pullback reversal validation, or watchlists (`src/stockfut/setups.py`).
- Tuning or debugging the 10 mechanical exit rules and trailing stops (`src/stockfut/exits.py`).
- Validating morning pre-market refresh (09:14 IST), market order execution (09:17 IST), or 15-minute position reconciliation (`src/stockfut/jobs.py`, `src/stockfut/execution.py`).
- Auditing or extending web dashboard cockpit endpoints (`/futures`, `/api/futures/*`) and Telegram commands (`/futscan`).
- Verifying physical delivery risk guards, NSE ban list filtering, and expiry rollover mechanics.

## When NOT to Use
- Index options trading (`NIFTY`, `BANKNIFTY`, `SENSEX`) or commodity derivatives (`NATURALGAS`, `CRUDEOIL`).
- Multi-leg option structures (Iron Condor, Strangle, Straddle, TFSS).
- General dashboard UI changes unrelated to the Stock Futures module.

---

## Architecture & Code Map

```
src/stockfut/
├── universe.py        # 216 F&O symbols, contract resolution (current month vs next month), NSE ban/MWPL hygiene
├── features.py        # Trend EMAs, ADX, RSI, ATR, 20d cross-universe relative strength, Volume/OI surges
├── scoring.py         # Multi-factor quant scoring (0-100), conviction thresholds (score >= 70)
├── setups.py          # Setup identification (Breakout / Pullback), support/resistance confirmation, sizing
├── execution.py       # Paper & Live order dispatch via Kite, slippage modeling, DB persistence
├── exits.py           # 10 deterministic exit rules (SL, T1/T2, ATR trail, 5-bar time decay, Expiry flat)
├── jobs.py            # APScheduler cron hooks: 15:45 & 19:30 scans, 09:14 refresh, 09:17 exec, 15m reconcile
config/
├── stockfut.json      # Master configuration (capital, risk %, max positions, score cutoffs, expiry sessions)
└── stockfut_loader.py # Dataclass loader with fail-closed defaults and atomic reload
scripts/
└── run_stockfut_scan.py # Standalone CLI trigger for full-universe on-demand EOD scanning
```

---

## Universe & Contract Lifecycle Invariants

1. **Universe Scope:** Exactly 216 active NSE single-stock F&O underlyings (excluding market indices).
2. **Contract Selection Rule:**
   - **Pre-Expiry Day (`sessions_to_exp >= 1`):** The engine exclusively monitors and trades the **Current Month (Near)** contract.
   - **Expiry Day (`sessions_to_exp == 0`):** Automatically advances to the **Next Month (Far)** contract for new scans and signals.
3. **Mandatory Physical Delivery Avoidance:**
   - SEBI mandates physical delivery for all in-the-money or unexpired stock futures contracts held past expiry.
   - **Rule 10 (`EXPIRY`):** All open near-month stock futures trades must be closed flat on or before **15:15 IST** on their respective expiry day (`today >= expiry_date` or `sessions_to_exp <= 0`). Holding overnight past expiry is strictly prohibited.
4. **NSE Ban / Hygiene Gate:**
   - Symbols under the NSE F&O ban period (MWPL > 95%) or subject to corporate actions within 5 sessions are automatically filtered out.
   - Primary ban list source: `https://nsearchives.nseindia.com/content/fo/fo_secban.csv`.

---

## Quantitative Feature & Scoring Framework

Candidates must meet a quantitative multi-factor score $\ge 70$ out of 100 based on five core pillars:

| Factor | Metric / Formula | Bullish Requirement | Bearish Requirement | Weight |
|---|---|---|---|---|
| **Trend Alignment** | EMA(20) vs EMA(50) + ADX(14) | Close > EMA(20) > EMA(50), ADX > 20 | Close < EMA(20) < EMA(50), ADX > 20 | 25 pts |
| **Momentum** | RSI(14) + MACD Histogram | 50 < RSI < 70, MACD Hist > 0 | 30 < RSI < 50, MACD Hist < 0 | 20 pts |
| **Cross-Universe RS** | 20-day return relative to NIFTY & 216 universe | 20d return $\ge$ 70th percentile of universe | 20d return $\le$ 30th percentile of universe | 20 pts |
| **Volume & OI Surge** | 20-day Volume SMA + Futures Contract OI | Vol > 1.2x SMA(20), OI increasing | Vol > 1.2x SMA(20), OI increasing | 20 pts |
| **Volatility / ATR** | ATR(14) / Close % | Healthy ATR range (1.2% – 4.0% of spot) | Healthy ATR range (1.2% – 4.0% of spot) | 15 pts |

### Pullback Reversal Requirement
To prevent chasing extended moves at candle highs:
- A setup must demonstrate a prior 2–5 session pullback into dynamic value (EMA 20 or prior swing high/low) followed by a confirmed reversal candle (Hammer, Bullish Engulfing, or Close > previous High).

---

## Position Sizing & Risk Management

1. **Portfolio Capital:** Dedicated ₹10,00,000 allocated for Stock Futures (configurable in `config/stockfut.json`).
2. **Risk Cap per Trade:** Fixed at **1.0% of portfolio equity** (`risk_per_trade_pct = 1.0`, i.e., ₹10,000 max risk).
3. **Max Open Positions:** Clamped at **3 concurrent positions** to prevent margin overcommitment.
4. **Lot Sizing Formula:**
   $$\text{Lots} = \left\lfloor \frac{\text{Capital} \times \text{Risk \%}}{|\text{Planned Entry} - \text{SL}| \times \text{Lot Size}} \right\rfloor$$
   Clamped between 1 and `max_lots_per_trade` (default: 5 lots).
5. **Reward-to-Risk Requirement:** Planned setup must offer an R-multiple $\ge 1.5R$ before being armed.

---

## 10 Deterministic Exit Rules

Position exits are evaluated sequentially every 15 minutes by `src/stockfut/exits.py`:

| Rule ID | Exit Type | Condition | Order Type |
|---|---|---|---|
| **Rule 1** | `HARD_SL` | Price crosses initial Stop Loss level ($1.5 \times \text{ATR}$) | Market |
| **Rule 2** | `TARGET_1` | Price reaches T1 ($1.5R$); partial close of 50% lots, SL moves to Breakeven | Limit / Market |
| **Rule 3** | `TARGET_2` | Price reaches T2 ($2.5R$); remaining 50% closed | Limit / Market |
| **Rule 4** | `ATR_TRAIL` | Post-T1 trailing stop: $1.0 \times \text{ATR}$ behind highest high (for Long) | Market |
| **Rule 5** | `TIME_DECAY` | Position stagnant for $> 5$ sessions without hitting T1 | Market |
| **Rule 6** | `TREND_FAIL` | Daily close crosses against EMA(20) | Market (Next Open) |
| **Rule 7** | `BAN_EXIT` | Underlying stock enters NSE F&O ban period | Market |
| **Rule 8** | `MAX_DD_CUT`| Trade loss exceeds $1.5 \times$ initial calculated risk budget | Immediate Market |
| **Rule 9** | `CORP_ACTION`| Corporate action (Dividend, Split, Bonus) within 2 sessions | Market |
| **Rule 10**| `EXPIRY` | Current day is contract expiry day and time $\ge$ 15:15 IST | Mandatory Market |

---

## Scheduling & Daily Execution Sequence

The module operates on a strict timeline managed by APScheduler in `src/stockfut/jobs.py`:

```
09:14 IST  ──► Morning Pre-Market Refresh (`job_stockfut_morning_refresh`)
               • Verifies overnight gaps vs planned entry bounds (+/- 1.5% max gap).
               • Re-checks NSE F&O ban list; cancels invalid setups.

09:17 IST  ──► Entry Execution (`job_stockfut_morning_execution`)
               • Dispatches market entry for ARMED setups that passed morning validation.
               • Records active trade in `stockfut_trades` with snapshot provenance.

Every 15m  ──► Position Reconciliation & Exit Evaluation (`job_stockfut_reconcile`)
(09:15-15:30)  • Ticks active trades against current LTP, manages trailing stops & exits.

15:45 IST  ──► Post-Market Close EOD Scan (`job_stockfut_eod_scan_close`)
               • Runs on finalized daily bars immediately after 15:40 F&O CAS close.
               • Identifies and arms candidates for tomorrow's session. Dispatches Telegram digest.

19:30 IST  ──► Settlement Bhavcopy EOD Scan (`job_stockfut_eod_scan_evening`)
               • Re-runs against official exchange-cleared settlement prices and MWPL files.
```

---

## Telegram & Web Cockpit Commands

- **Telegram:**
  - `/futscan` or `/scan_futures`: Triggers an on-demand full-universe EOD scan and dispatches the alert.
  - `/futpositions`: Displays current active stock futures positions, P&L, and trailing stop levels.
  - `/futwatchlist`: Shows armed setups waiting for tomorrow's open.
- **Web Cockpit:**
  - URL: `http://localhost:8080/futures` (OCI: `http://137.23.58.212:8080/futures`).
  - `⚡ Run Scan` button: Triggers immediate scan via `POST /api/futures/scan`.
  - Settings: Toggle Stock Futures module paper or broker trade execution via `/settings`.
