# Trading System V2.2 — Implementation Plan
## Incorporating GPT-5.5 Feedback (Rounds 1 + 2)

> **Changelog v2.2:** Risk engine moved to Phase 2. Fixed 6 bugs identified in v2.1 review.
> See BUGS_FIXED section for details.

---

## Architecture (7 Layers)

```
SCAN PIPELINE (existing)
        │
        ▼
LAYER 1: Scan Summary Engine        ← saves one row per scan (not per alert)
        │
        ▼
LAYER 2: Trend Context Engine       ← last 3/5/10 SCANS → trend_bias, regime
        │
        ▼
LAYER 3: Signal Classification      ← current scan verdict + confidence
        │
        ▼
LAYER 4: Entry Quality Engine       ← price location, premium, spread, R:R
        │
        ▼
LAYER 5: Trade Decision Engine      ← TRIGGERED_CORE / EXPERIMENTAL / BLOCKED
        │
        ▼
LAYER 6: Risk Engine (Phase 2)      ← frequency limits, cooldown, loss cap
        │
        ▼
LAYER 7: Paper Research Engine      ← execute + tag + measure
```

---

## Bugs Fixed in v2.2

| # | Bug | Fix |
|---|-----|-----|
| B1 | Risk engine in Phase 4 — too late | Moved to Phase 2 |
| B2 | Regime detector price direction inverted | `prices = list(reversed(prices))` |
| B3 | `classify_oi_direction()` missing `ltp_pct` | Use `BUILDUP_CLASSIFY` alerts only |
| B4 | Verdict text matching too loose (`"Bullish" in label`) | Use explicit set membership |
| B5 | Hard block on insufficient regime history | Tag EXPERIMENTAL, don't block |
| B6 | Entry quality silently skips R:R check | Add explicit validation + logging |
| B7 | Regex parsing of intelligence text is fragile | Structured intelligence object (Phase 3 refactor) |

---

## Shared Constants (used across all layers)

**File:** `src/engine/verdict_sets.py` (NEW — fixes B4)

```python
"""Shared verdict classification sets. Single source of truth."""

BULLISH_VERDICTS = frozenset({
    "Long Buildup",
    "Put Writing",
    "OI Bias Bullish",
    "Short Covering",
})

BEARISH_VERDICTS = frozenset({
    "Short Buildup",
    "Call Writing",
    "OI Bias Bearish",
    "Long Unwinding",
})

NEUTRAL_VERDICTS = frozenset({
    "Sideways",
    "Volatility Expansion",
    "Volatility Contraction",
})


def is_bullish(verdict: str) -> bool:
    return str(verdict or "").strip() in BULLISH_VERDICTS


def is_bearish(verdict: str) -> bool:
    return str(verdict or "").strip() in BEARISH_VERDICTS
```

**Update `paper_plan.py` to import from here instead of defining its own sets.**


---

## Phase 1: Foundation (Week 1)

### 1.1 Scan Summaries Table

**File:** `src/models/schema.py` — add to DDL

```sql
CREATE TABLE IF NOT EXISTS scan_summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    expiry          TEXT,
    fetched_at      TEXT NOT NULL,
    digest_id       TEXT,
    underlying      REAL,
    atm_strike      REAL,
    total_ce_oi     INTEGER,
    total_pe_oi     INTEGER,
    ce_oi_change    INTEGER,
    pe_oi_change    INTEGER,
    pcr             REAL,
    max_pain        REAL,
    support         REAL,
    resistance      REAL,
    verdict_label   TEXT,
    confidence      INTEGER,
    candle_1h       TEXT,
    candle_3h       TEXT,
    top_signal_type        TEXT,
    top_signal_strike      REAL,
    top_signal_option_type TEXT,
    top_signal_severity    TEXT,
    top_signal_oi_pct      REAL,
    trend_bias      TEXT,
    trend_strength  INTEGER,
    market_regime   TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scan_summaries_symbol_time
    ON scan_summaries (symbol, fetched_at DESC);
```

