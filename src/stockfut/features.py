"""Stock Futures Feature Engineering Engine.
Implements Section 3.2:
- Trend: EMA20, EMA50 slopes, ADX14
- Relative Strength: 20-day return rank vs Nifty and Sector
- OI Flow: 1d, 3d, 5d, 20d with 3% noise floor on combined near+next month OI
- Volume vs 20-day average
- Daily ATR(14) - strictly daily
- NR7 / Squeeze flag
- Market Regime: Nifty trend, universe breadth (% above EMA50), India VIX -> RISK_ON | NEUTRAL | RISK_OFF
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("stockfut.features")


def classify_oi_flow(price_pct: float, oi_pct: float, noise: float = 3.0) -> str:
    """
    Classify derivative price/OI flow quadrant with a noise floor on OI change.
    - Price ↑, OI ↑ (> noise)  -> LONG_BUILDUP
    - Price ↓, OI ↑ (> noise)  -> SHORT_BUILDUP
    - Price ↑, OI ↓ (< -noise) -> SHORT_COVERING
    - Price ↓, OI ↓ (< -noise) -> LONG_UNWINDING
    - OI change within [-noise, +noise] -> NONE
    """
    if abs(oi_pct) < noise:
        return "NONE"

    if price_pct > 0:
        return "LONG_BUILDUP" if oi_pct > 0 else "SHORT_COVERING"
    elif price_pct < 0:
        return "SHORT_BUILDUP" if oi_pct > 0 else "LONG_UNWINDING"
    return "NONE"


def compute_ema(series: List[float], period: int) -> List[float]:
    """Calculate Exponential Moving Average."""
    if not series or period <= 0:
        return []
    if len(series) < period:
        return [series[-1]] * len(series)

    k = 2.0 / (period + 1.0)
    ema_vals = [float(series[0])]
    for val in series[1:]:
        ema_vals.append(float(val) * k + ema_vals[-1] * (1.0 - k))
    return ema_vals


def compute_atr(daily: List[Dict[str, Any]], period: int = 14) -> float:
    """Calculate 14-period Average True Range from daily candles."""
    if len(daily) < 2:
        return 0.0

    trs: List[float] = []
    for i in range(1, len(daily)):
        high = float(daily[i]["high"])
        low = float(daily[i]["low"])
        prev_close = float(daily[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    if not trs:
        return 0.0
    if len(trs) < period:
        return sum(trs) / len(trs)

    # Wilder's smoothing
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 2)


def compute_adx(daily: List[Dict[str, Any]], period: int = 14) -> float:
    """Calculate Average Directional Index (ADX14)."""
    if len(daily) < period * 2:
        return 20.0  # safe neutral fallback

    plus_dm: List[float] = []
    minus_dm: List[float] = []
    tr_list: List[float] = []

    for i in range(1, len(daily)):
        h = float(daily[i]["high"])
        l = float(daily[i]["low"])
        prev_h = float(daily[i - 1]["high"])
        prev_l = float(daily[i - 1]["low"])
        prev_c = float(daily[i - 1]["close"])

        up_move = h - prev_h
        down_move = prev_l - l

        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr_list.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))

    if len(tr_list) < period:
        return 20.0

    # Smooth TR, +DM, -DM
    smooth_tr = sum(tr_list[:period])
    smooth_pdm = sum(plus_dm[:period])
    smooth_mdm = sum(minus_dm[:period])

    dx_list: List[float] = []
    for i in range(period, len(tr_list)):
        smooth_tr = smooth_tr - (smooth_tr / period) + tr_list[i]
        smooth_pdm = smooth_pdm - (smooth_pdm / period) + plus_dm[i]
        smooth_mdm = smooth_mdm - (smooth_mdm / period) + minus_dm[i]

        pdi = (smooth_pdm / smooth_tr * 100.0) if smooth_tr > 0 else 0.0
        mdi = (smooth_mdm / smooth_tr * 100.0) if smooth_tr > 0 else 0.0

        denom = pdi + mdi
        dx = (abs(pdi - mdi) / denom * 100.0) if denom > 0 else 0.0
        dx_list.append(dx)

    if not dx_list:
        return 20.0
    if len(dx_list) < period:
        return sum(dx_list) / len(dx_list)

    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx, 2)


def check_nr7(daily: List[Dict[str, Any]]) -> bool:
    """Return True if latest daily bar's range is the smallest of the last 7 bars."""
    if len(daily) < 7:
        return False
    ranges = [float(b["high"]) - float(b["low"]) for b in daily[-7:]]
    latest_range = ranges[-1]
    return latest_range <= min(ranges)


