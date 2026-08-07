# Multi-Leg Short Options Trading System — Implementation Plan

## Context

NSEBOT currently runs single-leg CORE and TIMEFRAME strategies, plus a TFSS (Trend Following Short Strangle) path embedded inside CORE. The user wants a new multi-leg system where the **LLM acts as an experienced options trader**, deciding the strategy (Iron Condor, Short Strangle, Straddle, spreads, Jade Lizard, etc.), selecting all legs, and managing exits/adjustments — using the core engine as a tool but thinking beyond pure math.

**Decisions confirmed:**
- **Symbols**: NSE indices only (NIFTY, BANKNIFTY, FINNIFTY, SENSEX)
- **TFSS**: Absorbed — becomes one strategy the LLM can choose (Short Strangle on trend)
- **LLM scope**: Entry + Exit + Adjustments — controlled per-strategy via dashboard toggle
- **Mode**: Paper + Live together
- **All legs are SELL (short premium)**

**Critical findings from code review:**
- `multi_leg_trades` + `multi_leg_legs` tables **already exist** in DDL (lines 351-378, schema.py) — extend, don't duplicate
- `get_ai_mode()` exists in `strategy_registry.py:103` but is **dead code** (never called) — must wire it up
- Dashboard HTML **already has** AI Mode `<select>` elements per strategy (`settings.html:1594,1626,1658`) and `collectStrategySettings()` collects them — leverage existing UI
- `live_trades` table **missing** `leg_group_id`/`tranche_index` — needs migration
- `active_strategies_for()` iterates hardcoded `["CORE", "TIMEFRAME"]` at line 60 — must add `"MULTILEG"`
- Greeks calculator (`src/utils/greeks_calculator.py`) — full BSM/Black-76, ready to use
- Entry quality (`src/engine/entry_quality.py`) — `calculate_entry_quality(symbol, option_type, strike, ctx)` exists

---

## Architecture Overview

```
Pipeline → MULTILEG Strategy Runner (via strategy_registry)
  → get_ai_mode("MULTILEG") reads from runtime_config.json
  → LLM gets full option chain + IV + regime + historical perf
  → LLM returns LLMMultiLegVerdict (strategy_type + legs + exit plan)
  → MultiLegStrategyEngine validates, computes Greeks, scores entry
  → ai_mode gates execution:
      advisory → log + Telegram only
      boost_only → promote blocked signals only
      full → atomic book entry (paper or live)
  → Book-level monitoring on every scan cycle
  → LLM exit advisor for adjustments/closures
```

---

## Phase 1: Config + Schema

### 1A. `config/multileg_strategies.py` (NEW)
Strategy types, allowed symbols, book-level risk caps, strategy constraints:
- `STRATEGY_TYPES` dict (IRON_CONDOR, SHORT_STRANGLE, SHORT_STRADDLE, BEAR_CALL_SPREAD, BULL_PUT_SPREAD, JADE_LIZARD, CUSTOM)
- `ALLOWED_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}`
- `MAX_BOOK_MARGIN = 500000`, `MAX_NET_DELTA = 0.60`, `MAX_LEGS_PER_BOOK = 6`
- `STRATEGY_CONSTRAINTS` — min/max legs per strategy type
- Reuses patterns from `config/trend_following_short_strangle.py`

### 1B. Schema migrations in `src/models/schema.py`

**Extend existing `multi_leg_trades` table** (already in DDL at line 351) — add missing columns via ALTER TABLE:
```sql
M077: ALTER TABLE multi_leg_trades ADD COLUMN book_id TEXT
M078: ALTER TABLE multi_leg_trades ADD COLUMN strategy_type TEXT
M079: ALTER TABLE multi_leg_trades ADD COLUMN expiry TEXT
M080: ALTER TABLE multi_leg_trades ADD COLUMN entry_underlying REAL
M081: ALTER TABLE multi_leg_trades ADD COLUMN exit_underlying REAL
M082: ALTER TABLE multi_leg_trades ADD COLUMN net_delta REAL
M083: ALTER TABLE multi_leg_trades ADD COLUMN net_theta REAL
M084: ALTER TABLE multi_leg_trades ADD COLUMN net_vega REAL
M085: ALTER TABLE multi_leg_trades ADD COLUMN max_profit REAL
M086: ALTER TABLE multi_leg_trades ADD COLUMN max_loss REAL
M087: ALTER TABLE multi_leg_trades ADD COLUMN breakeven_upper REAL
M088: ALTER TABLE multi_leg_trades ADD COLUMN breakeven_lower REAL
M089: ALTER TABLE multi_leg_trades ADD COLUMN profit_target_pct REAL
M090: ALTER TABLE multi_leg_trades ADD COLUMN stop_loss_pct REAL
M091: ALTER TABLE multi_leg_trades ADD COLUMN time_decay_exit_dte INTEGER
M092: ALTER TABLE multi_leg_trades ADD COLUMN adjustment_count INTEGER DEFAULT 0
M093: ALTER TABLE multi_leg_trades ADD COLUMN confidence_score INTEGER
M094: ALTER TABLE multi_leg_trades ADD COLUMN entry_quality_score INTEGER
M095: ALTER TABLE multi_leg_trades ADD COLUMN digest_id TEXT
M096: ALTER TABLE multi_leg_trades ADD COLUMN ai_model_name TEXT
```

