# NSE Stock Futures System — Design (v2)

Status: DRAFT, design only · 2026-11-22 · v2 folds in an architecture review and a trading-risk review.
Scope: one experienced trader, account up to ₹1 Cr, paper first, live via Zerodha Kite.
Implementation tasks: `docs/superpowers/plans/2026-11-22-stock-futures-system.md`.

> Thresholds are engineering defaults to validate in replay and paper trading, not
> investment advice. All live in `config/stockfut.json` and must earn their place with data.

## 0. Decisions (fixed)

1. Swing trades, 2–10 sessions, driven by the daily trend.
2. Long and short from day one, chosen by stock and market sentiment.
3. SL and targets come from each stock's own price levels.
4. No hedging.
5. **Sizing follows exchange lot sizes. Minimum 2 lots per trade.**
6. A dedicated `/futures` page (section 8).
7. v2 reviewer decisions: separate `stockfut_trades` storage, no rolling (exit before expiry), and v1 trimmed to the core (section 11).

## 1. What "high POP" means for futures

A future is linear, so there is no theta cushion. Win rate comes from three levers:
**selection** (trend, OI flow, sector and market agree), **geometry** (first target just
before the nearest strong level, at least 1.6R away) and **abstention** (skip events,
bans, expiry, gap risk).

Judge the system by **expectancy in R after costs**, with a "win" meaning a trade that closes net
positive. Worked friction model (2 lots, half booked at T1):
- T1 only, runner stopped at its post-T1 stop (entry − 0.25 × ATR): ≈ +0.8R (0.5 × 1.6R) − ≈0.1R
  on the runner − ~0.1R costs and slippage ≈ **+0.6R net**.
- T1 + T2 (≥ 2.5R): ≈ +2.0R gross ≈ **+1.9R net**.
- Full loss including gap slippage on the stop: **≈ −1.1R to −1.15R**.

At 55% wins (35% T1 only, 20% T1 + T2) and 45% full losses this is ≈ +0.08R per trade; +0.15R
needs either a better hit rate or more T2 hits, which replay must show. Two choices protect it:
the T1 gate is 1.6R (at 1.3R the T1-only win drops to ~+0.45R), and after T1 the runner's stop moves
to **entry − 0.25 × ATR**, not back to the support zone, where a daily trigger's low sits (leaving it
there turns many T1 wins into small net losses). Aim: 55–65% win rate with expectancy above +0.15R net,
measured in replay (Task 11) before any live trading. A claimed rate above ~70% usually hides tail risk.

## 2. Data inventory — and what must be built

| Need | Existing | Gap to close |
|---|---|---|
| F&O instruments, tokens, expiry | `KiteDataClient.get_instruments("NFO")` | Take **per-contract lot size** from it, never the static `LOT_SIZES` dict (NSE revises lots; near and next month can differ) |
| Futures LTP, OI, depth | `kite.quote` | `get_market_quotes` returns price and volume only (no OI, no depth) and resolves symbols via `resolve_tradingsymbol`. Add `get_futures_quotes(keys)` returning OI and top-of-book for `NFO:` contracts |
| Daily candles with OI | `get_historical_candles(oi=True)` | It returns `[]` silently on error. The feature layer must **fail closed per stock** (no data = no trade, logged) |
| Sector / index regime | `index_weights`, `regime_detector.py` | Add universe breadth and VIX regime |
| Ban list / MWPL, results calendar | none | Small NSE EOD fetcher (`src/fetchers/nse_eod_fetcher.py`) |
| Costs | `accounting.calculate_transaction_costs` | Verify it prices stock futures (STT, stamp, exchange charges) |

Rate limit: ~3 history requests/s → about 70 s for 200 stocks. History runs after market, not live.

## 3. Architecture

```
08:45  ban + event refresh ─► universe_today
19:30  EOD scan ─► features ─► levels ─► bias ─► entry matrix ─► WATCHLIST (≤20, with full plan)
09:30  daily exit pass on the last completed daily bar (runs first, frees slots)
09:30  ARMED watchlist ─► open check (gap, SL, spread) ─► risk gate ─► marketable-limit entry
every 15 min ─► reconciliation + portfolio risk monitor (GTTs guard the intraday risk)
```

