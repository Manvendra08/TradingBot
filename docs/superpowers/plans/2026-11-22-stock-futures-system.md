# NSE Stock Futures System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A swing-trading system for NSE stock futures (paper then live via Kite) with price-level SL/targets, entry and exit matrices, lot-based sizing and a dedicated `/futures` page.

**Architecture:** A new `src/stockfut/` package holds universe, features, levels, setups, exits, portfolio risk, executors and jobs. Shared logic goes where house rules say: SL/target in `trade_plan.py`, lots in `capital_allocator.py`, every live order through `broker_gate.py`. Storage is separate (`stockfut_*` tables). One registration call in `job_runner.py`.

**Tech Stack:** Python, SQLite (WAL), FastAPI, `kiteconnect`, pytest.

**Spec:** `docs/strategies/stock-futures-system-plan.md` (v2). Executors read both.

## Global Constraints

- Quantity = whole lots from the **instrument master's current lot size**; **minimum 2 lots**; `lots = max(2, floor(budget ÷ (SL_distance × lot_size)))`; skip with `MIN_LOTS_BREACH` if `risk(lots) > 1.25 × budget`. Budget default 0.75% of capital.
- GTT split: T1 leg `floor(n/2)` lots, T2 leg `n − floor(n/2)` lots; every quantity a multiple of lot size.
- Timeframes: daily bars = trend/bias/levels; 3H closes = entry only; 1H closes = exit only.
- All SL/target math in `trade_plan.py`. No ad-hoc stop math elsewhere.
- Every live order goes through `authorize_broker_execution`. No MARKET orders for stock futures. NRML only.
- Fail closed: missing data for a stock means no trade for that stock, logged.
- No hedging. No rolling: exit 5 sessions before expiry. Near month only if ≥ 12 sessions left.
- Hold 2–10 sessions; T1 distance ≥ 1.6 × SL distance; SL distance 0.75–2.0 × daily ATR.
- No entries after 14:45 IST. 12:15 3H triggers execute live; 15:15 3H triggers queue as NEXT_OPEN for a 09:45 re-check. Daily exit rows run on the 14:15 1H close.
- Levels from unadjusted cash-equity daily bars, projected to the future by live basis.
- Tests: targeted files only (`pytest tests/test_stockfut_<area>.py -v`), never the full suite. Windows: no heredocs.
- Config lives in `config/stockfut.json`; reads fail closed to code defaults.

## Review Focus

- Odd lot counts (3, 5): GTT legs must be non-empty and lot-multiple.
- A 2-lot floor on a wide-stop stock must skip, not trade over budget.
- Kite returns `[]`/empty quote for a stock: no trade, not a crash or zero-price entry.
- Order PENDING then filled late: protection must cover the actual filled quantity.
- GTT placement fails after a fill: position is flattened and entries halt.
- Bot restarts with broker positions the DB doesn't know (and the reverse).
- Held stock enters the ban list: exits allowed, no entries, no rolls.
- Corporate action or lot-size change mid-trade.

---

## File Structure

| File | Responsibility |
|---|---|
| `config/stockfut.json`, `config/stockfut_loader.py` | Thresholds, matrices, whitelist; hot reload, fail-closed defaults |
| `src/models/schema.py` (next free migration after M113) | `stockfut_trades`, `stockfut_watchlist`, `stockfut_decisions`, `stockfut_order_intents` |
| `src/fetchers/kite_fetcher.py` | `get_futures_quotes`, contract lookup with per-contract lot size |
| `src/fetchers/nse_eod_fetcher.py` | Ban list, MWPL, results calendar |
| `src/stockfut/universe.py` | Daily tradable list and contract choice |
| `src/stockfut/features.py` | Trend, RS, OI flow, ATR, regime |
| `src/stockfut/levels.py` | Zones, strength, basis shift |
| `src/stockfut/setups.py` | Bias, entry matrix, score, trigger, watchlist builder |
| `src/engine/trade_plan.py`, `capital_allocator.py` | `calculate_future_sl_target`, `calculate_future_lots` |
| `src/stockfut/portfolio_risk.py` | Limits, gap stress, loss breakers, halt flag |
| `src/stockfut/exits.py` | Exit matrix evaluator |
| `src/stockfut/replay.py` | Backtest harness |
| `src/stockfut/executor.py` | Paper and live executors, `validate_order`, GTT handling |
| `src/stockfut/reconcile.py` | Startup and periodic broker reconciliation |
| `src/stockfut/jobs.py` | Scheduled jobs |
| `src/dashboard/futures.html`, `dashboard_server.py` | Page, route, API; nav link in 8 templates |

