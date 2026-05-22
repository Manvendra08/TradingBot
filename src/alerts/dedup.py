"""
Alert deduplication v2.7.
- Severity-aware cooldown: HIGH = 30 min, others = 60 min.
- Strike-cluster collapsing: same symbol+alert_type, strike within
  ± (DEDUP_CLUSTER_STRIKES × symbol_strike_step) of a recently
  fired key → suppress for 30 min.
  Uses symbol_classes.get_strike_step() for symbol-aware spacing
  instead of a hardcoded × 100 multiplier.
"""
import logging
from datetime import datetime, timezone, timedelta
from src.models.schema import get_conn
from config.settings import (
    ALERT_COOLDOWN_MINUTES,
    ALERT_COOLDOWN_HIGH_MINUTES,
    DEDUP_CLUSTER_STRIKES,
)
from config.symbol_classes import get_strike_step

log = logging.getLogger(__name__)

# Zero-signal scan: send at most once per this many minutes
_ZERO_SIGNAL_COOLDOWN_MINUTES = 30
_zero_signal_last: dict[str, datetime] = {}  # symbol -> last sent time


def should_send_zero_signal(symbol: str) -> bool:
    """Rate-limit zero-signal Telegram sends to once per 30 min per symbol."""
    now = datetime.now(timezone.utc)
    last = _zero_signal_last.get(symbol)
    if last and (now - last).seconds < _ZERO_SIGNAL_COOLDOWN_MINUTES * 60:
        return False
    _zero_signal_last[symbol] = now
    return True


def _cooldown(severity: str) -> int:
    return ALERT_COOLDOWN_HIGH_MINUTES if severity == "HIGH" else ALERT_COOLDOWN_MINUTES


def _dedup_key(alert: dict) -> str:
    return "|".join([
        alert["symbol"],
        alert["alert_type"],
        str(alert.get("strike") or ""),
        str(alert.get("option_type") or ""),
    ])


def _is_strike_cluster_suppressed(alert: dict) -> bool:
    """Check if a nearby strike for same symbol+alert_type fired recently."""
    strike = alert.get("strike")
    if not strike:
        return False
    sym    = alert["symbol"]
    atype  = alert["alert_type"]
    ot     = alert.get("option_type") or ""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ALERT_COOLDOWN_HIGH_MINUTES)

    # Symbol-class-aware cluster width
    # e.g. NIFTY: 2 × 50 = 100pts (2 strikes), BANKNIFTY: 2 × 100 = 200pts (2 strikes)
    cluster_width = DEDUP_CLUSTER_STRIKES * get_strike_step(sym)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT dedup_key, last_fired_at FROM alert_dedup WHERE dedup_key LIKE ?",
            (f"{sym}|{atype}|%|{ot}",),
        ).fetchall()

    for row in rows:
        parts = row["dedup_key"].split("|")
        if len(parts) < 3:
            continue
        try:
            prev_strike = float(parts[2])
        except (ValueError, IndexError):
            continue
        if abs(prev_strike - strike) > cluster_width:
            continue
        try:
            last = datetime.fromisoformat(row["last_fired_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last >= cutoff:
                return True
        except Exception:
            pass
    return False


def is_duplicate(alert: dict) -> bool:
    key      = _dedup_key(alert)
    severity = alert.get("severity", "LOW")
    minutes  = _cooldown(severity)
    cutoff   = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_fired_at FROM alert_dedup WHERE dedup_key=?", (key,)
        ).fetchone()

    if row:
        try:
            last = datetime.fromisoformat(row["last_fired_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last >= cutoff:
                return True
        except Exception:
            pass

    return _is_strike_cluster_suppressed(alert)


def record_alert(alert: dict) -> None:
    key      = _dedup_key(alert)
    severity = alert.get("severity", "LOW")
    now_iso  = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO alert_dedup (dedup_key, last_fired_at, severity)
            VALUES (?, ?, ?)
            ON CONFLICT(dedup_key) DO UPDATE SET
                last_fired_at = excluded.last_fired_at,
                severity      = excluded.severity
            """,
            (key, now_iso, severity),
        )
    log.debug("Dedup recorded: %s [%s]", key, severity)
