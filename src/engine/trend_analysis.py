"""
Trend Analysis — reversal detection and trend alignment scoring.
B3 fix: uses BUILDUP_CLASSIFY alerts only for direction (not raw OI_SPIKE).
B4 fix: uses is_bullish/is_bearish from verdict_sets (explicit set membership).

Phase 4: Full hybrid trend-based trading logic implementation.
"""
from __future__ import annotations

import json
import logging

from src.models.schema import get_conn, get_alert_history
from src.engine.verdict_sets import is_bullish, is_bearish

log = logging.getLogger(__name__)


def get_trend_alignment_score(symbol: str, current_verdict: str) -> int:
    """
    Score 0-100: fraction of last 5 scans that agree with current verdict direction.
    Returns 50 when insufficient history (neutral — don't penalise early scans).
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT verdict_label FROM scan_summaries
            WHERE symbol = ?
            ORDER BY fetched_at DESC
            LIMIT 5
            """,
            (symbol,),
        ).fetchall()

    if len(rows) < 3:
        return 50  # neutral — insufficient history

    if is_bullish(current_verdict):
        aligned = sum(1 for r in rows if is_bullish(r["verdict_label"] or ""))
    elif is_bearish(current_verdict):
        aligned = sum(1 for r in rows if is_bearish(r["verdict_label"] or ""))
    else:
        return 50

    return int(aligned / len(rows) * 100)


