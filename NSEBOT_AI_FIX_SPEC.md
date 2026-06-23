# NSEBOT AI Layer — Fix Specification
**Version:** 1.0 | **Date:** 22 Jun 2026 | **Priority order:** Critical → High → Medium

Covers: `llm_enrichment.py`, `trade_decision.py`, `paper_trading.py`, `digest.py`, `telegram_formatter.py`, `main.py`

---

## BUG MAP (quick reference)

| # | Severity | File | Root cause | Status |
|---|----------|------|-----------|--------|
| 1 | 🔴 Critical | `paper_trading.py` | `bias_val` read from old schema field, absent in `LLMTradeVerdict` | Fix §1 |
| 2 | 🔴 Critical | `main.py` | LLM can write arbitrary values to runtime config | Fix §2 |
| 3 | 🟡 High | `llm_enrichment.py` | Conflict rule produces hallucinated directions vs OI | Fix §3 |
| 4 | 🟡 High | `llm_enrichment.py` | `thesis`/`reason` not required to cite supplied numbers | Fix §4 |
| 5 | 🟡 High | `llm_enrichment.py` | Static macro context treated as today's signal | Fix §5 |
| 6 | 🟡 High | `llm_enrichment.py` | Confidence is free-choice integer, uncalibrated | Fix §6 |
| 7 | 🟡 High | `llm_enrichment.py` | Prompt/schema contradiction on `thesis` sentence count | Fix §7 |
| 8 | 🟠 Medium | `llm_enrichment.py` | Cache TTL 30 min, no DTE/premium-move invalidation | Fix §8 |
| 9 | 🟠 Medium | `llm_enrichment.py` | Naive `datetime.now()` in prompts; rest of system uses IST | Fix §9 |
| 10 | 🟠 Medium | `llm_enrichment.py` / `digest.py` | `instrument` free-text not validated; expiry/symbol wrong | Fix §10 |
| 11 | 🟠 Medium | `llm_enrichment.py` | No validator that `instrument` option type matches `action` | Fix §10 |
| 12 | 🟠 Medium | `llm_enrichment.py` | `risk_rating` HIGH triggers never fire; always MEDIUM | Fix §11 |
| 13 | 🟠 Medium | `digest.py` | Six competing message builders, dead/drifting code | Fix §12 |
| 14 | 🟠 Medium | `digest.py` | Chart conflict not surfaced when it directly contradicts AI verdict | Fix §13 |
| 15 | 🟠 Medium | `digest.py` | Null target renders as `Tgt —` on an "executed" trade; exit then claims "target hit" | Fix §14 |

---

## FIX §1 — `bias_val` dead field in `paper_trading.py` [🔴 Critical]

### Problem
`LLMTradeVerdict` (schema in `llm_enrichment.py`) has no `bias` field. `paper_trading.py` reads it at three sites (lines 565, 779, and the timeframe bias alignment block at 794):

```python
bias_val = ai_verdict.get("bias")          # ← always None
ai_bias = "NEUTRAL"                         # ← always falls through
if action == "GO_LONG":
    ai_bias = "BULLISH"
elif action == "GO_SHORT":
    ai_bias = "BEARISH"
elif bias_val:                              # ← dead branch
    ai_bias = str(bias_val).upper()
```

`trade_decision.py` already has a clean `_extract_ai_bias()` helper that handles this correctly. `paper_trading.py` does not use it — it re-implements the logic and adds the dead branch.

### Fix
**Remove the `bias_val` fallback in all three sites in `paper_trading.py`.**  
Import and reuse `_extract_ai_bias` from `trade_decision.py`.

```python
# paper_trading.py — top of file, add import
from src.engine.trade_decision import _extract_ai_bias

# ── REPLACE all three ai_verdict bias extraction blocks with: ──────────
if ai_verdict is not None:
    ai_bias = _extract_ai_bias(ai_verdict) or "NEUTRAL"
    ai_conf = float(
        ai_verdict.get("confidence", 50) if isinstance(ai_verdict, dict)
        else getattr(ai_verdict, "confidence", 50)
    )
    ai_risk = str(
        ai_verdict.get("risk_rating", "LOW") if isinstance(ai_verdict, dict)
        else getattr(ai_verdict, "risk_rating", "LOW")
    ).upper()
```

**Affected sites in `paper_trading.py`:**
- Timeframe reversal exit block (~line 562)
- Main timeframe entry bias check (~line 776)
- LONG/SHORT exit_advice SL parse block (~line 866 / 929)

