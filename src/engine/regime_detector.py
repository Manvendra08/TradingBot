"""
Market Regime Detector — classifies market state from last 10 scan summaries.
B2 fix: prices reversed to oldest→newest before direction calculation.
B4 fix: uses is_bullish/is_bearish from verdict_sets (explicit set membership).
B7 fix: int(n*0.7) truncation replaced with math.ceil for correct 70% threshold.
B8 fix: excludes is_fallback=1 rows so stale-price inserts can't poison regime.
B9 fix: exponential recency weighting — newest scan outweighs oldest by ~5x
        so a mid-session breakout isn't suppressed by stale morning scans.
"""
from __future__ import annotations

import logging
import math

from src.models.schema import get_conn
from src.engine.verdict_sets import is_bullish, is_bearish

log = logging.getLogger(__name__)

REGIME_TRENDING_UP   = "TRENDING_UP"
REGIME_TRENDING_DOWN = "TRENDING_DOWN"
REGIME_RANGE         = "RANGE"
REGIME_VOLATILE      = "VOLATILE"
REGIME_NO_TRADE      = "NO_TRADE"

# Decay factor per step back in time.
# weight[i] = DECAY^(n-1-i), i=0 oldest, i=n-1 newest.
# DECAY=0.80 → newest weight=1.0, 9 steps back ≈ 0.134 (~7.4x difference).
_DECAY = 0.80


def detect_market_regime(symbol: str) -> str:
    """
    Classify market regime from last 10 non-fallback scan summaries.
    Returns one of: TRENDING_UP, TRENDING_DOWN, RANGE, VOLATILE, NO_TRADE.

    Verdict votes are exponentially weighted by recency (newest = weight 1.0,
    each step back multiplied by _DECAY) so a recent directional breakout
    overrides stale opposing scans from earlier in the session.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT verdict_label, underlying, confidence
            FROM scan_summaries
            WHERE symbol = ?
              AND (is_fallback IS NULL OR is_fallback = 0)
            ORDER BY fetched_at DESC
            LIMIT 10
            """,
            (symbol,),
        ).fetchall()

    if len(rows) < 5:
        return REGIME_NO_TRADE

    # rows are DESC (newest first) — reverse so oldest→newest for price calc
    rows = list(reversed(rows))
    n = len(rows)

    # --- Price direction (half-avg, oldest→newest) ---
    prices = [float(r["underlying"]) for r in rows if r["underlying"] and float(r["underlying"]) > 0]
    if len(prices) < 5:
        return REGIME_NO_TRADE

    mid = len(prices) // 2
    first_half_avg  = sum(prices[:mid]) / mid
    second_half_avg = sum(prices[mid:]) / len(prices[mid:])
    price_change_pct = (second_half_avg - first_half_avg) / first_half_avg * 100
    price_range_pct  = (max(prices) - min(prices)) / min(prices) * 100

    # --- Recency-weighted verdict scores ---
    # i=0 → oldest → weight = _DECAY^(n-1)
    # i=n-1 → newest → weight = _DECAY^0 = 1.0
    bullish_score = 0.0
    bearish_score = 0.0
    total_weight  = 0.0

    for i, row in enumerate(rows):
        w = _DECAY ** (n - 1 - i)
        total_weight += w
        label = row["verdict_label"] or ""
        if is_bullish(label):
            bullish_score += w
        elif is_bearish(label):
            bearish_score += w

    # Threshold: 70% of total weight (float, no truncation issue)
    threshold = total_weight * 0.70

    if bullish_score >= threshold and price_change_pct > 0.5:
        return REGIME_TRENDING_UP
    if bearish_score >= threshold and price_change_pct < -0.5:
        return REGIME_TRENDING_DOWN
    if price_range_pct > 3.0:
        return REGIME_VOLATILE
    if abs(price_change_pct) < 0.3 and abs(bullish_score - bearish_score) <= total_weight * 0.2:
        return REGIME_RANGE
    return REGIME_NO_TRADE


def regime_score_for_trade(regime: str, option_type: str) -> int:
    """
    Score 0-100: how favorable is this regime for a long-option trade.
    Long options decay in RANGE; whipsaw in VOLATILE.
    """
    if regime == REGIME_TRENDING_UP and option_type == "CE":
        return 100
    if regime == REGIME_TRENDING_DOWN and option_type == "PE":
        return 100
    if regime in (REGIME_TRENDING_UP, REGIME_TRENDING_DOWN):
        return 70   # trending but counter-direction
    if regime == REGIME_RANGE:
        return 30   # theta decay kills long options
    if regime == REGIME_VOLATILE:
        return 40   # whipsaw risk
    return 50       # NO_TRADE / unknown — neutral score
