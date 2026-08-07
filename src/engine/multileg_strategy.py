"""
Multi-Leg Strategy Engine — core computation for short-options books.

Phase 3 of the multi-leg strategy system. Provides:
  - Leg validation against option chain and strategy constraints
  - Book-level Greeks aggregation
  - Risk profile calculation per strategy type
  - Margin estimation with static SELL multiplier
  - Entry quality scoring for multi-leg books
  - Conflict detection against existing open books
  - Combined execution plan builder

Config source: config/multileg_strategies.py
Greeks engine: src/utils/greeks_calculator.py
"""
from __future__ import annotations

import logging
import math
from typing import Any

from config.multileg_strategies import (
    ALLOWED_SYMBOLS,
    CONFLICTING_STRATEGIES,
    MAX_BOOK_MARGIN,
    MAX_LEGS_PER_BOOK,
    MAX_NET_DELTA,
    MAX_NET_VEGA,
    STRATEGY_CONSTRAINTS,
    STRATEGY_TYPES,
)
from src.utils.greeks_calculator import get_greeks_calculator

try:
    from config.settings import LOT_SIZES
except ImportError:
    LOT_SIZES: dict[str, int] = {}  # pragma: no cover

log = logging.getLogger(__name__)


# ── Leg Validation ────────────────────────────────────────────────

def validate_legs(
    strategy_type: str,
    legs: list[dict],
    option_chain: dict,
    underlying: float,
) -> tuple[bool, str]:
    """
    Validate proposed legs against strategy constraints and option chain.

    Parameters
    ----------
    strategy_type : str
        One of the keys in STRATEGY_CONSTRAINTS (e.g. "IRON_CONDOR").
    legs : list[dict]
        Each leg must have at least: strike, option_type, side, premium.
    option_chain : dict
        Option chain keyed by strike price; each strike maps to a dict
        with "CE" and/or "PE" sub-dicts containing at least {"ltp": ...}.
    underlying : float
        Current underlying spot / futures price.

    Returns
    -------
    (is_valid, error_message) — error_message is empty string on success.
    """
    # 1. Strategy type check
    if strategy_type not in STRATEGY_CONSTRAINTS:
        valid = ", ".join(sorted(STRATEGY_CONSTRAINTS.keys()))
        return False, f"Unknown strategy_type '{strategy_type}'. Valid: {valid}"

    if not legs:
        return False, "No legs provided"

    constraints = STRATEGY_CONSTRAINTS[strategy_type]
    min_legs = constraints["min_legs"]
    max_legs = constraints["max_legs"]

    # 2. Leg count check
    if len(legs) < min_legs:
        return False, (
            f"{strategy_type} requires at least {min_legs} legs, got {len(legs)}"
        )
    if len(legs) > max_legs:
        return False, (
            f"{strategy_type} allows at most {max_legs} legs, got {len(legs)}"
        )

    # 3. All legs must be SELL
    for i, leg in enumerate(legs):
        if leg.get("side", "").upper() != "SELL":
            return False, f"Leg {i} has side '{leg.get('side')}' — all legs must be SELL for {strategy_type}"

    # 4. Each leg must exist in the option chain
    for i, leg in enumerate(legs):
        strike = leg.get("strike")
        opt_type = (leg.get("option_type") or "").upper()
        if strike is None:
            return False, f"Leg {i} missing 'strike'"
        if not opt_type:
            return False, f"Leg {i} missing 'option_type'"

        strike_chain = option_chain.get(float(strike))
        if strike_chain is None:
            # Try integer match
            strike_chain = option_chain.get(int(strike))
        if strike_chain is None:
            return False, f"Leg {i} strike {strike} not found in option chain"

        opt_data = strike_chain.get(opt_type)
        if opt_data is None:
            return False, f"Leg {i} {opt_type} at strike {strike} not found in option chain"

    # 5. Premium sanity check
    for i, leg in enumerate(legs):
        premium = leg.get("premium", 0)
        if premium <= 0:
            return False, f"Leg {i} premium {premium} must be > 0"
        if premium > underlying * 0.5:
            return False, (
                f"Leg {i} premium {premium:.2f} exceeds 50% of underlying ({underlying:.2f})"
            )

    return True, ""


# ── Book Greeks ───────────────────────────────────────────────────