---

### Task 1: Config and storage

**Files:** Create `config/stockfut.json`, `config/stockfut_loader.py`, `tests/test_stockfut_storage.py`; modify `src/models/schema.py`.

**Interfaces:**
- Produces: `load_stockfut_config() -> dict` (fail-closed defaults when file is missing or invalid); `insert_stockfut_trade(...) -> int`, `get_open_stockfut_trades() -> list[dict]`, `log_stockfut_decision(kind: str, symbol: str, payload: dict) -> None`; tables listed above. `stockfut_trades` has `lots`, `lot_size`, `t1_lots`, `t2_lots`, `sl`, `t1`, `t2`, `gtt_ids` (JSON list), `remaining_qty`, `snapshot_id`, `mode`.

- [ ] **Step 1: Tests** `test_loader_fails_closed_on_bad_json` (returns defaults with `stockfut_live_enabled == False`); `test_trade_roundtrip_persists_split_lots` (lots=5 → t1_lots=2, t2_lots=3); `test_decision_log_records_skip_reason`.
- [ ] **Step 2:** Run `pytest tests/test_stockfut_storage.py -v`; expect FAIL.
- [ ] **Step 3:** Implement loader and migration following the M101–M113 pattern in `schema.py`.
- [ ] **Step 4:** Run tests; expect PASS. **Step 5:** commit `feat(stockfut): config and storage`.

### Task 2: Kite futures data layer

**Files:** Modify `src/fetchers/kite_fetcher.py`; test `tests/test_stockfut_kite_data.py` (mock `kite`).

**Interfaces:**
- Produces: `KiteDataClient.get_futures_quotes(contracts: list[str]) -> dict[str, dict]` with `price, oi, volume, bid, ask, bid_qty, ask_qty` (empty dict for a contract that errors, never zeros); `KiteDataClient.list_stock_futures() -> list[dict]` with `symbol, tradingsymbol, token, expiry, lot_size`.

- [ ] **Step 1: Tests** `test_quote_includes_oi_and_depth`; `test_missing_quote_is_absent_not_zero`; `test_lot_size_read_per_contract` (near 500, next 550 → each returned as given).
- [ ] **Steps 2–5:** fail, implement with `kite.quote(["NFO:<tradingsymbol>"])`, pass, commit.

### Task 3: NSE EOD fetcher

**Files:** Create `src/fetchers/nse_eod_fetcher.py`, `tests/test_stockfut_nse_eod.py` (fixture files, no network).

**Interfaces:** Produces `fetch_ban_list() -> set[str]`, `fetch_mwpl() -> dict[str, float]`, `fetch_events(days: int = 5) -> dict[str, list[str]]`; each raises `FetchError` on failure (callers fail closed).

- [ ] **Step 1: Tests** `test_parses_ban_list_fixture`; `test_mwpl_percent_as_float`; `test_fetch_failure_raises`.
- [ ] **Steps 2–5:** fail, implement with `requests` and a short retry, pass, commit.

### Task 4: Universe

**Files:** Create `src/stockfut/universe.py`, `tests/test_stockfut_universe.py`.

**Interfaces:**
- Consumes: Tasks 2–3.
- Produces: `build_universe(asof: date, capital: float) -> list[Contract]` where `Contract` has `symbol, tradingsymbol, expiry, lot_size, sessions_to_expiry`; `exclusion_reason(symbol: str, ...) -> str | None`.

- [ ] **Step 1: Tests** `test_banned_and_event_names_excluded`; `test_near_month_used_only_with_12_sessions_left` (11 → next month, 12 → near); `test_unsizable_two_lots_at_2atr_excluded`; `test_fetch_error_excludes_everything_and_logs`.
- [ ] **Steps 2–5:** fail, implement, pass, commit.

### Task 5: Features

**Files:** Create `src/stockfut/features.py`, `tests/test_stockfut_features.py`.

**Interfaces:**
- Produces: `classify_oi_flow(price_pct: float, oi_pct: float, noise: float = 3.0) -> str` returning `LONG_BUILDUP | SHORT_BUILDUP | SHORT_COVERING | LONG_UNWINDING | NONE`; `compute_features(symbol: str, daily: list[dict], index_daily: list[dict], sector_daily: list[dict]) -> dict | None` (None when history is short or empty); `market_regime(...) -> str` (`RISK_ON | NEUTRAL | RISK_OFF`).