def detect_reversal_from_scans(
    symbol: str,
    current_verdict: str,
    current_confidence: int,
) -> tuple[bool, str]:
    """
    Detect trend reversal using scan-level data.

    Criteria:
    1. current_confidence >= 75
    2. Broader trend (scans 3-10) is opposite to current verdict
    3. Last 2 scans confirm new direction

    B3 fix: does NOT use raw OI_SPIKE alerts for direction.
            Relies on verdict_label from scan_summaries (which is derived
            from BUILDUP_CLASSIFY + price×OI matrix — already correct).
    B4 fix: explicit set membership via is_bullish/is_bearish.
    """
    if current_confidence < 75:
        return False, "Confidence too low for reversal (need ≥75)"

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT verdict_label, confidence
            FROM scan_summaries
            WHERE symbol = ?
            ORDER BY fetched_at DESC
            LIMIT 10
            """,
            (symbol,),
        ).fetchall()

    if len(rows) < 3:
        return False, "Insufficient scan history (need 3+)"

    # Broader trend from scans 3-10 (skip last 2 which may already be reversing)
    older = rows[2:]
    bull_older = sum(1 for r in older if is_bullish(r["verdict_label"] or ""))
    bear_older = sum(1 for r in older if is_bearish(r["verdict_label"] or ""))

    if bull_older > bear_older * 1.5:
        broader_trend = "BULLISH"
    elif bear_older > bull_older * 1.5:
        broader_trend = "BEARISH"
    else:
        return False, "Broader trend is neutral — no clear reversal setup"

    # Current verdict must be opposite to broader trend
    if is_bullish(current_verdict) and broader_trend != "BEARISH":
        return False, f"Not a reversal — broader trend is {broader_trend}"
    if is_bearish(current_verdict) and broader_trend != "BULLISH":
        return False, f"Not a reversal — broader trend is {broader_trend}"
    if not is_bullish(current_verdict) and not is_bearish(current_verdict):
        return False, "Current verdict is not directional"

    # Last 2 scans must confirm new direction
    last_2 = rows[:2]
    if is_bullish(current_verdict):
        if not all(is_bullish(r["verdict_label"] or "") for r in last_2):
            return False, "Last 2 scans not consistently bullish"
    else:
        if not all(is_bearish(r["verdict_label"] or "") for r in last_2):
            return False, "Last 2 scans not consistently bearish"

    return True, f"Reversal confirmed: {broader_trend} → {current_verdict}"


# ── New Phase 4 Functions: Full Hybrid Trend Logic ────────────────────────

def get_broader_trend_from_alerts(symbol: str) -> str:
    """
    Analyze last 50 alerts for the symbol to determine multi-scan trend.
    Extracted from intelligence.py _compute_broader_trend() for reuse in trade decisions.
    
    Returns:
        Trend label like "🟢 Strong Bullish Trend", "🔴 Strong Bearish Trend",
        "⚪ Rangebound", "⚪ Mixed", etc.
    """
    history = get_alert_history(symbol, limit=50)
    merged = list(history or [])
    if not merged:
        return "Insufficient history - first scan"

    # Count buildup types from BUILDUP_CLASSIFY alerts
    long_buildups = 0
    short_buildups = 0
    long_unwinds = 0
    short_covers = 0
    oi_spikes_ce = 0
    oi_spikes_pe = 0
    vol_aggr_ce = 0
    vol_aggr_pe = 0
    atm_bull = 0
    atm_bear = 0

    for h in merged:
        row = dict(h) if not isinstance(h, dict) else h
        atype = row.get("alert_type", "")
        ot = row.get("option_type", "")
        detail = {}
        try:
            detail_raw = row.get("detail_json") or "{}"
            detail = json.loads(detail_raw) if isinstance(detail_raw, str) else (detail_raw or {})
        except Exception:
            pass

        if atype == "BUILDUP_CLASSIFY":
            bt = detail.get("buildup_type", "")
            if "Long Buildup" in bt:
                long_buildups += 1
            elif "Short Buildup" in bt:
                short_buildups += 1
            elif "Long Unwinding" in bt:
                long_unwinds += 1
            elif "Short Covering" in bt:
                short_covers += 1

        if atype == "OI_SPIKE":
            if ot == "CE":
                oi_spikes_ce += 1
            else:
                oi_spikes_pe += 1
        if atype == "VOLUME_AGGRESSION":
            if ot == "CE":
                vol_aggr_ce += 1
            elif ot == "PE":
                vol_aggr_pe += 1
        if atype == "ATM_LEG_MOVE":
            bias = str(detail.get("bias") or "")
            if "Bullish" in bias:
                atm_bull += 1
            elif "Bearish" in bias:
                atm_bear += 1

    # Decision logic
    bull_score = long_buildups + short_covers + oi_spikes_pe + vol_aggr_pe + atm_bull
    bear_score = short_buildups + long_unwinds + oi_spikes_ce + vol_aggr_ce + atm_bear
    active_flow = vol_aggr_ce + vol_aggr_pe

    if bear_score >= 8 and bear_score > bull_score * 2:
        return "🔴 Strong Bearish Trend — persistent call writing + short buildup"
    if bear_score >= 5 and bear_score > bull_score * 1.5:
        return "🟠 Mild Bearish — resistance building, sellers active"
    if bull_score >= 8 and bull_score > bear_score * 2:
        return "🟢 Strong Bullish Trend — persistent put writing + long buildup"
    if bull_score >= 5 and bull_score > bear_score * 1.5:
        return "🟡 Mild Bullish — support building, buyers active"
    if active_flow >= 10:
        return "⚪ High Activity — aggressive flow on both sides"
    if oi_spikes_ce > 3 and oi_spikes_pe > 3 and abs(oi_spikes_ce - oi_spikes_pe) <= 2:
        return "⚪ Rangebound — balanced OI activity on both sides"
    if bull_score + bear_score < 3:
        return "⚪ Low Activity — insufficient signals for trend"

    return "⚪ Mixed — no dominant trend yet"


def check_trend_persistence(
    symbol: str,
    current_verdict: str,
    current_confidence: int,
    ctx: dict,
) -> tuple[bool, str]:
    """
    Logic 1: Trend Persistence Filter (Conservative)
    
    Trigger trade ONLY if:
    1. Current scan confidence ≥ 70% (raised from 65)
    2. Broader trend aligns with current verdict
    3. Last 3 scans show consistent directional bias (2/3 must agree)
    4. No conflicting chart signals (1H vs 3H)
    
    Returns:
        (should_trade, reason)
    """
    from config.settings import TREND_MIN_SCANS, TREND_CONSISTENCY_THRESHOLD
    
    # Step 1: Base confidence gate (stricter)
    if current_confidence < 70:
        return False, f"Confidence too low ({current_confidence}% < 70%)"
    
    # Step 2: Broader trend alignment
    trend = get_broader_trend_from_alerts(symbol)
    if is_bullish(current_verdict):
        if "Bearish" in trend or "Mixed" in trend:
            return False, f"Broader trend not aligned — {trend}"
    elif is_bearish(current_verdict):
        if "Bullish" in trend or "Mixed" in trend:
            return False, f"Broader trend not aligned — {trend}"
    else:
        return False, "Current verdict is not directional"
    
    # Step 3: Last 3 scans consistency check
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT verdict_label FROM scan_summaries
            WHERE symbol = ?
            ORDER BY fetched_at DESC
            LIMIT ?
            """,
            (symbol, TREND_MIN_SCANS),
        ).fetchall()
    
    if len(rows) < TREND_MIN_SCANS:
        return False, f"Insufficient scan history — need {TREND_MIN_SCANS}+ scans"
    
    bullish_count = sum(1 for r in rows if is_bullish(r["verdict_label"] or ""))
    bearish_count = sum(1 for r in rows if is_bearish(r["verdict_label"] or ""))
    
    required_agreement = int(TREND_MIN_SCANS * TREND_CONSISTENCY_THRESHOLD)
    
    if is_bullish(current_verdict):
        if bullish_count < required_agreement:
            return False, f"Inconsistent bias — only {bullish_count}/{TREND_MIN_SCANS} scans bullish"
    elif is_bearish(current_verdict):
        if bearish_count < required_agreement:
            return False, f"Inconsistent bias — only {bearish_count}/{TREND_MIN_SCANS} scans bearish"
    
    # Step 4: Chart conflict check
    if ctx.get("chart_conflict"):
        return False, "1H vs 3H chart conflict — wait for alignment"
    
    return True, "All trend persistence filters passed"


