# NSEBOT Code Audit — v2 (Code-Verified)

**Supersedes:** `BUG_REPORT.md` (v1, unverified LLM audit)
**Verification method:** Each item checked against actual source on disk via Filesystem MCP — not re-derived from the original report's claims.
**Coverage:** 23 of 38 items directly verified with code evidence. Remaining 15 carried forward from v1, unverified, explicitly flagged.

## Why this revision exists

v1 claimed "31 bugs" in its summary table (8+12+14+7=41, which doesn't even sum to 31 — the report's own arithmetic was wrong) against an actual count of 38 numbered items. Of the first 13 items checked line-by-line against source, **6 were false positives or materially overstated**. This is a ~45% error rate on the sample — high enough that acting on v1 verbatim would burn engineering time fixing non-bugs and mis-prioritizing real ones. v1's severity ratings are also unreliable: it double-counted one root cause as two separate "High" bugs (009/020) and rated a 1-string/day JSON file growth as "Critical" (005).

---

## ✅ CONFIRMED — Fix These

| ID | File | What's Actually Wrong | Verified Evidence |
|---|---|---|---|
| **BUG-001** | `live_trading.py::place_kite_order()` | SELL limit price uses identical formula to BUY (`ltp * (1+buffer_pct)` both branches). Shared by entry **and every exit** call — every SL/target/reversal close places its exit limit above LTP, degrading fill probability exactly when a stop needs to execute fast. | Exact code match, both branches identical. |
| **BUG-006** | `schema.py::close_paper_trade / close_live_trade` (futures P&L) | `if side == "SELL" or verdict_label == "SHORT" or is_bearish(verdict_label):` — three different bearish indicators OR'd to pick P&L sign. `side` alone should determine direction; if it ever disagrees with `verdict_label` for a FUT trade, P&L inverts silently. | Exact match in both functions. |
| **BUG-007** | `schema.py::get_prev_snapshots_bulk` | Staleness guard uses `datetime.now(timezone.utc)` instead of the scan's actual `fetched_at` — unlike the correct pattern in `get_previous_underlying_before()`. Causes intermittent noise-suppression failures on delayed cycles. | Exact match; confirmed correct pattern exists elsewhere in same file. |
| **BUG-010** | `live_trading.py` shadows `risk_engine.py::check_live_risk_limits` | **Confirmed and more severe than v1 stated.** `risk_engine.py` has a full 6-check version (daily loss cap, loss cooldown, consecutive-loss circuit breaker, max open, max/symbol, max/day). `live_trading.py` never imports it — defines its own 2-check local function (max concurrent + 1/symbol only) under the identical name. **Live trades categorically bypass daily loss cap, loss cooldown, and the circuit breaker.** This is your highest-priority fix — real capital, no risk backstop beyond position count. | Traced import list in `live_trading.py` — zero reference to `risk_engine`. Confirmed both function definitions independently. |
| ~~BUG-020~~ | *(duplicate of BUG-010)* | Same root cause, same fix. v1 listed this as a separate High-severity bug — it isn't, it's one bug described twice. | — |
| **BUG-014** | `paper_trading.py::monitor_paper_trades` (trailing stop R-multiple) | Guard is `sl_ul != entry_und` — doesn't check *distance*. A near-zero SL gap (possible with degenerate ATR output) produces an extreme R-multiple and fires the trailing stop instantly. | Exact match. |
| **BUG-028** | `schema.py::init_db` migrations | `ALTER TABLE ADD COLUMN` has no `IF NOT EXISTS` equivalent in SQLite; code catches `OperationalError` and string-matches `"duplicate column"`. Real portability risk if SQLite ever changes this message, though stable historically. | Exact match. Correctly Medium, not Critical. |
| **BUG-029** | `schema.py::_calc_transaction_costs` | Futures STT hardcoded at 0.0002 (0.02%) for **all** futures. That rate is correct for NSE index futures — but MCX commodity futures (NATURALGAS, CRUDEOIL) are subject to CTT (Commodity Transaction Tax) at 0.01% (0.0001), not equity STT. Currently overestimating MCX futures transaction cost by 2x, which will make your paper P&L on MCX FUT trades look worse than live reality. | Confirmed via current CTT vs STT distinction; code has no MCX/NSE branch for this rate. |
| **BUG-011** | `paper_trading.py::_build_ml_feature_snapshot` | `float((tf_data.get("1h") or {}).get("rsi") or 0) or None` — RSI exactly `0.0` collapses to `None`. Real but near-zero practical impact (RSI=0.0 exactly is a rare mathematical edge case). | Exact match. Confirmed low real-world frequency. |
| **BUG-023** | `trade_plan.py::parse_verdict_and_confidence` | Regex requires single-asterisk wrap and capital `Confidence:` — brittle. Low risk in practice since it only parses NSEBOT's own generated `telegram_text`, not external input — but a template change elsewhere breaks this silently with no test coverage. | Exact match. Confirmed it only consumes internally-generated text. |

