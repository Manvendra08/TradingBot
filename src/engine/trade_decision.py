"""
Trade Decision Engine — combines all layers into final trade decision.
B5 fix: NO_TRADE regime tags EXPERIMENTAL instead of hard-blocking in research mode.

Phase 4: Full hybrid trend-based trading logic integration.

P2 fix (#1): broader_trend computed once per cycle and passed down.
P2 fix (#6): TREND_MIN_SCANS gate added.
P2 fix (#7): MOMENTUM_SCORE_THRESHOLD from settings.

Autopsy fix #6: AI advisory mode default veto guard changed True → False.
Autopsy fix #7: Hybrid mode reversal (Priority 1) now requires
  confidence >= REVERSAL_MIN_CONFIDENCE before firing. Lower-confidence
  reversal signals fall through to persistence/momentum paths, preventing
  false top/bottom calls from overriding strong established trends.
Autopsy fix #8: PAPER_RESEARCH_MODE consistently bypasses BOTH scan-count
  gate AND regime gate. Previously scan gate was enforced while regime was
  silently overridden — misleading test/prod parity. Both bypasses are now
  explicit and tagged with soft_conflict entries.
"""
from __future__ import annotations

import logging

from src.engine.entry_quality import calculate_entry_quality
from src.engine.regime_detector import detect_market_regime, regime_score_for_trade, REGIME_NO_TRADE
from src.engine.trend_analysis import (
    detect_reversal_from_scans,
    get_trend_alignment_score,
    check_trend_persistence,
    calculate_momentum_score,
    get_broader_trend_from_alerts,
)
from src.engine.time_guards import is_trading_allowed_now
from src.engine.verdict_sets import is_bullish, is_bearish

# Map LLM action-oriented schema → legacy bias for trade decision engine
_ACTION_TO_BIAS = {
    "GO_LONG": "BULLISH",
    "GO_SHORT": "BEARISH",
    "NO_TRADE": "NEUTRAL",
}


def _extract_ai_bias(ai_verdict) -> str | None:
    """Extract bias from AI verdict, supporting both new (action) and old (bias) schemas."""
    if ai_verdict is None:
        return None
    # New schema: action field (GO_LONG/GO_SHORT/NO_TRADE)
    action = getattr(ai_verdict, 'action', None) or (ai_verdict.get('action') if isinstance(ai_verdict, dict) else None)
    if action and action in _ACTION_TO_BIAS:
        return _ACTION_TO_BIAS[action]
    # Legacy schema: bias field (BULLISH/BEARISH/NEUTRAL)
    bias = getattr(ai_verdict, 'bias', None) or (ai_verdict.get('bias') if isinstance(ai_verdict, dict) else None)
    return bias


def _extract_ai_veto_flag(ai_verdict) -> bool:
    """Extract veto_flag boolean from AI verdict across dict/dataclass schemas."""
    if not ai_verdict:
        return False
    flag = getattr(ai_verdict, 'veto_flag', None)
    if flag is None and isinstance(ai_verdict, dict):
        flag = ai_verdict.get('veto_flag')
    return bool(flag)


def _extract_ai_veto_reason(ai_verdict) -> str:
    """Extract veto_reason string from AI verdict across dict/dataclass schemas."""
    if not ai_verdict:
        return ""
    reason = getattr(ai_verdict, 'veto_reason', None)
    if reason is None and isinstance(ai_verdict, dict):
        reason = ai_verdict.get('veto_reason')
    return str(reason or "")


from config.settings import (
    PAPER_RESEARCH_MODE,
    MIN_CONFIDENCE_CORE,
    MIN_ENTRY_QUALITY_CORE,
    MIN_TREND_ALIGNMENT_CORE,
    MIN_REGIME_SCORE_CORE,
    MIN_CONFIDENCE_EXPERIMENTAL,
    MIN_ENTRY_QUALITY_EXPERIMENTAL,
    REVERSAL_MIN_CONFIDENCE,
    TREND_FILTER_MODE,
    MOMENTUM_SCORE_THRESHOLD,
    TREND_MIN_SCANS,
    AI_DECISION_MODE,
    AI_MIN_CONFIDENCE_BOOST,
    AI_MIN_CONFIDENCE_VETO,
    MCX_MIN_CONFIDENCE,
    MCX_SYMBOLS,
)

log = logging.getLogger(__name__)


