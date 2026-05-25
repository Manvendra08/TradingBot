"""Auto paper-trading engine based on bot verdict + scan context."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from src.models.schema import (
    close_paper_trade,
    get_open_paper_trade,
    insert_paper_trade,
    get_latest_snapshots_for_symbol,
)
from config.settings import LOT_SIZES, DEFAULT_LOTS_PER_TRADE


def _get_option_premium(symbol: str, expiry: str, strike: float, option_type: str) -> float | None:
    """Fetch current option premium (LTP) from latest snapshot."""
    try:
        snapshots = get_latest_snapshots_for_symbol(symbol, expiry)
        for snap in snapshots:
            if (abs(snap.get("strike", 0) - strike) < 0.01 and 
                snap.get("option_type") == option_type):
                return float(snap.get("ltp") or 0.0)
    except Exception:
        pass
    return None


def _calculate_option_sl_target(entry_premium: float, option_type: str, is_bullish: bool) -> tuple[float, float]:
    """
    Calculate SL and Target in premium terms for options.
    
    For CE (Call):
      - Bullish: SL = entry - 30%, Target = entry + 50%
      - Bearish: SL = entry + 30%, Target = entry - 50%
    
    For PE (Put):
      - Bullish: SL = entry + 30%, Target = entry - 50%
      - Bearish: SL = entry - 30%, Target = entry + 50%
    """
    if entry_premium <= 0:
        return 0.0, 0.0
    
    if option_type == "CE":
        if is_bullish:
            # Long CE: SL below, Target above
            sl = entry_premium * 0.70  # -30%
            target = entry_premium * 1.50  # +50%
        else:
            # Short CE: SL above, Target below
            sl = entry_premium * 1.30  # +30%
            target = entry_premium * 0.50  # -50%
    else:  # PE
        if is_bullish:
            # Short PE: SL above, Target below
            sl = entry_premium * 1.30  # +30%
            target = entry_premium * 0.50  # -50%
        else:
            # Long PE: SL below, Target above
            sl = entry_premium * 0.70  # -30%
            target = entry_premium * 1.50  # +50%
    
    return round(sl, 2), round(target, 2)


def _parse_verdict_and_confidence(intel_text: str) -> tuple[str, int]:
    verdict = ""
    confidence = 0
    m_v = re.search(r"\*Verdict:\s*([^\*]+)\*", intel_text or "")
    if m_v:
        verdict = m_v.group(1).strip()
    m_c = re.search(r"Confidence:\s*(\d+)%", intel_text or "")
    if m_c:
        confidence = int(m_c.group(1))
    return verdict, confidence


def _trade_plan_from_verdict(verdict: str, confidence: int, ctx: dict) -> dict | None:
    if confidence < 60:
        return None

    atm = float(ctx.get("atm_strike") or 0.0)
    underlying = float(ctx.get("underlying") or 0.0)
    support = float(ctx.get("support") or 0.0)
    resistance = float(ctx.get("resistance") or 0.0)
    expiry = ctx.get("expiry", "")
    symbol = ctx.get("symbol", "")
    
    if underlying <= 0:
        return None

    bullish = verdict in {"Long Buildup", "Put Writing", "OI Bias Bullish"}
    bearish = verdict in {"Short Buildup", "Call Writing", "OI Bias Bearish"}
    if not bullish and not bearish:
        return None

    option_type = "CE" if bullish else "PE"
    strike = atm if atm > 0 else round(underlying)
    
    # Calculate underlying-based SL/Target (for reference)
    if bullish:
        sl_underlying = support if support > 0 else underlying * 0.995
        target_underlying = resistance if resistance > 0 else underlying * 1.01
    else:
        sl_underlying = resistance if resistance > 0 else underlying * 1.005
        target_underlying = support if support > 0 else underlying * 0.99
    
    # Fetch option premium
    entry_premium = _get_option_premium(symbol, expiry, strike, option_type)
    
    # Calculate premium-based SL/Target
    sl_premium = None
    target_premium = None
    if entry_premium and entry_premium > 0:
        sl_premium, target_premium = _calculate_option_sl_target(entry_premium, option_type, bullish)

    return {
        "verdict_label": verdict,
        "option_type": option_type,
        "strike": strike,
        "entry_underlying": underlying,
        "entry_premium": entry_premium,
        "sl_underlying": round(sl_underlying, 4),
        "target_underlying": round(target_underlying, 4),
        "sl_premium": sl_premium,
        "target_premium": target_premium,
    }


def _maybe_close_open_trade(symbol: str, underlying: float, expiry: str, now_iso: str) -> None:
    open_trade = get_open_paper_trade(symbol)
    if not open_trade:
        return

    option_type = open_trade.get("option_type")
    strike = float(open_trade.get("strike") or 0.0)
    
    # Get current premium for exit
    exit_premium = _get_option_premium(symbol, expiry, strike, option_type)
    
    # Check premium-based SL/Target first (if available)
    sl_premium = open_trade.get("sl_premium")
    target_premium = open_trade.get("target_premium")
    
    if exit_premium and sl_premium and target_premium:
        # Use premium-based exit logic
        if option_type == "CE":
            if target_premium > 0 and exit_premium >= target_premium:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                return
            if sl_premium > 0 and exit_premium <= sl_premium:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                return
        else:  # PE
            if target_premium > 0 and exit_premium <= target_premium:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                return
            if sl_premium > 0 and exit_premium >= sl_premium:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                return
    else:
        # Fallback to underlying-based exit logic (legacy)
        target_underlying = float(open_trade.get("target_underlying") or 0.0)
        sl_underlying = float(open_trade.get("sl_underlying") or 0.0)
        
        if option_type == "CE":
            if target_underlying > 0 and underlying >= target_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                return
            if sl_underlying > 0 and underlying <= sl_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                return
        else:
            if target_underlying > 0 and underlying <= target_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                return
            if sl_underlying > 0 and underlying >= sl_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                return


def run_paper_trading(symbol: str, scan_context: dict, digest_id: str, intelligence_text: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    expiry = (scan_context or {}).get("expiry", "")
    
    if underlying <= 0:
        return

    _maybe_close_open_trade(symbol, underlying, expiry, now_iso)
    if get_open_paper_trade(symbol):
        return

    verdict, confidence = _parse_verdict_and_confidence(intelligence_text)
    
    # Add symbol and expiry to context for premium fetching
    ctx = {**(scan_context or {}), "symbol": symbol, "expiry": expiry}
    plan = _trade_plan_from_verdict(verdict, confidence, ctx)
    if not plan:
        return

    # Get lot size and number of lots
    lot_size = LOT_SIZES.get(symbol, 1)
    lots = DEFAULT_LOTS_PER_TRADE

    insert_paper_trade(
        {
            "opened_at": now_iso,
            "symbol": symbol,
            "verdict_label": plan["verdict_label"],
            "option_type": plan["option_type"],
            "strike": plan["strike"],
            "entry_underlying": plan["entry_underlying"],
            "entry_premium": plan.get("entry_premium"),
            "sl_underlying": plan["sl_underlying"],
            "sl_premium": plan.get("sl_premium"),
            "target_underlying": plan["target_underlying"],
            "target_premium": plan.get("target_premium"),
            "lots": lots,
            "status": "OPEN",
            "reason": f"auto by verdict={verdict} confidence={confidence}",
            "digest_id": digest_id,
        }
    )

