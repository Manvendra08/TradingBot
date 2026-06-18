"""
Risk engine — paper AND live trading.

Checks performed before allowing a new trade:
  1. Max open trades per symbol
  2. Max total open trades
  3. Max trades per symbol per day
  4. Daily loss cap  [FIX #3: sums only negative P&L so profits don't mask losses]
  5. Loss cooldown (wait N minutes after a loss before re-entering)
  6. Account-level consecutive-loss circuit breaker [FIX #11: new]

Both check_risk_limits() (paper) and check_live_risk_limits() (live) share
identical logic — they only differ in which DB table they query.

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

# FIX #11: Account-level consecutive-loss circuit breaker.
# If this many losing trades close within CONSECUTIVE_LOSS_WINDOW_MINUTES across
# ANY symbols, all new trading is halted until the window expires.
CONSECUTIVE_LOSS_LIMIT = 3
CONSECUTIVE_LOSS_WINDOW_MINUTES = 30


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


def _check_consecutive_loss_breaker(conn, trades_table: str, label: str) -> tuple[bool, str]:
    """
    FIX #11: Account-level consecutive-loss circuit breaker.
    Counts losing trades closed across ALL symbols in the last
    CONSECUTIVE_LOSS_WINDOW_MINUTES.  If >= CONSECUTIVE_LOSS_LIMIT, blocks
    all new entries until the rolling window moves past those losses.
    """
    window_start = (
        datetime.now(timezone.utc) - timedelta(minutes=CONSECUTIVE_LOSS_WINDOW_MINUTES)
    ).strftime("%Y-%m-%dT%H:%M:%S")

    recent_losses = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM {trades_table}
        WHERE pnl_rupees < 0
          AND closed_at >= ?
          AND status IN ('SL_HIT', 'CLOSED')
        """,
        (window_start,),
    ).fetchone()["cnt"]

    if recent_losses >= CONSECUTIVE_LOSS_LIMIT:
        return False, (
            f"[{label}] Account-level circuit breaker: {recent_losses} losing trades "
            f"in the last {CONSECUTIVE_LOSS_WINDOW_MINUTES} min across all symbols — "
            "all new entries halted until the window clears."
        )
    return True, "OK"


def _check_risk_limits_for_table(
    symbol: str,
    trades_table: str,
    label: str,
) -> tuple[bool, str]:
    """
    Core risk-check logic, parameterised over the trades table name.
    Used by both check_risk_limits (paper) and check_live_risk_limits (live).
    """
    today_start = _ist_day_start_utc()

    with get_conn() as conn:
        # 1. Max open trades per symbol
        open_symbol = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM {trades_table} WHERE symbol = ? AND status = 'OPEN'",
            (symbol,),
        ).fetchone()["cnt"]
        if open_symbol >= MAX_OPEN_TRADES_PER_SYMBOL:
            return False, (
                f"[{label}] Max open trades for {symbol} reached "
                f"({open_symbol}/{MAX_OPEN_TRADES_PER_SYMBOL})"
            )

        # 2. Max total open trades
        open_total = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM {trades_table} WHERE status = 'OPEN'"
        ).fetchone()["cnt"]
        if open_total >= MAX_OPEN_TRADES_TOTAL:
            return False, (
                f"[{label}] Max total open trades reached ({open_total}/{MAX_OPEN_TRADES_TOTAL})"
            )

        # 3. Max trades per symbol per day
        day_count = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt FROM {trades_table}
            WHERE symbol = ? AND opened_at >= ?
            """,
            (symbol, today_start),
        ).fetchone()["cnt"]
        if day_count >= MAX_TRADES_PER_SYMBOL_PER_DAY:
            return False, (
                f"[{label}] Daily trade limit for {symbol} reached "
                f"({day_count}/{MAX_TRADES_PER_SYMBOL_PER_DAY})"
            )

        # 4. Daily loss cap
        # FIX #3: Only sum negative P&L rows.  Previously SUM(pnl_rupees) included
        # profitable trades, so a day with +50k profit and -45k losses showed net
        # +5k — the cap never fired even though real losses were 45k.
        today_loss_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(pnl_rupees), 0) AS total
            FROM {trades_table}
            WHERE closed_at >= ? AND pnl_rupees < 0
            """,
            (today_start,),
        ).fetchone()
        today_realized_loss = float(today_loss_row["total"] if today_loss_row else 0.0)
        if today_realized_loss < -abs(MAX_DAILY_LOSS_RUPEES):
            return False, (
                f"[{label}] Daily loss limit hit "
                f"(realized losses \u20b9{today_realized_loss:,.0f} / "
                f"limit -\u20b9{MAX_DAILY_LOSS_RUPEES:,.0f})"
            )

        # 5. Cooldown after SL/loss (per-symbol)
        last_loss = conn.execute(
            f"""
            SELECT closed_at FROM {trades_table}
            WHERE symbol = ? AND status IN ('SL_HIT', 'CLOSED') AND pnl_rupees < 0
              AND closed_at >= ?
            ORDER BY closed_at DESC
            LIMIT 1
            """,
            (symbol, today_start),
        ).fetchone()
        if last_loss:
            try:
                last_loss_dt = datetime.fromisoformat(
                    last_loss["closed_at"].replace("Z", "+00:00")
                )
                if last_loss_dt.tzinfo is None:
                    last_loss_dt = last_loss_dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_loss_dt).total_seconds() / 60
                if elapsed < LOSS_COOLDOWN_MINUTES:
                    remaining = int(LOSS_COOLDOWN_MINUTES - elapsed)
                    return False, (
                        f"[{label}] Loss cooldown active for {symbol} — "
                        f"{remaining} min remaining"
                    )
            except Exception as exc:
                log.warning(
                    "[%s] Could not parse last_loss closed_at for %s: %s",
                    label, symbol, exc,
                )

        # 6. Account-level consecutive-loss circuit breaker (FIX #11)
        ok, reason = _check_consecutive_loss_breaker(conn, trades_table, label)
        if not ok:
            return False, reason

    return True, "OK"


def check_risk_limits(symbol: str, setup_type: str | None = None) -> tuple[bool, str]:
    """
    Paper-trading risk check.  Queries paper_trades table.
    Return (allowed: bool, reason: str).

    setup_type is accepted for call-site compatibility but not used in checks.
    """
    return _check_risk_limits_for_table(symbol, "paper_trades", "paper")


def check_live_risk_limits(symbol: str) -> tuple[bool, str]:
    """
    Live-trading risk check.  Queries live_trades table.

    Full parity with check_risk_limits():
      - daily loss cap (losses only — FIX #3)
      - loss cooldown
      - max trades per symbol per day
      - max open per symbol
      - max total open
      - account-level consecutive-loss circuit breaker (FIX #11)

    Return (allowed: bool, reason: str).
    """
    return _check_risk_limits_for_table(symbol, "live_trades", "live")
