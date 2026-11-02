# Decision matrix → code (regime table, delta strikes, ATR stops, rule exits, AI veto-only)

## Context
Prior review plan (phases 0-5) is committed (`24f5b39e`). Next step the user asked for: turn the
Indian-market decision matrix into deterministic code so outcomes stop depending on which LLM
answers, and shrink the AI to veto/tighten-only. NYMEX is used for direction confirmation only;
levels, strikes, OI and stops stay in MCX terms. All thresholds are starting values to calibrate
in shadow mode — none are backtested.

### Delta profile (user-specified; default = Aggressive; selectable in config)
| Structure / leg | Market | Defensive | **Aggressive (default)** | Danger ceiling (hard reject) |
|---|---|---|---|---|
| Credit spread short | Indices | 0.25–0.30 | **0.30–0.35** | > 0.40 |
| Credit spread long (hedge) | Indices | 0.08–0.12 | **0.14–0.18** | > 0.25 |
| Iron condor short | Indices | 0.18–0.22 | **0.24–0.28** | > 0.32 |
| Iron condor long wing | Indices | 0.05–0.08 | **0.08–0.12** | > 0.15 |
| Short strangle short (added in review) | Indices | 0.15–0.20 | **0.15–0.20** (not specified — assumed unchanged) | > 0.25 |
| Credit spread short | MCX NG/Crude | 0.20–0.25 | **0.24–0.28** | > 0.32 |
| Credit spread long | MCX | 0.08–0.12 | **0.10–0.14** | > 0.20 |
| Iron condor short | MCX | 0.12–0.18 | **0.18–0.22** | > 0.25 |

Knock-on rules for the aggressive profile (all configurable):
1. **Tested-side stop widened:** short-leg Δ ≥ 0.48–0.50 (ATM breach) OR a 15-min candle closes beyond the short strike.
   (Old Δ0.35 trigger would whipsaw on entries at Δ0.30–0.35.)
   **Defined-risk structures only.** A naked strangle has no wing to cap the loss at ATM, so it keeps
   tested-side Δ ≥ 0.35 plus the 1.0× credit book stop (separate config key).
2. **Narrow wings:** NIFTY spreads fixed 150–200 pts. Net credit must be ≥ 30% of wing width; < 25% → reject.
   The 25–30% band is not specified — assumed half size (configurable).
3. **Faster harvest:** target 40% of max credit (60% on expiry day), down from 50%.

### Regime table (cell = ordered allowed-set; first entry is the default; AI may pick any member, never outside it)
Vol buckets: **Low** IVP < 25 (≈ VIX < 12) · **Mid** IVP 25–70 (≈ VIX 12–18) · **High** IVP > 70 (≈ VIX > 18).
Bucketing uses ATM IVP from stored chain IV. A VIX-based option (`use_vix_for_indices`, default off) is only
added if the user wants it, since the repo has no India VIX feed today.

| Direction | Low vol | Mid vol | High vol |
|---|---|---|---|
| **Neutral** (user-revised) | `[SHORT_STRANGLE]` | `[IRON_CONDOR, SHORT_STRANGLE]` | `[IRON_CONDOR]` |
| Bullish | NO_TRADE | `[BULL_PUT_SPREAD]` | `[BULL_PUT_SPREAD, JADE_LIZARD]` (JL indices only) |
| Bearish | NO_TRADE | `[BEAR_CALL_SPREAD]` | `[BEAR_CALL_SPREAD]` |

- Overrides still filter the cell afterwards: **MCX never gets a naked structure**, so `SHORT_STRANGLE` → `IRON_CONDOR`
  there; event window → defined-risk only at half size; 0-DTE / monthly-DTE windows; liquidity fail → NO_TRADE.
- Low-vol neutral = a naked strangle on thin premium. Added a `min_credit` guard per structure in config
  (default conservative, user-editable) so it only fires when the credit is worth the tail risk.
- Bullish/Bearish Low-vol rows stay NO_TRADE (not revised); one-line edit in the JSON if wanted.