**Add to MIGRATIONS list:**
```python
# scan_summaries is created via DDL on first run — no migration needed
# But add paper_trades score columns:
"ALTER TABLE paper_trades ADD COLUMN trade_status TEXT DEFAULT 'TRIGGERED_CORE'",
"ALTER TABLE paper_trades ADD COLUMN setup_type TEXT",
"ALTER TABLE paper_trades ADD COLUMN decision_reason TEXT",
"ALTER TABLE paper_trades ADD COLUMN confidence_score INTEGER",
"ALTER TABLE paper_trades ADD COLUMN entry_quality_score INTEGER",
"ALTER TABLE paper_trades ADD COLUMN trend_alignment_score INTEGER",
"ALTER TABLE paper_trades ADD COLUMN regime_score INTEGER",
```


### 1.2 Structured Intelligence Object (B7 — partial fix)

**File:** `src/engine/intelligence.py` — change return type

```python
# generate_intelligence() currently returns str only.
# Add a companion function that returns structured data.
# Full refactor is Phase 3. For now, add alongside existing function.

def generate_intelligence_structured(
    symbol: str,
    current_alerts: list[dict],
    scan_context: dict | None = None,
) -> dict:
    """
    Returns structured intelligence dict alongside Telegram text.
    Eliminates fragile regex parsing in paper_trading.py and scan_summary.py.

    Returns:
    {
        "verdict_label": str,
        "bias": "BULLISH" | "BEARISH" | "NEUTRAL",
        "confidence": int,
        "chart_conflict": bool,
        "trend": str,
        "telegram_text": str,   # same as generate_intelligence() output
    }
    """
    from src.engine.verdict_sets import is_bullish, is_bearish

    telegram_text = generate_intelligence(symbol, current_alerts, scan_context)

    # Extract structured fields (regex as interim — replace in Phase 3)
    import re
    verdict_label = ""
    confidence = 0
    m_v = re.search(r"\*Verdict:\s*([^\*]+)\*", telegram_text or "")
    if m_v:
        verdict_label = m_v.group(1).strip()
    m_c = re.search(r"Confidence:\s*(\d+)%", telegram_text or "")
    if m_c:
        confidence = int(m_c.group(1))

    bias = "BULLISH" if is_bullish(verdict_label) else ("BEARISH" if is_bearish(verdict_label) else "NEUTRAL")
    chart_conflict = "Chart conflict" in telegram_text

    return {
        "verdict_label": verdict_label,
        "bias": bias,
        "confidence": confidence,
        "chart_conflict": chart_conflict,
        "telegram_text": telegram_text,
    }
```

**Update `pipeline.py` to call `generate_intelligence_structured()` and pass the dict downstream.**
This eliminates regex parsing in `paper_trading.py` and `scan_summary.py` immediately.


### 1.3 Scan Summary Engine

**File:** `src/engine/scan_summary.py` (NEW)

```python
"""Saves one row per scan. Foundation for multi-scan trend analysis."""
import json
import logging
from src.models.schema import get_conn

log = logging.getLogger(__name__)


def save_scan_summary(
    symbol: str,
    scan_context: dict,
    alerts: list[dict],
    intel: dict,          # structured intelligence dict (not raw text)
    digest_id: str,
    fetched_at: str,
) -> None:
    ctx = scan_context or {}
    verdict_label = intel.get("verdict_label", "")
    confidence = intel.get("confidence", 0)

    chart_data = ctx.get("chart_indicators", {})
    candle_1h = (chart_data.get("1h") or {}).get("sentiment", "NEUTRAL")
    candle_3h = (chart_data.get("3h") or {}).get("sentiment", "NEUTRAL")

    top = _find_top_signal(alerts)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO scan_summaries (
                symbol, expiry, fetched_at, digest_id,
                underlying, atm_strike, total_ce_oi, total_pe_oi,
                ce_oi_change, pe_oi_change, pcr, max_pain, support, resistance,
                verdict_label, confidence, candle_1h, candle_3h,
                top_signal_type, top_signal_strike, top_signal_option_type,
                top_signal_severity, top_signal_oi_pct,
                trend_bias, trend_strength, market_regime
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            symbol, ctx.get("expiry"), fetched_at, digest_id,
            ctx.get("underlying"), ctx.get("atm_strike"),
            ctx.get("total_ce_oi"), ctx.get("total_pe_oi"),
            ctx.get("ce_oi_change"), ctx.get("pe_oi_change"),
            ctx.get("pcr"), ctx.get("max_pain"),
            ctx.get("support"), ctx.get("resistance"),
            verdict_label, confidence, candle_1h, candle_3h,
            top.get("type"), top.get("strike"), top.get("option_type"),
            top.get("severity"), top.get("oi_pct"),
            None, None, None,   # trend_bias/strength/regime filled by Layer 2
        ))
    log.info("%s: scan summary saved | verdict=%s conf=%d", symbol, verdict_label, confidence)


def _find_top_signal(alerts: list[dict]) -> dict:
    if not alerts:
        return {}
    sev_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    def score(a):
        sev = sev_order.get(a.get("severity", "LOW"), 0)
        try:
            detail = json.loads(a.get("detail_json") or "{}")
            oi_pct = abs(float(detail.get("pct_change", 0)))
        except Exception:
            oi_pct = 0
        return (sev, oi_pct)
    top = max(alerts, key=score)
    detail = json.loads(top.get("detail_json") or "{}")
    return {
        "type": top.get("alert_type"),
        "strike": top.get("strike"),
        "option_type": top.get("option_type"),
        "severity": top.get("severity"),
        "oi_pct": abs(float(detail.get("pct_change", 0))),
    }
```

