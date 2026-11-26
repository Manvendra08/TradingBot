"""Tests for stock futures setups, bias, matrix permissions, and watchlist."""

from src.stockfut.levels import Zone
from src.stockfut.setups import (
    stock_bias,
    bias_label,
    matrix_permission,
    is_daily_reversal,
    open_check,
    midday_recheck,
    is_retryable_skip,
    WatchRow,
)


def test_matrix_permission_cells():
    # STRONG_BEAR in RISK_ON requires score >= 80
    perm = matrix_permission("STRONG_BEAR", "RISK_ON")
    assert perm == ("SHORT", 80)

    # BULL in RISK_OFF is not permitted
    assert matrix_permission("BULL", "RISK_OFF") is None

    # STRONG_BULL in RISK_ON requires score >= 65
    assert matrix_permission("STRONG_BULL", "RISK_ON") == ("LONG", 65)


def test_neutral_bias_never_trades():
    assert matrix_permission("NEUTRAL", "RISK_ON") is None
    assert matrix_permission("NEUTRAL", "NEUTRAL") is None
    assert matrix_permission("NEUTRAL", "RISK_OFF") is None


def test_stock_bias_scoring():
    # Bull conditions: EMA20>50 rising (+1), RS>=70 (+1), OI 20d Long Buildup (+1), Sector RS>=66 (+1) = +4
    f_strong_bull = {
        "close": 1500.0,
        "ema20": 1480.0,
        "ema50": 1420.0,
        "ema20_slope": 0.5,
        "ema50_slope": 0.3,
        "adx14": 25.0,
        "rs_rank_index": 75.0,
        "rs_rank_sector": 72.0,
        "oi_flow_20d": "LONG_BUILDUP",
    }
    score = stock_bias(f_strong_bull)
    assert score == 4
    assert bias_label(score) == "STRONG_BULL"

    # Bear conditions
    f_bear = {
        "close": 900.0,
        "ema20": 920.0,
        "ema50": 960.0,
        "ema20_slope": -0.4,
        "ema50_slope": -0.2,
        "adx14": 22.0,
        "rs_rank_index": 25.0,
        "rs_rank_sector": 28.0,
        "oi_flow_20d": "SHORT_BUILDUP",
    }
    score_b = stock_bias(f_bear)
    assert score_b == -4
    assert bias_label(score_b) == "STRONG_BEAR"


def test_reversal_upper_35_close_arms_long():
    # Zone at 980 (Support). Low touches 980. High is 1000. Close is 995 (upper 25% of range).
    daily = [
        {"high": 1010.0, "low": 990.0, "close": 992.0},
        {"high": 1000.0, "low": 980.0, "close": 995.0},
    ]
    zones = [Zone(low=978.0, high=980.0, kind="SUPPORT", strength=3)]
    rev = is_daily_reversal(
        side="LONG", daily=daily, zones=zones, atr=20.0, oi_flow_20d="NONE"
    )
    assert rev == "UPPER_35"


def test_reversal_requires_zone_touch():
    # Low at 995, support zone at 980 -> distance = 15 > 0.3*20 (6.0) -> No touch!
    daily = [
        {"high": 1010.0, "low": 990.0, "close": 992.0},
        {"high": 1005.0, "low": 995.0, "close": 1002.0},
    ]
    zones = [Zone(low=978.0, high=980.0, kind="SUPPORT", strength=3)]
    rev = is_daily_reversal(
        side="LONG", daily=daily, zones=zones, atr=20.0, oi_flow_20d="LONG_BUILDUP"
    )
    assert rev is None


def test_prior_close_branch_needs_20d_long_buildup():
    # Touches 980. Bar close = 993 > prev close (990). Range = 980 to 1010 (close in middle, not upper 35%).
    daily = [
        {"high": 1010.0, "low": 990.0, "close": 990.0},
        {"high": 1010.0, "low": 980.0, "close": 993.0},
    ]
    zones = [Zone(low=978.0, high=980.0, kind="SUPPORT", strength=3)]

    # With Long Buildup in 20d OI flow -> passes branch (b)
    rev_ok = is_daily_reversal(
        side="LONG", daily=daily, zones=zones, atr=20.0, oi_flow_20d="LONG_BUILDUP"
    )
    assert rev_ok == "PRIOR_CLOSE_OI"

    # Without Long Buildup -> None
    rev_fail = is_daily_reversal(
        side="LONG", daily=daily, zones=zones, atr=20.0, oi_flow_20d="SHORT_BUILDUP"
    )
    assert rev_fail is None


