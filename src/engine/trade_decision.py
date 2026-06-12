"""
Trade Decision Engine — combines all layers into final trade decision.
B5 fix: NO_TRADE regime tags EXPERIMENTAL instead of hard-blocking in research mode.

Phase 4: Full hybrid trend-based trading logic integration.

P2 fix (#1): broader_trend is computed once at the top of make_trade_decision()
  and passed into check_trend_persistence() and calculate_momentum_score() via
  the new `broader_trend` kwarg. Eliminates 2-3 redundant DB round trips per
  scan cycle in hybrid/conservative/balanced modes.
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
)

log = logging.getLogger(__name__)


def make_trade_decision(symbol: str, intel: dict, ctx: dict) -> dict:
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

    # ── Pre-fetch broader trend once — shared by all mode branches ─────────
    # Avoids 2-3 redundant get_recent_alerts_for_symbol() DB calls in hybrid
    # mode where check_trend_persistence and calculate_momentum_score both
    # need it. Passed via the broader_trend kwarg added in P2 fix #1.
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
        
        # Priority 2: Trend persistence (safe, high win rate)
        is_persistent, persist_reason = check_trend_persistence(
            symbol, verdict, confidence, ctx, broader_trend=broader_trend
        )
        if is_persistent and entry_quality >= MIN_ENTRY_QUALITY_CORE and regime_sc >= MIN_REGIME_SCORE_CORE:
            return _decision("TRIGGERED_CORE", "TREND_CONTINUATION",
                           persist_reason, soft_conflicts, scores)
        
        # Priority 3: Momentum scoring (balanced fallback)
        momentum_score = calculate_momentum_score(
            symbol, verdict, confidence, ctx, broader_trend=broader_trend
        )
        scores["momentum_score"] = momentum_score
        
        if momentum_score >= 80:  # higher threshold for fallback
            if entry_quality >= MIN_ENTRY_QUALITY_CORE:
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
        
        # Blocked
        block_parts = []
        if not is_rev:
            block_parts.append(f"No reversal: {rev_reason}")
        if not is_persistent:
            block_parts.append(f"No persistence: {persist_reason}")
        if momentum_score < 80:
            block_parts.append(f"Low momentum ({momentum_score})")
        if entry_quality < MIN_ENTRY_QUALITY_EXPERIMENTAL:
            block_parts.append(f"Poor entry quality ({entry_quality}/100): {'; '.join(entry_reasons)}")
        
        return _blocked("; ".join(block_parts) or "No qualifying condition met")
    
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
              soft_conflicts: list[str], scores: dict) -> dict:
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
