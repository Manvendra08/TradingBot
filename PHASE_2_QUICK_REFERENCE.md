# Phase 2 Quick Reference

## Module Overview

### 1. Verdict Sets
**File:** `src/engine/verdict_sets.py`  
**Purpose:** Single source of truth for verdict classification

```python
from src.engine.verdict_sets import is_bullish, is_bearish

is_bullish("Long Buildup")  # True
is_bearish("Call Writing")  # True
```

---

### 2. Regime Detector
**File:** `src/engine/regime_detector.py`  
**Purpose:** Classify market state from last 10 scans

```python
from src.engine.regime_detector import detect_market_regime, regime_score_for_trade

regime = detect_market_regime("NIFTY")
# Returns: TRENDING_UP | TRENDING_DOWN | RANGE | VOLATILE | NO_TRADE

score = regime_score_for_trade(regime, "CE")
# Returns: 0-100 (how favorable for this option type)
```

---

### 3. Entry Quality Scorer
**File:** `src/engine/entry_quality.py`  
**Purpose:** Validate trade entry location and timing

```python
from src.engine.entry_quality import calculate_entry_quality

score, reasons = calculate_entry_quality(
    symbol="NIFTY",
    option_type="CE",
    strike=24000,
    ctx={
        "underlying": 24000,
        "support": 23950,
        "resistance": 24050,
        "sl_underlying": 23900,
        "target_underlying": 24100,
        "option_rows": [...],
        "price_change_pct": 0.5,
    }
)
# Returns: (score: 0-100, reasons: list[str])
```

---

### 4. Trend Analysis
**File:** `src/engine/trend_analysis.py`  
**Purpose:** Detect reversals and trend alignment

```python
from src.engine.trend_analysis import (
    detect_reversal_from_scans,
    get_trend_alignment_score
)

# Reversal detection
is_rev, reason = detect_reversal_from_scans("NIFTY", "Long Buildup", 80)
# Returns: (bool, str)

# Trend alignment
score = get_trend_alignment_score("NIFTY", "Long Buildup")
# Returns: 0-100 (% of last 5 scans agreeing)
```

---

### 5. Risk Engine
**File:** `src/engine/risk_engine.py`  
**Purpose:** Enforce trade frequency controls

```python
from src.engine.risk_engine import check_risk_limits

allowed, reason = check_risk_limits("NIFTY")
# Returns: (bool, str)
# Checks: max open trades, daily loss cap, cooldown, etc.
```

---

### 6. Trade Decision Engine
**File:** `src/engine/trade_decision.py`  
**Purpose:** Combine all layers into final decision

```python
from src.engine.trade_decision import make_trade_decision

decision = make_trade_decision(
    symbol="NIFTY",
    intel={
        "verdict_label": "Long Buildup",
        "confidence": 75,
        "chart_conflict": False,
    },
    ctx={
        "underlying": 24000,
        "support": 23950,
        "resistance": 24050,
        # ... other context
    }
)

# Returns:
# {
#     "status": "TRIGGERED_CORE" | "TRIGGERED_EXPERIMENTAL" | "BLOCKED",
#     "setup_type": "CONFIRMED_REVERSAL" | "TREND_CONTINUATION" | "EXPERIMENTAL_SETUP",
#     "reason": str,
#     "soft_conflicts": list[str],
#     "scores": {
#         "confidence": int,
#         "entry_quality": int,
#         "trend_alignment": int,
#         "regime_score": int,
#     }
# }
```

---

### 7. Scan Summary Engine
**File:** `src/engine/scan_summary.py`  
**Purpose:** Persist one row per scan for trend analysis

```python
from src.engine.scan_summary import save_scan_summary

save_scan_summary(
    symbol="NIFTY",
    scan_context={...},
    alerts=[...],
    intel={
        "verdict_label": "Long Buildup",
        "confidence": 75,
        "chart_conflict": False,
    },
    digest_id="digest_123",
    fetched_at="2026-05-28T09:15:00Z"
)
```

---

## Integration Points

