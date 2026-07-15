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

IST_OFFSET = timedelta(hours=5, minutes=30)
IST = pytz.timezone("Asia/Kolkata")

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
    now_ist = datetime.now(IST)
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_ist.astimezone(timezone.utc)
    return midnight_utc.isoformat()


def _check_consecutive_loss_breaker(conn, trades_table: str, label: str) -> tuple[bool, str]:
    """
    FIX #11: Account-level consecutive-loss circuit breaker.
    Counts losing trades closed across ALL symbols in the last
    CONSECUTIVE_LOSS_WINDOW_MINUTES.  If >= CONSECUTIVE_LOSS_LIMIT, blocks
    all new entries until the rolling window moves past those losses.
    """
    # P2-9: SQL injection guard — f-string table names must be allowlisted
    assert trades_table in ("paper_trades", "live_trades"), f"Unexpected table: {trades_table}"
    window_start = (
        datetime.now(timezone.utc) - timedelta(minutes=CONSECUTIVE_LOSS_WINDOW_MINUTES)
    ).isoformat()

    recent_losses = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM {trades_table}
        WHERE pnl_rupees < 0
          AND closed_at >= ?
          AND status IN ('CLOSED_SL', 'CLOSED_MANUAL', 'CLOSED', 'SL_HIT', 'CLOSED_REVERSAL', 'CLOSED_TF_EXIT')
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
    setup_type: str | None = None
) -> tuple[bool, str, str]:
    """
    Core risk-check logic, parameterised over the trades table name.
    Used by both check_risk_limits (paper) and check_live_risk_limits (live).
    Returns (allowed, reason, sub_check_code).
    """
    # P2-9: SQL injection guard — f-string table names must be allowlisted
    assert trades_table in ("paper_trades", "live_trades"), f"Unexpected table: {trades_table}"

    today_start = _ist_day_start_utc()

    with get_conn() as conn:
        # Hook for NATURALGAS specific risk limits (position limit and daily loss cap) (XBUG-002)
        if symbol == "NATURALGAS":
            from src.engine.ng_risk_manager import check_ng_position_limit, check_ng_daily_loss_cap
            if not check_ng_position_limit(trades_table):
                return False, f"[{label}] NATURALGAS position limit reached.", "NG_POSITION_LIMIT"
            if check_ng_daily_loss_cap(trades_table):
                return False, f"[{label}] NATURALGAS daily loss cap (2 consecutive SL) hit today.", "NG_DAILY_LOSS_CAP"

        # 1. Max open trades per symbol
        open_symbol = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM {trades_table} WHERE symbol = ? AND status = 'OPEN'",
            (symbol,),
        ).fetchone()["cnt"]
        if open_symbol >= MAX_OPEN_TRADES_PER_SYMBOL:
            return False, (
                f"[{label}] Max open trades for {symbol} reached "
                f"({open_symbol}/{MAX_OPEN_TRADES_PER_SYMBOL})"
            ), "MAX_OPEN_TRADES_PER_SYMBOL"

        # 2. Max total open trades
        open_total = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM {trades_table} WHERE status = 'OPEN'"
        ).fetchone()["cnt"]
        if open_total >= MAX_OPEN_TRADES_TOTAL:
            return False, (
                f"[{label}] Max total open trades reached ({open_total}/{MAX_OPEN_TRADES_TOTAL})"
            ), "MAX_OPEN_TRADES_TOTAL"

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
            ), "MAX_TRADES_PER_SYMBOL_PER_DAY"

        # 4. Daily loss cap
        # BUG-M4 FIX: Use parameterized timestamps only instead of mixing parameterized
        # and CURRENT_TIMESTAMP. This prevents timezone offset mismatches.
        now_utc = datetime.now(timezone.utc).isoformat()
        today_loss_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(pnl_rupees), 0) AS total
            FROM {trades_table}
            WHERE closed_at >= ? AND closed_at <= ? AND pnl_rupees < 0
            """,
            (today_start, now_utc),
        ).fetchone()
        today_realized_loss = float(today_loss_row["total"] if today_loss_row else 0.0)
        if today_realized_loss < -abs(MAX_DAILY_LOSS_RUPEES):
            return False, (
                f"[{label}] Daily loss limit hit "
                f"(realized losses \u20b9{today_realized_loss:,.0f} / "
                f"limit -\u20b9{MAX_DAILY_LOSS_RUPEES:,.0f})"
            ), "DAILY_LOSS_CAP"

        # 5. Cooldown after SL/loss (per-symbol)
        cooldown_start = (
            datetime.now(timezone.utc) - timedelta(minutes=LOSS_COOLDOWN_MINUTES)
        ).isoformat()
        last_loss = conn.execute(
            f"""
            SELECT closed_at FROM {trades_table}
            WHERE symbol = ? AND status IN ('CLOSED_SL', 'CLOSED_MANUAL', 'CLOSED', 'SL_HIT', 'CLOSED_REVERSAL', 'CLOSED_TF_EXIT') AND pnl_rupees < 0
              AND closed_at >= ?
            ORDER BY closed_at DESC
            LIMIT 1
            """,
            (symbol, cooldown_start),
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
                    ), "LOSS_COOLDOWN"
            except Exception as exc:
                log.warning(
                    "[%s] Could not parse last_loss closed_at for %s: %s",
                    label, symbol, exc,
                )

        # 6. Account-level consecutive-loss circuit breaker (FIX #11)
        ok, reason = _check_consecutive_loss_breaker(conn, trades_table, label)
        if not ok:
            return False, reason, "CIRCUIT_BREAKER"
            
        # 7. TFSS specific checks (plan §3.5 — additive, does not disturb other checks)
        if setup_type and "TFSS" in setup_type:
            # Enforce max TFSS tranches per symbol (3 tranches max from config)
            from config.trend_following_short_strangle import TRANCHE_SEQUENCE
            max_tranches = len(TRANCHE_SEQUENCE)
            tfss_open = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt FROM {trades_table}
                WHERE symbol = ? AND status = 'OPEN'
                  AND signal_key LIKE '%CORE_TFSS%'
                """,
                (symbol,),
            ).fetchone()["cnt"]
            if tfss_open >= max_tranches:
                return False, (
                    f"[{label}] TFSS max tranches for {symbol} reached "
                    f"({tfss_open}/{max_tranches})"
                ), "TFSS_MAX_TRANCHES"

    return True, "OK", "OK"