**Update `pipeline.py`:**
```python
from src.engine.scan_summary import save_scan_summary
from src.engine.intelligence import generate_intelligence_structured

# Replace existing generate_intelligence call:
intel = generate_intelligence_structured(symbol, new_alerts, scan_context=scan_context)
intel_text = intel["telegram_text"]

# After digest is built:
try:
    save_scan_summary(symbol, scan_context, new_alerts, intel, digest_id, fetched_at)
except Exception:
    log.exception("%s: scan summary save failed", symbol)

# Pass structured intel to paper trading (eliminates regex parsing):
run_paper_trading(symbol, scan_context, digest_id, intel)
```


---

## Phase 2: Decision + Risk Engine (Week 2)

### 2.1 Market Regime Detector (B2 fixed)

**File:** `src/engine/regime_detector.py` (NEW)

```python
"""Market regime detection from scan history."""
import logging
from src.models.schema import get_conn

log = logging.getLogger(__name__)

REGIMES = ("TRENDING_UP", "TRENDING_DOWN", "RANGE", "VOLATILE", "NO_TRADE")


def detect_market_regime(symbol: str) -> str:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT verdict_label, underlying, pcr, confidence
            FROM scan_summaries
            WHERE symbol = ?
            ORDER BY fetched_at DESC
            LIMIT 10
        """, (symbol,)).fetchall()

    if len(rows) < 5:
        return "NO_TRADE"

    # B2 FIX: reverse so oldest → newest for correct direction calculation
    prices = list(reversed([float(r["underlying"] or 0) for r in rows if r["underlying"]]))
    if len(prices) < 5:
        return "NO_TRADE"

    first_half_avg = sum(prices[:5]) / 5
    second_half_avg = sum(prices[5:]) / len(prices[5:]) if len(prices) > 5 else prices[-1]
    price_change_pct = (second_half_avg - first_half_avg) / first_half_avg * 100

    price_range_pct = (max(prices) - min(prices)) / min(prices) * 100

    from src.engine.verdict_sets import is_bullish, is_bearish
    bullish = sum(1 for r in rows if is_bullish(r["verdict_label"] or ""))
    bearish = sum(1 for r in rows if is_bearish(r["verdict_label"] or ""))

    if bullish >= 7 and price_change_pct > 0.5:
        return "TRENDING_UP"
    if bearish >= 7 and price_change_pct < -0.5:
        return "TRENDING_DOWN"
    if price_range_pct > 3.0:
        return "VOLATILE"
    if abs(price_change_pct) < 0.3 and abs(bullish - bearish) <= 2:
        return "RANGE"
    return "NO_TRADE"
```

### 2.2 Entry Quality Scorer (B6 fixed)

**File:** `src/engine/entry_quality.py` (NEW)