## ❌ FALSE POSITIVE — Do Not Act On

| ID | v1 Claim | Why It's Wrong |
|---|---|---|
| **BUG-002** | GTT OCO legs use same `transaction_type` as entry, will double position instead of closing. | v1 only read `place_kite_gtt()`'s body. Every call site already inverts the type *before* calling it (`"SELL" if plan["side"]=="BUY" else "BUY"`). Both OCO legs correctly share this already-inverted exit-side type — that's how an OCO SL/target pair is supposed to work. No bug. |
| **BUG-003** | `insert_live_trade()` returns 0 for both dedup skip and silent DB errors. | False. `0` is returned *only* on genuine `INSERT OR IGNORE` dedup. Real SQL errors propagate as exceptions through `get_conn()`'s `except: rollback(); raise` — nothing is silently masked. |
| **BUG-012** | SELL-side SL bound `max(sl_premium, entry_premium+0.05)` prevents SL from being set at a reasonable distance above entry. | Misreads `max()`. This is a **floor**, not a cap — it only prevents a degenerate near-zero SL distance from landing at/below entry. There is no upper bound in this code at all; the report's own suggested "fix" would *add* a cap that doesn't currently exist and isn't needed. |
| **BUG-022** | DB premium staleness check raises unhandled `TypeError` on naive datetimes, silently swallowed. | Two errors in one claim: (a) it's already caught and logged with a warning + fail-closed `None` return, not silent; (b) the premise doesn't occur — every `fetched_at` in this codebase is written via `datetime.isoformat()` on a tz-aware object, which always emits `+00:00`, never a naive string. The `.replace("Z",...)` is defensive-only and never actually triggered. |

## ⚠️ MISCHARACTERIZED — Real Issue, Wrong Description