### Test
After fix: `ai_verdict.action == "GO_LONG"` → `ai_bias == "BULLISH"` in all three sites. Verify with a unit test passing a mock `LLMTradeVerdict(action="GO_SHORT", confidence=75, ...)`.

---

## FIX §2 — Unvalidated LLM config writes in `main.py` [🔴 Critical]

### Problem
`POST /api/config/update` calls `save_runtime_config(payload.changes)` directly. `get_strategy_optimization_advice` tells the model to "ensure values within bounds" — but that is a request to the LLM, not server-side enforcement. A hallucinated or malicious payload can:
- Set `live_min_confidence_core = 0` → trade on any signal
- Set `live_max_concurrent_positions = 99`
- Set `live_ai_decision_mode = "full"` without operator consent

### Fix
Add a **server-side allowlist with numeric clamps** before `save_runtime_config`.

```python
# main.py — add before save_runtime_config call

_CONFIG_SCHEMA: dict[str, tuple] = {
    # key: (type, min, max) or (type, allowed_values)
    "live_min_confidence_core":       (int,   40,  95),
    "live_max_concurrent_positions":  (int,    1,   5),
    "live_ai_decision_mode":          (str,  {"advisory", "boost_only", "full"}),
    "live_ai_min_confidence_boost":   (int,   60, 100),
    "live_ai_min_confidence_veto":    (int,   70, 100),
    "live_capital_per_trade_inr":     (float, 5000, 500000),
}

def _validate_config_changes(changes: dict) -> dict:
    """Strip unknown keys; clamp/cast known ones. Raises ValueError on type failure."""
    validated = {}
    for k, v in changes.items():
        if k not in _CONFIG_SCHEMA:
            log.warning("Config update: unknown key '%s' rejected", k)
            continue
        schema = _CONFIG_SCHEMA[k]
        typ = schema[0]
        try:
            cast_v = typ(v)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Config key '{k}': cannot cast {v!r} to {typ}: {e}")
        if isinstance(schema[1], set):          # allowed string values
            if cast_v not in schema[1]:
                raise ValueError(f"Config key '{k}': value '{cast_v}' not in {schema[1]}")
        else:                                    # numeric range
            lo, hi = schema[1], schema[2]
            if not (lo <= cast_v <= hi):
                raise ValueError(f"Config key '{k}': {cast_v} outside [{lo}, {hi}]")
        validated[k] = cast_v
    return validated

# In update_bot_config():
validated = _validate_config_changes(payload.changes)
save_runtime_config(validated)
```

---

## FIX §3 — Conflict resolution rule hallucinates direction [🟡 High]

### Problem
Prompt line 452: *"Pick stronger signal on conflict."*  
This let the CRUDEOIL scan (`Short Buildup` CE, `Bearish Flow`, all-bearish OI, both candles bearish) produce `GO_LONG 70%` citing invented "rising call OI and put unwind." The model resolved ambiguity toward macro/news and discarded hard OI data.

### Fix
**Replace the vague conflict rule with explicit precedence and a contradiction block.**

In `_build_deep_prompt()`, replace the last line of the prompt:
```python
# OLD:
"RULES: Reference actual levels from data. If NO_TRADE, fill what WOULD trigger. Pick stronger signal on conflict."

# NEW:
"""RULES:
1. Use ONLY levels from the DATA section above for all numeric fields.
2. Evidence hierarchy (highest to lowest):
   a. OI Δ direction + price action (non-negotiable — do not override)
   b. Chart candle sentiment (1H/3H)
   c. News headline direction
   d. Macro/seasonal context (background only — never overrides live data)
3. If OI Δ and candles CONFLICT: set action=NO_TRADE OR risk_rating=HIGH. Do NOT assert a direction that contradicts the sign of Net OI Δ (CE OI minus PE OI).
4. thesis and invalidation MUST cite at least one exact number from DATA (e.g., PCR value, OI Δ, underlying level).
5. If NO_TRADE: fill instrument/entry_trigger with what WOULD change the view."""
```

---

## FIX §4 — Thesis/reason does not reference supplied data [🟡 High]

### Problem
The model generates narratives ("rising call OI and put unwind") that contradict the OI numbers it was given. Nothing in the prompt forces citation.

### Fix
Already captured in Fix §3 Rule 4. Additionally, add explicit instruction in the `thesis` field description in `LLMTradeVerdict`:

