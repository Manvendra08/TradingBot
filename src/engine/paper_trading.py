"""Auto paper-trading engine based on bot verdict + scan context."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from src.models.schema import (
    close_paper_trade,
    get_open_paper_trade,
    insert_paper_trade,
)


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
    if underlying <= 0:
        return None

    bullish = verdict in {"Long Buildup", "Put Writing", "OI Bias Bullish"}
    bearish = verdict in {"Short Buildup", "Call Writing", "OI Bias Bearish"}
    if not bullish and not bearish:
        return None

    option_type = "CE" if bullish else "PE"
    strike = atm if atm > 0 else round(underlying)
    if bullish:
        sl = support if support > 0 else underlying * 0.995
        target = resistance if resistance > 0 else underlying * 1.01
    else:
        sl = resistance if resistance > 0 else underlying * 1.005
        target = support if support > 0 else underlying * 0.99

    return {
        "verdict_label": verdict,
        "option_type": option_type,
        "strike": strike,
        "entry_underlying": underlying,
        "sl_underlying": round(sl, 4),
        "target_underlying": round(target, 4),
    }


def _maybe_close_open_trade(symbol: str, underlying: float, now_iso: str) -> None:
    open_trade = get_open_paper_trade(symbol)
    if not open_trade:
        return

    option_type = open_trade.get("option_type")
    target = float(open_trade.get("target_underlying") or 0.0)
    sl = float(open_trade.get("sl_underlying") or 0.0)
    if option_type == "CE":
        if target > 0 and underlying >= target:
            close_paper_trade(open_trade["id"], now_iso, underlying, "CLOSED_TARGET", "target hit")
            return
        if sl > 0 and underlying <= sl:
            close_paper_trade(open_trade["id"], now_iso, underlying, "CLOSED_SL", "stop loss hit")
            return
    else:
        if target > 0 and underlying <= target:
            close_paper_trade(open_trade["id"], now_iso, underlying, "CLOSED_TARGET", "target hit")
            return
        if sl > 0 and underlying >= sl:
            close_paper_trade(open_trade["id"], now_iso, underlying, "CLOSED_SL", "stop loss hit")
            return


def run_paper_trading(symbol: str, scan_context: dict, digest_id: str, intelligence_text: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    if underlying <= 0:
        return

    _maybe_close_open_trade(symbol, underlying, now_iso)
    if get_open_paper_trade(symbol):
        return

    verdict, confidence = _parse_verdict_and_confidence(intelligence_text)
    plan = _trade_plan_from_verdict(verdict, confidence, scan_context or {})
    if not plan:
        return

    insert_paper_trade(
        {
            "opened_at": now_iso,
            "symbol": symbol,
            "verdict_label": plan["verdict_label"],
            "option_type": plan["option_type"],
            "strike": plan["strike"],
            "entry_underlying": plan["entry_underlying"],
            "sl_underlying": plan["sl_underlying"],
            "target_underlying": plan["target_underlying"],
            "status": "OPEN",
            "reason": f"auto by verdict={verdict} confidence={confidence}",
            "digest_id": digest_id,
        }
    )

