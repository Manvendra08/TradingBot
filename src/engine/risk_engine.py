"""
Risk engine for paper trading.

Checks performed before allowing a new trade:
  1. Max open trades per symbol
  2. Max total open trades
  3. Max trades per symbol per day
  4. Daily loss cap
  5. Loss cooldown (wait N minutes after a loss before re-entering)

Note: IST-aligned day boundaries are used so that trade
counts and daily loss cap align with the actual Indian market day.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.models.schema import get_conn
from config.settings import (
    MAX_OPEN_TRADES_PER_SYMBOL,
    MAX_OPEN_TRADES_TOTAL,
    MAX_TRADES_PER_SYMBOL_PER_DAY,
    MAX_DAILY_LOSS_RUPEES,
    LOSS_COOLDOWN_MINUTES,
)

log = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_day_start_utc() -> str:
    """
    Return the UTC ISO timestamp that corresponds to midnight IST today.
    SQLite stores timestamps in UTC; we compare against this floor so that
    daily counters reset at IST midnight rather than UTC midnight.
    """
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + IST_OFFSET
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_ist - IST_OFFSET
    return midnight_utc.strftime("%Y-%m-%dT%H:%M:%S")


def check_risk_limits(symbol: str, setup_type: str | None = None) -> tuple[bool, str]:
    """
    Return (allowed: bool, reason: str).
    'allowed' is True when a new trade for `symbol` may be opened.
    """
    today_start = _ist_day_start_utc()

    with get_conn() as conn:
        # 1. Max open trades per symbol (skip for TIMEFRAME as it has its own pyramid logic)
        if setup_type != 'TIMEFRAME':
            open_symbol = conn.execute(
                "SELECT COUNT(*) AS cnt FROM paper_trades WHERE symbol = ? AND status = 'OPEN'",
                (symbol,),
            ).fetchone()["cnt"]
            if open_symbol >= MAX_OPEN_TRADES_PER_SYMBOL:
                return False, (
                    f"Max open trades for {symbol} reached "
                    f"({open_symbol}/{MAX_OPEN_TRADES_PER_SYMBOL})"
                )

        # 2. Max total open trades
        open_total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM paper_trades WHERE status = 'OPEN'"
        ).fetchone()["cnt"]
        if open_total >= MAX_OPEN_TRADES_TOTAL:
            return False, (
                f"Max total open trades reached ({open_total}/{MAX_OPEN_TRADES_TOTAL})"
            )

        # 3. Max trades per symbol per day
        day_count = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM paper_trades
            WHERE symbol = ? AND opened_at >= ?
            """,
            (symbol, today_start),
        ).fetchone()["cnt"]
        if day_count >= MAX_TRADES_PER_SYMBOL_PER_DAY:
            return False, (
                f"Daily trade limit for {symbol} reached "
                f"({day_count}/{MAX_TRADES_PER_SYMBOL_PER_DAY})"
            )

        # 4. Daily loss cap
        today_pnl_row = conn.execute(
            "SELECT COALESCE(SUM(pnl_rupees), 0) AS total FROM paper_trades WHERE closed_at >= ?",
            (today_start,),
        ).fetchone()
        today_pnl = float(today_pnl_row["total"] if today_pnl_row else 0.0)
        if today_pnl < -abs(MAX_DAILY_LOSS_RUPEES):
            return False, f"Daily loss limit hit (₹{today_pnl:,.0f} / limit -₹{MAX_DAILY_LOSS_RUPEES:,.0f})"

        # 5. Cooldown after SL/loss
        last_loss = conn.execute(
            """
            SELECT closed_at FROM paper_trades
            WHERE symbol = ? AND status IN ('SL_HIT', 'CLOSED') AND pnl_rupees < 0
              AND closed_at >= ?
            ORDER BY closed_at DESC
            LIMIT 1
            """,
            (symbol, today_start),
        ).fetchone()
        if last_loss:
            try:
                last_loss_dt = datetime.fromisoformat(last_loss["closed_at"].replace("Z", "+00:00"))
                if last_loss_dt.tzinfo is None:
                    last_loss_dt = last_loss_dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_loss_dt).total_seconds() / 60
                if elapsed < LOSS_COOLDOWN_MINUTES:
                    remaining = int(LOSS_COOLDOWN_MINUTES - elapsed)
                    return False, (
                        f"Loss cooldown active for {symbol} — "
                        f"{remaining} min remaining"
                    )
            except Exception as exc:
                log.warning("Could not parse last_loss closed_at for %s: %s", symbol, exc)

    return True, "OK"