**Extend existing `multi_leg_legs` table** (already in DDL at line 367) — add missing columns:
```sql
M097: ALTER TABLE multi_leg_legs ADD COLUMN delta REAL
M098: ALTER TABLE multi_leg_legs ADD COLUMN theta REAL
M099: ALTER TABLE multi_leg_legs ADD COLUMN vega REAL
M100: ALTER TABLE multi_leg_legs ADD COLUMN iv REAL
M101: ALTER TABLE multi_leg_legs ADD COLUMN rationale TEXT
M102: ALTER TABLE multi_leg_legs ADD COLUMN status TEXT DEFAULT 'OPEN'
M103: ALTER TABLE multi_leg_legs ADD COLUMN closed_at TEXT
M104: ALTER TABLE multi_leg_legs ADD COLUMN exit_reason TEXT
M105: ALTER TABLE multi_leg_legs ADD COLUMN broker_order_id TEXT
```

**Extend `live_trades`** — add multi-leg columns (currently missing):
```sql
M106: ALTER TABLE live_trades ADD COLUMN leg_group_id TEXT
M107: ALTER TABLE live_trades ADD COLUMN tranche_index INTEGER DEFAULT 0
```

New helper functions in `schema.py`:
- `get_open_book_legs(book_id)` — query `multi_leg_legs` WHERE status='OPEN' by trade_id
- `get_open_books_for_symbol(symbol)` — query `multi_leg_trades` WHERE status='OPEN' grouped by book_id
- `insert_multileg_trade(trade_dict, legs_list)` — atomic insert into both tables
- `close_book(book_id, reason)` — update status on trade + all legs

`book_id` format: `{symbol}:{YYYYMMDD}:{strategy_type}:{sequence}`

---

## Phase 2: LLM Schema + Prompt

### 2A. `src/engine/multileg_llm_schema.py` (NEW)
Pydantic models for LLM structured output:

```python
class LLMLeg(BaseModel):
    side: str              # Always "SELL"
    option_type: str       # CE | PE
    strike: float
    premium: float
    delta: float
    rationale: str         # Why this specific leg

class LLMMultiLegVerdict(BaseModel):
    strategy_type: str     # IRON_CONDOR | SHORT_STRANGLE | etc.
    legs: List[LLMLeg]
    net_premium: float
    net_delta: float
    net_theta: float
    net_vega: float
    max_profit: float
    max_loss: float
    breakeven_upper: float
    breakeven_lower: float
    entry_rationale: str
    confidence: int
    thesis: str
    # Exit plan
    profit_target_pct: float
    stop_loss_pct: float
    time_decay_exit_dte: int
    per_leg_exit_triggers: str
    book_level_exit_triggers: str
    adjustment_plan: str
    model_name: Optional[str]
```

Flat structure (not deeply nested) — proven to work across all providers in the chain.

### 2B. `src/engine/multileg_llm_prompt.py` (NEW)
Builds the prompt that makes LLM think like an experienced options trader.

**What the LLM receives** (expanded from current `_build_deep_prompt()` at `llm_enrichment.py:659`):
- Full option chain (not just ATM±3) — all strikes with CE/PE LTP, OI, IV, delta
- IV data: ATM IV, IV skew across strikes, IV change from previous scan
- Support/Resistance/Max Pain (already in `scan_context`)
- Market regime from `regime_detector.py` (trending/rangebound/volatile)
- India VIX (new fetch — see Phase 6)
- Historical multi-leg trade performance (query from `multi_leg_trades`)
- News + macro context (existing)
- Open book status (if any)
- Upcoming events (expiry dates, policy dates)