- [ ] **Step 1: Tests** `test_oi_flow_truth_table` (4 quadrants plus below-noise → NONE); `test_empty_history_returns_none`; `test_flow_uses_combined_near_next_oi`; `test_atr_is_daily`.
- [ ] **Steps 2–5:** fail, implement, pass, commit.

### Task 6: Level engine

**Files:** Create `src/stockfut/levels.py`, `tests/test_stockfut_levels.py`.

**Interfaces:** Produces `build_zones(daily: list[dict], atr: float) -> list[Zone]` (`low, high, kind, strength`); `shift_zones(zones: list[Zone], basis: float) -> list[Zone]`; `nearest_zone(zones, price, side: str, strong_only: bool = True) -> Zone | None`.

- [ ] **Step 1: Tests** `test_pivots_within_0_3_atr_merge`; `test_more_touches_means_higher_strength`; `test_basis_shift_moves_all_zones`; `test_no_zone_returns_none`; `test_zones_built_from_cash_bars_not_futures` (caller passes `NSE:` equity daily bars; futures bars with a rollover jump must not create a pivot).
- [ ] **Steps 2–5:** fail, implement (pivots, breakout/gap edges, EMA20/50), pass, commit.

### Task 7: Bias, entry matrix, watchlist

**Files:** Create `src/stockfut/setups.py`, `tests/test_stockfut_setups.py`; matrix cells in `config/stockfut.json`.