def test_open_check_gates():
    row = WatchRow(
        scan_date="2026-11-23",
        scan_time="19:30:00",
        symbol="RELIANCE",
        tradingsymbol="RELIANCE26NOVFUT",
        expiry="2026-11-26",
        side="LONG",
        bias="STRONG_BULL",
        score=85,
        planned_entry=1300.0,
        sl=1260.0,
        t1=1370.0,
        t2=1410.0,
        lots=2,
        lot_size=250,
        r_multiple=1.75,
    )
    atr = 30.0  # max drift = 0.3 * 30 = 9.0

    # OK case: open at 1304.0, tight spread
    ok, reason = open_check(row, quote={"price": 1304.0, "bid": 1303.9, "ask": 1304.1}, atr=atr)
    assert ok is True

    # Drift too wide: open at 1315.0 (> 9.0 drift)
    ok_drift, reason_drift = open_check(row, quote={"price": 1315.0, "bid": 1314.9, "ask": 1315.1}, atr=atr)
    assert ok_drift is False
    assert "PRICE_DRIFT_EXCEEDED" in reason_drift

    # SL breached: open at 1255.0 (< 1260.0)
    ok_sl, reason_sl = open_check(row, quote={"price": 1255.0, "bid": 1254.9, "ask": 1255.1}, atr=atr)
    assert ok_sl is False
    assert "SL_BREACHED_AT_OPEN" in reason_sl

    # Wide spread: bid 1290, ask 1310 (spread > 0.05%)
    ok_spread, reason_spread = open_check(row, quote={"price": 1300.0, "bid": 1290.0, "ask": 1310.0}, atr=atr)
    assert ok_spread is False
    assert "WIDE_SPREAD" in reason_spread

    # Configured wider tolerances (0.6x ATR drift, 0.10% spread) allow typical overnight gap & open spread
    ok_cfg_drift, reason_cfg_drift = open_check(
        row,
        quote={"price": 1315.0, "bid": 1314.5, "ask": 1315.5},
        atr=atr,
        max_drift_atr=0.6,
        max_spread_pct=0.10,
    )
    assert ok_cfg_drift is True, reason_cfg_drift


def test_retryable_skip_classification():
    assert is_retryable_skip("PRICE_DRIFT_EXCEEDED (15.30 > 13.49)") is True
    assert is_retryable_skip("WIDE_SPREAD (0.120% > 0.100%)") is True
    assert is_retryable_skip("MAX_POSITIONS_REACHED (6 >= 6)") is True
    assert is_retryable_skip("SL_BREACHED_AT_OPEN (1255.0 <= 1260.0)") is False
    assert is_retryable_skip("IN_BAN_LIST") is False
    assert is_retryable_skip("ALREADY_OPEN") is False


def test_midday_recheck_gates():
    row = WatchRow(
        scan_date="2026-11-25",
        scan_time="19:30:00",
        symbol="RELIANCE",
        tradingsymbol="RELIANCE26DECFUT",
        expiry="2026-12-31",
        side="LONG",
        bias="STRONG_BULL",
        score=85,
        planned_entry=1300.0,
        sl=1260.0,
        t1=1364.0,
        t2=1400.0,
        lots=2,
        lot_size=250,
        r_multiple=1.6,
    )
    atr = 30.0

    # Valid pullback with intraday low safely above SL: R = (1364-1304)/(1304-1260) = 1.36R >= 1.25R
    ok, reason = midday_recheck(
        row,
        quote={"price": 1304.0, "bid": 1303.9, "ask": 1304.1, "ohlc": {"low": 1285.0, "high": 1310.0}},
        atr=atr,
    )
    assert ok is True, reason

    # SL touched earlier in morning session even though LTP recovered
    ok_touch, reason_touch = midday_recheck(
        row,
        quote={"price": 1300.0, "bid": 1299.9, "ask": 1300.1, "ohlc": {"low": 1259.5, "high": 1305.0}},
        atr=atr,
    )
    assert ok_touch is False and "SL_TOUCHED_INTRADAY" in reason_touch

    # Too close to SL (cushion < 0.4 * 30 = 12.0): rejects falling knife despite high R
    ok_knife, reason_knife = midday_recheck(
        row,
        quote={"price": 1265.0, "bid": 1264.9, "ask": 1265.1, "ohlc": {"low": 1263.0, "high": 1290.0}},
        atr=atr,
    )
    assert ok_knife is False and "TOO_CLOSE_TO_SL" in reason_knife

    # Chased too high (LTP 1320 -> R = 44/60 = 0.73R < 1.25R)
    ok_chase, reason_chase = midday_recheck(
        row,
        quote={"price": 1320.0, "bid": 1319.9, "ask": 1320.1, "ohlc": {"low": 1295.0, "high": 1322.0}},
        atr=atr,
    )
    assert ok_chase is False and "REWARD_RISK_INSUFFICIENT" in reason_chase

    # Regime flipped against BULL LONG (BULL x RISK_OFF is disallowed in matrix)
    bull_row = WatchRow(
        scan_date="2026-11-25",
        scan_time="19:30:00",
        symbol="RELIANCE",
        tradingsymbol="RELIANCE26DECFUT",
        expiry="2026-12-31",
        side="LONG",
        bias="BULL",
        score=78,
        planned_entry=1300.0,
        sl=1260.0,
        t1=1364.0,
        t2=1400.0,
        lots=2,
        lot_size=250,
        r_multiple=1.6,
    )
    ok_reg, reason_reg = midday_recheck(
        bull_row,
        quote={"price": 1304.0, "bid": 1303.9, "ask": 1304.1, "ohlc": {"low": 1290.0, "high": 1310.0}},
        atr=atr,
        current_regime="RISK_OFF",
    )
    assert ok_reg is False and "REGIME_FLIPPED" in reason_reg
