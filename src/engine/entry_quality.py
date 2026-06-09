"""
Entry Quality Scorer — validates trade entry location and timing.
B6 fix: explicit validation when sl_underlying/target_underlying missing.
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
                      option_rows, price_change_pct.
    Penalties:
      -25  price on wrong side of key level
      -25  poor R:R (target closer than SL)
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

    # 2. R:R check — B6: validate keys exist, log when missing
    sl     = float(ctx.get("sl_underlying") or 0)
    target = float(ctx.get("target_underlying") or 0)
    if sl <= 0 or target <= 0:
        log.debug("%s: entry quality R:R skipped — sl=%s target=%s (tag only)", symbol, sl, target)
        reasons.append("Missing SL/target — R:R check skipped")
    else:
        dist_sl     = abs(underlying - sl)
        dist_target = abs(underlying - target)
        if dist_sl > 0 and dist_target / dist_sl < 1.0:
            score -= 25
            reasons.append(f"Poor R:R {dist_target/dist_sl:.2f} — target closer than SL")

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