### Pipeline Integration
```python
# src/engine/pipeline.py

from src.engine.intelligence import generate_intelligence_structured
from src.engine.scan_summary import save_scan_summary
from src.engine.paper_trading import run_paper_trading

# 1. Generate structured intelligence
intel = generate_intelligence_structured(symbol, new_alerts, scan_context=scan_context)

# 2. Save scan summary
save_scan_summary(symbol, scan_context, new_alerts, intel, digest_id, fetched_at)

# 3. Run paper trading
run_paper_trading(symbol, scan_context, digest_id, intel)
```

### Paper Trading Integration
```python
# src/engine/paper_trading.py

from src.engine.risk_engine import check_risk_limits
from src.engine.trade_decision import make_trade_decision

# 1. Check risk limits
risk_ok, risk_reason = check_risk_limits(symbol)
if not risk_ok:
    log.info("%s: blocked by risk engine — %s", symbol, risk_reason)
    return

# 2. Make trade decision
decision = make_trade_decision(symbol, intel, ctx)
if decision["status"] == "BLOCKED":
    log.info("%s: blocked — %s", symbol, decision["reason"])
    return

# 3. Execute trade with decision metadata
insert_paper_trade({
    **plan,
    "trade_status": decision["status"],
    "setup_type": decision["setup_type"],
    "decision_reason": decision["reason"],
    "confidence_score": decision["scores"]["confidence"],
    "entry_quality_score": decision["scores"]["entry_quality"],
    "trend_alignment_score": decision["scores"]["trend_alignment"],
    "regime_score": decision["scores"]["regime_score"],
})
```

---

## Configuration

### Risk Engine Settings
```python
# config/settings.py

MAX_OPEN_TRADES_PER_SYMBOL   = 1      # max 1 open trade per symbol
MAX_OPEN_TRADES_TOTAL        = 4      # max 4 open trades total
MAX_TRADES_PER_SYMBOL_PER_DAY = 2     # max 2 trades per symbol per day
MAX_DAILY_LOSS_RUPEES        = 10000  # stop trading if loss > ₹10k
LOSS_COOLDOWN_MINUTES        = 30     # wait 30 min after SL/loss
```

### Decision Thresholds
```python
# config/settings.py

MIN_CONFIDENCE_CORE          = 70     # core trade confidence threshold
MIN_CONFIDENCE_EXPERIMENTAL = 50      # experimental trade threshold
MIN_ENTRY_QUALITY_CORE      = 60      # core entry quality threshold
MIN_ENTRY_QUALITY_EXPERIMENTAL = 40   # experimental entry quality threshold
MIN_TREND_ALIGNMENT_CORE    = 70      # trend alignment threshold
MIN_REGIME_SCORE_CORE       = 60      # regime score threshold
REVERSAL_MIN_CONFIDENCE     = 75      # reversal confidence threshold
```

---

## Decision Logic

### Priority 1: Confirmed Reversal
**Criteria:**
- Confidence ≥ 75%
- Broader trend (scans 3-10) opposite to current verdict
- Last 2 scans confirm new direction
- Entry quality ≥ 60

**Result:** `TRIGGERED_CORE` with `CONFIRMED_REVERSAL` setup

---

### Priority 2: Trend Continuation
**Criteria:**
- Confidence ≥ 70%
- Trend alignment ≥ 70%
- Entry quality ≥ 60%
- Regime score ≥ 60%

**Result:** `TRIGGERED_CORE` with `TREND_CONTINUATION` setup

---

### Priority 3: Experimental (Research Mode Only)
**Criteria:**
- Confidence ≥ 50%
- Entry quality ≥ 40%
- PAPER_RESEARCH_MODE = True

**Result:** `TRIGGERED_EXPERIMENTAL` with `EXPERIMENTAL_SETUP`

---

### Blocked
**Reasons:**
- Confidence < 50%
- Entry quality < 40%
- Verdict not directional
- Missing underlying price
- Risk limits exceeded

**Result:** `BLOCKED` with reason

---

## Database Queries