def compute_relative_strength_rank(sym_return_20d: float, benchmark_returns_20d: List[float]) -> float:
    """Calculate percentile rank (0 to 100) of a stock's 20-day return against a peer group."""
    if not benchmark_returns_20d:
        return 50.0
    below = sum(1 for r in benchmark_returns_20d if r < sym_return_20d)
    return round((below / len(benchmark_returns_20d)) * 100.0, 1)


def compute_features(
    symbol: str,
    daily: List[Dict[str, Any]],
    index_daily: Optional[List[Dict[str, Any]]] = None,
    sector_daily: Optional[List[Dict[str, Any]]] = None,
    universe_20d_returns: Optional[List[float]] = None,
    combined_oi_daily: Optional[List[int]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Compute daily deterministic features for a stock.
    Returns None if history is insufficient (< 25 candles) or empty.
    """
    if not daily or len(daily) < 25:
        log.warning("[%s] Insufficient daily history (%d candles < 25); returning None", symbol, len(daily) if daily else 0)
        return None

    closes = [float(b["close"]) for b in daily]
    volumes = [float(b.get("volume", 0)) for b in daily]

    # 1. EMAs and slopes
    ema20_series = compute_ema(closes, 20)
    ema50_series = compute_ema(closes, 50)
    ema20 = ema20_series[-1]
    ema50 = ema50_series[-1]
    prev_ema20 = ema20_series[-2] if len(ema20_series) >= 2 else ema20
    prev_ema50 = ema50_series[-2] if len(ema50_series) >= 2 else ema50

    ema20_slope = (ema20 - prev_ema20) / prev_ema20 * 100.0 if prev_ema20 > 0 else 0.0
    ema50_slope = (ema50 - prev_ema50) / prev_ema50 * 100.0 if prev_ema50 > 0 else 0.0

    # 2. ADX
    adx14 = compute_adx(daily, 14)

    # 3. ATR
    atr14 = compute_atr(daily, 14)
    if atr14 <= 0:
        atr14 = max(closes[-1] * 0.015, 1.0)

    # 4. Returns & Relative Strength
    ret_20d = (closes[-1] - closes[-20]) / closes[-20] * 100.0 if len(closes) >= 20 and closes[-20] > 0 else 0.0

    rs_nifty = 50.0
    if index_daily and len(index_daily) >= 20:
        idx_closes = [float(b["close"]) for b in index_daily]
        idx_ret_20d = (idx_closes[-1] - idx_closes[-20]) / idx_closes[-20] * 100.0 if idx_closes[-20] > 0 else 0.0
        # If stock outperformed Nifty by >= 5%, assign higher rank
        rs_nifty = 70.0 if ret_20d > idx_ret_20d + 3.0 else (30.0 if ret_20d < idx_ret_20d - 3.0 else 50.0)
    elif universe_20d_returns:
        rs_nifty = compute_relative_strength_rank(ret_20d, universe_20d_returns)

    rs_sector = 50.0
    if sector_daily and len(sector_daily) >= 20:
        sec_closes = [float(b["close"]) for b in sector_daily]
        sec_ret_20d = (sec_closes[-1] - sec_closes[-20]) / sec_closes[-20] * 100.0 if sec_closes[-20] > 0 else 0.0
        rs_sector = 70.0 if ret_20d > sec_ret_20d + 2.0 else (30.0 if ret_20d < sec_ret_20d - 2.0 else 50.0)
    else:
        rs_sector = rs_nifty

    # 5. OI Flows on combined near+next month OI
    # If explicit combined OI series provided, use it; else fallback to daily candle 'oi' field
    oi_series: List[float] = []
    if combined_oi_daily and len(combined_oi_daily) == len(daily):
        oi_series = [float(x) for x in combined_oi_daily]
    else:
        oi_series = [float(b.get("oi") or 0.0) for b in daily]

    def calc_flow(lookback: int) -> str:
        if len(closes) <= lookback or len(oi_series) <= lookback:
            return "NONE"
        c_now = closes[-1]
        c_prev = closes[-1 - lookback]
        oi_now = oi_series[-1]
        oi_prev = oi_series[-1 - lookback]

        if c_prev <= 0 or oi_prev <= 0:
            return "NONE"

        p_pct = (c_now - c_prev) / c_prev * 100.0
        o_pct = (oi_now - oi_prev) / oi_prev * 100.0
        return classify_oi_flow(p_pct, o_pct, noise=3.0)

    oi_flow_1d = calc_flow(1)
    oi_flow_3d = calc_flow(3)
    oi_flow_5d = calc_flow(5)
    oi_flow_20d = calc_flow(20)

    # 6. Volume vs 20-day average
    avg_vol_20d = sum(volumes[-21:-1]) / 20.0 if len(volumes) >= 21 else (sum(volumes) / len(volumes) if volumes else 1.0)
    vol_vs_avg = (volumes[-1] / avg_vol_20d) if avg_vol_20d > 0 else 1.0

    # 7. NR7 and Squeeze
    is_nr7 = check_nr7(daily)

    return {
        "symbol": symbol.upper(),
        "close": closes[-1],
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "ema20_slope": round(ema20_slope, 3),
        "ema50_slope": round(ema50_slope, 3),
        "adx14": adx14,
        "atr14": atr14,
        "ret_20d": round(ret_20d, 2),
        "rs_rank_index": round(rs_nifty, 1),
        "rs_rank_sector": round(rs_sector, 1),
        "oi_flow_1d": oi_flow_1d,
        "oi_flow_3d": oi_flow_3d,
        "oi_flow_5d": oi_flow_5d,
        "oi_flow_20d": oi_flow_20d,
        "vol_vs_avg": round(vol_vs_avg, 2),
        "is_nr7": is_nr7,
    }


def market_regime(
    index_daily: Optional[List[Dict[str, Any]]] = None,
    universe_breadth_pct_above_ema50: Optional[float] = None,
    vix_value: Optional[float] = None,
    vix_change_pct: Optional[float] = None,
) -> str:
    """
    Determine daily market regime: RISK_ON | NEUTRAL | RISK_OFF.
    - RISK_ON: Nifty > EMA50 rising, breadth > 60%, VIX < 16 or falling.
    - RISK_OFF: Nifty < EMA50, breadth < 40%, or VIX > 20 / spiking.
    - NEUTRAL: Mixed conditions.
    """
    score = 0

    if index_daily and len(index_daily) >= 50:
        closes = [float(b["close"]) for b in index_daily]
        ema50 = compute_ema(closes, 50)[-1]
        c = closes[-1]
        if c > ema50:
            score += 1
        elif c < ema50:
            score -= 1

    if universe_breadth_pct_above_ema50 is not None:
        if universe_breadth_pct_above_ema50 >= 60.0:
            score += 1
        elif universe_breadth_pct_above_ema50 <= 40.0:
            score -= 1

    if vix_value is not None:
        if vix_value <= 15.0:
            score += 1
        elif vix_value >= 20.0:
            score -= 1

    if vix_change_pct is not None:
        if vix_change_pct >= 8.0:
            score -= 1
        elif vix_change_pct <= -5.0:
            score += 1

    if score >= 2:
        return "RISK_ON"
    elif score <= -2:
        return "RISK_OFF"
    return "NEUTRAL"