```python
"""Entry quality scorer — validates trade location and timing."""
import logging

log = logging.getLogger(__name__)


def calculate_entry_quality(
    symbol: str,
    option_type: str,
    strike: float,
    ctx: dict,
) -> tuple[int, list[str]]:
    """
    Score 0-100. Returns (score, reasons).
    ctx must contain: underlying, support, resistance, sl_underlying,
                      target_underlying, option_rows, price_change_pct
    """
    score = 100
    reasons = []

    underlying = float(ctx.get("underlying") or 0)
    if underlying <= 0:
        return 0, ["Missing underlying price"]

    support = float(ctx.get("support") or 0)
    resistance = float(ctx.get("resistance") or 0)

    # 1. Price location vs support/resistance
    if support > 0 and resistance > 0:
        range_size = abs(resistance - support)
        if range_size > 0:
            if option_type == "PE":
                dist_to_support = abs(underlying - support)
                if dist_to_support < range_size * 0.15:
                    score -= 25
                    reasons.append(f"Price near support {support:.0f} — bounce risk")
            elif option_type == "CE":
                dist_to_resistance = abs(underlying - resistance)
                if dist_to_resistance < range_size * 0.15:
                    score -= 25
                    reasons.append(f"Price near resistance {resistance:.0f} — rejection risk")

    # 2. R:R check — B6 FIX: validate keys exist before scoring
    sl = float(ctx.get("sl_underlying") or 0)
    target = float(ctx.get("target_underlying") or 0)
    if sl <= 0 or target <= 0:
        reasons.append("Missing SL/target — R:R check skipped (tag only)")
        # Do not penalize in research mode, but log it
        log.debug("%s: entry quality R:R check skipped — sl=%s target=%s", symbol, sl, target)
    else:
        dist_to_sl = abs(underlying - sl)
        dist_to_target = abs(underlying - target)
        if dist_to_sl > 0:
            rr = dist_to_target / dist_to_sl
            if rr < 1.0:
                score -= 25
                reasons.append(f"Poor R:R {rr:.2f} — target closer than SL")

    # 3. Bid-ask spread
    for row in (ctx.get("option_rows") or []):
        try:
            if (abs(float(row.get("strike") or 0) - strike) < 0.01 and
                    str(row.get("option_type") or "").upper() == option_type):
                bid = float(row.get("bid") or 0)
                ask = float(row.get("ask") or 0)
                ltp = float(row.get("ltp") or 0)
                if ltp > 0 and bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / ltp * 100
                    if spread_pct > 5.0:
                        score -= 20
                        reasons.append(f"Wide spread {spread_pct:.1f}% — poor liquidity")
                break
        except Exception:
            continue

    # 4. Chasing check
    price_change_pct = float(ctx.get("price_change_pct") or 0)
    if option_type == "PE" and price_change_pct < -1.5:
        score -= 15
        reasons.append(f"Chasing after {price_change_pct:.1f}% drop")
    elif option_type == "CE" and price_change_pct > 1.5:
        score -= 15
        reasons.append(f"Chasing after +{price_change_pct:.1f}% rally")

    score = max(0, min(100, score))
    if score < 60:
        log.info("%s: entry quality LOW %d/100 — %s", symbol, score, "; ".join(reasons))
    return score, reasons
```


### 2.3 Reversal Detector (B3 + B4 fixed)

**File:** `src/engine/trend_analysis.py` (NEW)

