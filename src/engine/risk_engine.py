"""
Risk Engine — basic trade frequency controls.
Applies to paper trading too: overtrading distorts research results.
B1 fix: moved from Phase 4 to Phase 2.
B9 fix: today_start now uses IST midnight, not UTC midnight, so daily trade
        counts and daily loss cap align with the actual Indian market day.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytz

from src.models.schema import get_conn
from config.settings import (
    MAX_OPEN_TRADES_PER_SYMBOL,
    MAX_OPEN_TRADES_TOTAL,
    MAX_TRADES_PER_SYMBOL_PER_DAY,
    MAX_DAILY_LOSS_RUPEES,
    LOSS_COOLDOWN_MINUTES,
)

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def _ist_day_start_utc() -> str:
    """
    Return the UTC ISO timestamp for the start of the current IST calendar day.
    e.g. if IST is 2026-06-12 10:30, returns '2026-06-12T04:30:00+00:00'
    (IST midnight = 18:30 UTC previous day)
    """
    now_ist = datetime.now(IST)
    ist_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    return ist_midnight.astimezone(timezone.utc).isoformat()


def check_risk_limits(symbol: str, setup_type: str | None = None) -> tuple[bool, str]:
    """
    Hard frequency controls. Returns (allowed, reason).
    Call this BEFORE any paper trade execution.
    """
    now_utc = datetime.now(timezone.utc)
    today_start = _ist_day_start_utc()   # B9: IST-aligned day boundary

    with get_conn() as conn:

        # 1. Max open trades per symbol (bypass for TIMEFRAME)
        if setup_type != 'TIMEFRAME':
            open_sym = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE symbol=? AND status='OPEN' AND (setup_type IS NULL OR setup_type != 'TIMEFRAME')",
                (symbol,),
            ).fetchone()["c"]
            if open_sym >= MAX_OPEN_TRADES_PER_SYMBOL:
                return False, f"Max open trades per symbol ({open_sym}/{MAX_OPEN_TRADES_PER_SYMBOL})"

        # 2. Max total open trades across all symbols (bypass for TIMEFRAME)
        if setup_type != 'TIMEFRAME':
            open_total = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE status='OPEN' AND (setup_type IS NULL OR setup_type != 'TIMEFRAME')",
            ).fetchone()["c"]
            if open_total >= MAX_OPEN_TRADES_TOTAL:
                return False, f"Max total open trades ({open_total}/{MAX_OPEN_TRADES_TOTAL})"

        # 3. Max trades per symbol per day (bypass for TIMEFRAME)
        if setup_type != 'TIMEFRAME':
            today_count = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE symbol=? AND opened_at >= ?",
                (symbol, today_start),
            ).fetchone()["c"]
            if today_count >= MAX_TRADES_PER_SYMBOL_PER_DAY:
                return False, f"Max trades per day ({today_count}/{MAX_TRADES_PER_SYMBOL_PER_DAY})"

        # 4. Daily loss cap (Removed per user request)
        # today_pnl = conn.execute(
        #     "SELECT COALESCE(SUM(pnl_rupees), 0) AS total FROM paper_trades WHERE closed_at >= ?",
        #     (today_start,),
        # ).fetchone()["total"]
        # if float(today_pnl) < -abs(MAX_DAILY_LOSS_RUPEES):
        #     return False, f"Daily loss limit hit (\u20b9{float(today_pnl):,.0f})"

        # 5. Cooldown after SL/loss
        last_loss = conn.execute(
            """
            SELECT closed_at FROM paper_trades
            WHERE symbol=? AND status IN ('CLOSED_SL', 'CLOSED_MANUAL', 'Dead Trade', 'TF-1H-Cross')
            AND pnl_rupees < 0
            ORDER BY closed_at DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()

    if last_loss and last_loss["closed_at"]:
        try:
            loss_time = datetime.fromisoformat(last_loss["closed_at"])
            if loss_time.tzinfo is None:
                loss_time = loss_time.replace(tzinfo=timezone.utc)
            cooldown_end = loss_time + timedelta(minutes=LOSS_COOLDOWN_MINUTES)
            if now_utc < cooldown_end:
                mins_left = (cooldown_end - now_utc).total_seconds() / 60
                return False, f"Cooldown active after loss ({mins_left:.0f} min remaining)"
        except Exception:
            pass

    return True, "Risk checks passed"
