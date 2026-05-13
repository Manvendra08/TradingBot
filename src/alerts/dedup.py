"""
Alert deduplication v2.6.
- Severity-aware cooldown: HIGH = 30 min, others = 60 min.
- Strike-cluster collapsing: same symbol+alert_type, strike within ±DEDUP_CLUSTER_STRIKES
  of a recently fired key → suppress for 30 min.
"""
import logging
from datetime import datetime, timezone, timedelta
from src.models.schema import get_conn
from config.settings import (
    ALERT_COOLDOWN_MINUTES,
    ALERT_COOLDOWN_HIGH_MINUTES,
    DEDUP_CLUSTER_STRIKES,
)

log = logging.getLogger(__name__)


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
        if abs(prev_strike - strike) > DEDUP_CLUSTER_STRIKES * 100:
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