| ID | v1 Said | What's Actually True |
|---|---|---|
| **BUG-004** | `run_live_timeframe_strategy()` is defined twice, one a dead stub. | Wrong function. There's exactly one `run_live_timeframe_strategy` (its own comment confirms it replaced an earlier stub already). What's actually duplicated is `_run_live_trading_legacy` (~400 lines) vs. `run_live_trading` — near-identical logic under different names. **Confirm `_run_live_trading_legacy` is unreferenced by `pipeline.py` before deleting** — I did not trace every call site for this. |
| **BUG-009** | Paper vs. live reversal guard differ due to positional-vs-keyword calling convention. | Calling convention is a non-issue (same param names, same order — Python doesn't care). The **real** difference: live's version wraps the entry-quality check in `if ctx and option_type and strike:` and silently skips Guard 2 if any are falsy; paper's version has no such conditional and always applies it. Live trading has a real, narrower reversal guard than paper — worth aligning, but not for the reason v1 gave. |
| **BUG-005** | `_CLEANUP_DATES` set grows unbounded, critical. | Real (never trims), but it's a set of ~8-char date strings, one new entry per calendar day, persisted to a small JSON file. This is a hygiene item, not remotely Critical — reclassifying as Low. |
| **BUG-008** | Next-expiry fetch doubles API calls every scan cycle for all symbols. | Only fires when `0 <= dte <= 2` for that specific symbol — not universal, not every cycle. Real minor extra load during expiry week only. Reclassifying Critical → Low. |
| **BUG-013** | Missing `return` after trade close causes always-re-entering after close. | The `return` **is present** in the primary path (`if closed_trade:` block returns correctly). Gap only exists in the narrow race where `monitor_paper_trades` closes something but the immediate follow-up query somehow returns no row — extremely unlikely in this single-process, synchronous execution flow. Real gap, overstated as High; more accurately Low. |
| **BUG-015** | Early-return `None` paths in `run_timeframe_strategy` break the digest. | Confirmed the bare `return`s exist (violates the `-> dict | None` intent). But `pipeline.py`'s dispatch already filters `if report and ...` and falls back safely — a `None` here doesn't crash or break the digest unless *all* active strategies return `None` in the same cycle, which is a normal "no signal this scan" state, not a bug. Downgrading High → Low/cosmetic. |
| **BUG-030** | `math.ceil` import is unused (dead code) per stale docstring. | `math` IS actively used in this file (`math.exp`, `math.sqrt` for time-decay weighting) — the import isn't dead. What's real: the docstring's historical "B7 fix: ... math.ceil" comment is stale, since the actual code now uses a plain float threshold (`total_weight * 0.70`), not `math.ceil`. Cosmetic doc-drift, not a functional bug. |

## Final Pass — Remaining 15 Now Verified

### Confirmed Real

| ID | Finding |
|---|---|
| **BUG-016** | `sync_direct_kite_positions()` sets `entry_underlying` from the latest stored `underlying_price` row (not time-matched to when the manual Kite position actually appeared) while `entry_premium` comes from the position's real `avg_price`. Two different time references baked into one trade record — P&L on adopted manual positions can be off. |
| **BUG-019** | Confirmed — `run_live_timeframe_strategy()` builds its own entry logic directly and never calls `make_trade_decision()`. Live timeframe trades produce no `decision_audit` row, unlike every other trade path. |
| **BUG-027** | Confirmed architecture characteristic — `get_conn()` opens a fresh `sqlite3.connect()` per call, no pooling. Real risk class (WAL allows concurrent reads, one writer) but this codebase's execution is mostly sequential per symbol — low practical likelihood today, worth hardening before any concurrency is added (e.g., multi-threaded symbol processing). |
| **BUG-032/033/034** | All confirmed exactly as v1 described. All cosmetic/minor: IP fallback text, DNS-only reachability check, fragile Zerodha error-string matching. None are trading-logic risks. |

### False Positive — Confirmed Wrong, Do Not Action

| ID | Why |
|---|---|
| **BUG-017** | `ctx.underlying` (PipelineContext) and `ctx.scan_context["underlying"]` are set from the identical variable in the same synchronous function call, same scan cycle — no time gap exists for staleness. No bug. |
| **BUG-018** | Misunderstands this codebase's convention: buying a PE option on a SHORT verdict is still a **BUY** transaction (you buy the put). `side` defaulting to `"BUY"` when unset is correct, not a bug — `"SELL"` is reserved for option-writing (FUT short only, today). |
| **BUG-024** | The `isinstance(ai_verdict, dict)` guard in the ternary already prevents the `.get()`-on-non-dict crash the report describes. Also moot in practice — `action` is a required (non-optional) Pydantic field on `LLMTradeVerdict`, Pydantic itself would reject a response missing it before this code ever runs. |
| **BUG-031** | Traced the actual consumer (`decision_pipeline.py::step_trend_alignment_core`): `regime_ok = (regime_sc >= MIN_REGIME_SCORE_CORE) or (PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE)` — the research-mode bypass is explicitly OR'd in downstream, independent of the raw score. The scenario v1 worried about (score=50 silently failing a 60-threshold check) does not occur. |
| **BUG-035, BUG-036** | Not bugs. 035 is a plain function alias with no wrapping needed. 036 is shadow mode working as designed — simulated trades don't need real Kite tradingsymbol resolution. |

### Not a Bug / Non-Issue

- **BUG-021** — code matches v1's description, but tolerance risk is theoretical only (strikes are clean numbers from the same API response on both sides of the comparison).
- **BUG-025** — wrapping `calculate_trade_lots`'s import in try/except would hide a real dependency failure rather than fix anything. Fail-fast on import is the correct behavior here, not a defect.
- **BUG-037** — `now.strftime("%H:%M")` always zero-pads; the failure mode v1 describes only exists if `market_window()`'s own config strings lack zero-padding elsewhere — did not trace that far, but the code shown is not itself the problem.
- **BUG-038** — `place_kite_order()` either returns a valid `order_id` or raises an exception (confirmed from full-file read) — a non-shadow-mode `None` order_id reaching `confirm_order_fill()` is not a reachable path in normal flow.

### Genuinely Unverified

- **BUG-026** (`router.py::_filter_atm_strikes` in-place mutation) — did not read `router.py` this pass. Only remaining unconfirmed item.

---

## Revised Scoreboard (38 items, 37 checked)

| Status | Count |
|---|---|
| Confirmed real, fix these | 14 |
| False positive / not a bug | 15 |
| Mischaracterized (real issue, wrong description) | 8 |
| Unverified | 1 (BUG-026) |

**~60% of v1's claims were wrong or overstated once traced against actual code.** The 14 confirmed items above are your real backlog.

---

## Fix Priority (Revised)

1. **BUG-001** — SELL/exit limit price direction. Real money, affects every closing trade.
2. **BUG-010** — Live trading risk engine shadowing. No daily loss cap, no cooldown, no circuit breaker on live trades right now.
3. **BUG-006** — Futures P&L sign logic. Harden `side` as sole determinant, drop the OR chain.
4. **BUG-029** — MCX futures STT/CTT rate split.
5. **BUG-007, BUG-014** — Both real, both moderate blast radius (noise suppression, premature trailing stop).
6. Everything in the "Not Verified" list — verify before touching, same discipline as above, or deprioritize below the confirmed list.

**Do not action BUG-002, 003, 012, or 022** — confirmed non-issues.
