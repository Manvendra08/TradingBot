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

Momentum v2 (2026-06-28):
  calculate_momentum_score() rebuilt with four primary components
  (broader trend 40, scan agreement 30, confidence 10, OI delta 20)
  plus two modifiers (IV percentile penalty, TTe decay).
  Three private helpers carry the signal-specific logic and are
  individually exception-safe so no DB error can crash the engine.
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
              AND fetched_at >= datetime('now', '-24 hours')
            ORDER BY fetched_at DESC
            LIMIT 10
            """,
            (symbol,),
        ).fetchall()

    if len(rows) < 3:
        return 50

    bull_count = sum(1 for r in rows if is_bullish(r["verdict_label"] or ""))
    bear_count = sum(1 for r in rows if is_bearish(r["verdict_label"] or ""))
    
    total_directional = bull_count + bear_count
    if total_directional == 0:
        return 50

    if is_bullish(verdict):
        return round(bull_count / total_directional * 100)
    if is_bearish(verdict):
        return round(bear_count / total_directional * 100)
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


# ---------------------------------------------------------------------------
# Private helpers for the rebuilt momentum scorer
# ---------------------------------------------------------------------------

def _calc_oi_delta_bonus(symbol: str, verdict: str) -> int:
    """Return +20 when PCR shifted >10 % in the direction aligned with *verdict*
    over the last 2 non-fallback scan summaries; 0 otherwise.

    PCR = Put OI / Call OI.
    - Rising PCR (>10 %) + bearish verdict → institutions accumulating puts   → +20
    - Falling PCR (>10 %) + bullish verdict → institutions unwinding puts      → +20
    A counter-directional shift (e.g. PCR rising while verdict is bullish)
    returns 0, which acts as an implicit warning: the engine's directional call
    lacks OI confirmation.
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT pcr FROM scan_summaries
                WHERE symbol = ? AND pcr IS NOT NULL
                  AND (is_fallback IS NULL OR is_fallback = 0)
                ORDER BY fetched_at DESC
                LIMIT 2
                """,
                (symbol,),
            ).fetchall()

        if len(rows) < 2:
            return 0

        pcr_latest = float(rows[0]["pcr"])
        pcr_prev   = float(rows[1]["pcr"])

        if pcr_prev == 0:
            return 0

        shift_pct = (pcr_latest - pcr_prev) / pcr_prev  # +ve = PCR rose

        if abs(shift_pct) < 0.10:
            return 0  # shift below 10 % threshold — not significant

        if shift_pct > 0 and is_bearish(verdict):
            return 20  # PCR rising + bearish → OI-confirmed
        if shift_pct < 0 and is_bullish(verdict):
            return 20  # PCR falling + bullish → OI-confirmed

        return 0
    except Exception:
        return 0


def _calc_iv_percentile_penalty(symbol: str) -> int:
    """Return a penalty (≤ 0) when current ATM IV exceeds the 60th percentile
    of the last 30 days of IV snapshots for *symbol*.

    High IV = premium-selling environment; buying options is structurally
    unfavourable (overpaying for gamma/vega).  Penalty is -10 when IV ≥ 60th
    percentile, 0 otherwise.

    Uses all IV records (not just ATM) as a proxy for the implied-vol surface;
    sufficient for direction-filtering purposes without requiring extra data.
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT iv FROM option_chain_snapshots
                WHERE symbol = ?
                  AND iv IS NOT NULL AND iv > 0
                  AND fetched_at >= datetime('now', '-30 days')
                ORDER BY fetched_at DESC
                LIMIT 2000
                """,
                (symbol,),
            ).fetchall()

        if len(rows) < 10:
            return 0  # insufficient history — neutral

        ivs = sorted(float(r["iv"]) for r in rows)
        pct60_idx = int(len(ivs) * 0.60)
        pct60 = ivs[pct60_idx]

        with get_conn() as conn:
            cur_row = conn.execute(
                """
                SELECT iv FROM option_chain_snapshots
                WHERE symbol = ? AND iv IS NOT NULL AND iv > 0
                ORDER BY fetched_at DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()

        if not cur_row:
            return 0

        return -10 if float(cur_row["iv"]) >= pct60 else 0
    except Exception:
        return 0