def compute_book_greeks(
    legs: list[dict],
    option_chain: dict,
    underlying: float,
    expiry: str,
) -> dict:
    """
    Compute aggregated Greeks for the entire book.

    SELL legs have their deltas negated (selling a call has negative
    contribution to net delta, selling a put has positive, but since
    the calculator returns +delta for calls and -delta for puts, we
    negate all to get the position delta).

    Returns
    -------
    dict with keys: net_delta, net_theta, net_vega, per_leg_greeks.
    """
    calc = get_greeks_calculator()
    per_leg: list[dict] = []
    net_delta = 0.0
    net_theta = 0.0
    net_vega = 0.0

    for leg in legs:
        strike = float(leg["strike"])
        opt_type = leg.get("option_type", "").upper()
        premium = float(leg.get("premium", 0))

        greeks = calc.calculate_greeks(
            underlying_price=underlying,
            strike_price=strike,
            option_price=premium,
            expiry_date=expiry,
            option_type=opt_type,
        )

        # SELL legs: negate delta (short position), theta is earned (+),
        # vega exposure is negative (short vega)
        leg_delta = -greeks["delta"]   # short delta
        leg_theta = greeks["theta"]    # theta earned (already positive for short in BSM)
        leg_vega = -greeks["vega"]     # short vega

        # Theta from the calculator is negative (time decay). For SELL legs
        # we earn theta, so negate it.
        if leg_theta < 0:
            leg_theta = -leg_theta

        net_delta += leg_delta
        net_theta += leg_theta
        net_vega += leg_vega

        per_leg.append({
            "strike": float(strike),
            "type": opt_type,
            "delta": float(round(leg_delta, 4)),
            "theta": float(round(leg_theta, 4)),
            "vega": float(round(leg_vega, 4)),
        })

    return {
        "net_delta": float(round(net_delta, 4)),
        "net_theta": float(round(net_theta, 4)),
        "net_vega": float(round(net_vega, 4)),
        "per_leg_greeks": per_leg,
    }


# ── Risk Profile ──────────────────────────────────────────────────

def compute_book_risk_profile(
    strategy_type: str,
    legs: list[dict],
    net_premium: float,
    underlying: float,
) -> dict:
    """
    Compute max profit, max loss, and breakevens for a multi-leg book.

    Breakevens are derived from the short strikes (the strikes we are
    selling).  For IC-style books the upper breakeven is the short CE
    strike minus net premium; the lower is the short PE strike plus
    net premium.

    Returns
    -------
    dict with max_profit, max_loss, breakeven_upper, breakeven_lower.
    """
    # Separate PE and CE legs by option type
    pe_legs = [l for l in legs if (l.get("option_type") or "").upper() == "PE"]
    ce_legs = [l for l in legs if (l.get("option_type") or "").upper() == "CE"]

    # Find the short strikes (all legs are SELL, so take the relevant ones)
    short_pe_strike = max((float(l["strike"]) for l in pe_legs), default=0)
    short_ce_strike = min((float(l["strike"]) for l in ce_legs), default=0)

    max_profit = max(0.0, net_premium)
    max_loss = 0.0
    breakeven_upper = 0.0
    breakeven_lower = 0.0

    if strategy_type == "IRON_CONDOR":
        # Widest spread = max distance between PE legs or CE legs
        pe_strikes = sorted(float(l["strike"]) for l in pe_legs)
        ce_strikes = sorted(float(l["strike"]) for l in ce_legs)
        pe_width = (pe_strikes[-1] - pe_strikes[0]) if len(pe_strikes) > 1 else 0
        ce_width = (ce_strikes[-1] - ce_strikes[0]) if len(ce_strikes) > 1 else 0
        spread_width = max(pe_width, ce_width, 1)
        max_loss = max(0.0, spread_width - net_premium)
        breakeven_upper = short_ce_strike - net_premium if short_ce_strike else 0
        breakeven_lower = short_pe_strike + net_premium if short_pe_strike else 0

    elif strategy_type == "SHORT_STRANGLE":
        max_loss = underlying * 0.5  # practical cap (unlimited in theory)
        breakeven_upper = short_ce_strike - net_premium if short_ce_strike else 0
        breakeven_lower = short_pe_strike + net_premium if short_pe_strike else 0

    elif strategy_type == "SHORT_STRADDLE":
        max_loss = underlying * 0.5
        # Straddle: same strike for both; use CE strike as reference
        ref_strike = short_ce_strike or short_pe_strike
        breakeven_upper = ref_strike - net_premium if ref_strike else 0
        breakeven_lower = ref_strike + net_premium if ref_strike else 0

    elif strategy_type in ("BEAR_CALL_SPREAD", "BULL_PUT_SPREAD"):
        ce_strikes = sorted(float(l["strike"]) for l in ce_legs)
        pe_strikes = sorted(float(l["strike"]) for l in pe_legs)
        if strategy_type == "BEAR_CALL_SPREAD" and len(ce_strikes) >= 2:
            spread_width = ce_strikes[-1] - ce_strikes[0]
            max_loss = max(0.0, spread_width - net_premium)
            breakeven_upper = ce_strikes[-1] - net_premium
        elif strategy_type == "BULL_PUT_SPREAD" and len(pe_strikes) >= 2:
            spread_width = pe_strikes[-1] - pe_strikes[0]
            max_loss = max(0.0, spread_width - net_premium)
            breakeven_lower = pe_strikes[0] + net_premium
        else:
            max_loss = underlying * 0.5

    elif strategy_type == "JADE_LIZARD":
        # Jade Lizard: no upside risk if short CE premium + short PE premium
        # covers the spread.  PE side unlimited if market drops below PE strike.
        max_loss = underlying * 0.5  # practical cap on PE-side risk
        breakeven_upper = short_ce_strike - net_premium if short_ce_strike else 0
        breakeven_lower = short_pe_strike + net_premium if short_pe_strike else 0

    elif strategy_type == "CUSTOM":
        all_strikes = [float(l["strike"]) for l in legs]
        if all_strikes:
            max_distance = max(all_strikes) - min(all_strikes)
            max_loss = max_distance * underlying * 0.1
        else:
            max_loss = underlying * 0.5
        breakeven_upper = short_ce_strike - net_premium if short_ce_strike else 0
        breakeven_lower = short_pe_strike + net_premium if short_pe_strike else 0

    else:
        # Fallback for unknown types
        max_loss = underlying * 0.5

    return {
        "max_profit": float(round(max(0.0, max_profit), 2)),
        "max_loss": float(round(max(0.0, max_loss), 2)),
        "breakeven_upper": float(round(breakeven_upper, 2)),
        "breakeven_lower": float(round(breakeven_lower, 2)),
    }