```python
"""Trend analysis — reversal detection using scan-level data."""
import logging
from src.models.schema import get_conn
from src.engine.verdict_sets import is_bullish, is_bearish, BULLISH_VERDICTS, BEARISH_VERDICTS

log = logging.getLogger(__name__)


def detect_reversal_from_scans(
    symbol: str,
    current_verdict: str,
    current_confidence: int,
) -> tuple[bool, str]:
    """
    Detect trend reversal using scan-level data.
    B3 fix: does NOT use OI_SPIKE alerts for direction — uses BUILDUP_CLASSIFY only.
    B4 fix: uses explicit set membership, not string search.
    """
    if current_confidence < 75:
        return False, "Confidence too low for reversal"

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT verdict_label, confidence
            FROM scan_summaries
            WHERE symbol = ?
            ORDER BY fetched_at DESC
            LIMIT 10
        """, (symbol,)).fetchall()

    if len(rows) < 3:
        return False, "Insufficient scan history (need 3+)"

    # Broader trend from scans 3-10 (skip last 2 which may already be reversing)
    older = rows[2:]
    # B4 fix: explicit set membership
    bull_older = sum(1 for r in older if is_bullish(r["verdict_label"] or ""))
    bear_older = sum(1 for r in older if is_bearish(r["verdict_label"] or ""))

    if bull_older > bear_older * 1.5:
        broader_trend = "BULLISH"
    elif bear_older > bull_older * 1.5:
        broader_trend = "BEARISH"
    else:
        broader_trend = "NEUTRAL"

    # Check if current verdict is opposite to broader trend
    if is_bullish(current_verdict) and broader_trend != "BEARISH":
        return False, f"Not a reversal — broader trend is {broader_trend}"
    if is_bearish(current_verdict) and broader_trend != "BULLISH":
        return False, f"Not a reversal — broader trend is {broader_trend}"

    # Last 2 scans must confirm new direction
    last_2 = rows[:2]
    if is_bullish(current_verdict):
        if not all(is_bullish(r["verdict_label"] or "") for r in last_2):
            return False, "Last 2 scans not consistently bullish"
    elif is_bearish(current_verdict):
        if not all(is_bearish(r["verdict_label"] or "") for r in last_2):
            return False, "Last 2 scans not consistently bearish"

    return True, f"Reversal: {broader_trend} → {current_verdict}"


def get_trend_alignment_score(symbol: str, current_verdict: str) -> int:
    """
    Score 0-100: how well current verdict aligns with last 5 scans.
    B4 fix: uses explicit set membership.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT verdict_label FROM scan_summaries
            WHERE symbol = ? ORDER BY fetched_at DESC LIMIT 5
        """, (symbol,)).fetchall()

    if len(rows) < 3:
        return 50  # neutral if insufficient history

    if is_bullish(current_verdict):
        aligned = sum(1 for r in rows if is_bullish(r["verdict_label"] or ""))
    elif is_bearish(current_verdict):
        aligned = sum(1 for r in rows if is_bearish(r["verdict_label"] or ""))
    else:
        return 50

    return int(aligned / len(rows) * 100)
```


### 2.4 Risk Engine — Phase 2 (B1 fixed)

**File:** `src/engine/risk_engine.py` (NEW)

```python
"""
Risk Engine — basic trade frequency controls.
Must run BEFORE paper trade execution, even in paper research mode.
Without this, paper results are distorted by overtrading.
"""
import logging
from datetime import datetime, timedelta, timezone
from src.models.schema import get_conn
from config.settings import (
    MAX_OPEN_TRADES_PER_SYMBOL,
    MAX_OPEN_TRADES_TOTAL,
    MAX_TRADES_PER_SYMBOL_PER_DAY,
    MAX_DAILY_LOSS_RUPEES,
    LOSS_COOLDOWN_MINUTES,
)

log = logging.getLogger(__name__)


def check_risk_limits(symbol: str) -> tuple[bool, str]:
    """
    Hard frequency controls. Returns (allowed, reason).
    These apply to paper trading too — overtrading distorts research results.
    """
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    with get_conn() as conn:

        # 1. Max open trades per symbol (start with 1 — conservative)
        open_sym = conn.execute(
            "SELECT COUNT(*) as c FROM paper_trades WHERE symbol=? AND status='OPEN'",
            (symbol,)
        ).fetchone()["c"]
        if open_sym >= MAX_OPEN_TRADES_PER_SYMBOL:
            return False, f"Max open trades per symbol ({open_sym}/{MAX_OPEN_TRADES_PER_SYMBOL})"

        # 2. Max total open trades
        open_total = conn.execute(
            "SELECT COUNT(*) as c FROM paper_trades WHERE status='OPEN'"
        ).fetchone()["c"]
        if open_total >= MAX_OPEN_TRADES_TOTAL:
            return False, f"Max total open trades ({open_total}/{MAX_OPEN_TRADES_TOTAL})"

        # 3. Max trades per symbol per day
        today_count = conn.execute(
            "SELECT COUNT(*) as c FROM paper_trades WHERE symbol=? AND opened_at >= ?",
            (symbol, today_start)
        ).fetchone()["c"]
        if today_count >= MAX_TRADES_PER_SYMBOL_PER_DAY:
            return False, f"Max trades per day ({today_count}/{MAX_TRADES_PER_SYMBOL_PER_DAY})"

        # 4. Daily loss cap
        today_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_rupees), 0) as total FROM paper_trades WHERE closed_at >= ?",
            (today_start,)
        ).fetchone()["total"]
        if today_pnl < -abs(MAX_DAILY_LOSS_RUPEES):
            return False, f"Daily loss limit hit (₹{today_pnl:,.0f})"

        # 5. Cooldown after SL/loss
        last_loss = conn.execute("""
            SELECT closed_at FROM paper_trades
            WHERE symbol=? AND status IN ('CLOSED_SL', 'CLOSED_MANUAL')
            AND pnl_rupees < 0
            ORDER BY closed_at DESC LIMIT 1
        """, (symbol,)).fetchone()

    if last_loss:
        try:
            loss_time = datetime.fromisoformat(last_loss["closed_at"])
            if loss_time.tzinfo is None:
                loss_time = loss_time.replace(tzinfo=timezone.utc)
            cooldown_end = loss_time + timedelta(minutes=LOSS_COOLDOWN_MINUTES)
            if now_utc < cooldown_end:
                mins_left = (cooldown_end - now_utc).total_seconds() / 60
                return False, f"Cooldown active after loss ({mins_left:.0f} min remaining)"
        except Exception:
            pass

    return True, "Risk checks passed"
```