def _count_valid_scans(symbol: str) -> int:
    """Return count of non-fallback scan summaries for symbol."""
    from src.models.schema import get_conn
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM scan_summaries
            WHERE symbol = ?
              AND (is_fallback IS NULL OR is_fallback = 0)
            """,
            (symbol,),
        ).fetchone()
    return int(row[0]) if row else 0


def make_trade_decision(symbol: str, intel: dict, ctx: dict, ai_verdict=None, suppress_logs: bool = False) -> dict:
    """
    Combine all layers → TRIGGERED_CORE / TRIGGERED_EXPERIMENTAL / BLOCKED.
    Refactored in Phase 1 to route through the unified Decision Audit Pipeline.
    """
    from src.engine.decision_pipeline import PipelineContext, run_entry_pipeline
    from src.engine.decision_audit import log_decision

    underlying = float(ctx.get("underlying") or 0.0)

    # Initialise pipeline context
    pipeline_ctx = PipelineContext(
        engine="CORE_OI",
        symbol=symbol,
        direction=None,
        underlying=underlying,
        scan_context={**ctx, "intel": intel},
        ai_verdict=ai_verdict,
        steps=[]
    )

    # Run pipeline
    run_entry_pipeline(pipeline_ctx)

    # Determine final status and action
    status = "BLOCKED"
    setup_type = None
    reason = pipeline_ctx.block_reason
    soft_conflicts = []
    scores = {}

    if pipeline_ctx.passed:
        setup_type = pipeline_ctx.scan_context.get("_setup_type", "UNKNOWN")
        if setup_type in ("EXPERIMENTAL_SETUP", "AI_PROMOTED", "EMPIRICAL_PROMOTED"):
            status = "TRIGGERED_EXPERIMENTAL"
        else:
            status = "TRIGGERED_CORE"
        reason = pipeline_ctx.scan_context.get("_decision_reason", "Signal filters passed")
        soft_conflicts = pipeline_ctx.scan_context.get("_soft_conflicts") or []
        scores = pipeline_ctx.scan_context.get("_scores") or {}
    else:
        # Assemble best effort scores from steps
        scores = pipeline_ctx.scan_context.get("_scores") or {}

    # Log to decision_audit SQLite table if not suppressed
    audit_row_id = None
    if not suppress_logs:
        audit_row_id = log_decision(pipeline_ctx, action="TRADE" if pipeline_ctx.passed else "SKIP")
        log.info("Trade decision: %s | %s | %s", status, setup_type, reason)
    else:
        log.debug("Dry-run/preview decision: %s | %s | %s", status, setup_type, reason)

    plan = pipeline_ctx.scan_context.get("_pipeline_plan") or {}
    
    return {
        "status": status,
        "setup_type": setup_type,
        "reason": reason,
        "soft_conflicts": soft_conflicts,
        "scores": scores,
        "audit_row_id": audit_row_id,
        # TFSS specific extensions
        "execution_source": pipeline_ctx.scan_context.get("_execution_source", "TIMEFRAME" if pipeline_ctx.engine == "TIMEFRAME" else "CORE_TFSS" if pipeline_ctx.scan_context.get("_tfss_bias") else "CORE"),
        "core_verdict_family": intel.get("verdict_label", ""),
        "normalized_tfss_bias": pipeline_ctx.scan_context.get("_tfss_bias"),
        "action": plan.get("side"),
        "symbol": symbol,
        "option_side": plan.get("option_type"),
        "strike": plan.get("strike"),
        "delta": pipeline_ctx.scan_context.get("_candidate_delta"),
        "premium": plan.get("premium"),
        "risk_metrics": pipeline_ctx.scan_context.get("_risk_metrics"),
        "eligible_triggers": pipeline_ctx.scan_context.get("_eligible_triggers", []),
        "also_eligible_triggers": pipeline_ctx.scan_context.get("_also_eligible_triggers", []),
        "tested_side_status": pipeline_ctx.scan_context.get("_tested_side_status"),
        "combined_book_status": pipeline_ctx.scan_context.get("_combined_book_status"),
        "tranche_index": pipeline_ctx.scan_context.get("_tranche_index", 0),
    }


# ── Legacy Helpers ──────────────────────────────────────────────────────────

def _decision_global(status: str, setup_type: str, reason: str,
                     soft_conflicts: list[str], scores: dict,
                     ai_verdict=None, suppress_logs: bool = False) -> dict:
    # Retained for backward-compatibility if other legacy modules call it directly
    return {
        "status":         status,
        "setup_type":     setup_type,
        "reason":         reason,
        "soft_conflicts": soft_conflicts,
        "scores":         scores,
    }


def _blocked(reason: str) -> dict:
    log.debug("Trade blocked: %s", reason)
    return {
        "status":         "BLOCKED",
        "setup_type":     None,
        "reason":         reason,
        "soft_conflicts": [],
        "scores":         {},
    }

