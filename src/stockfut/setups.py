"""Stock Futures Setups & Watchlist Engine.
Implements Section 4.3:
- Step 1: Stock Bias scoring (-4 to +4) -> STRONG_BULL, BULL, NEUTRAL, BEAR, STRONG_BEAR
- Step 2: Bias x Regime matrix permission and minimum score
- Step 3: Daily Reversal Candle evaluation (touch + reversal close via upper 35% or prior close + 20d OI)
- Step 4: 09:30 Open Gate check (within 0.3x ATR, SL unbreached, spread ok)
- Watchlist builder capped at 20 names, frozen into scan snapshots.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config.stockfut_loader import load_stockfut_config
from src.engine.capital_allocator import calculate_future_lots
from src.engine.trade_plan import calculate_future_sl_target
from src.models.scan_snapshot import create_scan_snapshot
from src.models.schema import insert_stockfut_watchlist, log_stockfut_decision
from src.stockfut.levels import Zone, shift_zones
from src.stockfut.universe import Contract

log = logging.getLogger("stockfut.setups")


@dataclass
class WatchRow:
    scan_date: str
    scan_time: str
    symbol: str
    tradingsymbol: str
    expiry: str
    side: str  # "LONG" | "SHORT"
    bias: str
    score: int
    planned_entry: float
    sl: float
    t1: float
    t2: float
    lots: int
    lot_size: int
    r_multiple: float
    status: str = "ARMED"  # ARMED | TRIGGERED | EXPIRED | CANCELLED | SKIPPED
    skip_reason: Optional[str] = None
    snapshot_id: Optional[str] = None


def stock_bias(f: Dict[str, Any]) -> int:
    """
    Score stock bias from 4 inputs (-4 to +4):
    1. Daily Trend: EMA20 > EMA50, both rising, ADX >= 20 (+1 / -1)
    2. Relative Strength: RS rank >= 70 vs NIFTY and sector (+1 / -1)
    3. 20-day OI flow: Long Buildup or Short Covering (+1 / -1)
    4. Sector: sector RS >= 66 (+1 / -1)
    """
    score = 0

    # 1. Daily trend
    c = float(f.get("close", 0.0))
    ema20 = float(f.get("ema20", 0.0))
    ema50 = float(f.get("ema50", 0.0))
    ema20_slope = float(f.get("ema20_slope", 0.0))
    ema50_slope = float(f.get("ema50_slope", 0.0))
    adx = float(f.get("adx14", 0.0))

    if ema20 > ema50 and ema20_slope > 0 and ema50_slope > 0 and adx >= 20:
        score += 1
    elif ema20 < ema50 and ema20_slope < 0 and ema50_slope < 0 and adx >= 20:
        score -= 1

    # 2. Relative strength
    rs_idx = float(f.get("rs_rank_index", 50.0))
    rs_sec = float(f.get("rs_rank_sector", 50.0))
    if rs_idx >= 70.0 and rs_sec >= 70.0:
        score += 1
    elif rs_idx <= 30.0 and rs_sec <= 30.0:
        score -= 1

    # 3. 20-day OI flow (Trend OI)
    oi_20d = str(f.get("oi_flow_20d", "NONE")).upper()
    if oi_20d in ("LONG_BUILDUP", "SHORT_COVERING"):
        score += 1
    elif oi_20d in ("SHORT_BUILDUP", "LONG_UNWINDING"):
        score -= 1

    # 4. Sector RS
    if rs_sec >= 66.0:
        score += 1
    elif rs_sec <= 33.0:
        score -= 1

    return score


def bias_label(score: int) -> str:
    """Map bias score (-4..+4) to discrete bias label."""
    if score >= 3:
        return "STRONG_BULL"
    elif score == 2:
        return "BULL"
    elif score in (-1, 0, 1):
        return "NEUTRAL"
    elif score == -2:
        return "BEAR"
    else:
        return "STRONG_BEAR"


def matrix_permission(bias: str, regime: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[Tuple[str, int]]:
    """
    Evaluate bias x market regime permission matrix.
    Returns (side, min_score) if allowed, else None.
    """
    config = cfg or load_stockfut_config()
    perms = config.get("matrix_permissions", {})
    bias_row = perms.get(bias.upper(), {})
    cell = bias_row.get(regime.upper())
    if not cell or not isinstance(cell, dict):
        return None
    side = str(cell.get("side", "")).upper()
    min_score = int(cell.get("min_score", 70))
    return side, min_score


def calculate_setup_score(f: Dict[str, Any], bias: str, regime: str) -> int:
    """
    Calculate composite setup score 0-100:
    - Trend: 25
    - Relative Strength: 25
    - OI Flow: 30
    - Regime fit: 20
    """
    trend_score = 0
    adx = float(f.get("adx14", 20.0))
    adx_pts = min(15, int(adx * 0.5))
    slope_pts = 10 if abs(float(f.get("ema20_slope", 0.0))) > 0.1 else 5
    trend_score = min(25, adx_pts + slope_pts)

    rs_score = 0
    rs_idx = float(f.get("rs_rank_index", 50.0))
    rs_sec = float(f.get("rs_rank_sector", 50.0))
    rs_score = int(min(25, (rs_idx + rs_sec) * 0.25))

    oi_score = 0
    oi_20d = str(f.get("oi_flow_20d", "NONE"))
    oi_5d = str(f.get("oi_flow_5d", "NONE"))
    if "BUILDUP" in oi_20d:
        oi_score += 20
    elif "COVERING" in oi_20d or "UNWINDING" in oi_20d:
        oi_score += 10
    if float(f.get("vol_vs_avg", 1.0)) >= 1.2:
        oi_score += 10
    oi_score = min(30, oi_score)

    regime_score = 0
    if (bias in ("STRONG_BULL", "BULL") and regime == "RISK_ON") or \
       (bias in ("STRONG_BEAR", "BEAR") and regime == "RISK_OFF"):
        regime_score = 20
    elif regime == "NEUTRAL":
        regime_score = 12
    else:
        regime_score = 5

    total = trend_score + rs_score + oi_score + regime_score
    return max(0, min(100, total))


def is_daily_reversal(
    side: str,
    daily: List[Dict[str, Any]],
    zones: List[Zone],
    atr: float,
    oi_flow_20d: str,
) -> Optional[str]:
    """
    Evaluate whether the latest completed daily bar forms a valid reversal setup.
    Section 4.3 Step 3:
    1. Touch: Low touches/tests zone or EMA20 within 0.3 * ATR (mirror for Short).
    2. Reversal close:
       (a) Close in upper 35% of bar's range -> "UPPER_35"
       (b) Close above previous day's close AND 20d OI flow agrees -> "PRIOR_CLOSE_OI"
    """
    if not daily or len(daily) < 2 or atr <= 0:
        return None

    bar = daily[-1]
    prev_bar = daily[-2]

    high = float(bar["high"])
    low = float(bar["low"])
    close = float(bar["close"])
    prev_close = float(prev_bar["close"])
    bar_range = high - low

    side_upper = side.upper()
    is_long = side_upper in ("LONG", "BUY")

    if is_long:
        # 1. Touch: low within 0.3 * ATR of a support zone or EMA20
        touch_buffer = 0.3 * atr
        support_levels = [z.high for z in zones if z.kind == "SUPPORT"]
        closes_list = [float(b["close"]) for b in daily]
        if len(closes_list) >= 20:
            from src.stockfut.features import compute_ema
            ema20 = compute_ema(closes_list, 20)[-1]
            support_levels.append(ema20)

        touched = any(abs(low - lvl) <= touch_buffer or (low <= lvl <= high) for lvl in support_levels)
        if not touched:
            return None

        # 2. Reversal Close
        # Branch (a): Close in upper 35% of bar's own range
        if bar_range > 0:
            pct_in_range = (close - low) / bar_range
            if pct_in_range >= 0.65:  # upper 35%
                return "UPPER_35"

        # Branch (b): Close above previous day's close AND 20-day OI flow is net Long Buildup
        if close > prev_close and oi_flow_20d == "LONG_BUILDUP":
            return "PRIOR_CLOSE_OI"

        return None
    else:
        # SHORT: Mirror image
        touch_buffer = 0.3 * atr
        resist_levels = [z.low for z in zones if z.kind == "RESISTANCE"]
        closes_list = [float(b["close"]) for b in daily]
        if len(closes_list) >= 20:
            from src.stockfut.features import compute_ema
            ema20 = compute_ema(closes_list, 20)[-1]
            resist_levels.append(ema20)

        touched = any(abs(high - lvl) <= touch_buffer or (low <= lvl <= high) for lvl in resist_levels)
        if not touched:
            return None

        # Branch (a): Close in lower 35% of bar's own range
        if bar_range > 0:
            pct_in_range = (close - low) / bar_range
            if pct_in_range <= 0.35:  # lower 35%
                return "LOWER_35"

        # Branch (b): Close below previous day's close AND 20-day OI flow is Short Buildup
        if close < prev_close and oi_flow_20d == "SHORT_BUILDUP":
            return "PRIOR_CLOSE_OI"

        return None


def open_check(
    row: WatchRow,
    quote: Dict[str, Any],
    atr: float,
    max_drift_atr: float = 0.3,
    max_spread_pct: float = 0.05,
) -> Tuple[bool, str]:
    """
    09:30 Morning Gate Check.
    - Price within 0.3x ATR of planned trigger price.
    - SL unbreached at live price.
    - Spread within limit.
    """
    if not quote:
        return False, "MISSING_QUOTE"

    ltp = float(quote.get("price") or 0.0)
    bid = float(quote.get("bid") or ltp)
    ask = float(quote.get("ask") or ltp)

    if ltp <= 0:
        return False, "INVALID_LTP"

    # 1. Price drift check
    drift = abs(ltp - row.planned_entry)
    if drift > max_drift_atr * atr:
        return False, f"PRICE_DRIFT_EXCEEDED ({drift:.2f} > {max_drift_atr * atr:.2f})"

    # 2. SL breach check
    if row.side == "LONG" and ltp <= row.sl:
        return False, f"SL_BREACHED_AT_OPEN ({ltp:.2f} <= {row.sl:.2f})"
    elif row.side == "SHORT" and ltp >= row.sl:
        return False, f"SL_BREACHED_AT_OPEN ({ltp:.2f} >= {row.sl:.2f})"

    # 3. Spread check
    if bid > 0 and ask >= bid:
        spread_pct = ((ask - bid) / ltp) * 100.0
        if spread_pct > max_spread_pct:
            return False, f"WIDE_SPREAD ({spread_pct:.3f}% > {max_spread_pct:.3f}%)"

    return True, "OK"


def build_watchlist(
    universe: List[Contract],
    features_map: Dict[str, Dict[str, Any]],
    zones_map: Dict[str, List[Zone]],
    regime: str,
    asof_date: str,
    asof_time: str = "19:30:00",
    cfg: Optional[Dict[str, Any]] = None,
    capital: float = 10000000.0,
    daily_candles_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    live_basis_map: Optional[Dict[str, float]] = None,
) -> List[WatchRow]:
    """
    Build the EOD ARMED watchlist (max 20 names, sorted by score descending).
    Freezes scan snapshot for each candidate.
    """
    config = cfg or load_stockfut_config()
    budget_pct = float(config.get("risk_budget_pct", 0.75))
    target_r_ratio = float(config.get("target_r_ratio", 1.6))
    max_risk_mult = float(config.get("max_risk_multiplier", 1.25))

    candidates: List[WatchRow] = []

    for contract in universe:
        sym = contract.symbol
        feat = features_map.get(sym)
        if not feat:
            continue

        raw_zones = zones_map.get(sym, [])
        if not raw_zones:
            continue

        # Shift zones by basis to match futures contract
        basis = (live_basis_map or {}).get(sym, 0.0)
        fut_zones = shift_zones(raw_zones, basis=basis)

        # 1. Bias
        b_score = stock_bias(feat)
        b_label = bias_label(b_score)
        if b_label == "NEUTRAL":
            continue

        # 2. Permission and minimum score
        perm = matrix_permission(b_label, regime, cfg=config)
        if not perm:
            continue
        side, min_score = perm

        # 3. Composite score
        score = calculate_setup_score(feat, b_label, regime)
        if score < min_score:
            continue

        # 4. Daily reversal check
        daily_bars = (daily_candles_map or {}).get(sym, [])
        atr = float(feat.get("atr14", 0.0))
        oi_20d = str(feat.get("oi_flow_20d", "NONE"))

        rev_kind = is_daily_reversal(side=side, daily=daily_bars, zones=fut_zones, atr=atr, oi_flow_20d=oi_20d)
        if not rev_kind:
            continue

        # 5. SL and Target geometry
        entry_price = float(feat.get("close", 0.0)) + basis
        plan = calculate_future_sl_target(
            entry=entry_price,
            side=side,
            zones=fut_zones,
            atr=atr,
            target_r_ratio=target_r_ratio,
        )
        if not plan:
            log_stockfut_decision(
                kind="SKIP",
                symbol=sym,
                payload={"reason": "GEOMETRY_FAILED", "side": side, "entry": entry_price},
            )
            continue

        # 6. Sizing
        lots, skip_reason = calculate_future_lots(
            sl_distance=plan["sl_distance"],
            lot_size=contract.lot_size,
            capital=capital,
            budget_pct=budget_pct,
            max_risk_mult=max_risk_mult,
        )
        if skip_reason:
            log_stockfut_decision(
                kind="SKIP",
                symbol=sym,
                payload={"reason": skip_reason, "lots": lots, "sl_dist": plan["sl_distance"]},
            )
            continue

        # 7. Create Scan Snapshot for provenance
        snapshot_payload = {
            "symbol": sym,
            "tradingsymbol": contract.tradingsymbol,
            "side": side,
            "bias": b_label,
            "score": score,
            "plan": plan,
            "features": feat,
            "regime": regime,
            "reversal_kind": rev_kind,
        }
        snap = create_scan_snapshot(
            symbol=sym,
            underlying=entry_price,
            expiry=contract.expiry,
            option_rows=[],
            engine_verdict=b_label,
            engine_confidence=score,
            intel=snapshot_payload,
        )

        row = WatchRow(
            scan_date=asof_date,
            scan_time=asof_time,
            symbol=sym,
            tradingsymbol=contract.tradingsymbol,
            expiry=contract.expiry,
            side=side,
            bias=b_label,
            score=score,
            planned_entry=round(entry_price, 2),
            sl=plan["sl"],
            t1=plan["t1"],
            t2=plan["t2"],
            lots=lots,
            lot_size=contract.lot_size,
            r_multiple=plan["r_t1"],
            status="ARMED",
            snapshot_id=snap.snapshot_id,
        )
        candidates.append(row)

    # Sort by score descending and cap at 20
    candidates.sort(key=lambda x: x.score, reverse=True)
    top_watchlist = candidates[:20]

    # Persist to database
    for row in top_watchlist:
        try:
            insert_stockfut_watchlist({
                "scan_date": row.scan_date,
                "scan_time": row.scan_time,
                "symbol": row.symbol,
                "tradingsymbol": row.tradingsymbol,
                "expiry": row.expiry,
                "side": row.side,
                "bias": row.bias,
                "score": row.score,
                "planned_entry": row.planned_entry,
                "sl": row.sl,
                "t1": row.t1,
                "t2": row.t2,
                "lots": row.lots,
                "lot_size": row.lot_size,
                "r_multiple": row.r_multiple,
                "status": row.status,
                "skip_reason": row.skip_reason,
                "snapshot_id": row.snapshot_id,
            })
        except Exception as e:
            log.warning("Failed persisting watchlist row for %s: %s", row.symbol, e)

    return top_watchlist