Reuse (found via Graft, don't rewrite):
- `src/utils/greeks_calculator.py` `calculate_greeks()` — BSM (NSE) / Black-76 (MCX), solves IV if missing.
- `src/engine/multileg_strategy.py` `get_candidate_strikes()` (delta sweet-spot scoring), `validate_legs()`, `compute_book_risk_profile()`.
- `src/engine/trend_analysis.py` `_calc_iv_percentile_penalty()` — IV-history query to generalise into an ATM IVP helper.
- `src/engine/regime_detector.py` `regime_score_for_trade()` — already sell-aware.
- `src/engine/strategy_registry.py` `get_ai_mode()`; `src/engine/ng_momentum_strategy.py` `check_nymex_1h_trend()` (NG=F only).
- `src/engine/trade_plan.py` — all SL/target changes must go here (hard constraint).
- `src/engine/time_guards.py` — DST-aware EIA/close helper from Phase 0.

Not found: India VIX feed. Skip it: ATM IV percentile from `option_chain_snapshots` covers both
indices and MCX with no new data source.

## Phase 1 — Config + pure functions, shadow only (no behaviour change)
- **User-editable config (hard requirement): NO threshold lives in code.** NEW `config/decision_matrix.json`
  (the file the user edits) holds every decision parameter, grouped and commented via `_doc` keys:
  - `vol_buckets` (IVP cut-offs, lookback days) · `regime_table` (direction × vol → structure)
  - `overrides` (each with `enabled` + params: event window hours, 0-DTE no-entry time, MCX no-naked,
    liquidity min volume/OI, monthly-DTE entry window, blocked structures)
  - `delta_profile` (`aggressive` default | `defensive`) → `delta_bands` per profile (structure × leg × asset class: entry band + hard `max_delta` danger ceiling) · `wings` (per symbol, fixed points) · `credit_rules` (min net credit ÷ wing width: ≥30% full size, 25–30% half size, <25% reject) · `strike_rules` (beyond-OI-wall on/off)
  - `stops_targets` (ATR multipliers, breakeven/trail, credit-multiple stops, target %, capital cap %, gap-stress %)
  - `exits` (ordered priority list — reorderable/disable-able, tested-side delta, event-close minutes,
    MCX days-before-expiry, session-close buffer, dead-trade %) · `ai_authority` (per-action allow flags, close bands)
  - `nymex` (mode, symbol map, EMA span, cache secs) · `mode` (`off|shadow|enforce`) · per-symbol overrides block
  NEW `config/decision_matrix_loader.py`: Python `DEFAULTS` (= the matrix agreed in chat) deep-merged with the
  JSON; range/enum validation (e.g. delta ∈ (0,1), unknown structure rejected); mtime-based hot reload (no
  restart); **fail-closed**: invalid/missing file → log + use DEFAULTS (or `mode=off` if defaults can't load);
  atomic write helper reused from `config/runtime_config.py`. Config version stamped into each shadow/audit row.
- NEW `src/engine/decision_matrix.py` (pure, no I/O except one IV query):
  `atm_iv_percentile(symbol)` (extract shared query from `trend_analysis`), `vol_bucket()`,
  `select_structure(direction, vol, dte, event, symbol) → structure|None` (downgrade-only ladder
  strangle→IC→spread→NO_TRADE; each cell is an ordered allowed-set from `regime_table`, default = first, overrides filter it), `delta_band(structure, symbol)`, `nymex_trend(symbol)`
  (5-min cache; NG=F/CL=F/GC=F/SI=F; returns NEUTRAL on any failure — never blocks).
- Shadow hook in `pipeline.py` after CORE decision: append a `matrix_shadow` step (recommended
  structure, vol bucket, nymex vs OI agreement, what the LLM/CORE actually chose) to the existing
  decision-audit trail (`decision_audit.py` stores `trail_json`, no schema change).
- Mode is `mode` in `decision_matrix.json` (`off | shadow | enforce`, default `shadow`); NYMEX has its own mode.
- Optional (only if asked): dashboard Settings tab to edit the JSON, reusing the Strategies-tab save pattern. [COMPLETED - Subtab implemented in `settings.html` and backed by `/api/decision_matrix` GET/POST in `dashboard_server.py`]

## Phase 2 — Enforce structure + strikes (flag-gated)
- `multileg_validator.validate_multileg_trade` / `validate_legs`: reject an LLM proposal whose
  structure sits above its regime cell, whose sold/hedge leg is ITM, outside the active profile's delta band, or beyond its `max_delta` danger ceiling, whose net credit is below `credit_rules`
  (delta from row, else `calculate_greeks`), or is a naked short on MCX / CUSTOM / straddle.
- `get_candidate_strikes`: read delta bands/wings from config; snap short strikes beyond the OI wall.
- **Prompt must follow config (critical).** `multileg_llm_prompt.py` hard-codes "Δ 0.15–0.30" / "Δ 0.15–0.25" (L504–516)
  and tells the LLM *"Do NOT use IRON_CONDOR on MCX… illiquid wing contracts"* with strangle/straddle preferred (L516).
  That contradicts the regime table (MCX = IC, never naked) and the aggressive bands, so every proposal would be
  rejected. Generate the strategy-priority and delta text from the active regime cell + `delta_bands`; offer the
  LLM only the allowed structures and pre-filtered liquid strikes. MCX IC is allowed only when all four legs pass the gate.
- Rolls (`multileg_paper_trading` adjust path): short leg may only move further OTM inside its wing.

## Phase 3 — Stops/targets from MCX ATR (via `trade_plan.py` only)
- FUT: stop 1.5×ATR14(1H), target 2.0×ATR14(1H), breakeven at +1×ATR, trail 1×ATR; store
  `initial_sl_underlying` for R-multiples (verify B20 landed; add only if missing).
- Option books: stop = 1.0× credit (1.5× monthly) OR the tested-side trigger (Phase 4); target 40%
  of max credit (60% on expiry day); hard cap 1.5% capital, size so a 2×daily-ATR gap ≤ 2%.
- All constants read from the loader (`config/decision_matrix.json`); check C8 (explicit strangle stop) already matches.

## Phase 4 — Exit priority + AI authority
- Single ordered exit function (hard risk → tested side (short-leg Δ ≥ 0.48–0.50, or a 15-min candle closes beyond the short strike) → event close 30 min prior
  → expiry/time (idx 15:15; MCX options ≥1 trading day pre-expiry; DST-aware session close) →
  regime flip conf≥75 (+NYMEX agree on MCX) → profit target → dead trade). SELL legs closed first.
  Exits priced from fresh chain only.
- AI (`llm_enrichment._enforce_engine_alignment`, `multileg_*`, `decision_pipeline`): veto/size-cut/
  tighten-only. Final stop = tighter of rule stop and AI stop; AI close allowed only if profit ≥30%
  of credit or loss ≥0.5× stop, else logged as advice. Per-strategy mode via `get_ai_mode()`.
- Log each AI verdict with outcome (existing `decision_audit` row) for later veto-value scoring.

## Phase 5 — NYMEX gate (after ≥2 weeks of shadow data)
- `nymex_gate_mode: off|shadow|enforce`. When OI verdict and NYMEX 1H trend disagree on an MCX
  symbol → treat as neutral (regime cell → defined-risk/NO_TRADE). Enable only if the vetoed
  trades' shadow outcomes are worse than the passed ones.

## MCX liquidity gate — two-tier (user-specified; replaces static volume ≥ 500 / OI ≥ 2000)
`is_mcx_strike_liquid(strike_data, now_ist, cfg) → (ok, reason)` in `src/engine/decision_matrix.py`, params under
`liquidity.mcx` in `decision_matrix.json`. Order of checks, first failure returns its reason string:
1. **OI floor** — `min_oi` default 1500 (user suggests 1500–2000; note CLAUDE.md currently says 2000, so 1500 loosens it).
2. **Quote truth** — bid and ask must be > 0; spread passes if `abs_spread ≤ max_abs_spread` (0.30) **or**
   `spread_pct ≤ max_spread_pct` (2.0, computed on ask). Fails only when both are exceeded.
3. **Time-tiered volume** — IST hour tiers `[{before_hour: 12, min_vol: 50}, {before_hour: 17, min_vol: 150}, {default: 400}]`.
Config also exposes: `require_quote` (see risk 1 below), `spread_mode: either|both`, `hard_max_spread_pct` (default off),
per-symbol overrides (NG/CRUDE calibrated; GOLD/SILVER/COPPER inherit until tuned).
- **Single definition:** `paper_plan.mcx_option_liquidity_ok` (ATM-only; callers: `paper_plan.py`, `paper_trading.py`
  run_timeframe_strategy, `live_trading.py` run_live_timeframe_strategy, `decision_pipeline.step_risk`) and
  `data_validator._classify_leg_liquidity` (MCX path) both delegate to this gate; `get_candidate_strikes` and
  `_format_roll_candidates` filter MCX candidates through it. It runs per sold/hedge leg, not just ATM.
- **Spec inconsistency (user to confirm):** the pasted code passes on ₹0.30 *or* 2.0%, while the prose says ≤1.5% or
  ₹0.25. Default follows the code (0.30 / 2.0%); both are config values. A ₹0.30 allowance is loose on a ₹1–2
  premium (15–30% wide), so `hard_max_spread_pct` is the suggested backstop.
- Failure reason is carried into the skip alert and decision-audit row (e.g. `LIQ: Wide spread (0.45 pts / 3.1%)`).

## Telegram templates (new — required by this change)
Today there is no single trade-message formatter: digests come from `src/alerts/digest.py`
(`build_digest_wrapper` → `build_llm_consolidated_digest`, trade lines via `_bot_action_block`/`_render`),
while entry/exit alerts are ad-hoc f-strings at ~15 `send_text()` sites (`live_trading.py`, `multileg_live_trading.py`
L987/L1221/L2115/L2254, NG strategies, `pipeline.py`). Multi-leg *paper* trades only surface through the digest.
- NEW `src/alerts/matrix_alerts.py` (pure string builders, reuse `digest._esc` / `_fit_telegram`, the
  dispatcher's Markdown-v1 sanitiser and the ≤4096 limit): `matrix_digest_line()`, `format_entry()`,
  `format_exit()`, `format_skip()`, `format_config_event()`. No logic or thresholds inside — they only render
  the dict the decision engine already produced (structure, legs, bucket, reason codes, cfg version).
- **Digest (always on, ≤3 lines; dropped first when over budget):**
  `🧭 Matrix v3 · NEUTRAL · IV MID (IVP 48) → IRON CONDOR · AGG` + MCX only: `NYMEX 1H BULL · OI NEUTRAL · agree ✓`.
  Shadow mode prefixes `👁 SHADOW` and shows `would: …` vs `actual: …`. Skips show the winning override:
  `⛔ No trade: EVENT window (RBI 10:00) → defined-risk ½ size`.
- **Entry alert:** structure · expiry · cfg version · profile; legs with strike/Δ/premium; net credit and
  credit÷wing % (net of costs); max loss; stop (tested-side trigger + book stop); target (40%/60% expiry);
  capital-cap % used; lots and broker margin; AI line (`✓ agrees` / `✂ size ½` / `✋ veto` + reason).
- **Exit alert:** reason code taken from the configured exit list (`HARD_RISK`, `TESTED_SIDE`, `EVENT`, `EXPIRY`,
  `REGIME_FLIP`, `TARGET`, `DEAD_TRADE`, `AI_TIGHTEN`), P&L ₹ and % of credit, hold time, trigger detail
  (e.g. `CE 25000 Δ0.49`), exit order note (SELL legs first).
- **Config event alerts:** hot reload success `⚙️ Matrix cfg v4 loaded · changed: delta_profile → aggressive`;
  invalid file `⚠️ Matrix cfg INVALID (key) → defaults active`. The user edits this file by hand, so confirmation matters.
- **Rollout:** shadow = digest line only (no extra alerts, avoids noise and being mistaken for fills); entry/exit
  alerts start with `enforce`. Wire only matrix-driven paths (digest, multi-leg paper/live entry+exit, CORE/TIMEFRAME
  exits that now use the exit list). NG EIA/PARITY/MOMENTUM messages stay as they are (out of scope).
- **Config (same JSON):** `telegram: {verbosity: compact|full, events: {digest_line, entry, exit, skip, config}: bool,
  skip_cooldown_min}`. Skip alerts fire on state change per symbol+reason, not every scan.
- Hard constraint kept: Telegram text stays short and trader-readable (`compact` = 4–5 lines for entry/exit).
- Tests: NEW `tests/test_matrix_alerts.py` — each template with full, partial and `None` fields; ≤4096 chars;
  Markdown-safe with `_`, `*`, `[` in reason text; compact vs full; shadow prefix; digest line dropped under budget.

## Final-review additions (all values in `decision_matrix.json`)
- **Freeze params at entry (P1, critical).** Store `config_version` + resolved stop/target/exit params on the
  trade/book row (existing JSON/notes column if present, else one migration). Editing the JSON changes only
  *new* trades. Open trades can only be tightened (rule or AI), never loosened by a config edit.
- **Stops per structure (P3).** Credit spreads: stop at 50% of max loss (width − credit) or the tested-side
  trigger. IC/strangle: credit-multiple stop. One config key each, not a shared 1.0× for all.
- **Credit rule net of costs (P2).** The 30%/25% credit÷width check uses credit after brokerage, STT and
  per-leg slippage (reuse paper transaction-cost helper), since a 4-leg IC on a 150-pt wing loses a lot to fees.
- **MCX liquidity gate (P2, user-specified; see section above).** Applies to every leg incl. hedges and roll candidates (narrow MCX wings sit on thin
  strikes). Replaces the static volume ≥ 500 / OI ≥ 2000 ATM-only check.
- **Sizing from max loss (P3).** lots = floor(min(capital cap, gap-stress cap) ÷ max-loss-per-lot) through
  `capital_allocator`. 0 lots → NO_TRADE (A15). Margin from the broker helper, never the ₹ estimates.
- **Fail-closed inputs (P1).** IVP with < N days of history, or a delta that can't be computed → bucket `High`
  (defined-risk only) and skip the strike rather than guess.
- **Event calendar in config (P1).** `events: [{name, datetime_ist, symbols}]` for RBI/FOMC/Budget/OPEC. EIA
  stays computed (DST-aware). The repo has no generic macro-event feed for the overrides to read otherwise.
- **Churn guards (P2).** `cooldown_minutes` after a stop on the same symbol+side, `max_entries_per_day`, and
  `max_open_books_per_symbol`, all honouring the cross-book net-delta cap (C10).
- **Replay before shadow (P1b, recommended).** A script replays the matrix over stored `option_chain_snapshots`
  (last 4–8 weeks) and prints per-cell / per-profile win rate, average P&L and max drawdown. That gives
  calibration data in hours instead of two weeks. It has caveats: snapshot gaps, no fills, and stale rows are
  skipped (15-min rule).
- **Visibility (P1).** Digest line + dashboard row via `src/alerts/matrix_alerts.py` (see Telegram templates above).
- **Kill switch.** `mode: off` reverts to today's path on the next scan (hot reload). Risk-engine breakers are untouched.

## Order & scope notes
- Ship Phase 1 alone first; it changes nothing live. Phases 2-4 only flip with `enforce`.
- Verify before coding: exchange expiry weekdays, lot sizes, expiry-day extra margin — keep them
  in config, not code.
- Out of scope: India VIX feed, new data sources, ML changes, TFSS runner.
- Open risks to check before `enforce`:
  - **1. Bid/ask availability (blocks the MCX gate).** The review found `ask` = 0 in stored snapshots (C21). The gate
    hard-fails on zero quotes, so if the live MCX feed doesn't supply them, *every* MCX strike is rejected. First task:
    query `option_chain_snapshots` for MCX rows and report the share with bid>0 and ask>0 per source/symbol. If low, set
    `require_quote: false` (OI + volume tiers only, logged as `quote-less`) until a quoting source is wired.
  - **2. Volume semantics.** Time-tiered thresholds assume `volume` is cumulative day volume; confirm the feed doesn't
    return last-trade size. **3. OI units** (lots vs contracts) must match the 1500 floor.
  - **15-min candle source:** the Shoonya chart path only requests 1H bars and aggregates upward, so 15m
    closes may not exist there. Verify a 15m source; else fall back to two consecutive scan spots beyond the strike.
  - **Stop at ATM + aggressive entry on MCX:** overnight NYMEX gaps can jump past the stop, so keep the 1.5% capital
    cap and the 2×ATR gap-stress sizing active for the MCX symbols.
  - The user's margin/ROC figures (₹65k → ₹25–32k, 0.4% → 1.8–2.5%) are not verified; margin must come from the broker
    margin helper (A16/C9), not these estimates.

## Verification (targeted only, never full pytest)
- NEW `tests/test_decision_matrix_config.py`: edit a value in a temp JSON → behaviour changes without code
  (e.g. change a delta band, reorder an exit, disable an override); invalid value / missing file → DEFAULTS +
  warning; hot reload on mtime change; per-symbol override wins over global; **editing the JSON does not loosen an
  open trade's frozen stop**; insufficient IVP history → `High` bucket; naked strangle uses Δ0.35 not Δ0.48.
- NEW `tests/test_decision_matrix.py`: one parametrised case per matrix row (both delta profiles) + each danger-ceiling reject + the 30%/25% credit rule + each override
  (event, 0-DTE after 13:00, MCX naked short, liquidity fail, monthly DTE window, CUSTOM, straddle).
- Extend `tests/test_multileg_validator.py` (reject ITM short, above-cell structure),
  `tests/test_trade_plan.py` (ATR stop/target/breakeven), `tests/test_exit_lifecycle_fixes.py`
  (exit order, SELL-first, AI tighten-only).
- Shadow run: `matrix_shadow` rows over ≥2 weeks; compare recommended vs executed outcomes and
  NYMEX-disagree vs agree win rate before any `enforce`.
- Liquidity: parametrised tests for `is_mcx_strike_liquid` — OI 1499/1500, zero bid or ask, spread pass/fail on each
  branch (abs-only, pct-only, both), volume at each tier boundary (11:59, 12:00, 16:59, 17:00 IST), `require_quote=false`
  fallback, per-symbol override, config edit changes the result. Fixed injected IST times, no wall clock.
- Telegram: `tests/test_matrix_alerts.py` (above) plus one real-send smoke to a test chat in shadow before `enforce`.
- Command: `pytest tests/test_matrix_alerts.py tests/test_decision_matrix_config.py tests/test_decision_matrix.py tests/test_multileg_validator.py tests/test_trade_plan.py tests/test_exit_lifecycle_fixes.py -v`