```python
# llm_enrichment.py — LLMTradeVerdict
thesis: str = Field(
    description=(
        "One sentence why this trade works NOW. "
        "MUST reference at least one number from the data (PCR, OI Δ, underlying level, or candle direction). "
        "Example: 'PCR at 1.39 with PE OI building +42 confirms put writing support at 310.'"
    )
)
```

---

## FIX §5 — Static macro context treated as live signal [🟡 High]

### Problem
`_format_macro_context()` injects "Jun–Aug = structurally bearish bias" (NATURALGAS) and "summer driving season supports demand" (CRUDEOIL) as unqualified statements. The model reads these as current directional signals and can override live OI data with seasonal narrative. This is the likely root cause of the CRUDEOIL `GO_LONG` at -2.11% with all-bearish OI.

### Fix
Prefix every macro section with a background disclaimer and add an explicit override prohibition:

```python
# llm_enrichment.py — _format_macro_context()
# Add this prefix to ALL sections (replace the return statements):

_MACRO_PREFIX = (
    "  ⚠️ BACKGROUND CONTEXT ONLY — not a directional signal for today.\n"
    "  Do NOT use seasonality or macro narrative to override live OI Δ or price action.\n"
)

# Example for NATURALGAS:
return _MACRO_PREFIX + """  Symbol type: MCX Natural Gas Futures (USD-denominated, INR-settled)
  ...
  Seasonality: Jun-Aug = low demand — structural tendency only; trade live OI, not the season."""
```

Apply `_MACRO_PREFIX` to all five symbols (NATURALGAS, CRUDEOIL, GOLD, SILVER, BANKNIFTY, NIFTY) and the generic fallback.

---

## FIX §6 — Confidence is uncalibrated free-choice integer [🟡 High]

### Problem
The model emits 45% confidence on NATURALGAS (`GO_SHORT`) and 70% on CRUDEOIL (`GO_LONG` with contradicting data). Nothing derives this from evidence agreement.

### Fix
Replace the `confidence` field description with a forced derivation rule:

```python
# llm_enrichment.py — LLMTradeVerdict
confidence: int = Field(
    description=(
        "Confidence 0-100. DERIVE from evidence agreement — do not guess:\n"
        "  Count how many of these 4 agree with your action:\n"
        "    (1) Net OI Δ direction (CE vs PE change)\n"
        "    (2) Price action (underlying trend)\n"
        "    (3) Chart candle sentiment (1H/3H)\n"
        "    (4) News/macro direction\n"
        "  4/4 agree → 85-95 | 3/4 → 65-80 | 2/4 → 45-60 | ≤1/4 → 20-40.\n"
        "  If action=NO_TRADE, set confidence to how strongly NO_TRADE is supported (same scale)."
    )
)
```

---

## FIX §7 — `thesis` field sentence count contradiction [🟡 High]

### Problem
`LLMTradeVerdict.thesis` description says "1-sentence" (line 55). Prompt RULES say "Two sentences why NOW" (line 447). Model behaviour is undefined.

### Fix
Pick one. Recommended: **1 sentence** (forces precision, reduces token waste). Update both locations:

```python
# llm_enrichment.py — LLMTradeVerdict (already updated in Fix §4)
# Also update _build_deep_prompt():
# OLD: "• thesis: Two sentences why NOW"
# NEW: "• thesis: One sentence why NOW — must cite a data number"
```

---

## FIX §8 — Cache TTL ignores DTE and premium velocity [🟠 Medium]

### Problem
Verdict cache TTL is 30 minutes (line 910), invalidated only if underlying moves >0.2%. At 1–2 DTE, option gamma is extreme — premium can move 50%+ on a 0.1% underlying move. A 30-minute-old `GO_LONG` at 1 DTE is stale within one scan cycle.

### Fix
Add DTE-aware TTL and premium-move invalidation:

```python
# llm_enrichment.py — get_llm_verdict()

def _verdict_cache_ttl(scan_context: dict) -> float:
    """Return cache TTL in seconds, shorter near expiry."""
    dte = int(scan_context.get("days_to_expiry") or 7)
    if dte <= 1:   return 300.0    # 5 min at expiry day
    if dte <= 3:   return 600.0    # 10 min within 3 DTE
    return 1800.0                  # 30 min otherwise

# In the cache validity check, replace the TTL line:
ttl = _verdict_cache_ttl(scan_context)
if time_elapsed < ttl and price_moved_pct < 0.002 and verdict_same and confidence_same:
    ...
```

