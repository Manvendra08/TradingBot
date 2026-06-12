"""Trend analysis utilities — alignment, persistence, momentum, reversal detection.
P1 fix: detect_reversal_from_scans() now accepts skip_latest=True (default True)
  and fetches rows OFFSET 1 so the current scan (just inserted before
  make_trade_decision is called) is excluded from the 2-scan confirmation
  window. Previously rows[0] was the triggering scan itself, giving only
  1 real independent confirmation instead of 2.

P2 fix (#1): get_broader_trend_from_alerts() now accepts a pre-fetched result via
  the `cached` parameter. check_trend_persistence() and calculate_momentum_score()
  both accept an optional `broader_trend` kwarg so callers can compute once and
  pass down — eliminates 2-3 redundant DB+query round trips per scan cycle in
  hybrid mode.
P2 fix (#11): _is_reversal_against_open_trade threshold sourced from
  REVERSAL_MIN_CONFIDENCE (settings.py) instead of hardcoded 70.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.models.schema import get_conn, get_recent_alerts_for_symbol
from src.engine.verdict_sets import is_bullish, is_bearish
from config.settings import (
    REVERSAL_MIN_CONFIDENCE,
    MIN_CONFIDENCE_CORE,
)

log = logging.getLogger(__name__)


def get_broader_trend_from_alerts(symbol: str, limit: int = 50, cached: str | None = None) -> str:
    """
    Derive a broader trend label from the last `limit` alert verdicts.
    Returns one of: 'Strong Bullish Trend', 'Moderate Bullish Trend',
    'Strong Bearish Trend', 'Moderate Bearish Trend', 'Mixed/Unclear Trend'.

    Pass `cached` to skip the DB query when the caller already has the result
    (e.g. from a earlier call in the same pipeline iteration).
    """
    if cached is not None:
        return cached

    rows = get_recent_alerts_for_symbol(symbol, limit)
    if not rows:
        return "Mixed/Unclear Trend"

    bull_count = sum(1 for r in rows if is_bullish(r.get("verdict_label", "")))
    bear_count = sum(1 for r in rows if is_bearish(r.get("verdict_label", "")))
    total = len(rows)

    bull_pct = bull_count / total
    bear_pct = bear_count / total

    if bull_pct >= 0.70:
        return "Strong Bullish Trend"
    if bull_pct >= 0.55:
        return "Moderate Bullish Trend"
    if bear_pct >= 0.70:
        return "Strong Bearish Trend"
    if bear_pct >= 0.55:
        return "Moderate Bearish Trend"
    return "Mixed/Unclear Trend"


def get_trend_alignment_score(symbol: str, verdict: str) -> int:
    """
    Score 0-100: how well the current verdict aligns with broader trend.
    Returns 50 (neutral) when insufficient history (< 3 rows).
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT verdict_label FROM scan_summaries
            WHERE symbol = ?
              AND (is_fallback IS NULL OR is_fallback = 0)
            ORDER BY fetched_at DESC
            LIMIT 10
            """,
            (symbol,),
        ).fetchall()

    if len(rows) < 3:
        return 50

    bull_count = sum(1 for r in rows if is_bullish(r["verdict_label"] or ""))
    bear_count = sum(1 for r in rows if is_bearish(r["verdict_label"] or ""))
    total = len(rows)

    if is_bullish(verdict):
        return round(bull_count / total * 100)
    if is_bearish(verdict):
        return round(bear_count / total * 100)
    return 50


def check_trend_persistence(
    symbol: str,
    verdict: str,
    confidence: int,
    ctx: dict,
    broader_trend: str | None = None,
) -> tuple[bool, str]:
    """
    Check whether the current verdict is part of a persistent trend.
    Returns (is_persistent, reason_string).

    Pass `broader_trend` when the caller already has the result from
    get_broader_trend_from_alerts() to avoid a redundant DB round-trip.
    """
    bt = get_broader_trend_from_alerts(symbol, cached=broader_trend)

    if is_bullish(verdict):
        if "Bearish" in bt:
            return False, f"Counter-trend BUY — broader trend is {bt}"
        if "Mixed" in bt and confidence < MIN_CONFIDENCE_CORE:
            return False, f"Mixed trend + low confidence ({confidence}%) — no persistence"
        return True, f"Trend persistent: {bt} | conf={confidence}%"

    if is_bearish(verdict):
        if "Bullish" in bt:
            return False, f"Counter-trend SELL — broader trend is {bt}"
        if "Mixed" in bt and confidence < MIN_CONFIDENCE_CORE:
            return False, f"Mixed trend + low confidence ({confidence}%) — no persistence"
        return True, f"Trend persistent: {bt} | conf={confidence}%"

    return False, f"Non-directional verdict '{verdict}'"


def calculate_momentum_score(
    symbol: str,
    verdict: str,
    confidence: int,
    ctx: dict,
    broader_trend: str | None = None,
) -> int:
    """
    Score 0-100 combining recent trend strength, scan agreement, and chart signals.
    Components:
      - Broader trend strength from alerts  : 0-40
      - Recent scan agreement (last 5)      : 0-30
      - Chart indicator alignment           : 0-20
      - Current confidence contribution     : 0-10

    Pass `broader_trend` when the caller already has the result from
    get_broader_trend_from_alerts() to avoid a redundant DB round-trip.
    """
    score = 0

    # Component 1: broader trend strength (uses cache if provided)
    bt = get_broader_trend_from_alerts(symbol, cached=broader_trend)
    if "Strong Bullish" in bt and is_bullish(verdict):
        score += 40
    elif "Strong Bearish" in bt and is_bearish(verdict):
        score += 40
    elif "Moderate Bullish" in bt and is_bullish(verdict):
        score += 25
    elif "Moderate Bearish" in bt and is_bearish(verdict):
        score += 25
    elif "Mixed" in bt:
        score += 10

    # Component 2: recent scan agreement
    with get_conn() as conn:
        recent_rows = conn.execute(
            """
            SELECT verdict_label FROM scan_summaries
            WHERE symbol = ?
              AND (is_fallback IS NULL OR is_fallback = 0)
            ORDER BY fetched_at DESC
            LIMIT 5
            """,
            (symbol,),
        ).fetchall()

    if recent_rows:
        agreeing = 0
        for row in recent_rows:
            label = row["verdict_label"] or ""
            if is_bullish(verdict) and is_bullish(label):
                agreeing += 1
            elif is_bearish(verdict) and is_bearish(label):
                agreeing += 1
        score += round(agreeing / len(recent_rows) * 30)

    # Component 3: chart indicator alignment
    chart_indicators = ctx.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}

    if tf_data:
        aligned_tfs = 0
        for tf_key in ("1h", "3h"):
            tf = tf_data.get(tf_key) or {}
            tf_verdict = tf.get("verdict") or tf.get("signal") or ""
            if is_bullish(verdict) and is_bullish(tf_verdict):
                aligned_tfs += 1
            elif is_bearish(verdict) and is_bearish(tf_verdict):
                aligned_tfs += 1
        score += aligned_tfs * 10

    # Component 4: confidence contribution (scaled, capped at 10)
    score += min(round(confidence * 0.10), 10)

    return min(100, score)


def detect_reversal_from_scans(
    symbol: str,
    verdict: str,
    confidence: int,
    skip_latest: bool = True,
) -> tuple[bool, str]:
    """
    Detect if current verdict is a confirmed reversal from previous direction.

    P1 fix: skip_latest=True (default) offsets the query by 1 row so the scan
    that was just inserted before make_trade_decision() is called is excluded.
    Without this, rows[0] is always the current scan itself — meaning the
    '2-scan confirmation' was really only 1 independent historical confirmation.
    With skip_latest=True, rows[0] and rows[1] are the two most recent
    *historical* scans, providing genuine independent confirmation.

    Args:
        symbol: symbol to query.
        verdict: current verdict to check for reversal.
        confidence: current confidence level.
        skip_latest: if True (default), offset by 1 to exclude current scan.
    """
    if confidence < REVERSAL_MIN_CONFIDENCE:
        return False, f"Confidence {confidence}% below reversal threshold {REVERSAL_MIN_CONFIDENCE}%"

    offset = 1 if skip_latest else 0

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT verdict_label, confidence FROM scan_summaries
            WHERE symbol = ?
              AND (is_fallback IS NULL OR is_fallback = 0)
            ORDER BY fetched_at DESC
            LIMIT 10 OFFSET ?
            """,
            (symbol, offset),
        ).fetchall()

    if len(rows) < 4:
        return False, "Insufficient scan history for reversal detection"

    # Check last 2 historical scans (rows[0], rows[1]) agree on opposite direction
    last_2 = rows[:2]
    prev_directions = []
    for row in last_2:
        label = row["verdict_label"] or ""
        if is_bullish(label):
            prev_directions.append("bull")
        elif is_bearish(label):
            prev_directions.append("bear")
        else:
            prev_directions.append("neutral")

    current_dir = "bull" if is_bullish(verdict) else ("bear" if is_bearish(verdict) else "neutral")

    if current_dir == "neutral":
        return False, "Current verdict is non-directional"

    # Both prior scans must agree on the OPPOSITE direction
    opposite = "bear" if current_dir == "bull" else "bull"
    if all(d == opposite for d in prev_directions):
        # Also check that older scans (rows[2:4]) support the prior direction
        older_2 = rows[2:4]
        older_support = sum(
            1 for r in older_2
            if (opposite == "bull" and is_bullish(r["verdict_label"] or "")) or
               (opposite == "bear" and is_bearish(r["verdict_label"] or ""))
        )
        if older_support >= 1:
            return True, (
                f"Reversal confirmed: {opposite}\u2192{current_dir} "
                f"(last 2 historical scans {opposite}, older support={older_support}/2)"
            )
        return False, f"Prior direction ({opposite}) not sustained in older scans"

    return False, (
        f"No clean reversal: prior scan directions={prev_directions}, "
        f"current={current_dir}"
    )
