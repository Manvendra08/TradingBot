"""Stock Futures Telegram Digest & Alerting.

Dispatches high-density, professional Telegram summaries:
- 19:35 IST: EOD Watchlist Summary (Top ARMED candidates, Regime, Net Skew)
- 09:35 IST: Morning Execution Summary (Fills, Rejections, Portfolio State)
- Event alerts: SL hit, T1 hit, T2 hit, Breaker/Risk Halt trips.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytz

from src.alerts.digest import _esc
from src.alerts.telegram_dispatcher import send_text

log = logging.getLogger("stockfut.digest")
_IST = pytz.timezone("Asia/Kolkata")


def build_eod_watchlist_digest(
    date_str: str,
    regime: str,
    watchlist: List[Dict[str, Any]],
) -> str:
    """Format the 19:35 IST EOD Watchlist Telegram digest."""
    armed = [w for w in watchlist if w.get("status") == "ARMED"]
    lines = [
        f"🎯 <b>STOCK FUTURES EOD WATCHLIST</b> | {date_str}",
        f"📊 <b>Regime:</b> <code>{regime}</code> | <b>ARMED:</b> {len(armed)}/{len(watchlist)}",
        "──────────────────────────",
    ]

    if not armed:
        lines.append("<i>No ARMED setups qualified for tomorrow's 09:30 open.</i>")
    else:
        # Show top 5 armed sorted by score
        sorted_armed = sorted(armed, key=lambda x: x.get("score", 0), reverse=True)[:5]
        for row in sorted_armed:
            sym = row.get("symbol", "")
            direction = row.get("direction", "")
            icon = "🟢" if direction == "LONG" else "🔴"
            setup = row.get("setup", "")
            score = row.get("score", 0)
            entry = row.get("trigger_price", 0.0)
            sl = row.get("sl_price", 0.0)
            t1 = row.get("t1_price", 0.0)
            risk = row.get("risk_per_lot", 0.0)
            lot = row.get("lot_size", 0)
            lots = row.get("lots", 0)
            lots_str = f"Lots: {lots} (Size: {lot})" if lots else f"Lot Size: {lot}"

            lines.append(
                f"{icon} <b>{sym}</b> ({direction} • {setup}) [Score {score}/100]\n"
                f"   Entry: ₹{entry:.2f} | SL: ₹{sl:.2f} | T1: ₹{t1:.2f}\n"
                f"   {lots_str} | Risk/Lot: ₹{risk:,.0f}"
            )

    lines.append("──────────────────────────")
    lines.append("⚡ <i>Execution scheduled at 09:30 IST via Marketable Limit Orders.</i>")
    return "\n".join(lines)


def build_morning_execution_digest(
    date_str: str,
    fills: List[Dict[str, Any]],
    rejections: List[Dict[str, Any]],
    open_trades: List[Dict[str, Any]],
) -> str:
    """Format the 09:35 IST Morning Execution Telegram digest."""
    lines = [
        f"⚡ <b>STOCK FUTURES MORNING EXECUTION</b> | {date_str} 09:30 IST",
        f"💼 <b>Active Portfolio:</b> {len(open_trades)}/6 open positions",
        "──────────────────────────",
    ]

    if fills:
        lines.append(f"<b>FILLED ENTRIES ({len(fills)}):</b>")
        for f in fills:
            sym = f.get("tradingsymbol", f.get("symbol", ""))
            side = f.get("direction", "")
            icon = "🟢" if side == "LONG" else "🔴"
            lots = f.get("lots", 0)
            px = f.get("entry_price", 0.0)
            sl = f.get("sl_price", 0.0)
            t1 = f.get("t1_price", 0.0)
            lines.append(
                f"  {icon} <b>{sym}</b> {side} {lots} lots @ ₹{px:.2f} (SL: ₹{sl:.2f}, T1: ₹{t1:.2f})"
            )
    else:
        lines.append("<i>No new entries filled this morning.</i>")

    if rejections:
        lines.append("\n<b>SKIPPED / REJECTED:</b>")
        for r in rejections[:5]:
            sym = r.get("symbol", "")
            reason = r.get("reason", "UNKNOWN")
            lines.append(f"  ⚠️ <b>{sym}</b>: <code>{reason}</code>")

    lines.append("──────────────────────────")
    return "\n".join(lines)


def build_trade_event_digest(
    event_type: str,
    trade: Dict[str, Any],
    detail: str = "",
) -> str:
    """Format real-time trade event alerts (SL hit, T1 hit, T2 hit, Breaker trip)."""
    sym = trade.get("tradingsymbol", trade.get("symbol", "STOCKFUT"))
    side = trade.get("direction", "")
    pnl = trade.get("realized_pnl", 0.0)
    pnl_str = f"+₹{pnl:,.0f}" if pnl >= 0 else f"-₹{abs(pnl):,.0f}"

    if event_type == "SL_HIT":
        return (
            f"🛑 <b>STOCK FUTURES STOP LOSS HIT</b>\n"
            f"<b>{sym}</b> {side} position stopped out.\n"
            f"Exit: ₹{trade.get('exit_price', 0.0):.2f} | Realized P&L: <b>{pnl_str}</b>\n"
            f"Reason: {detail or 'Hard SL breach'}"
        )
    elif event_type == "T1_HIT":
        return (
            f"🎯 <b>STOCK FUTURES TARGET 1 HIT</b>\n"
            f"<b>{sym}</b> {side} reached Target 1 (1.6R+).\n"
            f"Partial exit (50% lots) locked | Realized P&L: <b>{pnl_str}</b>\n"
            f"Runner stop moved to entry − 0.25x ATR."
        )
    elif event_type == "T2_HIT":
        return (
            f"🏆 <b>STOCK FUTURES TARGET 2 (FINAL EXIT)</b>\n"
            f"<b>{sym}</b> {side} runner reached final target.\n"
            f"Position fully closed | Total Realized P&L: <b>{pnl_str}</b>"
        )
    elif event_type == "BREAKER_TRIP":
        return (
            f"🚨 <b>STOCK FUTURES CIRCUIT BREAKER TRIPPED</b>\n"
            f"<b>Cause:</b> <code>{detail}</code>\n"
            f"Trading system HALTED. No new entries permitted."
        )
    else:
        return (
            f"ℹ️ <b>STOCK FUTURES EVENT: {event_type}</b>\n"
            f"<b>{sym}</b>: {detail}"
        )


def send_eod_watchlist_alert(
    date_str: str,
    regime: str,
    watchlist: List[Dict[str, Any]],
) -> bool:
    """Dispatch EOD watchlist alert via Telegram."""
    try:
        text = build_eod_watchlist_digest(date_str, regime, watchlist)
        return send_text(text)
    except Exception as e:
        log.error("Failed to dispatch EOD watchlist alert: %s", e)
        return False


def send_morning_execution_alert(
    date_str: str,
    fills: List[Dict[str, Any]],
    rejections: List[Dict[str, Any]],
    open_trades: List[Dict[str, Any]],
) -> bool:
    """Dispatch morning execution alert via Telegram."""
    try:
        text = build_morning_execution_digest(date_str, fills, rejections, open_trades)
        return send_text(text)
    except Exception as e:
        log.error("Failed to dispatch morning execution alert: %s", e)
        return False


def send_trade_event_alert(
    event_type: str,
    trade: Dict[str, Any],
    detail: str = "",
) -> bool:
    """Dispatch trade event alert via Telegram."""
    try:
        text = build_trade_event_digest(event_type, trade, detail)
        return send_text(text)
    except Exception as e:
        log.error("Failed to dispatch trade event alert: %s", e)
        return False