# ── Margin Calculation ────────────────────────────────────────────

def calculate_combined_margin(legs: list[dict], symbol: str) -> float:
    """
    Estimate combined margin for all SELL legs.

    Uses a static 12x SELL margin multiplier (SPAN + exposure approximation)
    applied to: premium * lot_size * lots.

    Result is capped at MAX_BOOK_MARGIN from config.

    Parameters
    ----------
    legs : list[dict]
        Each leg needs: premium, lots (default 1).
    symbol : str
        Used to look up lot size from LOT_SIZES.

    Returns
    -------
    Total margin in ₹, capped at MAX_BOOK_MARGIN.
    """
    lot_size = LOT_SIZES.get(symbol, 1)
    SELL_MARGIN_MULTIPLIER = 12

    total_margin = 0.0
    for leg in legs:
        premium = float(leg.get("premium", 0))
        lots = int(leg.get("lots", 1))
        leg_margin = premium * lot_size * lots * SELL_MARGIN_MULTIPLIER
        total_margin += leg_margin

    return float(min(total_margin, MAX_BOOK_MARGIN))


# ── Entry Quality Scoring ─────────────────────────────────────────

def score_entry_quality(
    strategy_type: str,
    legs: list[dict],
    scan_context: dict,
    book_greeks: dict,
    risk_profile: dict,
) -> tuple[int, list[str]]:
    """
    Score multi-leg book entry quality 0-100.

    Scoring breakdown:
      +15  High IV environment (good for selling premium)
      +15  Net delta near 0 (market neutral)
      +10  Risk/reward ratio > 0.3
      +10  Premium yield on margin > 2%
      +10  Strategy-regime alignment
      +10  Wide breakeven width (> 3% of underlying)
      -20  Net delta > 0.4 (directional risk)
      -15  Max loss > 5x max profit

    Parameters
    ----------
    strategy_type : str
    legs : list[dict]
    scan_context : dict
        Expected keys: iv_rank (0-100), regime (rangebound/trending),
        underlying, net_premium, margin.
    book_greeks : dict
        From compute_book_greeks().
    risk_profile : dict
        From compute_book_risk_profile().

    Returns
    -------
    (score, reasons) — score clamped to [0, 100].
    """
    score = 0
    reasons: list[str] = []

    iv_rank = float(scan_context.get("iv_rank") or 0)
    regime = (scan_context.get("regime") or "").lower()
    underlying = float(scan_context.get("underlying") or 0)
    net_premium = float(scan_context.get("net_premium") or 0)
    margin = float(scan_context.get("margin") or 0)

    net_delta = abs(book_greeks.get("net_delta", 0))
    max_profit = risk_profile.get("max_profit", 0)
    max_loss = risk_profile.get("max_loss", 0)

    # ── Positives ──────────────────────────────────────────────────

    # 1. IV rank — high IV is good for selling premium
    if iv_rank >= 60:
        score += 15
        reasons.append(f"High IV rank {iv_rank:.0f} — good premium environment")
    elif iv_rank >= 40:
        score += 8
        reasons.append(f"Moderate IV rank {iv_rank:.0f}")

    # 2. Net delta near 0 — market neutral is ideal for non-directional
    if net_delta <= 0.15:
        score += 15
        reasons.append(f"Delta neutral ({book_greeks.get('net_delta', 0):+.2f})")
    elif net_delta <= 0.30:
        score += 8
        reasons.append(f"Near-delta-neutral ({book_greeks.get('net_delta', 0):+.2f})")

    # 3. Risk/reward ratio
    if max_loss > 0 and max_profit > 0:
        rr = max_profit / max_loss
        if rr > 0.3:
            score += 10
            reasons.append(f"Strong R:R {rr:.2f}")
        elif rr > 0.15:
            score += 5
            reasons.append(f"Moderate R:R {rr:.2f}")

    # 4. Premium yield on margin
    if margin > 0 and net_premium > 0:
        yield_pct = (net_premium / margin) * 100
        if yield_pct > 2.0:
            score += 10
            reasons.append(f"Good yield {yield_pct:.1f}% on margin")
        elif yield_pct > 1.0:
            score += 5
            reasons.append(f"Moderate yield {yield_pct:.1f}% on margin")

    # 5. Strategy-regime alignment
    rangebound_types = {"IRON_CONDOR", "SHORT_STRANGLE", "SHORT_STRADDLE", "JADE_LIZARD"}
    directional_types = {"BEAR_CALL_SPREAD", "BULL_PUT_SPREAD"}
    if regime == "rangebound" and strategy_type in rangebound_types:
        score += 10
        reasons.append(f"{strategy_type} suits rangebound regime")
    elif regime == "trending" and strategy_type in directional_types:
        score += 10
        reasons.append(f"{strategy_type} suits trending regime")
    elif regime:
        score += 3
        reasons.append(f"Neutral regime alignment ({regime})")

    # 6. Breakeven width
    bu = risk_profile.get("breakeven_upper", 0)
    bl = risk_profile.get("breakeven_lower", 0)
    if underlying > 0 and bu > 0 and bl > 0:
        width_pct = ((bu - bl) / underlying) * 100
        if width_pct > 3.0:
            score += 10
            reasons.append(f"Wide breakeven range {width_pct:.1f}%")
        elif width_pct > 1.5:
            score += 5
            reasons.append(f"Moderate breakeven range {width_pct:.1f}%")

    # ── Deductions ─────────────────────────────────────────────────

    if net_delta > 0.4:
        deduction = 20
        score -= deduction
        reasons.append(f"HIGH directional risk — net delta {book_greeks.get('net_delta', 0):+.2f}")

    if max_loss > 0 and max_profit > 0 and max_loss > 5 * max_profit:
        deduction = 15
        score -= deduction
        reasons.append(f"Unfavorable loss profile — max loss {max_loss:.0f} > 5x max profit {max_profit:.0f}")

    score = max(0, min(100, score))

    if score < 50:
        log.info(
            "multileg entry quality LOW %d/100 for %s — %s",
            score, strategy_type, "; ".join(reasons),
        )

    return score, reasons