**Add to `config/settings.py`:**
```python
# ── Risk Engine (Phase 2) ─────────────────────────────────────────────────
MAX_OPEN_TRADES_PER_SYMBOL   = 1    # conservative start
MAX_OPEN_TRADES_TOTAL        = 4    # across all symbols
MAX_TRADES_PER_SYMBOL_PER_DAY = 2   # configurable
MAX_DAILY_LOSS_RUPEES        = 10000
LOSS_COOLDOWN_MINUTES        = 30
```


### 2.5 Trade Decision Engine (B5 fixed)

**File:** `src/engine/trade_decision.py` (NEW)

```python
"""Trade Decision Engine — combines all layers."""
import logging
from src.engine.entry_quality import calculate_entry_quality
from src.engine.regime_detector import detect_market_regime
from src.engine.trend_analysis import detect_reversal_from_scans, get_trend_alignment_score
from src.engine.verdict_sets import is_bullish, is_bearish
from src.models.schema import get_conn

log = logging.getLogger(__name__)

PAPER_RESEARCH_MODE = True   # set False only for live trading


def make_trade_decision(symbol: str, intel: dict, ctx: dict) -> dict:
    """
    Returns:
    {
        "status": "TRIGGERED_CORE" | "TRIGGERED_EXPERIMENTAL" | "BLOCKED",
        "setup_type": str,
        "reason": str,
        "soft_conflicts": list[str],
        "scores": dict,
    }
    """
    verdict = intel.get("verdict_label", "")
    confidence = int(intel.get("confidence") or 0)
    soft_conflicts = []

    # Hard blocks
    if float(ctx.get("underlying") or 0) <= 0:
        return _blocked("Missing underlying price")

    if not is_bullish(verdict) and not is_bearish(verdict):
        return _blocked(f"Verdict '{verdict}' is not directional")

    # Build plan to get strike/option_type for entry quality
    from src.engine.paper_plan import build_paper_trade_plan
    plan = build_paper_trade_plan(verdict, confidence, ctx)
    if not plan:
        return _blocked("No valid trade plan from verdict")

    option_type = plan["option_type"]
    strike = plan["strike"]
    plan_ctx = {**ctx, **plan}

    # Entry quality (B6: pass merged ctx with sl/target)
    entry_quality, entry_reasons = calculate_entry_quality(symbol, option_type, strike, plan_ctx)

    # Trend alignment
    trend_alignment = get_trend_alignment_score(symbol, verdict)

    # Market regime (B5: don't hard-block on NO_TRADE in research mode)
    regime = detect_market_regime(symbol)
    if regime == "NO_TRADE":
        if PAPER_RESEARCH_MODE and confidence >= 65:
            regime_score = 50
            soft_conflicts.append("INSUFFICIENT_REGIME_HISTORY")
        else:
            return _blocked("Insufficient scan history for regime detection")
    else:
        regime_score = _regime_score(regime, option_type)

    scores = {
        "confidence": confidence,
        "entry_quality": entry_quality,
        "trend_alignment": trend_alignment,
        "regime_score": regime_score,
    }

    # Chart conflict is a soft conflict, not a hard block
    if intel.get("chart_conflict"):
        soft_conflicts.append("CHART_CONFLICT_1H_3H")

    # Priority 1: Reversal (high R:R)
    is_rev, rev_reason = detect_reversal_from_scans(symbol, verdict, confidence)
    if is_rev and entry_quality >= 60:
        return _decision("TRIGGERED_CORE", "CONFIRMED_REVERSAL", rev_reason, soft_conflicts, scores)

    # Priority 2: Trend continuation (safe)
    if confidence >= 70 and trend_alignment >= 70 and entry_quality >= 60 and regime_score >= 60:
        return _decision("TRIGGERED_CORE", "TREND_CONTINUATION",
                         "All filters passed", soft_conflicts, scores)

    # Priority 3: Experimental (research mode only)
    if PAPER_RESEARCH_MODE and confidence >= 50 and entry_quality >= 40:
        reason = f"Marginal — conf={confidence} eq={entry_quality} ta={trend_alignment}"
        if entry_reasons:
            reason += f" | entry issues: {'; '.join(entry_reasons)}"
        return _decision("TRIGGERED_EXPERIMENTAL", "EXPERIMENTAL_SETUP",
                         reason, soft_conflicts, scores)

    # Blocked
    block_reasons = []
    if confidence < 50:
        block_reasons.append(f"Low confidence ({confidence}%)")
    if entry_quality < 40:
        block_reasons.append(f"Poor entry quality ({entry_quality}/100)")
    if trend_alignment < 50:
        block_reasons.append(f"Trend not aligned ({trend_alignment}/100)")
    return _blocked("; ".join(block_reasons) or "No qualifying condition met")


def _decision(status, setup_type, reason, soft_conflicts, scores):
    return {
        "status": status,
        "setup_type": setup_type,
        "reason": reason,
        "soft_conflicts": soft_conflicts,
        "scores": scores,
    }


def _blocked(reason):
    return {"status": "BLOCKED", "setup_type": None,
            "reason": reason, "soft_conflicts": [], "scores": {}}


def _regime_score(regime: str, option_type: str) -> int:
    if regime == "TRENDING_UP" and option_type == "CE":
        return 100
    if regime == "TRENDING_DOWN" and option_type == "PE":
        return 100
    if regime in ("TRENDING_UP", "TRENDING_DOWN"):
        return 70
    if regime == "RANGE":
        return 30   # long options decay in range
    if regime == "VOLATILE":
        return 40
    return 50
```


