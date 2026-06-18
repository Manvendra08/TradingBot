"""
Trade Decision Engine — combines all layers into final trade decision.
B5 fix: NO_TRADE regime tags EXPERIMENTAL instead of hard-blocking in research mode.

Phase 4: Full hybrid trend-based trading logic integration.

P2 fix (#1): broader_trend computed once per cycle and passed down.
P2 fix (#6): TREND_MIN_SCANS gate added — blocks any trend-based trade when
  the symbol has fewer than TREND_MIN_SCANS non-fallback scan summaries.
  Prevents new symbols from firing TRIGGERED_CORE with zero trend validation.
P2 fix (#7): Hybrid mode momentum fallback threshold changed from hardcoded 80
  to settings.MOMENTUM_SCORE_THRESHOLD so it is tunable without a code change.

Autopsy fix 2: AI veto guard default changed from True to False.
  Previously `scores.get('ai_agrees', True)` meant veto could never fire when
  ai_verdict was None (key absent from scores). Now defaults False so the
  guard evaluates correctly: missing AI verdict → no veto (ai_agrees=False
  with ai_conf=0 will not meet the ai_min_confidence_veto threshold).
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
from src.engine.verdict_sets import is_bullish, is_bearish
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


def make_trade_decision(symbol: str, intel: dict, ctx: dict, ai_verdict=None) -> dict:
    """
    Combine all layers → TRIGGERED_CORE / TRIGGERED_EXPERIMENTAL / BLOCKED.

    Phase 4: Implements full hybrid trend-based trading logic with mode switching.

    Returns:
        {
            "status": str,
            "setup_type": str | None,
            "reason": str,
            "soft_conflicts": list[str],
            "scores": dict,
        }
    """
    from config.runtime_config import load_runtime_config
    rconf = load_runtime_config()
    ai_decision_mode = rconf.get("live_ai_decision_mode", "advisory")
    ai_min_confidence_boost = int(rconf.get("live_ai_min_confidence_boost", 80))

    verdict    = intel.get("verdict_label", "")
    confidence = int(intel.get("confidence") or 0)
    soft_conflicts: list[str] = []

    # ── Hard blocks ────────────────────────────────────────────────────────
    if float(ctx.get("underlying") or 0) <= 0:
        return _blocked("Missing underlying price")

    if not is_bullish(verdict) and not is_bearish(verdict):
        return _blocked(f"Verdict '{verdict}' is not directional")

    if intel.get("chart_conflict"):
        return _blocked("Timeframe conflict: 1H and 3H charts disagree")

    # ── Minimum scan history gate (#6) ─────────────────────────────────────
    # Blocks trade on any symbol that has fewer than TREND_MIN_SCANS valid
    # scan summaries. Without this, get_trend_alignment_score() returns a
    # neutral 50 and calculate_momentum_score() component 2 uses 0 rows —
    # both silently allow a TRIGGERED_CORE with zero trend validation.
    scan_count = _count_valid_scans(symbol)
    if scan_count < TREND_MIN_SCANS:
        return _blocked(
            f"Insufficient scan history: {scan_count} scans (need {TREND_MIN_SCANS})"
        )

    # Build plan to get option_type + strike for entry quality
    from src.engine.paper_plan import build_paper_trade_plan
    plan_ctx = {**ctx, "symbol": symbol}   # ensure symbol is in ctx for paper_plan
    plan = build_paper_trade_plan(verdict, confidence, plan_ctx)
    if not plan:
        return _blocked("No valid trade plan from verdict")

    option_type = plan["option_type"]
    strike      = plan["strike"]
    plan_ctx    = {**plan_ctx, **plan}   # merge so entry_quality sees sl/target (B6)

    # ── Score all layers ───────────────────────────────────────────────────
    entry_quality, entry_reasons = calculate_entry_quality(symbol, option_type, strike, plan_ctx)
    trend_alignment = get_trend_alignment_score(symbol, verdict)

    regime = detect_market_regime(symbol)
    if regime == REGIME_NO_TRADE:
        if PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE:
            regime_sc = 50
            soft_conflicts.append("INSUFFICIENT_REGIME_HISTORY")
        else:
            return _blocked("Insufficient scan history for regime detection")
    else:
        regime_sc = regime_score_for_trade(regime, option_type)

    scores = {
        "confidence":      confidence,
        "entry_quality":   entry_quality,
        "trend_alignment": trend_alignment,
        "regime_score":    regime_sc,
    }

    # ── AI verdict influence (Phase 2) ────────────────────────────────────
    if ai_verdict:
        ai_bias = getattr(ai_verdict, 'bias', None) or (ai_verdict.get('bias') if isinstance(ai_verdict, dict) else None)
        ai_conf = getattr(ai_verdict, 'confidence', 0) or (ai_verdict.get('confidence', 0) if isinstance(ai_verdict, dict) else 0)
        ai_risk = getattr(ai_verdict, 'risk_rating', '') or (ai_verdict.get('risk_rating', '') if isinstance(ai_verdict, dict) else '')
        verdict_bias = 'BULLISH' if is_bullish(verdict) else 'BEARISH'
        ai_agrees = (ai_bias == verdict_bias)
        scores['ai_confidence'] = ai_conf
        scores['ai_bias'] = ai_bias
        scores['ai_agrees'] = ai_agrees
        scores['ai_risk_rating'] = ai_risk
        log.info("%s: AI verdict — bias=%s conf=%d%% risk=%s agrees=%s (mode=%s)",
                 symbol, ai_bias, ai_conf, ai_risk, ai_agrees, AI_DECISION_MODE)

    # ── Pre-fetch broader trend once — shared by all mode branches ─────────
    broader_trend = get_broader_trend_from_alerts(symbol)

    # ── Apply Trend-Based Trading Logic Based on Mode ─────────────────────

    if TREND_FILTER_MODE == "conservative":
        is_persistent, persist_reason = check_trend_persistence(
            symbol, verdict, confidence, ctx, broader_trend=broader_trend
        )
        if not is_persistent:
            return _blocked(f"Conservative filter: {persist_reason}")

        if entry_quality >= MIN_ENTRY_QUALITY_CORE and regime_sc >= MIN_REGIME_SCORE_CORE:
            return _decision("TRIGGERED_CORE", "TREND_CONTINUATION",
                             f"Conservative: {persist_reason}", soft_conflicts, scores)
        else:
            return _blocked(f"Entry quality ({entry_quality}) or regime ({regime_sc}) insufficient")

    elif TREND_FILTER_MODE == "balanced":
        momentum_score = calculate_momentum_score(
            symbol, verdict, confidence, ctx, broader_trend=broader_trend
        )
        scores["momentum_score"] = momentum_score

        if momentum_score >= MOMENTUM_SCORE_THRESHOLD:
            if entry_quality >= MIN_ENTRY_QUALITY_CORE:
                return _decision("TRIGGERED_CORE", "MOMENTUM_TRADE",
                                 f"Momentum score={momentum_score}", soft_conflicts, scores)
            else:
                return _blocked(f"Momentum score high ({momentum_score}) but entry quality low ({entry_quality})")
        else:
            return _blocked(f"Momentum score too low ({momentum_score} < {MOMENTUM_SCORE_THRESHOLD})")

    elif TREND_FILTER_MODE == "aggressive":
        is_rev, rev_reason = detect_reversal_from_scans(symbol, verdict, confidence)
        if is_rev and entry_quality >= MIN_ENTRY_QUALITY_CORE:
            return _decision("TRIGGERED_CORE", "CONFIRMED_REVERSAL", rev_reason, soft_conflicts, scores)
        else:
            return _blocked(f"No reversal detected or poor entry quality: {rev_reason}")

    elif TREND_FILTER_MODE == "hybrid":
        # Priority 1: Reversal (high R:R)
        is_rev, rev_reason = detect_reversal_from_scans(symbol, verdict, confidence)
        if is_rev and entry_quality >= MIN_ENTRY_QUALITY_CORE:
            return _decision("TRIGGERED_CORE", "CONFIRMED_REVERSAL", rev_reason, soft_conflicts, scores)

        # Priority 2: Trend persistence
        is_persistent, persist_reason = check_trend_persistence(
            symbol, verdict, confidence, ctx, broader_trend=broader_trend
        )
        if is_persistent and entry_quality >= MIN_ENTRY_QUALITY_CORE and regime_sc >= MIN_REGIME_SCORE_CORE:
            return _decision("TRIGGERED_CORE", "TREND_CONTINUATION",
                             persist_reason, soft_conflicts, scores)

        # Priority 3: Momentum scoring — threshold from settings (#7)
        momentum_score = calculate_momentum_score(
            symbol, verdict, confidence, ctx, broader_trend=broader_trend
        )
        scores["momentum_score"] = momentum_score

        if momentum_score >= MOMENTUM_SCORE_THRESHOLD and entry_quality >= MIN_ENTRY_QUALITY_CORE:
            return _decision("TRIGGERED_CORE", "MOMENTUM_TRADE",
                             f"Momentum score={momentum_score}", soft_conflicts, scores)

        # Priority 4: Experimental (research mode only)
        if PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_EXPERIMENTAL and entry_quality >= MIN_ENTRY_QUALITY_EXPERIMENTAL:
            reason = (
                f"Marginal setup — conf={confidence} eq={entry_quality} "
                f"ta={trend_alignment} regime={regime} momentum={momentum_score}"
            )
            if entry_reasons:
                reason += f" | entry: {'; '.join(entry_reasons)}"
            return _decision("TRIGGERED_EXPERIMENTAL", "EXPERIMENTAL_SETUP", reason, soft_conflicts, scores)

        block_parts = []
        if not is_rev:
            block_parts.append(f"No reversal: {rev_reason}")
        if not is_persistent:
            block_parts.append(f"No persistence: {persist_reason}")
        if momentum_score < MOMENTUM_SCORE_THRESHOLD:
            block_parts.append(f"Low momentum ({momentum_score} < {MOMENTUM_SCORE_THRESHOLD})")
        if entry_quality < MIN_ENTRY_QUALITY_EXPERIMENTAL:
            block_parts.append(f"Poor entry quality ({entry_quality}/100): {'; '.join(entry_reasons)}")

        block_reason = "; ".join(block_parts) or "No qualifying condition met"

        # Priority 5: AI boost — if AI is confident and agrees, promote to EXPERIMENTAL
        if ai_verdict and ai_decision_mode in ("boost_only", "full"):
            ai_conf = scores.get('ai_confidence', 0)
            ai_agrees = scores.get('ai_agrees', False)
            if ai_agrees and ai_conf >= ai_min_confidence_boost:
                log.info("%s: AI BOOST — promoting BLOCKED to TRIGGERED_EXPERIMENTAL (AI conf=%d%%)",
                         symbol, ai_conf)
                soft_conflicts.append("AI_PROMOTED")
                return _decision(
                    "TRIGGERED_EXPERIMENTAL", "AI_PROMOTED",
                    f"AI boost: conf={ai_conf}% agrees with {verdict} | Rule blocked: {block_reason}",
                    soft_conflicts, scores,
                )

        return _blocked(block_reason)

    else:
        # Unknown mode — fall back to legacy logic
        log.warning("Unknown TREND_FILTER_MODE: %s, using legacy logic", TREND_FILTER_MODE)

        is_rev, rev_reason = detect_reversal_from_scans(symbol, verdict, confidence)
        if is_rev and entry_quality >= MIN_ENTRY_QUALITY_CORE:
            return _decision("TRIGGERED_CORE", "CONFIRMED_REVERSAL", rev_reason, soft_conflicts, scores)

        if (confidence      >= MIN_CONFIDENCE_CORE and
                trend_alignment >= MIN_TREND_ALIGNMENT_CORE and
                entry_quality   >= MIN_ENTRY_QUALITY_CORE and
                regime_sc       >= MIN_REGIME_SCORE_CORE):
            return _decision("TRIGGERED_CORE", "TREND_CONTINUATION",
                             "All filters passed", soft_conflicts, scores)

        if PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_EXPERIMENTAL and entry_quality >= MIN_ENTRY_QUALITY_EXPERIMENTAL:
            reason = (
                f"Marginal setup — conf={confidence} eq={entry_quality} "
                f"ta={trend_alignment} regime={regime}"
            )
            if entry_reasons:
                reason += f" | entry: {'; '.join(entry_reasons)}"
            return _decision("TRIGGERED_EXPERIMENTAL", "EXPERIMENTAL_SETUP", reason, soft_conflicts, scores)

        block_parts = []
        if confidence < MIN_CONFIDENCE_EXPERIMENTAL:
            block_parts.append(f"Low confidence ({confidence}%)")
        if entry_quality < MIN_ENTRY_QUALITY_EXPERIMENTAL:
            block_parts.append(f"Poor entry quality ({entry_quality}/100): {'; '.join(entry_reasons)}")
        if trend_alignment < MIN_TREND_ALIGNMENT_CORE:
            block_parts.append(f"Trend not aligned ({trend_alignment}/100)")
        if regime_sc < MIN_REGIME_SCORE_CORE:
            block_parts.append(f"Unfavorable regime ({regime})")
        return _blocked("; ".join(block_parts) or "No qualifying condition met")


# ── Helpers ────────────────────────────────────────────────────────────────

def _decision(status: str, setup_type: str, reason: str,
              soft_conflicts: list[str], scores: dict,
              ai_verdict=None) -> dict:
    # Autopsy fix 2: AI veto guard default changed from True → False.
    # The old default `scores.get('ai_agrees', True)` meant the veto condition
    # `not ai_agrees` was always False when ai_verdict was None (key absent),
    # making the veto dead code in 'full' mode for non-AI-enhanced calls.
    # New default False: when ai_agrees is absent, `not False = True` but
    # ai_conf will be 0 which cannot meet ai_min_confidence_veto (≥85),
    # so veto still does not fire — but the path is now structurally correct
    # and will fire correctly when ai_agrees IS populated as False.
    from config.runtime_config import load_runtime_config
    rconf = load_runtime_config()
    ai_decision_mode = rconf.get("live_ai_decision_mode", "advisory")
    ai_min_confidence_veto = int(rconf.get("live_ai_min_confidence_veto", 85))

    if (("ai_bias" in scores or ai_verdict) and ai_decision_mode == "full"
            and status.startswith("TRIGGERED")):
        ai_conf = scores.get('ai_confidence', 0)
        ai_agrees = scores.get('ai_agrees', False)  # FIX: was True — veto never fired when key absent
        ai_risk = scores.get('ai_risk_rating', '')
        if not ai_agrees and ai_conf >= ai_min_confidence_veto:
            log.warning(
                "Trade VETOED by AI: %s | AI bias disagrees (conf=%d%%, risk=%s)",
                status, ai_conf, ai_risk,
            )
            return {
                "status":        "BLOCKED",
                "setup_type":    None,
                "reason":        f"AI VETO: conf={ai_conf}% disagrees, risk={ai_risk} | was {status}: {reason}",
                "soft_conflicts": soft_conflicts + ["AI_VETOED"],
                "scores":        scores,
            }
    log.info("Trade decision: %s | %s | %s", status, setup_type, reason)
    return {
        "status":        status,
        "setup_type":    setup_type,
        "reason":        reason,
        "soft_conflicts": soft_conflicts,
        "scores":        scores,
    }


def _blocked(reason: str) -> dict:
    log.debug("Trade blocked: %s", reason)
    return {
        "status":        "BLOCKED",
        "setup_type":    None,
        "reason":        reason,
        "soft_conflicts": [],
        "scores":        {},
    }
