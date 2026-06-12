"""
Market Regime Detector — classifies market state from last 10 scan summaries.
B2 fix: prices reversed to oldest→newest before direction calculation.
B4 fix: uses is_bullish/is_bearish from verdict_sets (explicit set membership).
B7 fix: int(n*0.7) truncation replaced with math.ceil for correct 70% threshold.
B8 fix: excludes is_fallback=1 rows so stale-price inserts can't poison regime.
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


def detect_market_regime(symbol: str) -> str:
    """
    Classify market regime from last 10 non-fallback scan summaries.
    Returns one of: TRENDING_UP, TRENDING_DOWN, RANGE, VOLATILE, NO_TRADE.
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

    # B2 fix: rows are DESC (newest first) — reverse so oldest→newest
    rows = list(reversed(rows))

    prices = [float(r["underlying"]) for r in rows if r["underlying"] and float(r["underlying"]) > 0]
    if len(prices) < 5:
        return REGIME_NO_TRADE

    # Direction: compare first half avg vs second half avg (oldest→newest)
    mid = len(prices) // 2
    first_half_avg = sum(prices[:mid]) / mid
    second_half_avg = sum(prices[mid:]) / len(prices[mid:])
    price_change_pct = (second_half_avg - first_half_avg) / first_half_avg * 100

    # Volatility: price range over the window
    price_range_pct = (max(prices) - min(prices)) / min(prices) * 100

    # Verdict counts using explicit set membership (B4 fix)
    bullish_count = sum(1 for r in rows if is_bullish(r["verdict_label"] or ""))
    bearish_count = sum(1 for r in rows if is_bearish(r["verdict_label"] or ""))
    n = len(rows)

    # B7 fix: use math.ceil so threshold is genuinely >=70%, not truncated
    threshold = math.ceil(n * 0.7)

    if bullish_count >= threshold and price_change_pct > 0.5:
        return REGIME_TRENDING_UP
    if bearish_count >= threshold and price_change_pct < -0.5:
        return REGIME_TRENDING_DOWN
    if price_range_pct > 3.0:
        return REGIME_VOLATILE
    if abs(price_change_pct) < 0.3 and abs(bullish_count - bearish_count) <= 2:
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