**Model A: daily signal, next-morning execution.** The system has no 3H candles at all. The 19:30
scan arms a setup on a completed daily reversal candle; the entry happens at **09:30 the next
session**, 15 minutes after the open so the opening auction noise has cleared. Entering in the
morning also avoids the thin, wide-spread closing window and gives the position a full session
before the first close. The setup is **cancelled** (not chased) if at 09:30 price is not within
0.3 × ATR of the trigger price, SL is already breached, the spread exceeds the limit, or the 4.2
geometry fails on the live price. An unfilled order follows the 6.1 re-price-once-then-cancel
rule; there is no later retry that day.

**Replay assumptions (Task 11, daily bars only).** (1) Entry = next day's open + slippage
(half the typical spread + 1 tick + 0.05 × ATR for the 09:15→09:30 drift); the 0.3 × ATR open
check is applied to the open. (2) If a day's range contains both SL and T1/T2, assume the **stop
hit first**. (3) A gap beyond SL fills at the open, not at SL. Before trusting results, spot-check
the last ~400 days against Kite 60-minute bars for the ordering and 09:30 assumptions.
Replay also reports the **funnel**: scanned → ARMED → geometry pass → 09:30 pass → filled, with the
reason for every drop, so an over-tight gate (1.6R, 0.3 × ATR, single-shot) is visible.

The scan runs at 19:30, after NSE publishes ban and MWPL data. Everything in
`src/stockfut/` including its jobs (`jobs.py`) stays out of `job_runner.py`
(already 2,600+ lines) apart from one registration call.