# ── Conflict Detection ────────────────────────────────────────────

def check_book_conflicts(
    symbol: str,
    proposed_strategy: str,
    existing_books: list[dict],
) -> tuple[bool, str]:
    """
    Check if a proposed strategy conflicts with any existing open books.

    Parameters
    ----------
    symbol : str
    proposed_strategy : str
        Strategy type key (e.g. "BULL_PUT_SPREAD").
    existing_books : list[dict]
        Each book must have at least: strategy_type, status.
        Only books with status == "OPEN" are checked.

    Returns
    -------
    (conflict_found, reason) — reason is empty string if no conflict.
    """
    conflict_map = CONFLICTING_STRATEGIES.get(proposed_strategy, {})
    conflicts_with = set(conflict_map.get("conflicts_with", []))

    if not conflicts_with:
        return False, ""

    for book in existing_books:
        if book.get("status") != "OPEN":
            continue
        if book.get("symbol") != symbol:
            continue
        existing_type = book.get("strategy_type", "")
        if existing_type in conflicts_with:
            reason = (
                f"Conflicts with existing {existing_type} book on {symbol} "
                f"(book_id={book.get('book_id', 'unknown')})"
            )
            log.warning("multileg conflict: %s", reason)
            return True, reason

    return False, ""


# ── Execution Plan Builder ────────────────────────────────────────