def _calc_tte_decay(symbol: str, ctx: dict) -> float:
    """Return a decay multiplier [0.40, 1.0] based on days-to-expiry (DTE).

    Near expiry gamma risk explodes and spreads widen — discounting the
    momentum score reflects the reduced reward-to-risk of buying options.

    DTE > 3  →  1.00  (no decay)
    DTE == 3  →  0.85
    DTE == 2  →  0.70
    DTE == 1  →  0.55
    DTE == 0  →  0.40  (expiry day itself)
    DTE < 0   →  0.40  (stale expiry — treat as expiry day)
    """
    try:
        from datetime import date as _date

        expiry_str: str | None = ctx.get("expiry")
        if not expiry_str:
            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT expiry FROM scan_summaries
                    WHERE symbol = ? AND expiry IS NOT NULL
                    ORDER BY fetched_at DESC
                    LIMIT 1
                    """,
                    (symbol,),
                ).fetchone()
            if row:
                expiry_str = row["expiry"]

        if not expiry_str:
            return 1.0  # no expiry info → no decay

        expiry_date = _date.fromisoformat(str(expiry_str)[:10])
        dte = (expiry_date - _date.today()).days

        if dte > 3:
            return 1.00
        elif dte == 3:
            return 0.85
        elif dte == 2:
            return 0.70
        elif dte == 1:
            return 0.55
        else:  # dte <= 0
            return 0.40
    except Exception:
        return 1.0  # any parse/DB failure → no decay


# ---------------------------------------------------------------------------

def calculate_momentum_score(
    symbol: str,
    verdict: str,
    confidence: int,
    ctx: dict,
    broader_trend: str | None = None,
) -> int:
    """
    Score 0-100 combining trend strength, scan agreement, confidence,
    OI delta (PCR shift), IV percentile rank, and time-to-expiry decay.

    Primary components (max 100 before modifiers):
      1. Broader trend strength (0-40)  — long-horizon directional context
      2. Recent scan agreement (0-30)   — last-5-scan directional consistency
      3. Confidence contribution (0-10) — engine's own conviction
      4. OI delta / PCR shift (0-20)   — institutional positioning signal
                                          +20 when PCR shift >10 % is
                                          directionally aligned with verdict

    Modifiers applied after summing components:
      - IV percentile penalty (-10 or 0):  penalises buying when implied
        volatility is elevated (>60th pct over last 30 d); buying into
        high IV means overpaying for vega that will crush you on a move.
      - TTe decay factor (0.40 – 1.00):  discounts the score geometrically
        as expiry approaches (≤3 DTE), reflecting gamma-risk explosion and
        wider spreads near settlement.

    Pass `broader_trend` when the caller already has the result from
    get_broader_trend_from_alerts() to avoid a redundant DB round-trip.
    """
    score = 0

    # ── Component 1: broader trend strength (0-40) ─────────────────────────
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

    # ── Component 2: recent scan agreement (0-30) ──────────────────────────
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
        agreeing = sum(
            1 for row in recent_rows
            if (is_bullish(verdict) and is_bullish(row["verdict_label"] or ""))
            or (is_bearish(verdict) and is_bearish(row["verdict_label"] or ""))
        )
        score += round(agreeing / len(recent_rows) * 30)

    # ── Component 3: confidence contribution (0-10) ────────────────────────
    score += min(round(confidence * 0.10), 10)

    # ── Component 4: OI delta / PCR shift (0-20) ──────────────────────────
    score += _calc_oi_delta_bonus(symbol, verdict)

    # ── Modifier A: IV percentile penalty (-10 or 0) ──────────────────────
    # Apply BEFORE TTe decay so the penalty still scales with DTE.
    score = max(0, score + _calc_iv_percentile_penalty(symbol))

    # ── Modifier B: TTe decay (0.40 – 1.00) ───────────────────────────────
    decay = _calc_tte_decay(symbol, ctx)
    score = round(score * decay)

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