Additionally add premium-move check:
```python
# After price_moved_pct check, add:
entry_prem = float(scan_context.get("entry_premium") or 0)
cached_prem = float(cached.get("entry_premium") or 0)
prem_moved_pct = (abs(entry_prem - cached_prem) / cached_prem) if cached_prem > 0 else 0
if prem_moved_pct > 0.10:  # >10% premium move → stale
    pass  # fall through to fresh LLM call
```

Store `entry_premium` in the cache dict alongside `underlying`.

---

## FIX §9 — Naive `datetime.now()` in prompts [🟠 Medium]

### Problem
`_build_deep_prompt()` (line 413) and `_build_exit_prompt()` (line 468) use `datetime.now().strftime(...)` — naive local time. Every other time reference in the system uses IST. This makes the timestamp in the prompt wrong for production deployment (likely UTC or whatever the server TZ is).

### Fix
```python
# llm_enrichment.py — add at module top
import pytz
_IST = pytz.timezone("Asia/Kolkata")

# Replace both datetime.now() calls:
# OLD:  datetime.now().strftime("%a %H:%M")
# NEW:  datetime.now(_IST).strftime("%a %H:%M IST")
```

---

## FIX §10 — LLM `instrument` free-text is unvalidated [🟠 Medium]

### Problem
`instrument` is a free-text field (e.g. `"NIFTY 24500 CE 27Jun"`). Two observed failures:
1. BANKNIFTY scan → `instrument` said `"NIFTY 57900 PE"` (wrong symbol)
2. NG header `23 Jun / 1 DTE` → `instrument` said `27Jun` (wrong expiry)

Additionally, there is no check that `instrument` option type is consistent with `action` (e.g. `GO_LONG + PE` would silently pass).

### Fix
**Override symbol and expiry from the scan context; validate action/option-type consistency.**

```python
# llm_enrichment.py — after _call_llm_api returns result in get_llm_verdict()

def _sanitize_llm_verdict(
    result: LLMTradeVerdict,
    symbol: str,
    scan_context: dict,
) -> LLMTradeVerdict:
    """
    Post-process LLM verdict:
    1. Override symbol name in instrument field with the actual scanned symbol.
    2. Override expiry in instrument with the scan's expiry (nearest valid).
    3. Validate action/option-type consistency; downgrade to NO_TRADE on mismatch.
    """
    if result is None:
        return result

    # 1. Replace symbol in instrument string
    instr = result.instrument or ""
    # Strip any leading index name and replace with correct symbol
    for known in ("BANKNIFTY", "NIFTY", "NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"):
        if instr.upper().startswith(known) and known != symbol.upper():
            instr = symbol.upper() + instr[len(known):]
            break

    # 2. Replace expiry token — find "DDMon" pattern and replace
    scan_expiry = scan_context.get("expiry") or ""  # "YYYY-MM-DD"
    if scan_expiry:
        try:
            from datetime import datetime as _dt
            exp_dt = _dt.strptime(scan_expiry, "%Y-%m-%d")
            exp_token = exp_dt.strftime("%-d%b")  # e.g. "23Jun"
            import re
            instr = re.sub(r"\d{1,2}[A-Za-z]{3}", exp_token, instr)
        except Exception:
            pass

    # 3. Action / option-type consistency check
    action = result.action or "NO_TRADE"
    opt_type = ""
    if "CE" in instr.upper():
        opt_type = "CE"
    elif "PE" in instr.upper():
        opt_type = "PE"
    elif "FUT" in instr.upper():
        opt_type = "FUT"

    invalid_combo = (
        (action == "GO_LONG"  and opt_type == "PE") or
        (action == "GO_SHORT" and opt_type == "CE")
    )
    if invalid_combo:
        log.warning(
            "[llm] %s: instrument/action mismatch — action=%s but instrument=%s. "
            "Downgrading to NO_TRADE.",
            symbol, action, instr,
        )
        # Return a modified copy (Pydantic model_copy or dict rebuild)
        result = result.model_copy(update={"action": "NO_TRADE", "instrument": instr})
    else:
        result = result.model_copy(update={"instrument": instr})

    return result

# In get_llm_verdict(), after result = _call_llm_api(...):
if result:
    result = _sanitize_llm_verdict(result, symbol, scan_context)
    _VERDICT_CACHE[symbol] = { ... }  # cache the sanitized result
```

