# Revised Plan (Executor-First) — Algo Templates

**Replaces the P1 CRUD build (`implementation_plan.md` is superseded).**
Build the thing that *trades* first; defer storage/UI until a template proves it earns one.

## 0. Guiding principles

1. **Executor first.** The value is in running a user-defined book off the engine verdict.
   Everything else (schema, REST, HTML, nav) is deferred.
2. **Config-driven, not code-driven.** Templates live in `config/algo_templates.json`,
   edited by hand. Matches the established `runtime_config.json` pattern.
   The DB table + UI are P3 — only if templates earn them.
3. **Fail-closed, always.** Corrupt config → all templates disabled, loud log. A template
   that can't fully resolve legs → books nothing (no partial multi-leg books). A
   `paper_only=false` template → never books (live path not built). Never a half-book.
4. **All SL/Target through `trade_plan.py`** (CLAUDE.md hard constraint), never in the
   executor. Percentage overrides (`target_pct`/`sl_pct`) are P2 — added *inside* `trade_plan.py`, not there.
5. **Exclusivity.** A template-managed symbol is managed by templates, not built-ins,
   for the book's whole lifetime. Prevents CORE/TFSS stacking a second portfolio on top.
6. **One template per symbol per scan.** No multi-template stacking in MVP.

---

## 1. Scope gate (what ships in P1)

**In (P1):** config loader + validation + executor + pipeline hook + minimal monitor
integration + atomic booking + tests. Paper only.

**Deferred (P2):** `NEXT` expiry mode, trailing SL (`move_sl_to_cost`/`trail_sl_points`/
`trail_profit_points`), time-decay / delta-stop rebalancing, `target_pct`/`sl_pct`
semantics, live path (`paper_only=false`), MCX FUT auto-fallback.

**Deferred (P3):** DB table + REST + web UI + nav. Only if a template demonstrates value.

---

## 2. Config format — `config/algo_templates.json`

```json
{
  "enabled": true,
  "defaults": {
    "min_confidence": 0,
    "expiry_mode": "CURRENT",
    "product_type": "MIS",
    "paper_only": true
  },
  "templates": [
    {
      "id": "nifty_long_pcs",
      "name": "NIFTY Put Credit Spread",
      "symbol": "NIFTY",
      "trigger_direction": "LONG",
      "min_confidence": 70,
      "expiry_mode": "CURRENT",
      "paper_only": true,
      "max_margin": 300000,
      "max_net_delta": 0.50,
      "max_profit": 25000,
      "max_loss": 50000,
      "legs": [
        {"side": "SELL", "option_type": "PE", "strike_selection": "ATM_OFFSET", "offset": -400,  "lots": 1},
        {"side": "BUY",  "option_type": "PE", "strike_selection": "ATM_OFFSET", "offset": -800,  "lots": 1}
      ]
    }
  ]
}
```

Loader `config/algo_templates.py` (mirrors `runtime_config.py`):

```python
_DEFAULTS = {"min_confidence": 0, "expiry_mode": "CURRENT", "product_type": "MIS", "paper_only": True}

def load_algo_templates() -> list[dict]:
    """Read + validate config/algo_templates.json. Fail-closed: any error → [] + log."""
    try:
        raw = json.loads(Path(CONFIG_DIR / "algo_templates.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        log.error("algo_templates.json unreadable — templates disabled: %s", e)
        return []
    if not raw.get("enabled", False):
        return []
    templates = []
    for t in raw.get("templates", []):
        t = {**raw.get("defaults", {}), **t}
        errors = _validate_template(t)
        if errors:
            log.error("algo template %s rejected: %s", t.get("id"), errors)
            continue
        templates.append(t)
    return templates
```

`_validate_template` returns error strings (mirror of the old plan's 1d, minus server
concerns): `id`/`name`/`symbol` non-empty, `trigger_direction ∈ {LONG,SHORT,ALL}`,
`min_confidence ∈ [0,100]`, `expiry_mode ∈ {CURRENT,SPECIFIC}` (NEXT rejected with
"not yet supported"), `product_type ∈ {MIS,NRML}`, `paper_only` bool, `max_* ≥ 0`,
≥ 1 leg, each leg: `side ∈ {BUY,SELL}`, `option_type ∈ {CE,PE,FUT}`, `strike_selection ∈
{ATM,ATM_OFFSET,PREMIUM,STRADDLE_WIDTH,SPECIFIC}`, per-mode required field present
(`ATM_OFFSET→offset`, `PREMIUM→premium`, `STRADDLE_WIDTH→width`, `SPECIFIC→strike`),
`lots ≥ 1`.