def check_risk_limits(symbol: str, setup_type: str | None = None) -> tuple[bool, str]:
    """
    Paper-trading risk check.  Queries paper_trades table.
    Return (allowed: bool, reason: str).
    """
    allowed, reason, _ = _check_risk_limits_for_table(symbol, "paper_trades", "paper", setup_type=setup_type)
    return allowed, reason


def check_live_risk_limits(symbol: str, setup_type: str | None = None) -> tuple[bool, str]:
    """
    Live-trading risk check.  Queries live_trades table.
    """
    allowed, reason, _ = _check_risk_limits_for_table(symbol, "live_trades", "live", setup_type=setup_type)
    return allowed, reason

# --- TFSS Risk Helpers ---

class TestedSideStatus:
    def __init__(self, beyond_threshold: bool = False, current_delta: float = 0.0,
                 max_delta: float = 0.0, reason: str = ""):
        self.beyond_threshold = beyond_threshold
        self.current_delta = current_delta
        self.max_delta = max_delta
        self.reason = reason

class CombinedBookStatus:
    def __init__(self, within_caps: bool = True, total_delta: float = 0.0,
                 max_total_delta: float = 0.0, open_count: int = 0, reason: str = ""):
        self.within_caps = within_caps
        self.total_delta = total_delta
        self.max_total_delta = max_total_delta
        self.open_count = open_count
        self.reason = reason

# TFSS risk caps (plan §3.5 — additive to existing engine)
_TFSS_MAX_TOTAL_DELTA = 0.60     # max combined delta across open TFSS legs
_TFSS_MAX_OPEN_POSITIONS = 3     # max concurrent TFSS positions per symbol
_HARD_STOP_DELTA = 0.35          # delta beyond which tested side must be reduced/closed

