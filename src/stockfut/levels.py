"""Stock Futures Level & Zone Engine.
Implements Section 4.1:
- Operates on daily cash-equity bars (`NSE:<SYMBOL>`).
- Identifies swing pivots (60 and 120 sessions), breakout/gap edges, EMA20/50.
- Merges nearby levels within 0.3x ATR into cohesive Zones.
- Computes zone strength based on touch count, recency, and agreeing sources.
- Shifts zones by live basis (futures LTP - cash LTP) for GTT execution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("stockfut.levels")


@dataclass
class Zone:
    low: float
    high: float
    kind: str  # "SUPPORT" | "RESISTANCE"
    strength: int  # 1 to 5+
    touch_count: int = 1
    sources: List[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return round((self.low + self.high) / 2.0, 2)


def find_swing_pivots(daily: List[Dict[str, Any]], lookback: int = 60, window: int = 3) -> List[Dict[str, Any]]:
    """
    Identify swing highs and swing lows over lookback sessions using window (local extrema).
    """
    if len(daily) < lookback:
        lookback = len(daily)
    if lookback < window * 2 + 1:
        return []

    subset = daily[-lookback:]
    pivots: List[Dict[str, Any]] = []

    for i in range(window, len(subset) - window):
        curr_high = float(subset[i]["high"])
        curr_low = float(subset[i]["low"])

        # Swing high: higher than window bars before and after
        is_high = all(curr_high >= float(subset[i - j]["high"]) and curr_high >= float(subset[i + j]["high"]) for j in range(1, window + 1))
        # Swing low: lower than window bars before and after
        is_low = all(curr_low <= float(subset[i - j]["low"]) and curr_low <= float(subset[i + j]["low"]) for j in range(1, window + 1))

        recency_weight = i / len(subset)  # 0.0 to 1.0 (recent has higher weight)

        if is_high:
            pivots.append({
                "price": curr_high,
                "kind": "RESISTANCE",
                "source": f"SWING_HIGH_{lookback}D",
                "recency": recency_weight,
            })
        if is_low:
            pivots.append({
                "price": curr_low,
                "kind": "SUPPORT",
                "source": f"SWING_LOW_{lookback}D",
                "recency": recency_weight,
            })

    return pivots


def build_zones(daily: List[Dict[str, Any]], atr: float) -> List[Zone]:
    """
    Build merged support and resistance zones from cash equity daily bars.
    Levels within 0.3 * ATR merge into a single Zone.
    """
    if not daily or atr <= 0:
        return []

    merge_thresh = 0.3 * atr

    # 1. Gather raw pivot points (60d and 120d windows)
    pivots_60 = find_swing_pivots(daily, lookback=60, window=3)
    pivots_120 = find_swing_pivots(daily, lookback=120, window=4)
    all_raw = pivots_60 + pivots_120

    # 2. Add dynamic EMA20 and EMA50 levels from latest bar
    closes = [float(b["close"]) for b in daily]
    if len(closes) >= 50:
        from src.stockfut.features import compute_ema
        ema20 = compute_ema(closes, 20)[-1]
        ema50 = compute_ema(closes, 50)[-1]
        latest_c = closes[-1]

        all_raw.append({
            "price": ema20,
            "kind": "SUPPORT" if latest_c >= ema20 else "RESISTANCE",
            "source": "EMA20",
            "recency": 1.0,
        })
        all_raw.append({
            "price": ema50,
            "kind": "SUPPORT" if latest_c >= ema50 else "RESISTANCE",
            "source": "EMA50",
            "recency": 1.0,
        })

    if not all_raw:
        return []

    # 3. Cluster into zones by kind
    support_raw = [p for p in all_raw if p["kind"] == "SUPPORT"]
    resist_raw = [p for p in all_raw if p["kind"] == "RESISTANCE"]

    def cluster_points(points: List[Dict[str, Any]], kind_label: str) -> List[Zone]:
        if not points:
            return []
        # Sort ascending by price
        sorted_p = sorted(points, key=lambda x: x["price"])
        clusters: List[List[Dict[str, Any]]] = [[sorted_p[0]]]

        for pt in sorted_p[1:]:
            prev_cluster = clusters[-1]
            cluster_mid = sum(x["price"] for x in prev_cluster) / len(prev_cluster)
            if abs(pt["price"] - cluster_mid) <= merge_thresh:
                prev_cluster.append(pt)
            else:
                clusters.append([pt])

        zones: List[Zone] = []
        for cl in clusters:
            prices = [x["price"] for x in cl]
            low_p = min(prices)
            high_p = max(prices)
            # Expand single-point clusters slightly
            if high_p == low_p:
                low_p -= 0.05 * atr
                high_p += 0.05 * atr

            touch_count = len(cl)
            unique_sources = list(set(x["source"] for x in cl))
            avg_recency = sum(x.get("recency", 0.5) for x in cl) / len(cl)

            # Strength: touches (1-3) + sources (1-2) + recency bonus (0-1)
            raw_strength = int(round(min(touch_count, 3) + min(len(unique_sources), 2) + avg_recency))
            strength = max(1, min(5, raw_strength))

            zones.append(
                Zone(
                    low=round(low_p, 2),
                    high=round(high_p, 2),
                    kind=kind_label,
                    strength=strength,
                    touch_count=touch_count,
                    sources=unique_sources,
                )
            )
        return zones

    support_zones = cluster_points(support_raw, "SUPPORT")
    resist_zones = cluster_points(resist_raw, "RESISTANCE")

    return sorted(support_zones + resist_zones, key=lambda z: z.low)


def shift_zones(zones: List[Zone], basis: float) -> List[Zone]:
    """
    Project cash equity zones to the traded futures contract by adding basis
    (futures LTP - cash LTP).
    """
    shifted: List[Zone] = []
    for z in zones:
        shifted.append(
            Zone(
                low=round(z.low + basis, 2),
                high=round(z.high + basis, 2),
                kind=z.kind,
                strength=z.strength,
                touch_count=z.touch_count,
                sources=list(z.sources),
            )
        )
    return shifted


def nearest_zone(
    zones: List[Zone],
    price: float,
    side: str,
    strong_only: bool = False,
    min_strength: int = 2,
) -> Optional[Zone]:
    """
    Find the nearest qualified zone in the given direction.
    - If side == "SUPPORT", finds nearest zone BELOW price (z.high <= price + buffer).
    - If side == "RESISTANCE", finds nearest zone ABOVE price (z.low >= price - buffer).
    """
    candidates: List[Zone] = []
    for z in zones:
        if strong_only and z.strength < min_strength:
            continue
        if side.upper() == "SUPPORT":
            if z.kind == "SUPPORT" and z.high <= price:
                candidates.append(z)
        elif side.upper() == "RESISTANCE":
            if z.kind == "RESISTANCE" and z.low >= price:
                candidates.append(z)

    if not candidates:
        return None

    if side.upper() == "SUPPORT":
        # Closest below price has maximum z.high
        return max(candidates, key=lambda z: z.high)
    else:
        # Closest above price has minimum z.low
        return min(candidates, key=lambda z: z.low)