**Strategy selection guide** in prompt:
- Rangebound + high IV → IRON_CONDOR or SHORT_STRANGLE
- Trending + high IV → SHORT_STRANGLE aligned with trend, or directional spread
- Very high IV + neutral → SHORT_STRADDLE
- Bullish bias + high IV → JADE_LIZARD
- All legs are SELL (you are the seller of options)

### 2C. `src/engine/llm_enrichment.py` (MODIFY)
Add `get_multileg_verdict()` alongside existing `get_llm_verdict()`:
- Same provider chain, new schema (`LLMMultiLegVerdict`), new prompt
- Existing `get_llm_verdict()` unchanged — backward compatible
- Falls back to single-leg path if multi-leg LLM call fails

---

## Phase 3: Strategy Engine

### 3A. `src/engine/multileg_strategy.py` (NEW)
Core validation, Greeks, risk profile, margin, scoring:

- `validate_legs(strategy_type, legs, option_chain, underlying)` — strike exists, premium valid, all SELL, leg count matches strategy
- `compute_book_greeks(legs, option_chain, underlying, expiry)` — reuses `src/utils/greeks_calculator.py` `get_greeks_calculator().calculate_greeks()` per leg, sums for net book Greeks
- `compute_book_risk_profile(strategy_type, legs, net_premium, underlying)` — strategy-specific max_profit/max_loss/breakevens
- `calculate_combined_margin(legs, symbol)` — broker API first (`kite.order_margins()`), static fallback
- `score_entry_quality(strategy_type, legs, scan_context, book_greeks, risk_profile)` — 0-100 from IV rank, strike distance from S/R, net delta, risk/reward, regime alignment. Extends `entry_quality.calculate_entry_quality()` pattern.
- `check_book_conflicts(symbol, proposed_strategy, existing_books)` — no conflicting strategies (e.g., BULL_PUT_SPREAD + BEAR_CALL_SPREAD)
- `build_execution_plan(symbol, strategy_type, legs, book_id, scan_context)` — complete plan with exits + adjustments

---

## Phase 4: Multi-Leg Paper Trading

### 4A. `src/engine/multileg_paper_trading.py` (NEW)
Entry point follows `run_paper_trading()` signature pattern (confirmed at `paper_trading.py:1141`):

```python
def run_multileg_paper_strategy(symbol, scan_context, digest_id, intel, ai_verdict=None) -> dict | None:
```

**ai_mode integration** — reads from `strategy_registry.get_ai_mode("MULTILEG")`:
- `advisory` → call LLM, log verdict + send to Telegram, but do NOT insert trade
- `boost_only` → only act if signal was blocked and LLM promotes it
- `full` → execute normally

**CRITICAL FIX**: `get_ai_mode()` is currently dead code (defined at `strategy_registry.py:103` but never called). This plan wires it up by having the runner call it at entry.

Flow:
1. Check market hours (`_is_market_open()` from `paper_trading.py`)
2. Read `ai_mode = get_ai_mode("MULTILEG")`
3. Monitor existing open books (exits/adjustments — rule-based checks always run; LLM exit advice gated by ai_mode)
4. If no open books or room for new book:
   a. Call `get_multileg_verdict()` — LLM picks strategy + legs
   b. Validate via `MultiLegStrategyEngine.validate_legs()`
   c. Compute book Greeks, risk profile, margin
   d. Score entry quality
   e. If ai_mode == "advisory": log + Telegram only, skip execution
   f. If ai_mode == "full": execute all legs atomically via `insert_multileg_trade()`
5. Return result dict

### 4B. Monitor integration in `src/engine/paper_trading.py`
Add multi-leg book monitoring section after existing TFSS section (line ~1037):
- Query `get_open_books_for_symbol(symbol)` from `multi_leg_trades`
- Check book-level profit target, stop loss, time decay
- Call LLM exit advisor for adjustment recommendations (gated by ai_mode)
- Execute adjustments (roll, add leg, close leg)

---

## Phase 5: Multi-Leg Live Trading

### 5A. `src/engine/multileg_live_trading.py` (NEW)
Live counterpart to paper trading:

- `_place_multileg_order(kite, symbol, legs, strategy_type)` — sequential Kite orders (Kite has no native spread orders), error handling with square-off on partial fill
- `_monitor_live_book_exits(symbol, scan_context)` — GTT/poll based exits
- `_execute_live_book_adjustment(symbol, book_id, adjustment_type, scan_context)` — close + reopen legs

