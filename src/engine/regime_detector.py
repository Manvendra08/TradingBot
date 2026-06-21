"""
Market Regime Detector — classifies market state from last 10 scan summaries.
B2 fix: prices reversed to oldest→newest before direction calculation.
B4 fix: uses is_bullish/is_bearish from verdict_sets (explicit set membership).
B7 fix: int(n*0.7) truncation replaced with math.ceil for correct 70% threshold.
B8 fix: excludes is_fallback=1 rows so stale-price inserts can't poison regime.
B9 fix: exponential recency weighting — newest scan outweighs oldest by ~5x
        so a mid-session breakout isn't suppressed by stale morning scans.
P3 fix (#10): Added explicit REGIME_RANGE branch before the REGIME_NO_TRADE
        catch-all. Low-vol mid-sessions where abs(price_change_pct) < 0.5 and
        price_range_pct < 1.5 now correctly classify as RANGE (regime_score=30)
        rather than NO_TRADE (regime_score=50 in research mode, hard-block live).
        Thresholds are sourced from REGIME_RANGE_MAX_CHANGE_PCT and
        REGIME_RANGE_MAX_RANGE_PCT in settings.py.
M3 fix: Time-weighted decay — combines index-based decay with wall-clock time
        gap weighting so scans separated by hours (e.g. failed/skipped scans)
        are down-weighted appropriately, not treated as adjacent.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from src.models.schema import get_conn
from src.engine.verdict_sets import is_bullish, is_bearish
from config.settings import REGIME_RANGE_MAX_CHANGE_PCT, REGIME_RANGE_MAX_RANGE_PCT

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

    Decision order:
      1. TRENDING_UP   — weighted bullish score >= 70% AND price_change > +0.5%
      2. TRENDING_DOWN — weighted bearish score >= 70% AND price_change < -0.5%
      3. VOLATILE      — price_range_pct > 3.0%
      4. RANGE         — low directional movement (abs change < REGIME_RANGE_MAX_CHANGE_PCT
                         AND range < REGIME_RANGE_MAX_RANGE_PCT) — explicit branch
                         before NO_TRADE so quiet mid-sessions are classified correctly.
      5. NO_TRADE      — catch-all for ambiguous conditions.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT verdict_label, underlying, confidence, fetched_at
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
    # M3 fix: combine index-based decay with wall-clock time gap weighting.
    # Time decay: exp(-time_gap_hours / DECAY_HOURS) where DECAY_HOURS=1.5
    # means a scan from 1.5 hours ago has ~37% weight vs newest scan.
    # This ensures that scans separated by hours (e.g. failed/skipped scans)
    # are down-weighted appropriately, not treated as adjacent.
    DECAY_HOURS = 1.5
    now_utc = datetime.now(timezone.utc)

    bullish_score = 0.0
    bearish_score = 0.0
    total_weight  = 0.0

    # Parse newest timestamp for time-gap calculation
    newest_ts = None
    try:
        newest_raw = rows[-1]["fetched_at"]
        if newest_raw:
            newest_ts = datetime.fromisoformat(str(newest_raw).replace("Z", "+00:00"))
    except Exception:
        pass

    for i, row in enumerate(rows):
        # Index-based decay (unchanged)
        index_w = _DECAY ** (n - 1 - i)

        # M3: Time-based decay
        time_w = 1.0
        if newest_ts and row["fetched_at"]:
            try:
                row_ts = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
                gap_hours = (newest_ts - row_ts).total_seconds() / 3600.0
                time_w = math.exp(-max(0, gap_hours) / DECAY_HOURS)
            except Exception:
                pass

        # Combine: geometric mean of index and time weights
        w = math.sqrt(index_w * time_w)
        total_weight += w
        label = row["verdict_label"] or ""
        if is_bullish(label):
            bullish_score += w
        elif is_bearish(label):
            bearish_score += w

    threshold = total_weight * 0.70

    if bullish_score >= threshold and price_change_pct > 0.5:
        return REGIME_TRENDING_UP
    if bearish_score >= threshold and price_change_pct < -0.5:
        return REGIME_TRENDING_DOWN
    if price_range_pct > 3.0:
        return REGIME_VOLATILE
    # Explicit RANGE branch (#10): low-vol mid-sessions that previously fell
    # through to NO_TRADE are now classified as RANGE (regime_score=30).
    # This is more honest — the market is ranging, not unclassifiable.
    if (abs(price_change_pct) < REGIME_RANGE_MAX_CHANGE_PCT
            and price_range_pct < REGIME_RANGE_MAX_RANGE_PCT):
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