def check_tested_side(side: str, market_state: dict, config: dict) -> TestedSideStatus:
    """
    Evaluate if the tested side has breached its delta-stop threshold.
    Plan §4.6: when delta-stop and profit-decay are both true, delta-stop wins.
    
    Args:
        side: "SELL_PE" or "SELL_CE"
        market_state: dict with 'current_delta' (abs delta of the tested leg),
                      'underlying', 'entry_underlying' optional
        config: dict with optional 'hard_stop_delta' override
    """
    hard_stop = config.get("hard_stop_delta", _HARD_STOP_DELTA) if isinstance(config, dict) else _HARD_STOP_DELTA
    current_delta = 0.0

    if isinstance(market_state, dict):
        current_delta = abs(float(market_state.get("current_delta", 0.0)))
    elif hasattr(market_state, "current_delta"):
        current_delta = abs(float(getattr(market_state, "current_delta", 0.0)))

    beyond = current_delta >= hard_stop
    reason = ""
    if beyond:
        reason = f"DELTA_STOP: {side} delta {current_delta:.3f} >= threshold {hard_stop:.3f}"

    return TestedSideStatus(
        beyond_threshold=beyond,
        current_delta=current_delta,
        max_delta=hard_stop,
        reason=reason,
    )

def compute_combined_book(symbol_state: dict, market_state: dict) -> CombinedBookStatus:
    """
    Evaluate combined portfolio risk for a symbol's TFSS book.
    Checks: total open TFSS positions cap, combined delta cap.
    
    Args:
        symbol_state: dict with 'symbol', 'open_count' (int), optionally 'open_sides' (list)
        market_state: dict with 'total_delta' (combined abs delta), or individual leg deltas
    """
    open_count = 0
    total_delta = 0.0

    if isinstance(symbol_state, dict):
        open_count = int(symbol_state.get("open_count", 0))
    elif hasattr(symbol_state, "open_count"):
        open_count = int(getattr(symbol_state, "open_count", 0))

    if isinstance(market_state, dict):
        total_delta = abs(float(market_state.get("total_delta", 0.0)))
    elif hasattr(market_state, "total_delta"):
        total_delta = abs(float(getattr(market_state, "total_delta", 0.0)))

    within = True
    reason = ""

    try:
        from config.runtime_config import load_runtime_config
        ENABLE_TFSS_TRADE_BLOCKED_RULES = load_runtime_config().get("enable_tfss_trade_blocked_rules", False)
    except Exception:
        from config.trend_following_short_strangle import ENABLE_TFSS_TRADE_BLOCKED_RULES
    if ENABLE_TFSS_TRADE_BLOCKED_RULES:
        if open_count >= _TFSS_MAX_OPEN_POSITIONS:
            within = False
            reason = f"TFSS_OPEN_CAP: {open_count} >= {_TFSS_MAX_OPEN_POSITIONS}"
        elif total_delta >= _TFSS_MAX_TOTAL_DELTA:
            within = False
            reason = f"TFSS_DELTA_CAP: total delta {total_delta:.3f} >= {_TFSS_MAX_TOTAL_DELTA:.3f}"

    return CombinedBookStatus(
        within_caps=within,
        total_delta=total_delta,
        max_total_delta=_TFSS_MAX_TOTAL_DELTA,
        open_count=open_count,
        reason=reason,
    )

def exit_trigger_priority_list(active_triggers: list[str]) -> str | None:
    """
    Determine the winning exit trigger when multiple triggers are simultaneously active.
    Example active_triggers: ["PROFIT_TARGET", "DELTA_STOP"]
    """
    if not active_triggers:
        return None
        
    from config.trend_following_short_strangle import EXIT_PRIORITY_MAP
    
    # Sort active triggers by priority. Lower number = higher priority.
    # Triggers not in the map get a default low priority (e.g., 99).
    sorted_triggers = sorted(
        active_triggers, 
        key=lambda t: EXIT_PRIORITY_MAP.get(t, 99)
    )
    return sorted_triggers[0]

