"""
Data Legitimacy & Pre-Flight Validator
Validates market data (Spot, Option Chain, Expiry, Candles, Liquidity)
for accuracy, legitimacy, and internal consistency before feeding to
Core strategies, Decision Pipelines, or LLM Enrichment.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time, date
from typing import Any, Dict, List, Optional, Tuple
import pytz

from src.engine.trade_plan import is_valid_option_premium

log = logging.getLogger("nsebot.data_validator")
_IST = pytz.timezone("Asia/Kolkata")

# Plausible underlying price ranges per symbol to catch bogus ticks
_SANITY_PRICE_RANGES: dict[str, tuple[float, float]] = {
    "NIFTY": (12000.0, 45000.0),
    "BANKNIFTY": (25000.0, 80000.0),
    "SENSEX": (45000.0, 140000.0),
    "FINNIFTY": (12000.0, 40000.0),
    "MIDCPNIFTY": (6000.0, 25000.0),
    "NATURALGAS": (80.0, 900.0),
    "CRUDEOIL": (2000.0, 16000.0),
    "GOLD": (35000.0, 160000.0),
    "GOLDM": (35000.0, 160000.0),
    "SILVER": (40000.0, 220000.0),
    "SILVERM": (40000.0, 220000.0),
}

_COMMODITY_SYMBOLS = {
    "NATURALGAS", "CRUDEOIL", "GOLD", "GOLDM", "SILVER", "SILVERM"
}

# NSE F&O (index) — close extended to 15:40 IST per SEBI (effective Aug 2026).
_NSE_FNO_SYMBOLS = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}


@dataclass
class DataLegitimacyResult:
    """Outcome of pre-flight data validation."""
    is_legitimate: bool
    score: int  # 0 to 100
    symbol: str
    underlying_price: float
    issues: list[str] = field(default_factory=list)  # Critical blockers
    warnings: list[str] = field(default_factory=list)  # Non-blocking anomalies
    cleaned_strikes: list[dict] = field(default_factory=list)
    dte: int = 0
    fractional_t: float = 0.0  # Annualized trading time to expiry (T > 0)
    atm_strike: float = 0.0
    is_0dte_cutoff: bool = False
    execution_guardrail: str = "NORMAL"  # "NORMAL" or "FORCE_LIMIT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_legitimate": self.is_legitimate,
            "score": self.score,
            "symbol": self.symbol,
            "underlying_price": self.underlying_price,
            "issues": self.issues,
            "warnings": self.warnings,
            "dte": self.dte,
            "fractional_t": self.fractional_t,
            "atm_strike": self.atm_strike,
            "strike_count": len(self.cleaned_strikes),
            "is_0dte_cutoff": self.is_0dte_cutoff,
            "execution_guardrail": self.execution_guardrail,
        }


def _classify_leg_liquidity(
    ltp: float,
    bid: float,
    ask: float,
    volume: float,
    oi: float,
) -> tuple[str, str]:
    """
    Hierarchical liquidity validation for an option leg.
    
    Primary Check (Market Depth Available):
        Ask > Bid > 0 and (Ask - Bid) / LTP <= Spread Threshold
    Secondary Fallback (Missing Depth):
        LTP > 0 and OI > 0 (or Volume > 0)
    Illiquid / Inactive:
        LTP <= 0 and OI <= 0 and Volume <= 0 and Bid <= 0
        
    Returns:
        tuple[liquidity_tier ("primary" | "secondary" | "illiquid"), reason_str]
    """
    # 1. Primary Check: Market Depth available
    if ask > 0 and bid > 0 and ask >= bid:
        spread = ask - bid
        ref_price = ltp if ltp > 0 else bid
        spread_ratio = spread / ref_price if ref_price > 0 else 1.0
        # Allow wider relative spreads for very cheap/penny options (< 5.0)
        if spread_ratio <= 0.40 or (ref_price < 5.0 and spread <= 2.0):
            return "primary", f"depth_ok(spread={spread:.2f}, ratio={spread_ratio:.1%})"

    # 2. Secondary Fallback: Missing depth but active quote & OI / Volume
    if ltp > 0 and (oi > 0 or volume > 0):
        return "secondary", f"fallback_oi(oi={int(oi)}, vol={int(volume)})"

    # 3. Illiquid / Dead
    return "illiquid", "no_active_depth_or_oi"


def validate_market_data(
    symbol: str,
    oc_data: Optional[dict[str, Any]],
    chart_payload: Optional[dict[str, Any]] = None,
    prev_price: Optional[float] = None,
) -> DataLegitimacyResult:
    """
    Comprehensive pre-flight legitimacy check across Option Chain, Spot,
    and Chart Indicators.
    """
    sym_base = symbol.upper().strip().split()[0]
    is_commodity = sym_base in _COMMODITY_SYMBOLS
    is_nse_fno = sym_base in _NSE_FNO_SYMBOLS
    issues: list[str] = []
    warnings: list[str] = []
    score = 100
    execution_guardrail = "NORMAL"
    is_0dte_cutoff = False

    if not oc_data or not isinstance(oc_data, dict):
        return DataLegitimacyResult(
            is_legitimate=False,
            score=0,
            symbol=symbol,
            underlying_price=0.0,
            issues=["Option chain payload is empty or None"],
        )

    # ── 1. Spot / Underlying Price Sanity ──
    raw_price = oc_data.get("underlying_price")
    try:
        underlying = float(raw_price or 0.0)
    except (ValueError, TypeError):
        underlying = 0.0

    if underlying <= 0.0:
        issues.append(f"Invalid underlying price: {underlying}")
        score -= 50
    else:
        # Check against sanity bounds if symbol is known
        if sym_base in _SANITY_PRICE_RANGES:
            min_p, max_p = _SANITY_PRICE_RANGES[sym_base]
            if not (min_p <= underlying <= max_p):
                issues.append(
                    f"Underlying price {underlying} is outside realistic bounds ({min_p}-{max_p}) for {sym_base}"
                )
                score -= 40

        # Check against previous price jump (> 35% single-scan gap is critical, > 15% is warning)
        # Note: Percentage anomaly checks are strictly restricted to underlying spot, never option premiums.
        if prev_price and prev_price > 0:
            pct_jump = abs(underlying - prev_price) / prev_price
            if pct_jump > 0.35:
                issues.append(
                    f"Extreme spot price jump of {pct_jump*100:.1f}% from prev {prev_price} to {underlying}"
                )
                score -= 40
            elif pct_jump > 0.15:
                warnings.append(
                    f"Large spot price movement of {pct_jump*100:.1f}% from prev {prev_price} to {underlying}"
                )
                score -= 10

    # ── 2. Expiry, DTE, Fractional T & 0DTE Cut-Off ──
    expiry_str = oc_data.get("expiry")
    dte_calendar = 0
    fractional_t = 0.0
    now_ist = datetime.now(_IST)
    today_date = now_ist.date()

    # Define market hours per asset class
    # NSE F&O (index): 09:15 to 15:40 (385 minutes). Cut-off on expiry: 15:25 IST
    # NSE/BSE equity: 09:15 to 15:30 (375 minutes). Cut-off on expiry: 15:15 IST
    # MCX: 09:00 to 23:30 (870 minutes). Cut-off on expiry: 23:15 IST
    if is_commodity:
        market_close_time = time(23, 30)
        entry_cutoff_time = time(23, 15)
        trading_day_minutes = 870.0
    elif is_nse_fno:
        market_close_time = time(15, 40)
        entry_cutoff_time = time(15, 25)
        trading_day_minutes = 385.0
    else:
        market_close_time = time(15, 30)
        entry_cutoff_time = time(15, 15)
        trading_day_minutes = 375.0

    if not expiry_str:
        warnings.append("No expiry date provided in option chain")
        score -= 15
        fractional_t = max(1e-5, 1.0 / 252.0)  # Default fallback
    else:
        try:
            exp_date = datetime.strptime(str(expiry_str).strip().split("T")[0].split()[0], "%Y-%m-%d").date()
            dte_calendar = (exp_date - today_date).days
            if dte_calendar < 0:
                issues.append(f"Expired contract date: {expiry_str} (DTE={dte_calendar})")
                score -= 40
                fractional_t = 1e-5
            elif dte_calendar == 0:
                # 0DTE Expiry Day
                cutoff_dt = datetime.combine(today_date, entry_cutoff_time, tzinfo=_IST)
                close_dt = datetime.combine(today_date, market_close_time, tzinfo=_IST)

                if now_ist >= cutoff_dt:
                    is_0dte_cutoff = True
                    warnings.append(
                        f"0DTE entry cutoff reached ({now_ist.strftime('%H:%M')} IST >= {cutoff_dt.strftime('%H:%M')} IST) — new entries prohibited"
                    )

                remaining_sec = max(6.0, (close_dt - now_ist).total_seconds())
                remaining_min = remaining_sec / 60.0
                # T = (minutes remaining) / (minutes_per_day * 252)
                fractional_t = max(1e-5, remaining_min / (trading_day_minutes * 252.0))
            else:
                # DTE >= 1
                close_dt = datetime.combine(today_date, market_close_time, tzinfo=_IST)
                remaining_sec_today = max(0.0, (close_dt - now_ist).total_seconds())
                remaining_min_today = remaining_sec_today / 60.0
                total_trading_minutes = (dte_calendar * trading_day_minutes) + remaining_min_today
                fractional_t = max(1e-5, total_trading_minutes / (trading_day_minutes * 252.0))
        except Exception as ex:
            warnings.append(f"Unparseable expiry date '{expiry_str}': {ex}")
            score -= 10
            fractional_t = max(1e-5, 1.0 / 252.0)

    # ── 3. Strike List, Asset-Class Proximity & Hierarchical Liquidity ──
    raw_strikes = oc_data.get("strikes") or []
    cleaned_strikes: list[dict] = []
    atm_strike = 0.0

    min_required_strikes = 4
    if not raw_strikes or len(raw_strikes) < min_required_strikes:
        issues.append(f"Insufficient strikes in option chain (got {len(raw_strikes)}, min={min_required_strikes})")
        score -= 40
    else:
        # Dynamic Proximity Bounds:
        # Indices: +/- 8% or +/- 25 strikes
        # Commodities (Natural Gas, Crude, Metals): +/- 20% or +/- 10 strikes
        atm_max_dist_pct = 0.20 if is_commodity else 0.08

        # Find ATM strike
        try:
            strikes_numeric = []
            for r in raw_strikes:
                stk = float(r.get("strike_price") or r.get("strike") or 0.0)
                if stk > 0:
                    strikes_numeric.append((abs(stk - underlying), stk))
            if strikes_numeric:
                strikes_numeric.sort()
                atm_strike = strikes_numeric[0][1]
                # Distance of ATM to spot
                if underlying > 0:
                    atm_dist_pct = abs(atm_strike - underlying) / underlying
                    if atm_dist_pct > atm_max_dist_pct:
                        issues.append(
                            f"ATM strike {atm_strike} is too far ({atm_dist_pct*100:.1f}% > allowed {atm_max_dist_pct*100:.0f}%) from spot {underlying} for {sym_base}"
                        )
                        score -= 30
        except Exception as e:
            warnings.append(f"Could not determine ATM strike: {e}")

        # Validate individual strikes & classify liquidity
        liquid_strikes: set[float] = set()
        corrupt_count = 0
        has_secondary_only_strikes = False

        for r in raw_strikes:
            if not isinstance(r, dict):
                continue
            stk = float(r.get("strike_price") or r.get("strike") or 0.0)
            if stk <= 0:
                continue

            opt_type_val = str(r.get("option_type") or "").upper().strip()
            is_ce_row = opt_type_val in ("CE", "CALL")
            is_pe_row = opt_type_val in ("PE", "PUT")
            is_dual_row = not opt_type_val

            ce_ltp = float(r.get("ce_ltp") or r.get("call_ltp") or (r.get("ltp") if is_ce_row else 0.0) or 0.0)
            pe_ltp = float(r.get("pe_ltp") or r.get("put_ltp") or (r.get("ltp") if is_pe_row else 0.0) or 0.0)
            ce_vol = float(r.get("ce_volume") or r.get("call_volume") or (r.get("volume") if is_ce_row else 0.0) or 0.0)
            pe_vol = float(r.get("pe_volume") or r.get("put_volume") or (r.get("volume") if is_pe_row else 0.0) or 0.0)
            ce_oi = float(r.get("ce_oi") or r.get("call_oi") or (r.get("oi") if is_ce_row else 0.0) or 0.0)
            pe_oi = float(r.get("pe_oi") or r.get("put_oi") or (r.get("oi") if is_pe_row else 0.0) or 0.0)

            ce_bid = float(r.get("ce_bid") or r.get("call_bid") or (r.get("bid") if is_ce_row else 0.0) or 0.0)
            ce_ask = float(r.get("ce_ask") or r.get("call_ask") or (r.get("ask") if is_ce_row else 0.0) or 0.0)
            pe_bid = float(r.get("pe_bid") or r.get("put_bid") or (r.get("bid") if is_pe_row else 0.0) or 0.0)
            pe_ask = float(r.get("pe_ask") or r.get("put_ask") or (r.get("ask") if is_pe_row else 0.0) or 0.0)

            # Hierarchical liquidity check
            ce_tier, _ = _classify_leg_liquidity(ce_ltp, ce_bid, ce_ask, ce_vol, ce_oi)
            pe_tier, _ = _classify_leg_liquidity(pe_ltp, pe_bid, pe_ask, pe_vol, pe_oi)

            if is_ce_row:
                if ce_tier != "illiquid":
                    liquid_strikes.add(stk)
                if ce_tier == "secondary":
                    has_secondary_only_strikes = True
            elif is_pe_row:
                if pe_tier != "illiquid":
                    liquid_strikes.add(stk)
                if pe_tier == "secondary":
                    has_secondary_only_strikes = True
            else:
                if ce_tier != "illiquid" or pe_tier != "illiquid":
                    liquid_strikes.add(stk)
                if ce_tier == "secondary" or pe_tier == "secondary":
                    has_secondary_only_strikes = True

            # Validate premiums using theoretical bounds
            valid_ce = True
            valid_pe = True
            if ce_ltp > 0 and underlying > 0:
                valid_ce = is_valid_option_premium(stk, "CE", ce_ltp, underlying)
            if pe_ltp > 0 and underlying > 0:
                valid_pe = is_valid_option_premium(stk, "PE", pe_ltp, underlying)

            cleaned_row = dict(r)
            cleaned_row["ce_liquidity_tier"] = ce_tier
            cleaned_row["pe_liquidity_tier"] = pe_tier

            if not valid_ce or not valid_pe:
                corrupt_count += 1
                if not valid_ce:
                    cleaned_row["ce_ltp"] = 0.0
                    cleaned_row["call_ltp"] = 0.0
                if not valid_pe:
                    cleaned_row["pe_ltp"] = 0.0
                    cleaned_row["put_ltp"] = 0.0

            cleaned_strikes.append(cleaned_row)

        liquid_count = len(liquid_strikes)
        if liquid_count < 3:
            warnings.append(f"Very low liquidity: only {liquid_count} strikes have active depth/OI/quotes")
            score -= 20

        if corrupt_count > 0:
            warnings.append(f"Filtered {corrupt_count} corrupt strike tick(s) with theoretical boundary violations")
            score -= min(15, corrupt_count * 5)

        if has_secondary_only_strikes:
            execution_guardrail = "FORCE_LIMIT"

    # ── 4. Chart / Candle OHLC & Spot Envelope Sanity ──
    if chart_payload and isinstance(chart_payload, dict):
        chart_data = chart_payload.get("data") or chart_payload
        if isinstance(chart_data, dict):
            for tf in ("1h", "3h", "15m"):
                tf_data = chart_data.get(tf)
                if isinstance(tf_data, dict) and "candles" in tf_data:
                    candles = tf_data["candles"]
                    if isinstance(candles, list) and candles:
                        last_c = candles[-1]
                        if isinstance(last_c, dict):
                            o = float(last_c.get("open") or 0.0)
                            h = float(last_c.get("high") or 0.0)
                            l = float(last_c.get("low") or 0.0)
                            c = float(last_c.get("close") or 0.0)

                            # Internal continuity check: H >= max(O, C, L) and L <= min(O, C, H)
                            if h < l or h < o or h < c or l > o or l > c:
                                warnings.append(f"Malformed candle OHLC in timeframe {tf}: O={o}, H={h}, L={l}, C={c}")
                                score -= 10

                            # Spot vs Forming Candle Envelope:
                            # Ticks stream continuously while candles lag; validate spot against envelope with 5% margin
                            if underlying > 0 and h > 0 and l > 0:
                                env_low = l * 0.95
                                env_high = h * 1.05
                                if underlying < env_low or underlying > env_high:
                                    diff_pct = min(abs(underlying - env_low), abs(underlying - env_high)) / underlying
                                    if diff_pct > 0.25:
                                        warnings.append(
                                            f"Spot {underlying} diverges by {diff_pct*100:.1f}% from {tf} candle envelope [{l:.1f}, {h:.1f}]"
                                        )
                                        score -= 10

    # Determine legitimacy
    score = max(0, min(100, score))
    is_legitimate = len(issues) == 0 and score >= 40

    if not is_legitimate:
        log.warning(
            "[data_validator] %s: Market data validation FAILED ❌ (score=%d/100, issues=%s, warnings=%s)",
            symbol, score, issues, warnings
        )
    else:
        log.info(
            "[data_validator] %s: Market data validated OK ✅ (score=%d/100, strikes=%d, DTE=%d, T=%.5f, guard=%s, warnings=%d)",
            symbol, score, len(cleaned_strikes), dte_calendar, fractional_t, execution_guardrail, len(warnings)
        )

    return DataLegitimacyResult(
        is_legitimate=is_legitimate,
        score=score,
        symbol=symbol,
        underlying_price=underlying,
        issues=issues,
        warnings=warnings,
        cleaned_strikes=cleaned_strikes if cleaned_strikes else raw_strikes,
        dte=dte_calendar,
        fractional_t=fractional_t,
        atm_strike=atm_strike,
        is_0dte_cutoff=is_0dte_cutoff,
        execution_guardrail=execution_guardrail,
    )


def validate_trade_leg_data(
    legs: list[dict],
    oc_data: Optional[dict[str, Any]],
    underlying: float,
) -> tuple[bool, list[str]]:
    """
    Strict 100% Binary Data Integrity Validation for Multi-Leg Orders before dispatch.
    
    Ensures:
    1. Every target leg exists in the option chain.
    2. Every target leg has valid LTP > 0.
    3. Every target leg conforms to theoretical intrinsic bounds.
    4. No leg is completely illiquid (missing depth and 0 OI and 0 Volume).
    
    Returns:
        tuple[is_valid: bool, issues: list[str]]
    """
    if not legs:
        return False, ["No trade legs provided for validation"]

    if underlying <= 0:
        return False, [f"Invalid underlying price {underlying} for leg validation"]

    if not oc_data or not isinstance(oc_data, dict):
        return False, ["Option chain data missing during leg validation"]

    strikes = oc_data.get("strikes") or []
    if not strikes:
        return False, ["Option chain contains no strikes for leg validation"]

    issues = []

    for idx, leg in enumerate(legs, 1):
        target_strike = float(leg.get("strike") or 0.0)
        target_opt = str(leg.get("option_type") or "").upper()
        if target_strike <= 0:
            issues.append(f"Leg #{idx}: invalid strike {target_strike}")
            continue
        if target_opt not in ("CE", "PE", "CALL", "PUT"):
            issues.append(f"Leg #{idx}: invalid option type '{target_opt}'")
            continue

        target_opt_norm = "CE" if target_opt in ("CE", "CALL") else "PE"

        matched = False
        for row in strikes:
            if not isinstance(row, dict):
                continue
            row_strike = float(row.get("strike_price") or row.get("strike") or 0.0)
            if abs(row_strike - target_strike) >= 0.01:
                continue

            # If the row has an explicit option_type (per-contract row format), ensure it matches target_opt_norm
            row_opt = str(row.get("option_type") or "").upper().strip()
            if row_opt:
                row_opt_norm = "CE" if row_opt in ("CE", "CALL") else ("PE" if row_opt in ("PE", "PUT") else "")
                if row_opt_norm and row_opt_norm != target_opt_norm:
                    continue

            matched = True
            if target_opt_norm == "CE":
                ltp = float(row.get("ce_ltp") or row.get("call_ltp") or (row.get("ltp") if row_opt in ("CE", "CALL", "") else 0.0) or 0.0)
                oi = float(row.get("ce_oi") or row.get("call_oi") or (row.get("oi") if row_opt in ("CE", "CALL", "") else 0.0) or 0.0)
                vol = float(row.get("ce_volume") or row.get("call_volume") or (row.get("volume") if row_opt in ("CE", "CALL", "") else 0.0) or 0.0)
                bid = float(row.get("ce_bid") or row.get("call_bid") or (row.get("bid") if row_opt in ("CE", "CALL", "") else 0.0) or 0.0)
                ask = float(row.get("ce_ask") or row.get("call_ask") or (row.get("ask") if row_opt in ("CE", "CALL", "") else 0.0) or 0.0)
            else:
                ltp = float(row.get("pe_ltp") or row.get("put_ltp") or (row.get("ltp") if row_opt in ("PE", "PUT", "") else 0.0) or 0.0)
                oi = float(row.get("pe_oi") or row.get("put_oi") or (row.get("oi") if row_opt in ("PE", "PUT", "") else 0.0) or 0.0)
                vol = float(row.get("pe_volume") or row.get("put_volume") or (row.get("volume") if row_opt in ("PE", "PUT", "") else 0.0) or 0.0)
                bid = float(row.get("pe_bid") or row.get("put_bid") or (row.get("bid") if row_opt in ("PE", "PUT", "") else 0.0) or 0.0)
                ask = float(row.get("pe_ask") or row.get("put_ask") or (row.get("ask") if row_opt in ("PE", "PUT", "") else 0.0) or 0.0)

            if ltp <= 0:
                issues.append(f"Leg #{idx} ({target_strike} {target_opt_norm}): LTP is zero or missing")
            elif not is_valid_option_premium(target_strike, target_opt_norm, ltp, underlying):
                issues.append(f"Leg #{idx} ({target_strike} {target_opt_norm}): LTP {ltp:.2f} violates intrinsic/boundary limits")

            tier, _ = _classify_leg_liquidity(ltp, bid, ask, vol, oi)
            if tier == "illiquid":
                issues.append(f"Leg #{idx} ({target_strike} {target_opt_norm}): completely illiquid with 0 OI and no market depth")
            break

        if not matched:
            issues.append(f"Leg #{idx} ({target_strike} {target_opt_norm}): strike not found in option chain")

    is_valid = len(issues) == 0
    if not is_valid:
        log.warning("[data_validator] Multi-leg binary validation FAILED ❌: %s", issues)
    else:
        log.info("[data_validator] Multi-leg binary validation PASSED ✅ for %d legs", len(legs))

    return is_valid, issues