### 5B. Margin in `src/engine/capital_allocator.py` (MODIFY)
Add `calculate_combined_book_margin()` — pass all legs to `kite.order_margins()` for combined margin, fall back to static multiplier. Uses existing `_fetch_broker_margin_requirement()` pattern.

---

## Phase 6: Risk Management

### 6A. `src/engine/risk_engine.py` (MODIFY)
Add multi-leg book checks alongside existing TFSS checks (lines 217-240):
- Max book margin (reuse `TFSS_MAX_BOOK_MARGIN` pattern)
- Max net delta (reuse `TFSS_COMBINED_DELTA_CAP` pattern)
- Max legs per book
- Conflict check (no opposing strategies on same symbol)
- Multi-leg book excluded from per-symbol open-trade count (like TFSS is excluded at line 128)

---

## Phase 7: Strategy Registry + Pipeline + Dashboard LLM Scope

### 7A. `src/engine/strategy_registry.py` (MODIFY)
- Add `"MULTILEG"` to `DEFAULT_STRATEGIES` (line 13-16):
  ```python
  "MULTILEG": { "enabled": True, "ai_mode": "advisory", "symbols": {} }
  ```
- `active_strategies_for()` (line 60): add `"MULTILEG"` to the loop: `for sid in ["CORE", "TIMEFRAME", "MULTILEG"]:`
- `get_runner()` (line 72): add `elif sid == "MULTILEG"` → lazy-import `run_multileg_paper_strategy`
- **FIX dead code**: `get_ai_mode()` already reads from runtime_config (line 103-113) — no changes needed to the function itself, just ensure it's called

### 7B. Settings Page — LLM Scope per Strategy

**Location**: Settings page (`settings.html`) → Strategies tab — NOT the main Dashboard overview.

**Existing UI already supports this.** The Settings page HTML (`settings.html`) already has:
- AI Mode `<select>` elements per strategy (lines 1594, 1626, 1658) with options: "Advisory Only", "Boost Only", "Full Authority"
- `collectStrategySettings()` (line 2250-2269) collects `ai_mode` from each `<select>`
- `saveSettings()` POSTs the full config to `/api/settings`

**What to add:**
- Add `"MULTILEG"` to the `collectStrategySettings()` iteration (currently `["CORE", "TIMEFRAME", "TFSS"]` at line 2252)
- Add a MULTILEG row in the strategies table HTML on the Settings page
- Add MULTILEG-specific params section (if needed)

**AI Mode behavior** (same 3 modes, now actually wired up):

| Mode | Entry behavior | Exit behavior |
|------|---------------|---------------|
| **Advisory** | LLM suggests strategy + legs, logged but NOT auto-executed | LLM suggests exit/adjustment, logged but NOT auto-executed. Rules still run. |
| **Boost Only** | LLM can promote blocked signals to TRIGGERED_EXPERIMENTAL | LLM exit advice is advisory only |
| **Full** | LLM has full authority: picks strategy, legs, auto-enters | LLM can auto-close/adjust books |

**How it works:**
- Setting stored in `runtime_config.json` under `strategies.MULTILEG.ai_mode`
- `strategy_registry.get_ai_mode("MULTILEG")` reads from runtime_config (already works)
- Runner calls `get_ai_mode("MULTILEG")` at entry to gate execution
- Hot-reload — no restart needed

### 7C. `src/engine/pipeline.py` (MODIFY)
In `_process_prefetched_symbol()` strategy dispatch loop (~line 830):
- Add `MULTILEG` live trading dispatch after the runner call:
  ```python
  elif sid == "MULTILEG":
      from src.engine.multileg_live_trading import run_multileg_live_strategy
      run_multileg_live_strategy(symbol, scan_context, scan_digest_id, intel, ai_verdict=llm_verdict)
  ```

### 7D. TFSS Absorption
- TFSS continues as deterministic path (existing code unchanged)
- MULTILEG SHORT_STRANGLE is the LLM-driven alternative
- Both coexist — user can disable one via dashboard Settings → Strategies

---

## Phase 8: Telegram Digest

### 8A. `src/alerts/digest.py` (MODIFY)
Add `_format_multileg_book(trade, legs)` — strategy name, per-leg details, combined Greeks, P&L, risk/reward. Pattern follows existing `_format_trade_status()` (line 610) and TFSS section in `build_tfss_timeframe_digest()` (line 710).

### 8B. `build_digest()` update (line 942)
New section after existing trade status sections for multi-leg books. Query `get_open_books_for_symbol(symbol)` and format each book.