def build_execution_plan(
    symbol: str,
    strategy_type: str,
    legs: list[dict],
    book_id: str,
    scan_context: dict,
) -> dict:
    """
    Build a complete execution plan by running all validation and
    computation steps.

    Parameters
    ----------
    symbol : str
        Trading symbol (e.g. "NIFTY").
    strategy_type : str
        Key from STRATEGY_CONSTRAINTS.
    legs : list[dict]
        Proposed legs (strike, option_type, side, premium, lots).
    book_id : str
        Unique book identifier.
    scan_context : dict
        Must contain: option_chain, underlying, expiry, iv_rank,
        regime, net_premium, margin.
        Optionally: existing_books (list of open books for conflict check).

    Returns
    -------
    dict with the full execution plan, or an error-dict with
    ``"error"`` key if validation fails.
    """
    option_chain = scan_context.get("option_chain", {})
    underlying = float(scan_context.get("underlying") or 0)
    expiry = scan_context.get("expiry", "")

    # ── Step 1: Validate legs ──────────────────────────────────────
    is_valid, err = validate_legs(strategy_type, legs, option_chain, underlying)
    if not is_valid:
        log.warning("multileg validation failed for %s/%s: %s", symbol, strategy_type, err)
        return {"error": err, "symbol": symbol, "strategy_type": strategy_type, "book_id": book_id}

    # ── Step 2: Conflict check ─────────────────────────────────────
    existing_books = scan_context.get("existing_books", [])
    conflict, conflict_reason = check_book_conflicts(symbol, strategy_type, existing_books)
    if conflict:
        return {
            "error": conflict_reason,
            "symbol": symbol,
            "strategy_type": strategy_type,
            "book_id": book_id,
        }

    # ── Step 3: Compute Greeks ─────────────────────────────────────
    book_greeks = compute_book_greeks(legs, option_chain, underlying, expiry)

    # ── Step 4: Compute net premium ────────────────────────────────
    net_premium = sum(float(l.get("premium", 0)) for l in legs)

    # ── Step 5: Risk profile ───────────────────────────────────────
    risk_profile = compute_book_risk_profile(strategy_type, legs, net_premium, underlying)

    # ── Step 6: Margin ─────────────────────────────────────────────
    margin = calculate_combined_margin(legs, symbol)

    # ── Step 7: Entry quality score ────────────────────────────────
    quality_context = {
        **scan_context,
        "net_premium": net_premium,
        "margin": margin,
    }
    quality_score, quality_reasons = score_entry_quality(
        strategy_type, legs, quality_context, book_greeks, risk_profile,
    )

    plan = {
        "symbol": symbol,
        "strategy_type": strategy_type,
        "book_id": book_id,
        "legs": legs,
        "book_greeks": book_greeks,
        "risk_profile": risk_profile,
        "margin": float(round(margin, 2)),
        "net_premium": float(round(net_premium, 2)),
        "entry_quality_score": quality_score,
        "entry_quality_reasons": quality_reasons,
        "underlying": underlying,
        "expiry": expiry,
    }

    log.info(
        "multileg plan built: %s %s — %d legs, margin=₹%.0f, quality=%d/100",
        symbol, strategy_type, len(legs), margin, quality_score,
    )

    return plan