def calculate_momentum_score(
    symbol: str,
    current_verdict: str,
    current_confidence: int,
    ctx: dict,
) -> int:
    """
    Logic 2: Trend Momentum Scoring (Balanced)
    
    Score 0-100 based on:
    - Current scan confidence (40% weight)
    - Broader trend alignment (30% weight)
    - Recent scan consistency (20% weight)
    - Chart confluence (10% weight)
    
    Returns:
        Momentum score 0-100
    """
    score = 0
    
    # 1. Current scan confidence (max 40 pts)
    score += min(current_confidence * 0.4, 40)
    
    # 2. Broader trend alignment (max 30 pts)
    trend = get_broader_trend_from_alerts(symbol)
    if is_bullish(current_verdict):
        if "Strong Bullish" in trend:
            score += 30
        elif "Mild Bullish" in trend:
            score += 20
        elif "Mixed" in trend or "Rangebound" in trend:
            score += 10
        # else: Bearish trend = 0 pts
    elif is_bearish(current_verdict):
        if "Strong Bearish" in trend:
            score += 30
        elif "Mild Bearish" in trend:
            score += 20
        elif "Mixed" in trend or "Rangebound" in trend:
            score += 10
        # else: Bullish trend = 0 pts
    
    # 3. Recent scan consistency (max 20 pts)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT verdict_label FROM scan_summaries
            WHERE symbol = ?
            ORDER BY fetched_at DESC
            LIMIT 5
            """,
            (symbol,),
        ).fetchall()
    
    if len(rows) >= 3:
        if is_bullish(current_verdict):
            bullish_pct = sum(1 for r in rows if is_bullish(r["verdict_label"] or "")) / len(rows)
            score += bullish_pct * 20
        elif is_bearish(current_verdict):
            bearish_pct = sum(1 for r in rows if is_bearish(r["verdict_label"] or "")) / len(rows)
            score += bearish_pct * 20
    
    # 4. Chart confluence (max 10 pts)
    chart_indicators = ctx.get("chart_indicators", {})
    chart_1h_sentiment = None
    chart_3h_sentiment = None
    
    # Extract chart sentiments (handle both dict and nested dict formats)
    if isinstance(chart_indicators, dict):
        if "1h" in chart_indicators:
            chart_1h = chart_indicators.get("1h", {})
            chart_1h_sentiment = chart_1h.get("sentiment") if isinstance(chart_1h, dict) else None
        if "3h" in chart_indicators:
            chart_3h = chart_indicators.get("3h", {})
            chart_3h_sentiment = chart_3h.get("sentiment") if isinstance(chart_3h, dict) else None
    
    if is_bullish(current_verdict):
        if chart_1h_sentiment == "BULLISH" and chart_3h_sentiment == "BULLISH":
            score += 10
        elif chart_1h_sentiment == "BULLISH" or chart_3h_sentiment == "BULLISH":
            score += 5
    elif is_bearish(current_verdict):
        if chart_1h_sentiment == "BEARISH" and chart_3h_sentiment == "BEARISH":
            score += 10
        elif chart_1h_sentiment == "BEARISH" or chart_3h_sentiment == "BEARISH":
            score += 5
    
    return int(min(score, 100))
