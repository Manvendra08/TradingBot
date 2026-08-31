"""Contra (counter-trend) trade detection for NSEBOT.

Evaluates whether a signal that opposes the broader trend qualifies as a
high-quality contra setup.  Requires stronger confirmation than trend trades:
multiple consecutive scans in the reversal direction, OI/PCR divergence,
and sufficient confidence.

Integrated into decision_pipeline.py as Priority 1.5 in hybrid mode —
between CONFIRMED_REVERSAL (Priority 1) and TREND_CONTINUATION (Priority 2).
"""
from __future__ import annotations

import logging

from config.settings import (
    CONTRA_ENABLED,
    CONTRA_MIN_CONFIDENCE,
    CONTRA_CONFIRM_SCANS,
    CONTRA_MAX_LOOKBACK,
    CONTRA_ALLOWED_REGIMES,
    CONTRA_REQUIRE_DIVERGENCE,
    CONTRA_PCR_MOVE,
    CONTRA_MIN_SCORE,
    CONTRA_RISK_SCALE,
    CONTRA_SL_SCALE,
    CONTRA_MAX_PER_DAY,
)
from src.models.schema import get_conn
from src.engine.verdict_sets import is_bullish, is_bearish

log = logging.getLogger(__name__)


def evaluate_contra_setup(
    symbol: str,
    verdict: str,
    confidence: int,
    broader_trend: str,
    regime: str = "",
) -> tuple[bool, int, str, dict]:
    """Evaluate whether this signal qualifies as a contra trade.

    Returns:
        (is_contra, score, reason, checks_dict)
    """
    checks: dict = {}

    # ── Gate 0: Feature enabled ────────────────────────────────────────────
    if not CONTRA_ENABLED:
        return False, 0, "Contra trades disabled", checks

    # ── Gate 1: Must oppose a DIRECTIONAL broader trend ────────────────────
    v_bull, v_bear = is_bullish(verdict), is_bearish(verdict)
    bt_bull = "Bullish" in broader_trend
    bt_bear = "Bearish" in broader_trend

    is_counter = (v_bull and bt_bear) or (v_bear and bt_bull)
    checks["counter_trend"] = is_counter
    if not is_counter:
        return False, 0, f"Not counter-trend (verdict={verdict}, broader={broader_trend})", checks

    # ── Gate 2: Confidence threshold ───────────────────────────────────────
    checks["confidence"] = confidence >= CONTRA_MIN_CONFIDENCE
    if not checks["confidence"]:
        return False, 0, (
            f"Confidence {confidence}% below contra threshold {CONTRA_MIN_CONFIDENCE}%"
        ), checks

    # ── Gate 3: Regime filter ──────────────────────────────────────────────
    if CONTRA_ALLOWED_REGIMES and regime and regime not in CONTRA_ALLOWED_REGIMES:
        checks["regime"] = False
        return False, 0, f"Contra not allowed in regime: {regime}", checks
    checks["regime"] = True

    # ── Gate 4: Consecutive confirming scans ───────────────────────────────
    confirming, total_fetched = _count_confirming_scans(symbol, verdict)
    checks["confirming_scans"] = confirming
    checks["total_fetched"] = total_fetched
    if confirming < CONTRA_CONFIRM_SCANS:
        return False, 0, (
            f"Only {confirming}/{CONTRA_CONFIRM_SCANS} consecutive confirming scans "
            f"(fetched {total_fetched})"
        ), checks

    # ── Gate 5: PCR/OI divergence (optional) ──────────────────────────────
    divergence = _check_pcr_divergence(symbol, verdict)
    checks["divergence"] = divergence
    if CONTRA_REQUIRE_DIVERGENCE and not divergence:
        return False, 0, "No PCR divergence to support reversal", checks

    # ── Composite score ────────────────────────────────────────────────────
    score = _compute_contra_score(confidence, confirming, divergence, broader_trend)
    checks["score"] = score

    if score < CONTRA_MIN_SCORE:
        return False, score, f"Contra score {score} below threshold {CONTRA_MIN_SCORE}", checks

    reason = (
        f"CONTRA {verdict} vs {broader_trend} | conf={confidence}% | "
        f"{confirming} confirming scans | divergence={divergence} | score={score}"
    )
    return True, score, reason, checks