Timeframes (this package differs from the options engine's 3H/1H rule): daily bars set trend,
bias and levels (no 60-min volume profile), entries and exits (Model A). No intraday candles are used for any decision: no 3H, no 1H.

### 3.1 Universe & hygiene (excluded for the day if any is true)
- In the ban list, or MWPL above 80%.
- Results, AGM, ex-date, split or other corporate action within **7 sessions** (or a lot-size change pending), so most trades can run their course before the EVENT exit (4.4 #4) forces them out.
- Near-month OI value below ₹50 Cr or median spread above 0.05%.
- **Cannot be sized:** two lots at the maximum stop distance (2 × daily ATR) risk more than the hard cap (section 5).
- **Contract choice:** trade the near month only if it has ≥ 12 sessions left; otherwise the next month, if it passes the same liquidity test. Else no entry.

### 3.2 Features (daily, deterministic)
Trend (EMA20/50 slope, ADX14), relative strength (20-day return rank vs NIFTY and sector),
OI flow (price Δ and OI Δ over 1, 3, 5 and **20** days, with a 3% OI noise floor; the 20-day
window is the trend's OI and spans a 2–5 day pullback, the 5-day window describes the pullback itself; computed on
**combined near+next-month OI** so expiry-week roll does not read as unwinding),
volume vs its 20-day average, ATR (14, daily — passed explicitly, never an intraday fallback),
and NR7/squeeze flag.

Market regime (daily): NIFTY trend, universe breadth (% above EMA50, advance/decline), India VIX
level and change → `RISK_ON / NEUTRAL / RISK_OFF`.

### 3.3 Setup (v1 ships one)
**A. Trend pullback continuation.** Long: stock bias Bull or Strong Bull (section 4.3), then a
2–5 day pullback to EMA20 or the last breakout level on **falling OI and volume** (5-day OI flow
reads unwinding — expected, and not held against the trade, because bias uses the 20-day flow). The
trigger is a **daily reversal candle** at support or EMA20 (section 4.3 step 3), read on the 19:30
scan and executed at 09:30 next session. Short is the mirror image.
Setups B (squeeze breakout) and C (short-trap reversal) wait until A passes paper.

### 3.4 Scoring and watchlist
Score 0–100: trend 25, relative strength 25, OI flow 30, regime fit 20. Watchlist = bias is not
Neutral, the matrix cell allows the side, score meets the cell's minimum, and the plan passes the
geometry gates (4.2). Each row is frozen into a `scan_snapshot` (SHA-256, as today).

## 4. Price levels, trade plan, entry and exit matrices

### 4.1 Level engine (`src/stockfut/levels.py`, daily bars only)
Sources: swing pivots (60 and 120 sessions, clustered within 0.3 × ATR), breakout and gap
edges, EMA20/EMA50 as dynamic levels. Nearby levels merge into a **zone**; strength = touch
count + recency + number of agreeing sources.

Levels are computed on **cash-equity daily bars** (`NSE:<SYMBOL>`), where resting institutional
orders actually sit, not on a spliced continuous futures series (rollover premium and carry
distort old pivots). The bars must be **adjusted for splits and bonuses** (otherwise every old
pivot is wrong by the split ratio) but **not for dividends** (that would move levels away from
the prices traders saw). Phase 0 confirms which adjustment Kite's daily history applies. Each level is then projected to the traded contract by
adding today's basis (`futures LTP − cash LTP`), refreshed at the 09:30 entry check and at every stop move,
because GTTs trigger on the future's LTP. OI features still come from futures (section 3.2).

**Validation:** zone reaction rates are measured **pooled across the universe by zone type** in
replay (per-stock samples are too small). A zone type below a 55% reaction rate is dropped from use.

### 4.2 SL, targets and geometry gates (`trade_plan.py` → `calculate_future_sl_target`)
Long (mirror for short): **SL** = beyond the nearest strong support under entry minus 0.25 × ATR.
**T1** = just before the nearest strong resistance above entry. **T2** = the next strong resistance.
Skip the trade if any gate fails:
- SL distance outside 0.75–2.0 × ATR.
- **T1 distance < 1.6 × SL distance** (see the friction model in section 1; 1.3 leaves near-zero expectancy).
- A strong opposing zone within 0.5 × SL distance of entry.
- No qualifying level exists (the system never invents one).

### 4.3 Entry matrix
**Step 1: stock bias.** Four inputs each score +1 / 0 / −1:

| Input | +1 | −1 |
|---|---|---|
| Daily trend | EMA20 > EMA50, both rising, ADX ≥ 20 | the mirror image |
| Relative strength | rank ≥ 70 vs NIFTY **and** sector | rank ≤ 30 vs both |
| 20-day OI flow | Long Build-up or Short Covering | Short Build-up or Long Unwinding |
| Sector | sector trend up, RS top third | sector trend down, RS bottom third |

Bias: +3/+4 Strong Bull · +2 Bull · −1..+1 Neutral (no trade) · −2 Bear · −3/−4 Strong Bear.

**Step 2: bias × market regime → permission and minimum score.** Counter-trend cells demand
a higher score; **size is never reduced below 2 lots**, so difficulty is expressed as score, not size.

| Bias ↓ / Market → | RISK_ON | NEUTRAL | RISK_OFF |
|---|---|---|---|
| Strong Bull | LONG ≥ 65 | LONG ≥ 70 | LONG ≥ 80 |
| Bull | LONG ≥ 70 | LONG ≥ 75 | — |
| Neutral | — | — | — |
| Bear | — | SHORT ≥ 75 | SHORT ≥ 70 |
| Strong Bear | SHORT ≥ 80 | SHORT ≥ 70 | SHORT ≥ 65 |

**Step 3: daily reversal candle (read at the 19:30 EOD scan).** Both must hold on the latest
completed daily bar (LONG shown; SHORT is the mirror image):

1. **Touch.** The bar's low touches or tests the supporting zone or EMA20 (within 0.3 × ATR).
2. **Reversal close.** Either (a) the close is in the **upper 35% of the bar's own range**, or
   (b) the close is above the **previous day's close** and the 20-day OI flow is net Long Build-up.

The first bounce off support is usually short covering (price up, OI flat or falling), so that
pattern is accepted only when the 20-day OI flow still shows net Long Build-up, never on the single
bar's OI alone. The 5-day flow is not used here: after a real pullback it reads unwinding by design. If the geometry gate (4.2, T1 ≥ 1.6R) passes on the planned
entry (the bar's close), the setup is **ARMED**. Replay reports win rate per branch (a/b) so the
weaker branch can be dropped.

**Step 4: 09:30 entry gates (next session).** Execute a marketable limit only if price is within
0.3 × ATR of the trigger price, SL is not breached, the spread is within limit, 4.2 geometry is
still valid on the live price, pre-trade validation passes (section 6.1) and portfolio limits have
room (section 5). Otherwise cancel the setup with the reason logged. An ARMED setup is single-shot:
it is evaluated at the next 09:30 only; it is not carried forward.

### 4.4 Exit matrix (priority order, first match wins; sibling of `exit_evaluator`)

| # | Exit | Condition (long; mirror for short) | Action | Evaluated |
|---|---|---|---|---|
| 1 | HARD_STOP | Price trades through SL | Exit all (GTT at broker + watcher, see 6.2) | Broker + 1-min watcher |
| 2 | GAP_THROUGH | Opens beyond SL | The SL GTT has already fired and left a resting limit order that the gap skipped: `cancel_order` it, poll until `CANCELLED` (or filled), re-read net quantity, then send a marketable limit ladder for what remains. Never send the ladder while that order rests (double exit / unintended reverse position). | Open, from 09:15:30 |
| 3 | THESIS_BREAK | Daily close back through the supporting zone, or 2 straight days of opposing OI flow | Exit all | 09:30 (last daily bar) |
| 4 | EVENT | Results, AGM or ex-date within 1 session; ban entry for a held name (reduce-only) | Exit all | 08:45 (ban and event refresh) |
| 5 | T1_HIT | Reaches T1 | Book `floor(n/2)` lots; move the runner's stop to **entry − 0.25 × ATR** (breakeven with a noise buffer), then let TRAIL raise it | Broker GTT |
| 6 | T2_HIT | Reaches T2 | Exit the remainder | Broker GTT |
| 7 | TRAIL | After T1: daily close below the higher of the last daily swing low or the newest support zone | Exit the remainder | 09:30 (last daily bar) |
| 8 | REGIME_TURN | Market regime flips against the trade | Tighten stop to max(current stop, last daily swing low), never loosened | 09:30 |
| 9 | TIME_STOP | T1 not hit in 5 sessions, or 10 sessions total | Exit all | 09:30 |
| 10 | EXPIRY | Flat **5 sessions before expiry**, before the delivery-margin window | Exit all. **No rolling.** | 09:30 |

**Why one daily pass.** Entries are decided on daily bars, so exits are too. Intraday exits on
1H closes would cut trades on noise the entry logic never saw. The daily pass runs once a session
at 09:30, on the last completed daily bar, before entries. Exits triggered at 09:30 are
marketable limits executed the same morning. The broker GTTs (rows 1, 5, 6) and the 1-minute watcher
still cover moves between sessions. Rows 3, 7, 8, 9 and 10 can be at most one session late; replay
measures that cost.

**Exit rules in order:** HARD_STOP, GAP_THROUGH, THESIS_BREAK, EVENT, T1_HIT, T2_HIT, TRAIL,
REGIME_TURN, TIME_STOP, EXPIRY.

## 5. Sizing and portfolio risk (₹1 Cr reference)

**Sizing follows lot sizes, minimum 2 lots.**
`lots = max(2, floor(risk_budget ÷ (SL_distance × lot_size)))`, where `risk_budget` = 0.75% of capital (₹75k).

Because the 2-lot floor can push risk above the budget on wide-ATR names, add a hard check **after** rounding:
`risk(lots) ≤ 1.25 × risk_budget` (about ₹94k), otherwise skip and log `MIN_LOTS_BREACH`.
Also enforce a per-order cap (quantity and value) in the executor, independent of the signal.

| Control | Default |
|---|---|
| Max open positions / per sector | 6 / 2 |
| Net directional exposure | ≤ 60% of gross notional on one side |
| Gross notional | ≤ 2× capital |
| **Gap stress test** (no hedge) | Simulate a 5% adverse gap on every open position plus the candidate; block entry if stressed loss > 6% of capital |
| Margin headroom | ≥ 40% free (Kite margins API) |
| Daily loss limit | 2% of start-of-day equity, **realised + unrealised** → no new entries |
| Weekly loss limit | 4% → no new entries |
| Consecutive losses | 3 → pause entries (mirror the existing consecutive-loss breaker) |
| Drawdown kill switch | 8% from the equity peak → persisted flag `stockfut_halted`; blocks entries, keeps GTT protection, manual re-enable; optional flatten threshold (default off) |

Lot splitting for GTTs: `n` lots → T1 leg = `floor(n/2)` lots, T2 leg = `n − floor(n/2)` lots;
every GTT quantity is a multiple of that contract's lot size. With the 2-lot minimum both legs
are always non-empty.

Implementation: `calculate_future_lots` (`capital_allocator.py`), portfolio checks in a new
`src/stockfut/portfolio_risk.py` (kept out of `risk_engine.py`'s option-specific limits).

## 6. Execution

### 6.1 Entry flow (live)
1. **Intent first:** write an `order_intent` row (deterministic id, shown to the broker as order `tag` ≤ 20 chars, e.g. `SF` + base36 id).
2. `authorize_broker_execution(symbol, "ENTRY", snapshot)` — fail-closed. Needs changes (6.4).
3. `validate_order()` (cannot be bypassed): quantity is a multiple of the **current** lot size, price within the circuit band and within X% of LTP, tick-rounded, NRML, contract not expired, no duplicate open order, per-order cap.
4. Re-quote: abort if price is beyond 0.3 ATR of the trigger or the spread widened.
5. **Marketable LIMIT** (best bid/ask ± 1–2 ticks). No MARKET orders. Unfilled after 30 s → re-price once → else cancel and log `MISSED`.
6. If `confirm_order_fill` returns PENDING: **cancel, re-read the filled quantity**, and protect the **actual filled quantity** (including partials). Never assume zero.
7. Place protection on the filled quantity: two GTT-OCOs (T1 leg: SL + T1; T2 leg: SL + T2). **If GTT placement fails, flatten at once and halt entries.**
8. Persist the trade, then send the Telegram entry alert (after broker confirmation only).

### 6.2 GTT safety (the existing GTT code sends LIMIT orders on both legs)
- **SL leg limit** is set with a wide buffer beyond the trigger (default 0.3 × ATR), so a fast move still fills.
- **Post-trigger watcher** (1-minute): if a GTT is `triggered` and the position is still open after 60 s, cancel the resting order, **wait for `CANCELLED` and re-read net quantity**, then send a marketable-limit ladder for the remainder, and alert. Same cancel-confirm-then-ladder sequence as GAP_THROUGH.
- **After a GTT fires:** OCO cancels its own sibling leg. Then:
  - **SL fired** (on either GTT): cancel the other GTT, verify, flatten any remainder, reconcile.
  - **T1 leg's target fired:** keep the T2 GTT; replace it (place new, then cancel old) with its SL at entry − 0.25 × ATR (4.4 #5), verify, reconcile.
  - **T2 leg's target fired:** cancel the T1 GTT if still live, verify flat, reconcile.
- **Invariant, checked after every change:** broker net quantity = sum of live GTT quantities. Mismatch → flatten the excess or alert.
- **Replace before cancel** on stop moves (place the new GTT, then cancel the old).
- **Manual or bot exits:** cancel GTTs → verify cancelled → flatten → verify net quantity is 0.
- The orphan-GTT sweep in `live_trading.py` only covers index and MCX names; extend it to the `STOCK_FUT` whitelist.

### 6.3 Reconciliation and recovery
Broker is the source of truth. **Startup reconciliation runs before any scan:** DB open but
broker flat → mark closed from GTT history; broker open but DB flat → adopt or flatten; quantity mismatch → alert.
Every 15 minutes the same check runs. Alert if the bot has been down > 30 minutes with open
positions. If the Kite token has expired, the bot cannot manage positions: alert, and the GTTs carry the risk.

### 6.4 Gate and emergency changes
- `authorize_broker_execution` gets a per-strategy flag `stockfut_live_enabled` (so the stock futures kill switch does not stop options) and treats **protective GTT placement as `EXIT`** (today a paused bot blocks it, leaving a fresh fill unprotected).
- The gate's daily-loss query reads only `live_trades` and `multi_leg_trades`; extend it to `stockfut_trades`.
- `emergency_flat.py` is account-wide and uses MARKET orders. Add a stock futures path: cancel orders and GTTs first, flatten **only** `STOCK_FUT` whitelist or bot-tagged symbols with a limit ladder, then verify net quantity is 0.

### 6.5 Ban list and physical settlement
A held name that enters the ban list becomes reduce-only: allow exits, block entries, re-place the stop,
never roll. Stock futures settle by **physical delivery** and brokers raise margin or square off in
expiry week, so EXPIRY (4.4 #10) exits 5 sessions early.

### 6.6 Paper mode
Same pipeline, plan and gates. Fills at the 09:30 quote ± (half the observed spread + 1 tick) using the
depth from `get_futures_quotes`, with full costs. Paper keeps running when the live gate says no.

### 6.7 Audit
Log **every** decision, including skips and risk blocks: inputs, config hash, `snapshot_id`, gate result,
order intent and broker responses.

## 7. Learning loop
Journal every watchlist row (taken, missed, skipped + reason, MAE/MFE). Weekly per-setup and per-side report
(long and short reported separately): net-positive rate, expectancy in R, hold time, slippage vs paper.
Promotion: **≥ 30 paper trades, expectancy > +0.15R, net-positive rate ≥ 55%**. Auto-demote to paper if the
rolling-30 expectancy goes below 0. Tune on walk-forward splits only.

30 trades cannot prove a 55% win rate (the 95% margin is about ±18 points). **Replay is the edge
evidence**; paper proves the plumbing (fills vs model, slippage, GTT behaviour, no reconciliation
breaks) and must not be visibly worse than replay. Live results are judged against replay, with
size kept at the 2-lot minimum until ~100 live trades.

## 8. The `/futures` page

A dedicated page, built like the existing pages (Swiss Modern Flat, `theme.css`, `theme.js`).

**Wiring:** `GET /futures` in `dashboard_server.py` serving `src/dashboard/futures.html` (same pattern as `/paper`);
the 8 existing templates each carry their own copy of the nav, so each gets one new link.
Data comes from `GET /api/futures/{overview,watchlist,positions,trades,risk,journal,performance}`.

| Block | Content |
|---|---|
| Status strip | Mode (PAPER / LIVE / SHADOW), market regime, kill-switch and halt state, margin used, today's P&L, drawdown |
| Watchlist | Symbol, side, bias, score, trigger, SL, T1, T2, lots, R:R, status (armed / triggered / expired / skipped + reason) |
| Open positions | Entry, LTP, P&L (₹ and R), an SL–entry–T1–T2 ladder, GTT status, days held, the next exit rule that applies |
| Risk panel | Exposure by sector, net long/short, gap-stress loss, loss-limit usage, 2-lot-floor breaches |
| Trades and journal | Closed trades with exit reason, MAE/MFE, R multiple |
| Performance | Equity curve, expectancy by setup and side, slippage vs paper |
| Controls | Paper/Live tab, pause entries, arm live, flatten all (with confirmation); live toggle reads the same gate as the rest of the bot |

Telegram digest at 19:45 (new watchlist) and on entries, exits and halts.

## 9. Code placement

| Piece | Location |
|---|---|
| Universe, features, levels, setups, exit matrix, portfolio risk, executor, jobs | `src/stockfut/` (new package) |
| NSE ban / events fetcher | `src/fetchers/nse_eod_fetcher.py` (new) |
| Futures quotes with OI + depth | `kite_fetcher.py` (extend) |
| SL/target | `trade_plan.py` (extend, house rule) |
| Lots | `capital_allocator.py` (extend) |
| Gate, orphan-GTT sweep, emergency flat | `broker_gate.py`, `live_trading.py`, `emergency_flat.py` (extend) |
| Storage | Migration with `stockfut_trades`, `stockfut_watchlist`, `stockfut_decisions` (new; **not** `live_trades`, whose poller assumes option logic) |
| Config | `config/stockfut.json`, hot-reloaded like `decision_matrix.json` |
| UI | `futures.html`, route and API in `dashboard_server.py`, nav link in 8 templates |

## 10. Phases (detailed tasks in the implementation plan)

| Phase | Deliverable | Exit criteria |
|---|---|---|
| 0 Foundation | Storage, futures data layer, NSE fetcher, universe, features; confirm Kite stop order types, history limits, cost model | Features for the universe in < 3 min; 5 stocks spot-checked against charts |
| 1 Replay | Levels, matrices, plan, sizing, exits over 3+ years | Expectancy > 0 after costs; net-positive rate ≥ 55%; long and short each reported; worst single-gap loss reported; pooled zone reaction ≥ 55% |
| 2 Paper | Scan, watchlist, triggers, paper executor, journal, `/futures` page | ≥ 30 paper trades within the replay range |
| 3 Safety | Gate changes, GTT watcher, reconciliation, kill switches, emergency flat | Every control proven by a targeted test and a mocked-Kite scenario |
| 4 Live, minimum size | 2 lots per trade, 20-name whitelist | 20 live trades, slippage ≤ 1.5× paper, zero reconciliation breaks |
| 5 Extend | Setups B/C, LLM news veto (entries only, JSON enum output), delivery %, participant data | Each passes the paper promotion rule |

## 11. Trimmed from v1 (and why)
Volume profile (60-min data leaked into entry planning), round-number levels, options OI walls, carry score,
FII/participant OI, setups B/C, roll logic, per-stock zone reaction rates (too few samples), and size multipliers
(dead cells under the 2-lot floor) are out of v1. The 5-minute risk monitor became 15 minutes because GTTs cover
intraday risk. The LLM veto, if added, applies to entries only and never touches exits, GTTs or sizing.

## 12. Open items (defaults apply)
1. The 1.25× hard cap on a 2-lot trade's risk and the 5%/6% gap-stress numbers are starting values; the replay's worst-gap result should set them.
2. Verify in Phase 0: Kite stop order types for stock futures, `accounting.py` futures costs, whether Kite's cash daily history is split/bonus-adjusted and not dividend-adjusted (section 4.1), the exact `live_trades` poller behaviour (reviewer claim), and the gate's daily-loss query (reviewer claim).