> **Design call:** loader lives behind the `load_algo_templates()` function, so P3 can swap
> the source to the DB table without touching the executor.

---

## 3. Critical fix: `get_open_paper_trade` corrupts ALGO legs

`schema.py:get_open_paper_trade` (line ~993) filters only `setup_type != 'TIMEFRAME'`
and `!= 'TFSS'`:

```python
WHERE symbol=? AND status='OPEN'
  AND (setup_type IS NULL OR (setup_type != 'TIMEFRAME' AND setup_type != 'TFSS'))
```

An ALGO leg (we'll store `setup_type='ALGO'`) is **NOT** excluded. Consequences today:
- `monitor_paper_trades` would treat one ALGO leg as a lone CORE trade → per-leg SL/Target
  (mostly harmless, but wrong semantics) **and** `_monitor_single_paper_trade`'s **reversal
  block** would fire on it (harmful).
- `execute_paper_trade` / live path would see an ALGO leg as an open CORE position → could
  reverse-close a strategy leg, corrupting the book.

**Fix (schema.py, one line):** add `AND setup_type != 'ALGO'` to the exclusion:
```python
AND (setup_type IS NULL OR (setup_type NOT IN ('TIMEFRAME','TFSS','ALGO')))
```
(The old CRUD plan never touched this — it was silently off the table. Bug.)

---

## 4. Executor — `src/engine/algo_template_runner.py`

### Signature (called directly by pipeline, NOT via strategy_registry)

```python
def run_algo_templates(symbol: str, scan_context: dict, digest_id: str, intel: dict) -> bool:
    """Book paper legs per the first matching active template for this symbol.
    Returns True iff `symbol` is *managed by templates right now* — i.e. a template
    matched the signal OR an ALGO book is already open — so the pipeline suppresses
    built-in strategies (exclusivity). Never raises."""
```

### Flow (each failure path logs and returns — fail-closed)

```
0. if not market_open(symbol): return False              # reuse _is_market_open from paper_trading
   base = _get_base_symbol(symbol)                        # live_trading
1. if _has_open_algo_book(symbol): return True            # exclusive owner; monitor handles exits
2. templates = [t for t in _get_template_loader()()       # load fresh each scan (hand-edited file)
                 if t["active"] and base == t["symbol"]]
   return False if not templates
3. verdict  = intel.get("verdict_label") or ""
   conf     = int(intel.get("confidence") or 0)
   slim = _family_match(t, verdict, conf)                 # LONG→is_bullish, SHORT→is_bearish, ALL→either
   t = first slim template (ordered by id)
   return False if none                                 # no matched → built-ins run
   # → from here the template owns this scan, regardless of booked/blocked (intent)
   #   so built-ins are suppressed. log decision.
4. resolve expiry: CURRENT → scan_context["expiry"];
   SPECIFIC → t["expiry"]; if that expiry's rows absent from scan_context → skip template (return True, log)
   rows = _get_option_rows_for_expiry(scan_context, expiry)
5. RESOLVE ALL LEGS (no backend): for each leg:
     strike = _resolve_strike(leg, scan_context, rows)      # §5
     expiry each GET premium via trade_plan.get_option_premium(...)
     rejected premium → whole template skipped (atomic)
     sl_ul,tgt_ul = trade_plan.calculate_buy/sell_sl_target(entry_premium, underlying, scan_context, step, option_type)
     sl_prem,tgt_prem = trade_plan.convert_underlying_sl_to_premium(...)
   if ANY leg unresolved or ANY premium None → log + return True (suppress built-ins, book nothing)
6. risk gates (book-level, before any insert):
     current ALGO legs for base + these resolved legs → compute net_delta (via risk_engine._leg_delta),
     margin (via risk_engine._leg_margin); enforce t["max_margin"], t["max_net_delta"] → block → return True
7. ATOMIC BOOK: one transaction, all legs share leg_group_id
     leg_group_id = f"{base}:{IST%Y%m%d}:ALGO:{t['id']}"
     signal_key    = f"{base}:{ot}:{int(strike)}:{Y%m%d}:ALGO:{t['id']}:paper"   # per-leg, dedup-replay-safe
     setup_type   = "ALGO";  legs_json = t["legs"]
     lots = calculate_trade_lots(base, prem, side, is_paper=True, setup_type="ALGO") * leg_lots
     each insert with conn= the shared txn (see §6)
8. stamp pattern caches / return True
```

### Atomicity (risk-critical)

A spread is only valid whole. **Book all legs or none.** `insert_paper_trade` currently
opens its own `get_conn()`; the executor must hold a single transaction so a mid-insert
crash can't leave a one-legged "spread". Add an `conn: sqlite3.Connection | None = None`
parameter to `insert_paper_trade` (exact mirror of the existing `insert_live_trade`, which
already does this — `schema.py:1679`). The runner:
```
resolved_legs = [resolve each]                  # pure, no DB
if not all: return True
with get_conn() as conn:
    for leg in resolved_legs: insert_paper_trade(..., conn=conn)
```
If any insert raises, the `with` block rolls back the whole group. The FOR is inside one txn.

---

## 5. Strike resolution — pure function (testable)

```python
def _resolve_strike(leg, base, option_rows, underlying, strike_step) -> float | None:
    mode = leg["strike_selection"]
    if mode == "ATM":            return round((atm_strike or underlying)/step)*step
    if mode == "ATM_OFFSET":     return round((underlying + leg["offset"])/step)*step
    if mode == "PREMIUM":        row → min |premium − leg["premium"]| over option_type; else None
    if mode == "STRADDLE_WIDTH": CE→atm+width/2  PE→atm−width/2  (rounded to step); both → a strangle
    if mode == "SPECIFIC":       return leg["strike"] if leg["strike"]>0 else None
    return None
```

- Uses `atm` from `_atm_strike` (already in `anomaly_detector`), `step` from
  `config.symbol_classes.get_strike_step`.
- `premium`-mode and `SPECIFIC` must have a row in the target expiry's option rows.

---

## 6. Schema changes (3, minimal)

| Change | Why |
|---|---|
| `get_open_paper_trade` — exclude `ALGO` | §3 |
| `insert_paper_trade(..., conn=None)` | atomicity §4 |
| add `get_open_algo_legs(symbol, table='paper_trades')` | monitor: select `setup_type='ALGO' AND status='OPEN'` (Order by leg_group_id, side, strike — reuse `get_open_tfss_legs` shape) |

NULL. `monitor` must fetch ALGO legs and the book's shared `leg_group_id`. `paper_trades`
already stores `leg_group_id`, `setup_type`, `tranche_index`. No new column. **No new table.**

No DDL block change. `init_db()` untouched.

---

## 7. Monitoring — `monitor_paper_trades` in `paper_trading.py`

Add an ALGO branch right after the TFSS branch (`~paper_trading.py:1018`):

```python
algo_legs = get_open_algo_legs(symbol)
for leg in algo_legs:
    _monitor_single_paper_trade(symbol, strategy_leg, current_ctx, underlying)   # per-leg SL/Target
# book-level P&L close
book_pnl = sum(current_pnl(leg) for leg in algo_legs)   # via trade_plan.get_option_premium, side/lots/lot_size
if book_pnl <= -template_max_loss: close all legs ("CLOSED_MANUAL", "ALGO book max_loss")
elif book_pnl >= template_max_profit: close all legs ("CLOSED_TARGET","ALGO book max_profit")
```

- Per-leg underlying/premium SL/Target is **already handled** by `_monitor_single_paper_trade` —
  reuse, no new SL logic (holds the trade_plan constraint).
- Book P&L (max_loss / max_profit) is *book-level* gating, distinct from per-leg ATR
  SL/Target — new code, but it only reuses `trade_plan.get_option_premium` + per-leg math.
- Add `reason` strings are explicit so the digest is trader-readable.

## 8. Missing: MCX FUT auto-fallback, NEXT expiry — deferred P2 (documented)

None of these block the MVP. Keep them named in section 1 so nobody silently assumes support.

---

## 9. Files touched (≤ 8)

| File | Change |
|---|---|
| new `config/algo_templates.json` | definitions |
| new `config/algo_templates.py` | loader + `_validate_template` |
| new `src/engine/algo_template_runner.py` | executor + strike resolution |
| `src/models/schema.py` | §3 two lines + `get_open_algo_legs` |
| `src/engine/paper_trading.py` | monitor ALGO branch |
| `src/engine/pipeline.py` | call `run_algo_templates` before the strategy loop; suppress built-ins when True |
| new `tests/test_algo_template_runner.py` | tests |
| `data/sentinel/KNOWLEDGE_BASE.md` | CLAUDE.md mandate |

No `strategy_registry` change; the `do-not-register-unbuilt-runner` guard (CLAUDE.md)
is explicitly **not violated** — ALGO is a direct pipeline call, not a registered sid.

**Pipeline diff (the `_process_prefetched_symbol` serialized section, `pipeline.py:823`ish):**

```python
with serialized_commit_gate.section(f"commit:{symbol}"):
    timeframe_res = None
    if not is_test:
        # BEGIN: ALGO template exclusivity — direct call, not a registered strategy
        algo_active = run_algo_templates(symbol, scan_context, scan_digest_id, intel)
        # END
        if not algo_active:
            for sid in active_strategies_for(symbol):
                ...existing...
```

---

## 10. Fail-closed checklist (review this PR by PR)

- [ ] Corrupt/absent `algo_templates.json` → templates disabled, executor no-ops, **built-ins
      unaffected**.
- [ ] Template `paper_only=false` → never books (logged "live path not implemented").
- [ ] Any leg unresolvable (strike/premium/expiry) → **whole template skipped, no partial insert**.
- [ ] ALGO legs excluded from `get_open_paper_trade` → never reversed/re-monitored as CORE.
- [ ] signal_key collision → `INSERT OR IGNORE` (dedup), never duplicates on replay.
- [ ] Market-closed → executor returns before any query/insert.
- [ ] `max_margin` / `max_net_delta` → bounds check computed from live rows before insert; never
      book a leg that violates the cap.

---

## 11. Tests — `tests/test_algo_template_runner.py`

| Case | Expect |
|---|---|
| loader: clean file → templates list | correct |
| loader: malformed JSON → `[]` + error | fail-closed, no exception |
| loader: one bad template → dropped, others loaded | partial-but-normal, logged |
| `_validate_template`: bad side/option_type/expiry_mode/strike_selection | error list |
| verdict-family match LONG/SHORT/ALL (bullish/bearish/neutral verdict) | only matching runs |
| confidence gate: below `min_confidence` → no match | skip |
| strike resolution: ATM / ATM_OFFSET / PREMIUM / STRADDLE_WIDTH / SPECIFIC | correct value or None |
| atomicity: leg resolution fails → no DB row | 0 paper_trades created |
| atomicity: mid-`conn` crash → rollback → 0 rows | no partial book |
| book margin/net-delta gate excess → not booked | skip + True |
| `has_open_algo_book` → returns True, built-ins suppressed for that symbol | exclusivity |
| `insert_paper_trade(conn=...)` path | 2-leg book, shared leg_group_id, correct signal keys |
| `get_open_algo_legs` vs `get_open_paper_trade` separation | ALGO excluded from the latter |
| monitor: per-leg SL/Target triggers close | leg closed, book intact |
| monitor: book max_loss / max_profit close-all | all legs closed same status |

Run: `pytest tests/test_algo_template_runner.py -v`

---

## 12. Open risks this plan answers

1. **Double-book stacking (CORE + template)** → exclusivity via `algo_active` suppression.
2. **Partial spread on crash** → all-or-nothing txn.
3. **ALGO leg mistaken for CORE** → `get_open_paper_trade` exclusion (§3) — the one bug the
   CRUD plan would have shipped.
4. **Unbounded concurrent books** → one template per symbol per scan + `has_open_algo_book`
   (interpretable).
5. **Live path blast radius** → `paper_only=false` never books; live landing explicitly P2/P3.

---

## 13. Decision to make at P2

If templates earn their UI: promote `load_algo_templates()` to the DB `algo_templates` table
(already designed), add REST + `/algo` page + nav (old plan §2-§4), then wire an "ALGO"
strategy-registry flag. Until then: JSON file + direct pipeline call is the smallest whole.

---

*Supersedes `implementation_plan.md`. Reference schemas / paper-trade mechanics:*
`schema.py::insert_live_trade(conn=)` · `paper_trading.py::execute_paper_trade`,
`monitor_paper_trades`, `_monitor_single_..` · `risk_engine.py::compute_combined_book`,
`_leg_delta`, `_leg_margin` · `trade_plan.py::calculate_*_sl_target`, `get_option_premium`,
`convert_underlying_sl_to_premium` · `config/trend_following_short_strangle.py`.