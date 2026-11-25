"""Evaluate NATURALGAS scan signals against forward 3-hour returns.

Reads historical non-fallback NATURALGAS rows from ``scan_summaries`` and
reports sample size, directional hit rate, and mean forward 3h return across:
  - OI verdicts (all vs confidence >= 72 vs confidence < 72)
  - Market regimes (TRENDING_UP, TRENDING_DOWN, VOLATILE, RANGE)
  - Regime x OI verdict agreement (to validate _compute_confidence calibration)
  - Prior 3h price-move buckets (to test momentum continuation vs mean-reversion)

Usage:
    python scripts/ng_signal_eval.py --db data/nsebot.db --days 30
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.engine.verdict_sets import is_bearish, is_bullish


def _parse_ts(val: str) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _match_scan(
    timestamps: list[float],
    rows: list[dict],
    target_ts: float,
    min_delta_sec: float,
    max_delta_sec: float,
) -> dict | None:
    lo = bisect_left(timestamps, target_ts - max_delta_sec)
    best = None
    best_dist = float("inf")
    for idx in range(lo, len(timestamps)):
        dt = timestamps[idx]
        if dt > target_ts + max_delta_sec:
            break
        # Note: caller passes target_ts = row_ts +/- horizon; check distance to target_ts
        dist = abs(dt - target_ts)
        if dist < best_dist:
            best_dist = dist
            best = rows[idx]
    if best is None:
        return None
    # Verify actual offset from origin wasn't smaller than min_delta_sec handled by caller
    return best


def _summarize(label: str, items: list[tuple[float, float]]) -> str:
    """items: list of (signed_aligned_return_pct, raw_return_pct)."""
    n = len(items)
    if n == 0:
        return f"{label:<38} | n=  0 | hit=  n/a | aligned=   n/a | raw=   n/a"
    hits = sum(1 for aligned, _ in items if aligned > 0)
    avg_aligned = sum(aligned for aligned, _ in items) / n
    avg_raw = sum(raw for _, raw in items) / n
    return (
        f"{label:<38} | n={n:>3} | hit={hits/n*100:5.1f}% | "
        f"aligned={avg_aligned:+6.2f}% | raw={avg_raw:+6.2f}%"
    )


def evaluate_ng_signals(db_path: str, days: int = 30, horizon_hours: float = 3.0) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    raw_rows = conn.execute(
        """
        SELECT id, fetched_at, underlying, verdict_label, confidence,
               market_regime, candle_1h, candle_3h, trend_bias
        FROM scan_summaries
        WHERE symbol = 'NATURALGAS'
          AND (is_fallback IS NULL OR is_fallback = 0)
          AND underlying > 0
          AND fetched_at >= ?
        ORDER BY fetched_at ASC
        """,
        (cutoff,),
    ).fetchall()
    conn.close()

    rows: list[dict] = []
    timestamps: list[float] = []
    for r in raw_rows:
        dt = _parse_ts(r["fetched_at"])
        if dt is None:
            continue
        d = dict(r)
        d["_ts"] = dt.timestamp()
        rows.append(d)
        timestamps.append(d["_ts"])

    horizon_sec = horizon_hours * 3600.0
    tol_sec = 1.5 * 3600.0  # allow [1.5h, 4.5h] around 3h horizon

    oi_all: list[tuple[float, float]] = []
    oi_ge72: list[tuple[float, float]] = []
    oi_lt72: list[tuple[float, float]] = []
    oi_50_71: list[tuple[float, float]] = []
    oi_lt50: list[tuple[float, float]] = []

    reg_up: list[tuple[float, float]] = []
    reg_down: list[tuple[float, float]] = []
    reg_dir_all: list[tuple[float, float]] = []
    reg_volatile: list[tuple[float, float]] = []
    reg_range: list[tuple[float, float]] = []

    agree_items: list[tuple[float, float]] = []
    disagree_items: list[tuple[float, float]] = []
    agree_ge72: list[tuple[float, float]] = []
    disagree_ge72: list[tuple[float, float]] = []

    prior_1_2: list[tuple[float, float]] = []
    prior_gt2: list[tuple[float, float]] = []

    evaluated_scans = 0
    for row in rows:
        ts = row["_ts"]
        px = float(row["underlying"])
        fwd = _match_scan(timestamps, rows, ts + horizon_sec, 0.0, tol_sec)
        if fwd is None or abs((fwd["_ts"] - ts) - horizon_sec) > tol_sec or fwd["_ts"] <= ts:
            continue
        evaluated_scans += 1
        ret_3h = (float(fwd["underlying"]) - px) / px * 100.0

        verdict = str(row.get("verdict_label") or "")
        conf = int(row.get("confidence") or 0)
        regime = str(row.get("market_regime") or "").upper().strip()

        oi_dir = 1 if is_bullish(verdict) else (-1 if is_bearish(verdict) else 0)
        if oi_dir != 0:
            aligned = oi_dir * ret_3h
            oi_all.append((aligned, ret_3h))
            if conf >= 72:
                oi_ge72.append((aligned, ret_3h))
            else:
                oi_lt72.append((aligned, ret_3h))
                if conf >= 50:
                    oi_50_71.append((aligned, ret_3h))
                else:
                    oi_lt50.append((aligned, ret_3h))

        reg_dir = 1 if regime == "TRENDING_UP" else (-1 if regime == "TRENDING_DOWN" else 0)
        if reg_dir == 1:
            reg_up.append((ret_3h, ret_3h))
            reg_dir_all.append((ret_3h, ret_3h))
        elif reg_dir == -1:
            reg_down.append((-ret_3h, ret_3h))
            reg_dir_all.append((-ret_3h, ret_3h))
        elif regime == "VOLATILE":
            reg_volatile.append((abs(ret_3h), ret_3h))
        elif regime == "RANGE":
            reg_range.append((ret_3h, ret_3h))

        if oi_dir != 0 and reg_dir != 0:
            aligned = oi_dir * ret_3h
            if oi_dir == reg_dir:
                agree_items.append((aligned, ret_3h))
                if conf >= 72:
                    agree_ge72.append((aligned, ret_3h))
            else:
                disagree_items.append((aligned, ret_3h))
                if conf >= 72:
                    disagree_ge72.append((aligned, ret_3h))

        prev = _match_scan(timestamps, rows, ts - horizon_sec, 0.0, tol_sec)
        if prev is not None and prev["_ts"] < ts and abs((ts - prev["_ts"]) - horizon_sec) <= tol_sec:
            prev_px = float(prev["underlying"])
            prior_ret = (px - prev_px) / prev_px * 100.0
            abs_prior = abs(prior_ret)
            move_sign = 1 if prior_ret > 0 else (-1 if prior_ret < 0 else 0)
            if move_sign != 0:
                if 1.0 <= abs_prior < 2.0:
                    prior_1_2.append((move_sign * ret_3h, ret_3h))
                elif abs_prior >= 2.0:
                    prior_gt2.append((move_sign * ret_3h, ret_3h))

    print(f"NATURALGAS Signal Eval | db={db_path} | window={days}d | total={len(rows)} | paired_3h={evaluated_scans}")
    print("-" * 92)
    for lbl, grp in [
        ("OI Directional (ALL)", oi_all),
        ("OI Directional (conf >= 72)", oi_ge72),
        ("OI Directional (conf < 72)", oi_lt72),
        ("OI Directional (conf 50-71)", oi_50_71),
        ("OI Directional (conf < 50)", oi_lt50),
        ("Regime TRENDING_UP (+)", reg_up),
        ("Regime TRENDING_DOWN (-)", reg_down),
        ("Regime TRENDING_* (combined)", reg_dir_all),
        ("Regime VOLATILE (raw move)", reg_volatile),
        ("Regime RANGE (raw +)", reg_range),
        ("OI x Regime AGREE", agree_items),
        ("OI x Regime DISAGREE", disagree_items),
        ("OI x Regime AGREE (conf >= 72)", agree_ge72),
        ("OI x Regime DISAGREE (conf >= 72)", disagree_ge72),
        ("Prior 3h move 1%-2% (cont hit)", prior_1_2),
        ("Prior 3h move >=2% (cont hit)", prior_gt2),
    ]:
        print(_summarize(lbl, grp))

    return {
        "total_scans": len(rows),
        "evaluated_scans": evaluated_scans,
        "oi_all": oi_all,
        "oi_ge72": oi_ge72,
        "oi_lt72": oi_lt72,
        "reg_dir_all": reg_dir_all,
        "agree_items": agree_items,
        "disagree_items": disagree_items,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate NATURALGAS scan signals vs 3h forward returns.")
    parser.add_argument("--db", default="data/nsebot.db", help="Path to SQLite database")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days")
    parser.add_argument("--horizon-hours", type=float, default=3.0, help="Forward return horizon in hours")
    args = parser.parse_args()
    evaluate_ng_signals(args.db, days=args.days, horizon_hours=args.horizon_hours)


if __name__ == "__main__":
    main()
