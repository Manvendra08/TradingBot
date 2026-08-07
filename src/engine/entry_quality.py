"""
Entry Quality Scorer — validates trade entry location and timing.
B6 fix: explicit validation when sl_underlying/target_underlying missing.
P0 fix: R:R penalty is now direction-aware for SELL trades.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def calculate_entry_quality(
    symbol: str,
    option_type: str,
    strike: float,
    ctx: dict,
) -> tuple[int, list[str]]:
    """
    Score 0-100. Returns (score, reasons).

    ctx must contain: underlying, support, resistance,
                      sl_underlying, target_underlying,
                      option_rows, price_change_pct, side.
    Penalties:
      -25  price on wrong side of key level
      -25  poor R:R (target closer than SL) — direction-aware for SELL
      -20  wide bid-ask spread (>5% of LTP)
      -15  chasing after large move (>1.5%)
    """
    score = 100
    reasons: list[str] = []

    underlying = float(ctx.get("underlying") or 0)
    if underlying <= 0:
        return 0, ["Missing underlying price"]

    support    = float(ctx.get("support") or 0)
    resistance = float(ctx.get("resistance") or 0)
    side       = ctx.get("side") or "BUY"

    # 1. Price location vs key level
    if support > 0 and resistance > 0:
        range_size = abs(resistance - support)
        if range_size > 0:
            if (option_type == "PE" and side == "BUY") or (option_type == "CE" and side == "SELL"):
                if abs(underlying - support) < range_size * 0.15:
                    score -= 25
                    reasons.append(f"Price near support {support:.0f} — bounce risk")
            elif (option_type == "CE" and side == "BUY") or (option_type == "PE" and side == "SELL"):
                if abs(underlying - resistance) < range_size * 0.15:
                    score -= 25
                    reasons.append(f"Price near resistance {resistance:.0f} — rejection risk")

    # 2. R:R check — direction-aware (P0 fix)
    #    For BUY trades:  target > underlying > sl  → dist_target = target - underlying, dist_sl = underlying - sl
    #    For SELL trades: sl > underlying > target  → dist_target = underlying - target, dist_sl = sl - underlying
    #    Using signed distances ensures SELL premium-decay setups (sl above, target below) are evaluated correctly.
    sl     = float(ctx.get("sl_underlying") or 0)
    target = float(ctx.get("target_underlying") or 0)
    if sl <= 0 or target <= 0:
        log.debug("%s: entry quality R:R skipped — sl=%s target=%s (tag only)", symbol, sl, target)
        reasons.append("Missing SL/target — R:R check skipped")
    else:
        is_long = (side == "BUY" and option_type in ("CE", "FUT")) or \
                  (side == "SELL" and option_type == "PE")
        if is_long:
            # Underlying must move UP to hit target, DOWN to hit SL
            dist_target = target - underlying
            dist_sl     = underlying - sl
        else:
            # Underlying must move DOWN to hit target, UP to hit SL
            dist_target = underlying - target
            dist_sl     = sl - underlying

        if dist_sl > 0 and dist_target > 0 and dist_target / dist_sl < 1.0:
            score -= 25
            reasons.append(f"Poor R:R {dist_target/dist_sl:.2f} — target closer than SL")
        elif dist_sl <= 0 or dist_target <= 0:
            # SL or target is on the wrong side of current price — structural plan error
            score -= 25
            reasons.append("SL or target inverted vs current price")

    # 3. Bid-ask spread
    for row in (ctx.get("option_rows") or []):
        try:
            if (abs(float(row.get("strike") or 0) - strike) < 0.01 and
                    str(row.get("option_type") or "").upper() == option_type):
                bid = float(row.get("bid") or 0)
                ask = float(row.get("ask") or 0)
                ltp = float(row.get("ltp") or 0)
                if ltp > 0 and bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / ltp * 100
                    if spread_pct > 5.0:
                        score -= 20
                        reasons.append(f"Wide spread {spread_pct:.1f}% — poor liquidity")
                break
        except Exception:
            continue

    # 4. Chasing check
    price_change_pct = float(ctx.get("price_change_pct") or 0)
    if (side == "BUY" and option_type == "PE") or (side == "SELL" and option_type == "CE"):
        if price_change_pct < -1.5:
            score -= 15
            reasons.append(f"Chasing after {price_change_pct:.1f}% drop")
    elif (side == "BUY" and option_type == "CE") or (side == "SELL" and option_type == "PE"):
        if price_change_pct > 1.5:
            score -= 15
            reasons.append(f"Chasing after +{price_change_pct:.1f}% rally")

    score = max(0, min(100, score))
    if score < 60:
        log.info("%s: entry quality LOW %d/100 — %s", symbol, score, "; ".join(reasons))
    return score, reasons


def calculate_multileg_entry_quality(
    symbol: str,
    strategy_type: str,
    legs: list[dict],
    book_greeks: dict,
    risk_profile: dict,
    scan_context: dict,
) -> tuple[int, list[str]]:
    """
    Score 0-100 for multi-leg entry quality.
    Returns (score, reasons).
    """
    score = 100
    reasons: list[str] = []

    underlying = float(scan_context.get("underlying") or 0)
    if underlying <= 0:
        return 0, ["Missing underlying price"]

    net_delta = abs(float(book_greeks.get("net_delta") or 0))
    max_profit = float(risk_profile.get("max_profit") or 0)
    max_loss = float(risk_profile.get("max_loss") or 1)
    net_premium = float(scan_context.get("net_premium") or 0)

    # 1. IV signal — high IV is good for selling premium
    atm_iv = 0.0
    for row in scan_context.get("option_rows", []):
        strike = float(row.get("strike") or 0)
        if abs(strike - underlying) < 1 and float(row.get("iv") or 0) > 0:
            atm_iv = float(row["iv"])
            break
    if atm_iv >= 20:
        score += 15
        reasons.append(f"High IV ({atm_iv:.1f}%) — premium selling edge")
    elif atm_iv < 12:
        score -= 15
        reasons.append(f"Low IV ({atm_iv:.1f}%) — thin premium")

    # 2. Net delta near 0 (market neutral)
    if net_delta < 0.10:
        score += 15
        reasons.append(f"Market neutral (Δ={net_delta:.2f})")
    elif net_delta > 0.40:
        score -= 20
        reasons.append(f"High directional exposure (Δ={net_delta:.2f})")

    # 3. Risk/reward ratio
    rr = max_profit / max_loss if max_loss > 0 else 0
    if rr > 0.3:
        score += 10
        reasons.append(f"Good R:R ({rr:.2f})")
    elif rr < 0.1:
        score -= 10
        reasons.append(f"Poor R:R ({rr:.2f})")

    # 4. Premium yield on margin
    margin = float(risk_profile.get("margin", 0) or 0)
    if margin > 0 and net_premium > 0:
        yield_pct = (net_premium / margin) * 100
        if yield_pct > 2:
            score += 10
            reasons.append(f"Good yield on margin ({yield_pct:.1f}%)")

    # 5. Breakeven width
    be_upper = float(risk_profile.get("breakeven_upper") or 0)
    be_lower = float(risk_profile.get("breakeven_lower") or 0)
    if be_upper > 0 and be_lower > 0 and underlying > 0:
        width_pct = ((be_upper - be_lower) / underlying) * 100
        if width_pct > 3:
            score += 10
            reasons.append(f"Wide breakeven ({width_pct:.1f}% of underlying)")

    # 6. Max loss cap
    if max_loss > 5 * max_profit and max_profit > 0:
        score -= 15
        reasons.append(f"Max loss {max_loss/max_profit:.1f}x max profit")

    return max(0, min(100, score)), reasons
