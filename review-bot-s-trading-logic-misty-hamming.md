# NSEBOT trading-logic review — findings & fix plan

## Context
User asked for a review of the bot's trading logic with correction/improvement suggestions.
Three parallel deep reads covered (A) entry/decision, (B) single-leg exit lifecycle & P&L,
(C) multi-leg + LLM layer. Items marked ✔ were re-verified in source by me.
Live is in shadow mode with the broker disabled, so today these hit paper P&L. They turn into
live risk once `is_broker_trade_enabled()` goes true.

**Doc/code mismatch (fix first, it's misleading every reviewer):** CLAUDE.md says CORE buys
CE/PE. `paper_plan.py:131-150` `VERDICT_ACTION_MAP` maps every CORE verdict to **SELL**
(bullish→sell PE, bearish→sell CE, neutral→strangle). Several modules still score as if
CORE buys (see A5).

---

## A. Entry / decision path

### P0
- **A1 ✔ MCX FUT fallback always SELL.** `paper_plan.py:217-225` sets `side="SELL"` for FUT
  regardless of verdict, *and* it runs before the STRANGLE branch → neutral verdicts also
  short futures. Fix: bullish→BUY FUT, bearish→SELL FUT, neutral→`return None`.
- **A2 ✔ PE buildup alerts scored with wrong sign.** `anomaly_detector._detect_buildup`
  (`:404-411`) labels per-contract; `intelligence.py:349-354` maps "Long Buildup" → BULLISH
  regardless of `option_type`. On PE, long buildup = put buying = bearish. Same at
  `intelligence.py:963-972, 992-993`. Fix: flip when `option_type=="PE"` (one helper, 3 call sites).
- **A3 ✔ AI veto can never fire.** `decision_pipeline.py:305-307` gates on
  `_extract_ai_veto_flag`, but `LLMTradeVerdict` (`llm_enrichment.py:124`) has no `veto_flag`
  field → always False. In `full` mode a NO_TRADE / direction-flip-forced-NO_TRADE / HIGH-risk
  LLM verdict does not block CORE. Fix: in full mode block CORE when AI action is NO_TRADE,
  disagrees in direction, or `risk_rating=="HIGH"` (derive the veto, don't add a schema field).

### P1
- **A4 ✔ Tiered composite flips failed soft gates to passed** (`decision_pipeline.py:1402-1410`),
  incl. `trend`; heavyweight step returns 100 when N/A (`:1214-1251`) = free +10; confidence
  lowers the threshold a second time (`:1391-1396`). Fix: make `trend` a hard gate; N/A → 50;
  drop the confidence reduction (confidence is already gated upstream).
- **A5 Regime/momentum scoring assumes long options** (`regime_detector.py:192-207`):
  sell-PE (bullish) scores 100 in a *down*trend; RANGE scores 30 though it's best for selling.
  Fix: score on (direction, side) not option_type.
- **A6 Price-OI matrix fallback** (`intelligence.py:180-181, 227-228`): both-sides-unwinding
  or zero OI change → "Long Buildup". Fix: return Sideways when both Δ<0 or both ==0.
- **A7 OI Δ with no baseline / window drift** (`anomaly_detector.py:1108-1109, 1170`): first
  scan's Δ = total OI. Fix: sum Δ only over strikes present in both snapshots (`prev_by_key`
  exists); no baseline → None → Sideways.
- **A8 Missing PCR → bullish** (`intelligence.py:196-197` `pcr is None or pcr>=0.85` → contra);
  asymmetric thresholds vs `:167`. Fix: None → no flip; symmetric thresholds.
- **A9 "CE accumulating despite dip" labelled bullish** (`intelligence.py:218-226`): CE OI
  build = call writing = bearish. Fix: use `hi_pe`.
- **A10 Direction-blind bonuses** (`intelligence.py:456-537`): PCR/support/max-pain bonuses
  ignore verdict direction; broken support (negative distance) earns "bounce" bonus; neutral
  alerts add +20; one strike event counted ~4×; chart adds +10 despite "zero weight" rule.
  Fix: pass bias in, `0<=dist`, neutral=0, dedupe per (strike, ot), drop chart bonus.
- **A11 Neutral verdicts skip confidence & MCX 72 floor** (`decision_pipeline.py:123-132, 281`);
  NEUTRAL becomes "BEARISH" in AI alignment. Fix: same floor for neutral; explicit NEUTRAL.
- **A12 Live ignores NG strategy gates** (`pipeline.py:1474-1477`): paper runs NG runners, live
  runs generic CORE. Fix: skip live entry when the paper runner returned BLOCKED.
- **A13 NG keyword direction** (`strategy_registry.py:105-109`): "LONG UNWINDING"→BUY,
  "SHORT COVERING"→SELL. Fix: use `is_bullish`/`is_bearish` from `verdict_sets.py`.
- **A14 Decision-engine error ⇒ trade opens** (`paper_trading.py:1366-1389`). Fix: missing/
  failed decision = BLOCKED.
- **A15 ✔ Sizing** (`capital_allocator.py:133-136, 204, 212`): `max(1, …)` forces 1 lot even
  when 1 lot breaches capital/risk; `live_symbol_lots` override (NIFTY/BANKNIFTY/SENSEX=10)
  returns before any risk/margin/cap check on naked shorts. Fix: return 0 when computed 0;
  clamp overrides by risk & margin caps.
- **A16 ✔ Short margin = 12× premium; broker margin path dead** (`capital_allocator.py:156,180`):
  `_current_expiry` is never set by any caller. SPAN scales with notional, so OTM shorts are
  under-margined several-fold. Fix: pass expiry; fallback `underlying × lot × 0.12`.
- **A17 Single-leg SELL doesn't enforce OTM** (`trade_plan.py:684-701`): delta estimated from
  `abs()` distance lets ITM strikes qualify. Fix: filter OTM inside `select_candidate`.
- **A18 ✔ EIA blackout hardcoded 20:00 IST** (`time_guards.py:60-64`). US DST ends 1 Nov 2026
  → report moves to 21:00 IST; entries allowed through the print from next week. Fix: derive
  from 10:30 America/New_York (pytz already imported). **Time-sensitive.**
- **A19 Latent (TFSS off)**: LLM `instrument` overrides option_type while side stays SELL
  (`paper_plan.py:198-204`). Fix: ignore LLM option type for SELL plans.

### P2
- Fake premium (1% of underlying) when chain empty (`paper_plan.py:327-330`); DB fallback skips
  staleness when `fetched_at` missing (`trade_plan.py:399-426`); SELL entries use LTP not bid.
- ATR silently falls back to any timeframe (`trade_plan.py:63-70`); paper vs live SL fallback
  differ (`paper_plan.py:453` vs live).
- Entry-quality R:R is constant 1.33 (never penalises); MCX liquidity checked at ATM, not the
  OTM strike sold; GOLD/SILVER skip liquidity; CORE lacks market-hours check.
- `IntelligenceResult` not a dict → `pipeline.py:1204,1255,1371` updates swallowed (ML
  prediction never attached, decision recomputed).
- FII boost sums bull+bear; no freshness check. `trend_analysis.py:83` ISO 'T' vs SQLite
  string compare → window up to ~48h.

---

## B. Exit lifecycle & P&L (single-leg)
Config today: `manage_direct_kite_positions=false`, shadow on, broker disabled → live P0s are
dormant but all fire the day live goes on.

### P0
- **B1 ✔ Uncommitted Gemini FUT safety net uses spot for NIFTY** (`paper_trading.py:1146-1152`,
  in current diff). Gemini stores *futures* levels; bot `underlying` is spot. NIFTY basis ≈680pt
  → every open NIFTY Gemini FUT stops out on first check with a fake loss pushed to the ledger.
  Fix before committing: restrict the net to MCX symbols; add a NIFTY case to
  `tests/test_exit_lifecycle_fixes.py`.
- **B2 ✔ Unfilled exit LIMIT never cancelled** (`live_trading.py:915-944`; no `cancel_order`
  anywhere). Next poll sends a 2nd exit → both fill → reversed naked position. Fix: cancel on
  PENDING, re-read `filled_quantity`, re-open only the unfilled qty.
- **B3 ✔ GTT fills never reach the DB** (`live_trading.py:2592-2594`): whole sync early-returns
  on the flag, incl. stale-row auto-close and orphan-GTT cleanup. Row stays OPEN → reversal/
  Friday/expiry sweep exits an already-flat position = fresh naked position. Fix: run step 1 +
  orphan cleanup unconditionally; flag gates only *adopting* manual positions; check
  `kite.positions()` inside `_exit_open_live_trade` before sending any exit.
- **B4 Reconciled closes have NULL P&L** (`live_trading.py:2724-2727`) → daily-loss and
  consecutive-loss breakers (`risk_engine.py:73, 218-257`) count GTT stops as ₹0. Fix: close
  via `close_live_trade(...)` with fill price from GTT order history.
- **B5 TIMEFRAME live: GTT *and* POLL on the same trade** (`live_trading.py:2027, 2057`) →
  double exit. Fix: `gtt_order_id` set ⇒ `exit_mode="GTT"`, skip polling.
- **B6 Zero feed price triggers real FUT exits** (`live_trading.py:1216-1260`,
  `job_runner.py:1142-1161`): `underlying=0` hits BUY SL / SELL target; then close rejects 0 →
  EXIT_PENDING → recovered to OPEN. Fix: skip when `underlying<=0` or premium `<=0`.
- **B7 New `recover_stale_exit_pending` can resurrect filled exits** (`schema.py:2213-2252`,
  uncommitted). Fix: check claim's `broker_order_id` status + Kite net position first; if filled,
  close the row instead.
- **B8 Exit poll ignores expiry** (`job_runner.py:1328, 1147-1157`) → priced off another
  week's same-strike contract. Fix: pass `expiry=trade["expiry"]`, filter rows by expiry.
- **B9 Weekly tradingsymbol strike regex** (`live_trading.py:2695`): `NIFTY24O1025000CE` →
  strike 1025000 → open trade auto-closed + GTT cancelled. Fix: resolve via instrument token
  (`resolve_instrument`), key includes expiry. (Prereq for enabling B3.)
- **B10 (suspected) Sync closes rows whose entry hasn't filled** (`:2704`, insert at `:1399`).
  Fix: skip `broker_status!='COMPLETE'` or `opened_at` < 2 min.

### P1
- **B11 ✔ Reversal trend guard is inverted** (`paper_trading.py:302-309`, `live_trading.py:309-316`):
  scores the *new reversal* verdict and blocks when >40, i.e. blocks real turns and allows lone
  counter-signals. Fix: block unless reversal's own score ≥ 60.
- **B12 Monitor checks only newest main trade per symbol** (`schema.py:1236-1245` `LIMIT 1`),
  up to 3 can be open. Fix: iterate `get_all_open_paper_trades`.
- **B13 Trailing/delta-stop exits clamped to full SL** (`paper_trading.py:959-977`) → winners
  booked as full losses, poisons ML retraining. Fix: clamp only on plain underlying-SL hits.
- **B14 ✔ Live TIMEFRAME 1H exit never fires** (`live_trading.py:1804,1813`): `verdict_label`
  stored as `"LONG"/"SHORT"` (`:2083`) but checked with `is_bullish()` which only knows OI/LLM
  labels. Fix: `== "LONG"` / `== "SHORT"` as `:1734` already does.
- **B15 Broker gate blocks EXITS** in event-shock, CME holiday/early close, 09:15-09:30
  (`time_guards.py:113-133`). Fix: `operation != "EXIT"` on those windows.
- **B16 Expiry sweep misses `DD-MMM-YYYY` expiries** (`job_runner.py:508-515`; 14 rows with
  `29-OCT-2026`). Fix: `_normalize_expiry_date`; normalise on insert.
- **B17 Shadow trades close from any old paper trade on the same strike** (`live_trading.py:1049`).
  Fix: `opened_at >= live.opened_at` (or shared `signal_key`).
- **B18 GTT cancelled after exit fills** (`live_trading.py:896-927`) → race doubles exit. Fix:
  cancel GTT first; if already triggered, book from its fill and skip our order.
- **B19 ✔ MCX close hardcoded 23:30** (`settings.py:95-99`). US DST ends 1 Nov 2026 → MCX runs
  to 23:55 from next week. Fix: DST-aware close. **Time-sensitive (same root as A18).**
- **B20 R-multiple flips sign once SL trailed past entry** (`paper_trading.py:798-818`). Fix:
  store `initial_sl_underlying`.
- **B21 Live FUT polled every 15 min, no GTT** (`job_runner.py:2403-2411`). Fix: SL-M/GTT for FUT.
- **B22 Duplicate GTT race / missing GTT after restart** (`live_trading.py:1079-1137` vs
  `446-487`; `2413-2418`). Fix: atomic claim `UPDATE … SET gtt_order_id='PLACING' WHERE gtt_order_id IS NULL`.

### P2
- Paper slippage applied twice (`paper_trading.py:953-957` + `schema.py:1660-1670`).
- Live P&L uses quoted LTP, not `order_history` average fill (`schema.py:2113-2128`).
- Gemini ledger double push; paper monitor runs twice per scan (`pipeline.py:1411` + `paper_trading.py:1300`);
  dead-trade timer counts calendar hours; Friday sweep prices default expiry.

---

## C. Multi-leg + LLM layer

**Autopsy 23-Oct explained:** the BANKNIFTY strangle stop (−₹96,772 MTM, 2.9× the ₹33,446 cap)
was *correct* — gap open, first scan 10:55. The booked **+₹12,052 is fabricated**: the 54500 PE
was booked out at the *previous day's* snapshot (227.4) instead of 472.7 → overstated ≈₹110k.
92 legs across 38 books are booked >25% off the price that drove the close → win-rate stats
feeding `emp_boost_*` and ML are inflated.

### P0
- **C1 ✔ Multi-leg exits priced from stale DB snapshot** (`multileg_paper_trading.py:1693-1706`,
  no freshness check; `is_valid_option_premium` floor of 15% intrinsic let 227.4 pass for a put
  265pt ITM). Fix: prefer `leg["current_premium"]` (same chain as the decision), else reuse
  `get_option_premium(option_rows=...)` which already enforces 15-min staleness.
- **C2 AI adjustments can add risk unchecked** (`multileg_paper_trading.py:657-718`): leg match
  ignores side (can close long wing, open a SELL → naked); no "roll further OTM" rule (trade 266
  rolled short put *up* 54000→54500 in a falling market, gap hit exactly that strike);
  `new_net_premium` never passed → stop stays at entry values; new leg loses expiry/book_id and
  Greeks. Fix: same side+type, shorts must move OTM and stay inside the wing, recompute risk profile.
- **C3 ✔ Delta-fallback sign wrong for short calls** (`multileg_pnl.py:116`,
  `multileg_paper_trading.py:1718`, `multileg_llm_prompt.py:765`): stored deltas are position
  deltas (SELL CE = −0.27) → short call premium *falls* in a rally, stop suppressed during data
  outage. Fix: `abs(delta) if CE else -abs(delta)` at all 3 sites.
- **C4 ✔ Live exits/rollback close hedges first** (`schema.py:3177` `ORDER BY strike`;
  `_rollback_placed_legs` walks BUY-first). Fix: SELL legs first on exit; `reversed(placed_legs)`.
- **C5 Partial-fill-then-cancel not decremented** (`multileg_live_trading.py:1096-1104`) → next
  cycle overfills into reverse. Fix: decrement by `_filled_qty`.

### P1
- **C6 Live uses LLM's `max_loss`/breakevens for the stop** (`multileg_live_trading.py:2155-2193`).
  Fix: use `risk_profile` like paper (`paper:1485`).
- **C7 Live P&L/GTT from planned premium, not fill** (`:1897, 1969, 2005, 2057`).
- **C8 ✔ Strangle stop is effectively 30% of credit** (`multileg_paper_trading.py:98-101`:
  `margin_req*0.025` with margin = 12×premium); prompt tells LLM "150% credit". Fix: one explicit
  rupee stop, and put that number into the prompt.
- **C9 Margin premium-based** (`multileg_strategy.py:472`): cheap OTM strangles pass ₹6L cap
  that really need ~₹25L; long legs *add* margin. Shared root with A16 — one notional-based
  helper used by both.
- **C10 No cross-book risk cap** — IC (20 lots) and strangle (15 lots) both short BANKNIFTY
  54500 PE on 23-Oct. Fix: cap Σ net-delta exposure across open books per symbol.
- **C11 LLM self-grades confidence for neutral structures** (prompt `:635` "≥75% independent of
  engine"); a 98% bearish engine doesn't block an IC. Fix: require neutral engine verdict or
  blend engine conviction.
- **C12 CUSTOM structures: no direction check, `max_loss = points × price × 0.1`**
  (`multileg_strategy.py:403`). Fix: block CUSTOM from execution.
- **C13 Proposal validator fails open + margin checked for 1 lot** (`paper:1140`, `live:1575`).
- **C14 ✔ Credit spreads all rejected since 22-Oct** (`multileg_strategy.py:650-652`):
  breakevens can be `None` → TypeError → score 0. Fix: `.get(...) or 0`.
- **C15 `time_decay_exit_dte` ignored for NIFTY/BANKNIFTY/SENSEX** (`paper:413`, `live:550`);
  BANKNIFTY is monthly-only but treated as weekly.
- **C16 Greeks never refreshed after entry** — exit LLM reasons on entry deltas (autopsy's
  "delta 0.35" was really ~0.6).
- **C17 Per-strategy `ai_mode` ignored** (`decision_pipeline.py:251, 537` read global "full";
  "boost_only" not handled); full-mode promotion doesn't check AI direction == trade direction.
  Fix: `strategy_registry.get_ai_mode(engine)` + direction match. (Pairs with A3.)
- **C18 LLM flips direction via instrument** (`llm_enrichment.py:4232`, `paper_plan.py:199-209`).
  Same fix as A19: derive bias from (side, option_type) *after* override.
- **C19 MTM expiry guard never fires** (`multileg_pnl.py:57-60`; chain rows have no `expiry`).
- **C20 One LLM CLOSE can exit at 28% of credit** (`paper:581-584`); paper never clears the
  pending-close flag on HOLD. Fix: co-sign only ≥ actual target; clear flag on non-CLOSE.
- **C21 Liquidity gate ≈ `ltp>0`** (`llm_enrichment.py:5090`; `ask` is 0 in every snapshot).
- **C22 Live exit poll feeds AI fake "Neutral 50%"** (`live_trading.py:3154`). Fix: skip AI
  advice on exit-only polls or pass last real intel.

### P2
- Adjustment fills at LTP with no slippage; failed adjustment still consumes a slot;
  `close_book` treats P&L 0.0 as missing (`schema.py:2723`); LLM `profit_target_pct` unclamped;
  neutral structures get one-sided invalidation.

---

## Decisions (user, 2026-10-26)
- CORE is a **premium-selling** strategy: keep `VERDICT_ACTION_MAP`; fix A5 regime/momentum
  scoring to (direction, SELL) semantics and correct CLAUDE.md/AGENTS.md "BUY CE/PE" text.
- Scope: **all phases** (0→5), executed in order, each phase committed separately with its
  targeted tests. Phase 4 completion is the gate for any `live_broker_disabled=false`.

## Recommended fix order

Guiding rule (from memory): deterministic Python wins over LLM; every fix lands with a
targeted test file, not full pytest.

**Phase 0 — before next commit / before 1 Nov (small, urgent)**
1. B1: narrow the uncommitted Gemini FUT net to MCX, add NIFTY test; B7: guard
   `recover_stale_exit_pending` with order-status check.
2. A18 + B19: DST-aware EIA time & MCX close (one helper in `time_guards.py`, 10:30/23:55 ET-derived).
3. C14: `or 0` on breakevens (credit spreads currently 100% rejected).

**Phase 1 — P&L truth (paper stats & ML depend on it)**
4. C1 fresh exit pricing; B13 SL clamp; double slippage; C3 delta sign. Then a one-off script
   to flag (not rewrite) historical books booked >25% off decision price, and exclude them from
   `emp_boost_*` / ML training until re-verified.

**Phase 2 — Entry correctness**
5. A1 FUT side; A2 PE buildup sign; A3+C17 AI veto/mode; A6–A9 OI matrix; A11 neutral floor;
   A13/A12 NG direction & live gate; A14 fail-closed; A15/A16+C9 sizing & notional margin helper.

**Phase 3 — Multi-leg risk**
6. C2 adjustment guards; C8 explicit stop + prompt; C10 cross-book cap; C11/C12 neutral-only &
   block CUSTOM; C13 fail-closed; C15/C16/C20.

**Phase 4 — Live-readiness gate (must complete before `live_broker_disabled=false`)**
7. B2–B6, B8–B10, B14, B15, B18, B21, B22, C4–C7, C22. Re-enable sync (B3) only after B9.

**Phase 5 — Signal quality (P1/P2 tuning)**
8. A4, A5, A10, A17, remaining P2s. Measure each against paper autopsies before/after.

## Verification
- Per fix: targeted pytest (e.g. `pytest tests/test_exit_lifecycle_fixes.py tests/test_multileg_validator.py -v`)
  with a regression case reproducing the finding (NIFTY Gemini basis, PE Long Buildup, SELL CE
  delta fallback, `None` breakeven, trade-266 stale snapshot exit).
- Phase 1: rerun `autopsy_writer` for 23-Oct; strangle should book ≈ −₹96k, not +₹12k.
- Phase 0 DST: unit test `time_guards` for dates 30-Oct and 2-Nov-2026.
- Phase 4: a shadow-mode day with `manage_direct_kite_positions` simulated against a mocked Kite
  (pending order, partial fill, GTT-fired) before any broker enable.