### Get Last 10 Scans
```python
from src.models.schema import get_conn

with get_conn() as conn:
    rows = conn.execute("""
        SELECT verdict_label, confidence, underlying
        FROM scan_summaries
        WHERE symbol = ?
        ORDER BY fetched_at DESC
        LIMIT 10
    """, (symbol,)).fetchall()
```

### Get Trend Alignment
```python
with get_conn() as conn:
    rows = conn.execute("""
        SELECT verdict_label FROM scan_summaries
        WHERE symbol = ?
        ORDER BY fetched_at DESC
        LIMIT 5
    """, (symbol,)).fetchall()
```

### Check Risk Limits
```python
with get_conn() as conn:
    # Open trades per symbol
    open_sym = conn.execute(
        "SELECT COUNT(*) AS c FROM paper_trades WHERE symbol=? AND status='OPEN'",
        (symbol,)
    ).fetchone()["c"]
    
    # Daily loss
    today_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl_rupees), 0) AS total FROM paper_trades WHERE closed_at >= ?",
        (today_start,)
    ).fetchone()["total"]
```

---

## Testing

### Run Regression Tests
```bash
python -m pytest tests/test_phase2_regression.py -v
```

### Expected Output
```
17 passed in 11.35s
```

---

## Troubleshooting

### "Insufficient scan history"
- **Cause:** Less than 5 scans in database
- **Fix:** Wait for more scans to accumulate
- **In Research Mode:** Tagged as EXPERIMENTAL, not blocked

### "Max open trades per symbol"
- **Cause:** Already have 1 open trade for this symbol
- **Fix:** Close existing trade or wait for it to close
- **Config:** Adjust MAX_OPEN_TRADES_PER_SYMBOL

### "Daily loss limit hit"
- **Cause:** Lost more than ₹10,000 today
- **Fix:** Wait until next day or adjust MAX_DAILY_LOSS_RUPEES
- **Cooldown:** Also applies 30-min cooldown after each loss

### "Cooldown active after loss"
- **Cause:** Lost a trade, waiting for cooldown
- **Fix:** Wait 30 minutes before next trade
- **Config:** Adjust LOSS_COOLDOWN_MINUTES

---

## Performance Tips

1. **Batch Queries:** Use `get_conn()` context manager
2. **Index Usage:** Queries use `idx_scan_summaries_symbol_time`
3. **Caching:** Consider caching regime/trend scores for 5 min
4. **Async:** Can parallelize decision engine for multiple symbols

---

## Common Patterns

### Check if Trade Should Be Triggered
```python
# 1. Check risk limits
risk_ok, _ = check_risk_limits(symbol)
if not risk_ok:
    return

# 2. Make decision
decision = make_trade_decision(symbol, intel, ctx)
if decision["status"] == "BLOCKED":
    return

# 3. Execute
execute_trade(...)
```

### Log Decision Metadata
```python
log.info(
    "%s: %s | %s | conf=%d eq=%d ta=%d regime=%d",
    symbol,
    decision["status"],
    decision["setup_type"],
    decision["scores"]["confidence"],
    decision["scores"]["entry_quality"],
    decision["scores"]["trend_alignment"],
    decision["scores"]["regime_score"],
)
```

### Display to User
```python
msg = f"""
Trade Decision: {decision['status']}
Setup: {decision['setup_type']}
Reason: {decision['reason']}

Scores:
- Confidence: {decision['scores']['confidence']}/100
- Entry Quality: {decision['scores']['entry_quality']}/100
- Trend Alignment: {decision['scores']['trend_alignment']}/100
- Regime: {decision['scores']['regime_score']}/100
"""
```

---

## Files to Update

### Immediate
- [ ] `src/engine/pipeline.py` — Add Phase 2 integration
- [ ] `src/engine/paper_trading.py` — Use decision engine

### Short Term
- [ ] `src/engine/intelligence.py` — Phase 3 refactor
- [ ] `tests/` — Add unit tests for each module

### Medium Term
- [ ] `src/engine/trend_analysis.py` — Advanced multi-scan logic
- [ ] `src/dashboard/app.py` — Display decision metadata