**Interfaces:**
- Consumes: Tasks 5–6, 8.
- Produces: `stock_bias(f: dict) -> int` (−4..+4) and `bias_label(score: int) -> str`; `matrix_permission(bias: str, regime: str) -> tuple[str, int] | None` (side, min score); `is_reversal_candle(side: str, bars_3h: list[dict], min_run: int = 3) -> bool` (prior `min_run` bars form a continuous run against `side` — lower closes and lower highs before a LONG, higher closes and higher highs before a SHORT — and the last bar closes above the previous bar's high for LONG / below its low for SHORT); `trigger_ok(side: str, price_pct: float, oi_pct: float, oi_flow_5d: str) -> bool` (called only after `is_reversal_candle` passes); `next_open_ok(row: WatchRow, open_price: float, atr: float) -> tuple[bool, str]` (cancel on gap > 0.5 ATR, beyond SL, or past half of T1); `build_watchlist(universe, features, zones, regime) -> list[WatchRow]` (each row frozen to a scan snapshot).

- [ ] **Step 1: Tests** one assertion per matrix cell (e.g. `matrix_permission("STRONG_BEAR", "RISK_ON") == ("SHORT", 80)`, `matrix_permission("BULL", "RISK_OFF") is None`); `test_neutral_bias_never_trades`; `test_reversal_long_requires_close_above_prev_high` (close inside prev range → False; wick above but close inside → False); `test_reversal_short_requires_close_below_prev_low`; `test_reversal_rejected_without_3_bar_run` (2-bar run → False; run broken by a higher close → False); `test_short_covering_trigger_allowed_when_5d_flow_long_buildup`; `test_short_covering_trigger_rejected_when_5d_flow_not_long_buildup`; `test_next_open_cancelled_on_gap_beyond_half_atr`; `test_no_entry_after_1445`; `test_watchlist_capped_at_20_by_score`.
- [ ] **Steps 2–5:** fail, implement, pass, commit.

### Task 8: Trade plan and lots

**Files:** Modify `src/engine/trade_plan.py`, `src/engine/capital_allocator.py`; test `tests/test_stockfut_plan.py`.

**Interfaces:**
- Produces: `calculate_future_sl_target(entry: float, side: str, zones: list, atr: float) -> dict | None` returning `sl, t1, t2, r_t1` or None when a geometry gate fails; `calculate_future_lots(sl_distance: float, lot_size: int, capital: float, budget_pct: float = 0.75) -> tuple[int, str | None]` returning `(lots, skip_reason)`; `split_lots(n: int) -> tuple[int, int]`.

- [ ] **Step 1: Tests** `test_split_lots_5_is_2_and_3`; `test_split_lots_2_is_1_and_1`; `test_min_two_lots_even_when_budget_gives_one`; `test_min_lots_breach_skips` (2 lots risk > 1.25 × budget → `(0, "MIN_LOTS_BREACH")`); `test_t1_under_1_6r_returns_none`; `test_sl_outside_atr_band_returns_none`; `test_opposing_zone_too_close_returns_none`; `test_short_is_mirror_of_long`.
- [ ] **Steps 2–5:** fail, implement, pass, commit.

### Task 9: Portfolio risk

**Files:** Create `src/stockfut/portfolio_risk.py`, `tests/test_stockfut_portfolio_risk.py`.

**Interfaces:** Produces `check_entry(candidate: dict, open_positions: list[dict], equity: float) -> tuple[bool, str]` covering max positions, sector cap, net exposure, gross cap, 5% gap stress ≤ 6% of capital, margin headroom; `daily_loss_exceeded(...)`, `weekly_loss_exceeded(...)`, `consecutive_losses(...)`; `halt(reason: str)` and `is_halted() -> bool` backed by the persisted flag.

- [ ] **Step 1: Tests** one per limit (boundary just under and just over); `test_gap_stress_blocks_candidate_that_breaches`; `test_daily_loss_counts_unrealised`; `test_halt_flag_survives_restart`.
- [ ] **Steps 2–5:** fail, implement, pass, commit.

### Task 10: Exit matrix

**Files:** Create `src/stockfut/exits.py`, `tests/test_stockfut_exits.py`.

**Interfaces:** Produces `evaluate_future_exit(trade: dict, market: dict, now: datetime) -> ExitDecision | None` with `rule, action, lots, new_sl`; rule names `HARD_STOP, GAP_THROUGH, THESIS_BREAK, EVENT, T1_HIT, T2_HIT, TRAIL, REGIME_TURN, TIME_STOP, EXPIRY`, evaluated in that order.

- [ ] **Step 1: Tests** one per rule; `test_priority_hard_stop_beats_time_stop`; `test_expiry_exit_5_sessions_before`; `test_banned_held_name_exits_only`; `test_t1_books_floor_half_and_moves_stop_to_trigger_bar_swing_low` (not breakeven); `test_short_mirrors_long`.
- [ ] **Steps 2–5:** fail, implement, pass, commit.

### Task 11: Replay harness

**Files:** Create `src/stockfut/replay.py`, `scripts/replay_stockfut.py`, `tests/test_stockfut_replay.py`.

**Interfaces:** Produces `run_replay(start: date, end: date, config: dict) -> ReplayReport` with expectancy (R), net-positive rate, per-side stats, max drawdown, worst single-gap loss, pooled zone reaction rate by kind; costs from `accounting.py`; walk-forward folds.

- [ ] **Step 1: Tests** on a synthetic series: `test_known_series_yields_expected_trades`; `test_costs_reduce_pnl`; `test_long_and_short_reported_separately`; `test_no_lookahead` (shifting future bars changes no earlier decision).
- [ ] **Steps 2–5:** fail, implement, pass; run the script on 3+ years and **check the Phase 1 exit criteria before Task 12**. Commit.

### Task 12: Paper executor and journal

**Files:** Create `src/stockfut/executor.py` (paper part), `tests/test_stockfut_paper.py`.

**Interfaces:**
- Produces: `PaperExecutor.enter(row: WatchRow, quotes: dict) -> int | None`; `PaperExecutor.apply_exit(trade_id: int, decision: ExitDecision, quotes: dict) -> None`; fills at trigger ± (half spread + 1 tick) with costs; `journal_row(row, outcome: str, reason: str)`.

- [ ] **Step 1: Tests** `test_fill_uses_spread_and_costs`; `test_t1_partial_reduces_remaining_qty`; `test_skip_logged_with_reason`; `test_paper_runs_when_live_gate_denies`.
- [ ] **Steps 2–5:** fail, implement, pass, commit.

### Task 13: Gate changes, live executor and GTT safety

**Files:** Modify `src/engine/broker_gate.py`, `src/engine/live_trading.py` (orphan sweep), `emergency_flat.py`; extend `src/stockfut/executor.py`; tests `tests/test_stockfut_live.py`, `tests/test_broker_gate.py` (add cases).

**Interfaces:**
- Produces: `validate_order(order: dict, contract: Contract, ltp: float, cfg: dict) -> tuple[bool, str]`; `LiveExecutor.enter(row) -> int | None`; `place_protection(trade_id: int, filled_lots: int) -> bool` (two GTT-OCOs, SL limit buffered by 0.3 × ATR); `watch_triggered_gtts() -> None` (60-second ladder); gate honours `stockfut_live_enabled` and treats protective GTT placement as `EXIT`; `emergency_flat` stock futures path (cancel orders and GTTs, flatten `STOCK_FUT` only, verify net 0).

- [ ] **Step 1: Tests** (mock Kite): `test_validate_rejects_non_lot_multiple`; `test_validate_rejects_price_outside_band`; `test_pending_order_cancelled_and_actual_fill_protected`; `test_gtt_failure_flattens_and_halts`; `test_order_tag_max_20_chars_and_idempotent`; `test_triggered_gtt_unfilled_after_60s_sends_ladder`; `test_gap_through_cancels_resting_gtt_order_and_waits_for_cancelled_before_ladder` (ladder never sent while the order is OPEN; ladder qty = re-read net qty); `test_net_qty_equals_sum_of_gtt_qty_invariant`; `test_paused_bot_can_still_place_protective_gtt`; `test_orphan_sweep_covers_stock_futures`; `test_emergency_flat_ignores_non_stockfut_positions`; `test_gate_blocks_stockfut_when_flag_off_but_options_unaffected`.
- [ ] **Steps 2–5:** fail, implement (reuse `confirm_order_fill`, `place_kite_gtt` patterns), pass, commit.

### Task 14: Reconciliation

**Files:** Create `src/stockfut/reconcile.py`, `tests/test_stockfut_reconcile.py`.

**Interfaces:** Produces `reconcile_startup() -> ReconcileReport`; `reconcile_periodic() -> ReconcileReport` covering DB-open/broker-flat, broker-open/DB-flat, quantity mismatch; `heartbeat_gap_alert(open_positions: int, minutes_down: int) -> bool` (true above 30); `kite_token_alert(open_positions: int) -> bool` (true when the token is expired and positions are open; GTTs carry the risk meanwhile).

- [ ] **Step 1: Tests** one per case above; `test_scan_blocked_until_startup_reconcile_done`; `test_expired_token_with_open_positions_alerts`.
- [ ] **Steps 2–5:** fail, implement, pass, commit.

### Task 15: Jobs and scheduling

**Files:** Create `src/stockfut/jobs.py`, `tests/test_stockfut_jobs.py`; modify `src/scheduler/job_runner.py` (one `register_stockfut_jobs(scheduler)` call).

**Interfaces:** Produces `register_stockfut_jobs(scheduler) -> None` adding: 08:45 refresh, 19:30 EOD scan, 12:15 trigger check (live), 15:15 trigger check (queue NEXT_OPEN only), 09:45 NEXT_OPEN re-check, 14:15 daily-exit pass, 14:45 entry blackout, hourly trail, 15-minute reconcile + risk monitor, 1-minute GTT watcher (only while positions are open). All weekday-only and holiday-aware.

- [ ] **Step 1: Tests** `test_jobs_skip_weekends`; `test_jobs_noop_when_config_disabled`; `test_register_adds_expected_job_ids`.
- [ ] **Steps 2–5:** fail, implement, pass, commit.

### Task 16: `/futures` page, API and digest

**Files:** Create `src/dashboard/futures.html`; modify `dashboard_server.py` (route `GET /futures`, `GET /api/futures/{overview,watchlist,positions,trades,risk,journal,performance}`), the 8 templates (`index, paper, broker, ai, contributors, report, settings, ops`) for the nav link; create `src/alerts/futures_digest.py`; tests `tests/test_stockfut_api.py`.

**Interfaces:** Page blocks and fields per spec section 8; control endpoints (`pause`, `arm_live`, `flatten`) call the same gate and require a confirmation token; digest `build_futures_digest(watchlist, positions) -> str` sent via the existing Telegram dispatcher.

- [ ] **Step 1: Tests** `test_futures_route_serves_page`; `test_api_watchlist_shape`; `test_flatten_requires_confirmation_token`; `test_nav_link_present_in_all_templates` (asserts `href="/futures"` in each of the 8 files); `test_digest_lists_side_lots_sl_t1_t2`.
- [ ] **Step 2:** fail. **Step 3:** implement following the Swiss Modern Flat conventions in `theme.css` (tabular numbers, 1px borders, 3-tier surfaces). **Step 4:** pass; open the page in the browser pane and check the layout, console and API calls. **Step 5:** commit.

---

## Sequencing and gates

Build **Task 8 before Task 7** (the watchlist builder calls the plan and lot functions). Tasks 1–10 build the engine; **Task 11 (replay) is a gate**: if expectancy after costs is not positive, stop and revisit the matrices before building executors. Tasks 12–16 follow; paper runs for ≥ 30 trades before any live work in Task 13 is enabled. Live starts at the 2-lot minimum on a 20-name whitelist.