---

## FIX §11 — `risk_rating` HIGH triggers never fire [🟠 Medium]

### Problem
The prompt says "HIGH if macro event <2h, <2 DTE, or chart conflict" (line 449) but this is advisory text to the model. The model consistently returns "MEDIUM." The prompt gives no structured data about upcoming macro events so the model cannot evaluate the first condition.

### Fix
Two changes:

**A. Add structured macro-event proximity to the prompt data section:**
```python
# llm_enrichment.py — in _build_deep_prompt(), add to DATA section:
dte = scan_context.get("days_to_expiry", 99)
chart_conflict = intel.get("chart_conflict", False)

risk_flags = []
if int(dte) <= 2:
    risk_flags.append(f"EXPIRY IMMINENT ({dte} DTE)")
if chart_conflict:
    risk_flags.append("CHART CONFLICT (1H/3H disagree)")
# Add macro event proximity here when news_data is available
if news_data:
    high_impact = [i for i in (news_data.get("items") or []) if abs(i.get("score", 0)) >= 3]
    if high_impact:
        risk_flags.append(f"HIGH-IMPACT NEWS ACTIVE ({len(high_impact)} articles)")

prompt += f"RISK FLAGS: {', '.join(risk_flags) or 'None'}\n"
```

**B. Tighten the risk_rating instruction:**
```python
# In OUTPUT FIELDS:
# OLD: "• risk_rating: LOW | MEDIUM | HIGH (HIGH if macro event <2h, <2 DTE, or chart conflict)"
# NEW:
"• risk_rating: LOW | MEDIUM | HIGH.\n"
"  Set HIGH if ANY of these appear in RISK FLAGS above: EXPIRY IMMINENT, CHART CONFLICT, HIGH-IMPACT NEWS.\n"
"  Set MEDIUM for moderate uncertainty (mixed signals, low PCR trend data).\n"
"  Set LOW only when OI, price, chart all agree and no RISK FLAGS."
```

---

## FIX §12 — Six competing message builders [🟠 Medium]

### Problem
`digest.py` has at minimum three builders (`build_digest` legacy, `build_llm_consolidated_digest` premium, and the no-alerts branch within each). `telegram_formatter.py` adds three more (`format_user_friendly_message`, `format_compact_message`, `format_detailed_message`). Production uses `build_llm_consolidated_digest` (via `build_digest_wrapper`). The other five are dead code that will drift.

### Fix
**Canonical builder: `build_llm_consolidated_digest` in `digest.py`.**

1. Add a module-level comment to `telegram_formatter.py`:
   ```python
   # DEPRECATED — production uses digest.build_llm_consolidated_digest.
   # These functions are retained only for offline testing/demo.
   # Do not add features here.
   ```
2. Delete or `#noqa: F401`-mark all three functions in `telegram_formatter.py` in the next cleanup sprint.
3. In `digest.py`, remove or guard the fallback `build_digest` (legacy path at line ~484) behind an explicit `_LEGACY_DIGEST = False` flag so it can't be accidentally re-activated.

---

## FIX §13 — Chart conflict not surfaced in alert when it contradicts AI verdict [🟠 Medium]

### Problem
`_conflict_tag()` in `digest.py` (line 1571) generates a `⚠️ Chart conflict` tag but it appends it to `verdict_line` (line 1717) only when `conflict_tag` is non-empty. However, `_conflict_tag` checks the *AI verdict direction* (`bias_upper`) vs candles.

In the NG case: AI was `GO_SHORT` but candles were `1H BULLISH / 3H BULLISH`. `_conflict_tag` should have fired but the SHADOW execution line appeared **before** the AI section in the alert — the user sees the execution without the warning.

### Fix
1. Move the conflict warning **above** the execution block in `build_llm_consolidated_digest`, not appended to the verdict line:

```python
# After SECTION 2 header, before bot action block:
if conflict_tag:
    lines.append(f"⚠️ *WARNING:* AI direction conflicts with chart candles (1H {c1} | 3H {c3}) — treat this setup with extra caution")
```

2. Also flag it in the Paper/Live execution line when a trade was entered despite conflict:

```python
# In _render() inside _bot_action_block():
if action == "EXECUTED" and conflict_tag:
    return f"✅ {label} ENTERED ⚠️ (chart conflict): {side} {instrument} {' | '.join(parts)}"
```