def _count_confirming_scans(symbol: str, verdict: str) -> tuple[int, int]:
    """Count consecutive recent scans agreeing with *verdict* direction.

    Returns (consecutive_count, total_rows_fetched).
    Uses OFFSET 1 to skip the current scan (same convention as
    detect_reversal_from_scans with skip_latest=True).
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT verdict_label FROM scan_summaries
                WHERE symbol = ?
                  AND (is_fallback IS NULL OR is_fallback = 0)
                  AND fetched_at >= datetime('now', '-24 hours')
                ORDER BY fetched_at DESC
                LIMIT ? OFFSET 1
                """,
                (symbol, CONTRA_MAX_LOOKBACK),
            ).fetchall()
    except Exception:
        return 0, 0

    count = 0
    for row in rows:
        label = row["verdict_label"] or ""
        if (is_bullish(verdict) and is_bullish(label)) or \
           (is_bearish(verdict) and is_bearish(label)):
            count += 1
        else:
            break  # must be consecutive
    return count, len(rows)


def _check_pcr_divergence(symbol: str, verdict: str) -> bool:
    """Check if PCR shifted against the broader trend (institutional reversal signal).

    - Bearish verdict + PCR rising > CONTRA_PCR_MOVE → put accumulation → reversal signal
    - Bullish verdict + PCR falling > CONTRA_PCR_MOVE → put unwinding → reversal signal
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT pcr FROM scan_summaries
                WHERE symbol = ? AND pcr IS NOT NULL
                  AND (is_fallback IS NULL OR is_fallback = 0)
                ORDER BY fetched_at DESC
                LIMIT 4 OFFSET 1
                """,
                (symbol,),
            ).fetchall()

        if len(rows) < 3:
            return False

        pcr_recent = float(rows[0]["pcr"])
        pcr_older = float(rows[2]["pcr"])
        move = pcr_recent - pcr_older

        if is_bearish(verdict) and move > CONTRA_PCR_MOVE:
            return True
        if is_bullish(verdict) and move < -CONTRA_PCR_MOVE:
            return True
    except Exception:
        pass
    return False


def _compute_contra_score(
    confidence: int,
    confirming_scans: int,
    divergence: bool,
    broader_trend: str,
) -> int:
    """Compute composite contra score (0-100).

    Components:
      - Confidence contribution:  max(0, confidence - 60)   → 0-35 pts
      - Confirming scans:         confirming_scans × 10     → 0-30 pts
      - PCR divergence bonus:     20 if present             → 0-20 pts
      - Strong trend penalty:     -15 if fading strong trend
    """
    score = 0
    score += max(0, confidence - 60)          # conf 75→15, 85→25, 95→35
    score += confirming_scans * 10            # 2→20, 3→30
    score += 20 if divergence else 0
    if "Strong" in broader_trend:
        score -= 15                           # fading a strong trend is riskier
    return max(0, min(100, int(score)))


def contra_position_size(base_qty: float, contra_count_today: int) -> tuple[float, str]:
    """Scale position size for contra trades and enforce daily cap.

    Returns:
        (adjusted_qty, block_reason)  — block_reason is empty string if allowed.
    """
    if contra_count_today >= CONTRA_MAX_PER_DAY:
        return 0.0, f"Daily contra limit reached ({contra_count_today}/{CONTRA_MAX_PER_DAY})"
    return base_qty * CONTRA_RISK_SCALE, ""


def count_contra_trades_today(symbol: str, table: str = "paper_trades") -> int:
    """Count CONTRA_REVERSAL trades opened today for a symbol."""
    try:
        with get_conn(read_only=True) as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt FROM {table}
                WHERE symbol = ?
                  AND setup_type = 'CONTRA_REVERSAL'
                  AND DATE(opened_at) = DATE('now')
                """,
                (symbol,),
            ).fetchone()
            return int(row["cnt"]) if row else 0
    except Exception:
        return 0
