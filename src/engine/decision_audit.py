import logging
import json
from datetime import datetime, timezone
from dataclasses import asdict
from typing import Any

from src.models.schema import get_conn
from config.settings import DECISION_AUDIT_ENABLED, DECISION_AUDIT_RETENTION_DAYS
from src.engine.decision_pipeline import PipelineContext

log = logging.getLogger(__name__)


def log_decision(ctx: PipelineContext, action: str, trade_id: int | None = None) -> int | None:
    """
    Log a decision to the SQLite database (TRADE or SKIP).
    Returns the row ID or None on failure.
    """
    if not DECISION_AUDIT_ENABLED:
        return None

    try:
        # Extract individual steps to fill flat columns
        signal_score = None
        rule_passed = None
        ai_score = None
        ai_agrees = None
        entry_quality = None
        trend_score = None
        regime_score = None
        risk_passed = None
        risk_sub_check = None

        for step in ctx.steps:
            if step.name == "signal":
                signal_score = step.score if step.passed else None
            elif step.name == "rule":
                rule_passed = 1 if step.passed else 0
            elif step.name == "ai":
                ai_score = step.score
                ai_agrees = 1 if step.data.get("ai_agrees") else 0
            elif step.name == "entry_quality":
                entry_quality = step.score
            elif step.name == "regime":
                regime_score = step.score
            elif step.name == "trend":
                trend_score = step.score
            elif step.name == "risk":
                risk_passed = 1 if step.passed else 0
                risk_sub_check = step.data.get("sub_check")

        # Serialise full trail to JSON for debugging
        trail_json = json.dumps([asdict(s) for s in ctx.steps])
        timestamp = datetime.now(timezone.utc).isoformat()

        # Extract underlying and target timestamps
        scan_fetched_at = ctx.scan_context.get("fetched_at")
        bar_end_utc = ctx.scan_context.get("chart_indicators", {}).get("1h", {}).get("bar_end_utc")

        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO decision_audit (
                    timestamp, engine, symbol, direction, action,
                    signal_score, rule_passed, ai_score, ai_agrees,
                    entry_quality, trend_score, regime_score, risk_passed,
                    risk_sub_check, block_step, block_reason, trail_json,
                    trade_id, bar_end_utc, scan_fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    ctx.engine,
                    ctx.symbol,
                    ctx.direction,
                    action,
                    signal_score,
                    rule_passed,
                    ai_score,
                    ai_agrees,
                    entry_quality,
                    trend_score,
                    regime_score,
                    risk_passed,
                    risk_sub_check,
                    ctx.block_step,
                    ctx.block_reason,
                    trail_json,
                    trade_id,
                    bar_end_utc,
                    scan_fetched_at,
                ),
            )
            row_id = cur.lastrowid
            log.debug(
                "[audit] Logged decision %s for %s %s (row=%s)",
                action, ctx.symbol, ctx.engine, row_id
            )
            return row_id

    except Exception as e:
        log.exception("[audit] Failed to log decision to database")
        return None


def cleanup_old_decisions(days: int | None = None) -> int:
    """
    Remove decision audit rows older than retention settings.
    Returns count of rows deleted.
    """
    retention_days = days if days is not None else DECISION_AUDIT_RETENTION_DAYS
    import datetime as dt_mod
    cutoff = (datetime.now(timezone.utc) - dt_mod.timedelta(days=retention_days)).isoformat()
    try:
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM decision_audit WHERE timestamp < ?", (cutoff,))
            deleted = cur.rowcount
            if deleted > 0:
                log.info("[audit] Cleaned up %d decisions older than %s days", deleted, retention_days)
            return deleted
    except Exception as e:
        log.warning("[audit] Failed to cleanup old decisions: %s", e)
        return 0


def update_decision_audit(
    audit_row_id: int | None,
    action: str,
    trade_id: int | None = None,
    block_step: str | None = None,
    block_reason: str | None = None
) -> None:
    """
    Update an existing decision audit row with execution outcome (e.g. trade_id or skip reason).
    """
    if not DECISION_AUDIT_ENABLED or not audit_row_id:
        return

    try:
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE decision_audit
                SET action = ?,
                    trade_id = ?,
                    block_step = COALESCE(?, block_step),
                    block_reason = COALESCE(?, block_reason)
                WHERE id = ?
                """,
                (action, trade_id, block_step, block_reason, audit_row_id)
            )
            log.debug(
                "[audit] Updated decision ID %d: action=%s, trade_id=%s, block=%s",
                audit_row_id, action, trade_id, block_step
            )
    except Exception as e:
        log.warning("[audit] Failed to update decision audit record: %s", e)