---

## Phase 9: Entry Quality

### 9A. `src/engine/entry_quality.py` (MODIFY)
Add `calculate_multileg_entry_quality()` alongside existing `calculate_entry_quality()` (line 13). Extends the same penalty pattern (wrong side of S/R, poor R:R, wide bid-ask, chasing) with multi-leg factors: IV rank, net delta magnitude, premium yield on margin, regime alignment.

---

## Phase 10: Testing

New test files:
- `tests/test_multileg_strategy.py` — validation, Greeks, risk profile, margin
- `tests/test_multileg_paper_trading.py` — entry, exit, adjustment lifecycle
- `tests/test_multileg_llm_schema.py` — schema parsing, provider compatibility
- `tests/test_multileg_risk.py` — book-level risk checks
- `tests/test_multileg_prompt.py` — prompt construction, data formatting

---

## Key Files Summary

| File | Action | Purpose |
|------|--------|---------|
| `config/multileg_strategies.py` | NEW | Strategy types, risk caps, constraints |
| `src/models/schema.py` | MODIFY | Migrations (extend multi_leg_trades/legs + live_trades) + book helpers |
| `src/engine/multileg_llm_schema.py` | NEW | Pydantic schemas for LLM output |
| `src/engine/multileg_llm_prompt.py` | NEW | Enhanced prompt with full chain + IV + regime |
| `src/engine/multileg_strategy.py` | NEW | Strategy engine: validate, Greeks, margin, score |
| `src/engine/multileg_paper_trading.py` | NEW | Paper trading: atomic entry, monitoring, adjustments |
| `src/engine/multileg_live_trading.py` | NEW | Live trading: Kite orders, GTT exits, adjustments |
| `src/engine/llm_enrichment.py` | MODIFY | Add `get_multileg_verdict()` |
| `src/engine/strategy_registry.py` | MODIFY | Register MULTILEG, fix get_ai_mode dead code |
| `src/engine/pipeline.py` | MODIFY | Dispatch MULTILEG in strategy loop |
| `src/engine/risk_engine.py` | MODIFY | Book-level risk checks |
| `src/engine/capital_allocator.py` | MODIFY | Combined book margin |
| `src/engine/paper_trading.py` | MODIFY | Multi-leg monitor section |
| `settings.html` | MODIFY | Add MULTILEG row + AI Mode dropdown to Settings → Strategies tab |
| `src/alerts/digest.py` | MODIFY | Multi-leg book formatting |
| `src/engine/entry_quality.py` | MODIFY | Multi-leg entry quality scoring |
| `tests/test_multileg_*.py` (5 files) | NEW | Test coverage |

---

## Verification

1. **Unit tests**: `pytest tests/test_multileg_*.py -v`
2. **Full suite**: `pytest tests/ -v` — no regressions
3. **Settings page AI Mode**: Settings → Strategies tab shows MULTILEG row with AI Mode dropdown. Change from Advisory → Full, confirm `runtime_config.json` updates. Confirm `get_ai_mode("MULTILEG")` returns the new value.
4. **Paper dry run (advisory)**: `python main.py --now` with MULTILEG in Advisory mode — LLM verdict logged + sent to Telegram, but NO paper trade inserted in `multi_leg_trades`
5. **Paper dry run (full)**: Switch to Full mode — confirm trades logged in `multi_leg_trades` + `multi_leg_legs` with book_id, strategy_type, legs
6. **Settings page**: Confirm MULTILEG row appears in Strategies tab with AI Mode dropdown and symbol toggles
7. **Telegram**: Confirm strategy name, legs, Greeks, P&L in digest
8. **Live (shadow mode)**: Enable shadow mode, confirm orders would be placed correctly
9. **Edge cases**: LLM returns invalid strikes → validation catches it; atomic entry partial failure → rollback; conflicting strategies blocked

---

## Risks

| Risk | Mitigation |
|------|-----------|
| LLM schema parsing across providers | Test `LLMMultiLegVerdict` against all providers; flat schema reduces parse failures |
| Partial fill on live multi-leg | Square off filled legs immediately if next leg fails |
| LLM hallucinated strikes | `validate_legs()` checks every strike against actual option chain |
| Margin calculation accuracy | Broker API first, static fallback |
| TFSS backward compat | TFSS path unchanged; MULTILEG runs alongside |
| `get_ai_mode()` dead code | Plan wires it up by having runner call it at entry |