---

## FIX §14 — Null target on executed trade allows phantom "target hit" exit [🟠 Medium]

### Problem
`build_paper_trade_plan` can return `target_underlying = None` when `_near_level` returns None and the fallback also fails. `_bot_action_block._render()` renders this as `Tgt —`. The CMP poll exit monitor then compares `current_premium > target_premium` — but if `target_premium` was persisted as `NULL` in `paper_trades.target_premium`, SQLite comparisons with NULL always return false *except* when the comparison is done in Python where `None` may compare as `<` any float depending on implementation. This produced the phantom "target hit" on the NG trade.

### Fix in two parts:

**A. Reject plans with null target in `paper_trading.py`:**
```python
# In run_paper_trading() / run_timeframe_strategy(), after build_paper_trade_plan():
if plan is None:
    return {"action": "BLOCKED_PLAN", "reason": "No valid trade plan"}

# Add null target guard:
if plan.get("target_underlying") is None and plan.get("option_type") != "FUT":
    log.warning("%s: plan has null target_underlying — rejecting to prevent phantom exit", symbol)
    return {"action": "BLOCKED_PLAN", "reason": "Null target — plan incomplete"}
```

**B. Guard the CMP poll exit comparison:**
```python
# Wherever CMP poll exit checks target:
target_prem = trade.get("target_premium")
if target_prem is None:
    log.debug("%s: CMP poll — skipping target-hit check, target_premium is NULL", symbol)
else:
    if current_premium >= target_prem:
        # close trade as target hit
        ...
```

---

## Summary: files to change

| File | Fixes | Estimated lines changed |
|------|-------|------------------------|
| `llm_enrichment.py` | §3 §4 §5 §6 §7 §8 §9 §10 §11 | ~120 |
| `paper_trading.py` | §1 §14A §14B | ~40 |
| `main.py` | §2 | ~45 |
| `digest.py` | §12 §13 §14B | ~25 |
| `telegram_formatter.py` | §12 | ~5 (comments) |

**Implementation order:**
1. §1 (broken bias field — affecting live execution now)
2. §2 (security — unchecked config writes)
3. §3 + §5 (bad AI calls — high daily P&L impact)
4. §14 (phantom exits)
5. §6 + §4 + §7 (prompt calibration)
6. §8 + §9 + §10 + §11 (hardening)
7. §12 + §13 (cleanup)

---

## Regression tests to add

```python
# test_ai_bias.py
from src.engine.trade_decision import _extract_ai_bias
from src.engine.llm_enrichment import LLMTradeVerdict

def test_go_long_maps_to_bullish():
    v = LLMTradeVerdict(action="GO_LONG", confidence=75, ...)
    assert _extract_ai_bias(v) == "BULLISH"

def test_go_short_maps_to_bearish():
    v = LLMTradeVerdict(action="GO_SHORT", confidence=70, ...)
    assert _extract_ai_bias(v) == "BEARISH"

def test_no_trade_maps_to_neutral():
    v = LLMTradeVerdict(action="NO_TRADE", confidence=50, ...)
    assert _extract_ai_bias(v) == "NEUTRAL"

# test_instrument_sanitize.py
def test_wrong_symbol_corrected():
    v = LLMTradeVerdict(action="GO_SHORT", instrument="NIFTY 57900 PE 27Jun", ...)
    sanitized = _sanitize_llm_verdict(v, "BANKNIFTY", {"expiry": "2026-06-27"})
    assert sanitized.instrument.startswith("BANKNIFTY")

def test_go_long_pe_downgraded():
    v = LLMTradeVerdict(action="GO_LONG", instrument="NIFTY 24500 PE 27Jun", ...)
    sanitized = _sanitize_llm_verdict(v, "NIFTY", {})
    assert sanitized.action == "NO_TRADE"

# test_config_validation.py
def test_unknown_key_rejected():
    with pytest.raises(KeyError):
        _validate_config_changes({"rogue_key": 999})

def test_out_of_range_clamped():
    with pytest.raises(ValueError):
        _validate_config_changes({"live_min_confidence_core": 5})  # below min 40

# test_null_target.py
def test_null_target_blocks_execution(mock_plan):
    mock_plan["target_underlying"] = None
    result = run_paper_trading(...)
    assert result["action"] == "BLOCKED_PLAN"
    assert "Null target" in result["reason"]
```