### 2.6 Update paper_trading.py

**Replace the current trigger section:**

```python
# OLD (remove):
# verdict, confidence = _parse_verdict_and_confidence(intelligence_text)
# ...
# plan = _trade_plan_from_verdict(verdict, confidence, ctx)
# if not plan:
#     return

# NEW:
from src.engine.trade_decision import make_trade_decision
from src.engine.risk_engine import check_risk_limits

# intel is now a structured dict passed from pipeline.py
verdict = intel.get("verdict_label", "")
confidence = int(intel.get("confidence") or 0)

# Risk check first (even in paper mode)
risk_ok, risk_reason = check_risk_limits(symbol)
if not risk_ok:
    log.info("%s: paper trade blocked by risk engine — %s", symbol, risk_reason)
    return

# Trade decision
decision = make_trade_decision(symbol, intel, ctx)
if decision["status"] == "BLOCKED":
    log.info("%s: paper trade blocked — %s", symbol, decision["reason"])
    return

# Build plan
plan = _trade_plan_from_verdict(verdict, confidence, ctx)
if not plan:
    return

# Insert with decision metadata
insert_paper_trade({
    **plan,
    "opened_at": now_iso,
    "symbol": symbol,
    "lots": DEFAULT_LOTS_PER_TRADE,
    "status": "OPEN",
    "reason": f"auto | {decision['reason']}",
    "digest_id": digest_id,
    "trade_status": decision["status"],
    "setup_type": decision["setup_type"],
    "decision_reason": decision["reason"],
    "decision_scores": decision["scores"],
})
```

**Update `run_paper_trading()` signature:**
```python
# OLD:
def run_paper_trading(symbol: str, scan_context: dict, digest_id: str, intelligence_text: str) -> None:

# NEW:
def run_paper_trading(symbol: str, scan_context: dict, digest_id: str, intel: dict) -> None:
    # intel is structured dict from generate_intelligence_structured()
```

---

## Phase 3: Structured Intelligence Refactor (Week 3)

### 3.1 Full Refactor of generate_intelligence()

