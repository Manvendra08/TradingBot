"""Auto paper-trading engine based on bot verdict + scan context."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytz

from src.engine.paper_plan import (
    build_paper_trade_plan,
    is_bearish_verdict,
    is_bullish_verdict,
)
from src.models.schema import (
    close_paper_trade,
    get_open_paper_trade,
    insert_paper_trade,
    get_latest_snapshots_for_symbol,
)
from config.settings import LOT_SIZES, DEFAULT_LOTS_PER_TRADE
from config.symbol_classes import market_window

IST = pytz.timezone("Asia/Kolkata")


def _is_market_open(symbol: str) -> bool:
    """Return True only if current IST time is within the symbol's market window."""
    now = datetime.now(IST)
    open_t, close_t, days = market_window(symbol)
    if now.weekday() not in days:
        return False
    t = now.strftime("%H:%M")
    return open_t <= t <= close_t


def _get_option_premium(
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    option_rows: list[dict] | None = None,
) -> float | None:
    """Fetch current option premium (LTP) from latest snapshot."""
    for row in option_rows or []:
        try:
            if (
                abs(float(row.get("strike") or 0) - strike) < 0.01
                and str(row.get("option_type") or "").upper() == option_type
            ):
                premium = float(row.get("ltp") or 0.0)
                return premium if premium > 0 else None
        except Exception:
            continue
    try:
        snapshots = get_latest_snapshots_for_symbol(symbol, expiry)
        for snap in snapshots:
            if (abs(snap.get("strike", 0) - strike) < 0.01 and 
                snap.get("option_type") == option_type):
                return float(snap.get("ltp") or 0.0)
    except Exception:
        pass
    return None


def _calculate_option_sl_target(entry_premium: float) -> tuple[float, float]:
    """
    Calculate SL and Target in premium terms.
    Engine always takes LONG positions (buys CE for bullish, buys PE for bearish).
    Long option: profit when premium rises.
      SL     = entry * 0.70  (exit if premium drops 30%)
      Target = entry * 1.50  (exit when premium rises 50%)
    """
    if entry_premium <= 0:
        return 0.0, 0.0
    sl     = round(entry_premium * 0.70, 2)   # -30%
    target = round(entry_premium * 1.50, 2)   # +50%
    return sl, target


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


def _is_reversal_against_open_trade(open_trade: dict, verdict: str, confidence: int) -> bool:
    if confidence < 70:
        return False
    ot = str(open_trade.get("option_type") or "").upper()
    if ot == "CE" and is_bearish_verdict(verdict):
        return True
    if ot == "PE" and is_bullish_verdict(verdict):
        return True
    return False


def _trade_plan_from_verdict(verdict: str, confidence: int, ctx: dict) -> dict | None:
    plan = build_paper_trade_plan(verdict, confidence, ctx)
    if not plan:
        return None

    expiry = ctx.get("expiry", "")
    symbol = ctx.get("symbol", "")
    option_rows = ctx.get("option_rows") or []
    strike = float(plan["strike"])
    option_type = str(plan["option_type"])
    entry_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows)

    sl_premium = None
    target_premium = None
    if entry_premium and entry_premium > 0:
        sl_premium, target_premium = _calculate_option_sl_target(entry_premium)

    return {
        **plan,
        "entry_premium": entry_premium,
        "sl_premium": sl_premium,
        "target_premium": target_premium,
    }


def _maybe_close_open_trade(
    symbol: str,
    underlying: float,
    expiry: str,
    now_iso: str,
    option_rows: list[dict] | None = None,
) -> None:
    open_trade = get_open_paper_trade(symbol)
    if not open_trade:
        return

    option_type = open_trade.get("option_type")
    strike = float(open_trade.get("strike") or 0.0)
    
    # Get current premium for exit
    exit_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows)
    
    # Check premium-based SL/Target first (if available)
    sl_premium = open_trade.get("sl_premium")
    target_premium = open_trade.get("target_premium")
    
    if exit_premium and sl_premium and target_premium:
        # Premium-based exit — direction depends on option type
        # Long CE (bullish): target = premium rises, SL = premium falls
        # Long PE (bearish): target = premium rises, SL = premium falls
        # (engine always takes long positions — buys CE for bullish, buys PE for bearish)
        if exit_premium >= float(target_premium):
            close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
            return
        if exit_premium <= float(sl_premium):
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
    verdict, confidence = _parse_verdict_and_confidence(intelligence_text)

    if underlying <= 0:
        return

    # ── Market hours guard ────────────────────────────────────────────────
    # Never open or close paper trades outside the symbol's market window.
    if not _is_market_open(symbol):
        import logging
        logging.getLogger(__name__).debug(
            "%s: paper-trading skipped — outside market hours", symbol
        )
        return

    option_rows = list((scan_context or {}).get("option_rows") or [])
    _maybe_close_open_trade(symbol, underlying, expiry, now_iso, option_rows)
    open_trade = get_open_paper_trade(symbol)
    if open_trade and _is_reversal_against_open_trade(open_trade, verdict, confidence):
        strike = float(open_trade.get("strike") or 0.0)
        option_type = str(open_trade.get("option_type") or "")
        exit_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows) if strike > 0 else None
        close_paper_trade(
            open_trade["id"],
            now_iso,
            underlying,
            exit_premium,
            "CLOSED_MANUAL",
            f"reversal: verdict={verdict} conf={confidence}",
        )
        open_trade = None

    if open_trade:
        return
    
    # Add symbol and expiry to context for premium fetching
    ctx = {**(scan_context or {}), "symbol": symbol, "expiry": expiry, "option_rows": option_rows}
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