**Goal:** Return structured object natively. No more regex parsing anywhere.

```python
# generate_intelligence() returns IntelligenceResult dataclass:
from dataclasses import dataclass, field

@dataclass
class IntelligenceResult:
    verdict_label: str
    bias: str                    # BULLISH | BEARISH | NEUTRAL
    confidence: int
    chart_conflict: bool
    trend: str
    bull_forces: list[str]
    bear_forces: list[str]
    action_plan: str
    risk_note: str
    paper_trade_text: str
    telegram_text: str           # formatted for Telegram
    reason_codes: list[str] = field(default_factory=list)
```

**Impact:** Eliminates regex parsing in:
- `paper_trading.py` (currently parses verdict/confidence from text)
- `scan_summary.py` (currently parses verdict/confidence from text)
- `dashboard_server.py` (`_parse_intel_fields()` function)
- `digest.py` (`_parse_intelligence()` function)

This is the most impactful refactor in the system. Do it once, fix 4 places.

---

## Phase 4: Analytics Engine (Week 4)

### 4.1 Score Correlation Analysis

**Run weekly to tune thresholds:**

```python
def analyze_score_correlation():
    """Which component scores predict profitable trades?"""
    # Query paper_trades with all score columns
    # Split CORE vs EXPERIMENTAL
    # For each score: compare win rate and avg P&L at high vs low score
    # Output: which scores matter most
    # Use results to tune MIN_CONFIDENCE_CORE, MIN_ENTRY_QUALITY_CORE, etc.
```

**Key questions to answer after 4 weeks:**
- Do CORE trades outperform EXPERIMENTAL? (validates the decision engine)
- Does entry_quality_score correlate with P&L? (validates entry quality logic)
- Does trend_alignment_score matter? (validates multi-scan approach)
- Does regime_score matter? (validates regime detection)

---

## Revised Phase Timeline

| Phase | Week | Deliverables |
|-------|------|-------------|
| 1 | Week 1 | Scan summaries table, structured intel object, scan summary engine |
| 2 | Week 2 | Regime detector (fixed), entry quality, reversal detector (fixed), risk engine, trade decision engine |
| 3 | Week 3 | Full intelligence refactor (structured return type) |
| 4 | Week 4+ | Analytics, threshold tuning, score correlation |

---

## Configuration (config/settings.py additions)

```python
# ── Trading System V2 ─────────────────────────────────────────────────────
PAPER_RESEARCH_MODE           = True   # False = live trading only

# Trade decision thresholds
MIN_CONFIDENCE_CORE           = 70
MIN_ENTRY_QUALITY_CORE        = 60
MIN_TREND_ALIGNMENT_CORE      = 70
MIN_REGIME_SCORE_CORE         = 60
MIN_CONFIDENCE_EXPERIMENTAL   = 50
MIN_ENTRY_QUALITY_EXPERIMENTAL = 40
REVERSAL_MIN_CONFIDENCE       = 75

# Risk engine (Phase 2 — applies to paper trading too)
MAX_OPEN_TRADES_PER_SYMBOL    = 1     # start conservative
MAX_OPEN_TRADES_TOTAL         = 4
MAX_TRADES_PER_SYMBOL_PER_DAY = 2
MAX_DAILY_LOSS_RUPEES         = 10000
LOSS_COOLDOWN_MINUTES         = 30
```

---

## Summary of All Bugs Fixed

| Bug | Where | Fix |
|-----|-------|-----|
| B1: Risk engine too late | Phase 4 → Phase 2 | Moved to Week 2 |
| B2: Regime price direction inverted | `detect_market_regime()` | `prices = list(reversed(prices))` |
| B3: `ltp_pct` missing in OI_SPIKE | `classify_oi_direction()` | Use BUILDUP_CLASSIFY only; function removed |
| B4: Verdict text matching loose | All trend functions | Explicit set membership via `verdict_sets.py` |
| B5: Hard block on NO_TRADE regime | `make_trade_decision()` | Tag EXPERIMENTAL, don't block in research mode |
| B6: Entry quality silently skips R:R | `calculate_entry_quality()` | Explicit validation + log when sl/target missing |
| B7: Regex parsing of intel text | `paper_trading.py`, `scan_summary.py` | `generate_intelligence_structured()` in Phase 1; full refactor in Phase 3 |
