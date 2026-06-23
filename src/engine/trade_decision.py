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
    """
    def _decision(status: str, setup_type: str, reason: str,
                  soft_conflicts: list[str], scores: dict,
                  ai_verdict_arg=None) -> dict:
        effective_ai_verdict = ai_verdict_arg if ai_verdict_arg is not None else ai_verdict
        return _decision_global(
            status, setup_type, reason, soft_conflicts, scores,
            ai_verdict=effective_ai_verdict, suppress_logs=suppress_logs
        )

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

    # MCX confidence floor: MCX OI data is thinner than NSE index — require
    # higher conviction before trading commodity options/futures.
    sym_base = str(symbol).upper().split()[0]
    effective_min_conf = MCX_MIN_CONFIDENCE if sym_base in MCX_SYMBOLS else MIN_CONFIDENCE_CORE
    if confidence < effective_min_conf:
        return _blocked(
            f"Confidence {confidence}% below {'MCX' if sym_base in MCX_SYMBOLS else 'core'} "
            f"threshold {effective_min_conf}% for {symbol}"
        )

    # Chart conflict is NO LONGER a hard block. 1H/3H candle sentiments are
    # for timeframe strategy only. Core OI-based trades should not be blocked
    # by chart disagreement. Instead, a -20 penalty is applied to entry_quality
    # below, allowing strong OI setups to proceed while filtering weak ones.

    # ── Fix #8: Research mode scan-count gate bypass (consistent with regime) ──
    # In PAPER_RESEARCH_MODE both gates are bypassed together so test/prod
    # parity is clear. Each bypass is explicitly tagged as a soft_conflict.
    scan_count = _count_valid_scans(symbol)
    if scan_count < TREND_MIN_SCANS:
        if PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE:
            soft_conflicts.append("INSUFFICIENT_SCAN_HISTORY")
            log.debug("%s: research mode — scan gate bypassed (%d scans)", symbol, scan_count)
        else:
            return _blocked(
                f"Insufficient scan history: {scan_count} scans (need {TREND_MIN_SCANS})"
            )

    # Build plan to get option_type + strike for entry quality
    from src.engine.paper_plan import build_paper_trade_plan
    plan_ctx = {**ctx, "symbol": symbol}
    plan = build_paper_trade_plan(verdict, confidence, plan_ctx)
    if not plan:
        return _blocked("No valid trade plan from verdict")

    option_type = plan["option_type"]
    strike      = plan["strike"]
    plan_ctx    = {**plan_ctx, **plan}

    # ── Score all layers ───────────────────────────────────────────────────
    entry_quality, entry_reasons = calculate_entry_quality(symbol, option_type, strike, plan_ctx)

    trend_alignment = get_trend_alignment_score(symbol, verdict)

    regime = detect_market_regime(symbol)
    if regime == REGIME_NO_TRADE:
        # Fix #8: bypass regime gate consistently with scan gate
        if PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE:
            regime_sc = 50
            soft_conflicts.append("INSUFFICIENT_REGIME_HISTORY")
            log.debug("%s: research mode — regime gate bypassed", symbol)
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

    # ── AI verdict influence ───────────────────────────────────────────────
    if ai_verdict:
        ai_bias = _extract_ai_bias(ai_verdict)
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

    # ── Pre-fetch broader trend once ───────────────────────────────────────
    broader_trend = get_broader_trend_from_alerts(symbol)

    # ── Mode-based decision logic ──────────────────────────────────────────

    if TREND_FILTER_MODE == "conservative":
        is_persistent, persist_reason = check_trend_persistence(
            symbol, verdict, confidence, ctx, broader_trend=broader_trend
        )
        if not is_persistent:
            return _blocked(f"Conservative filter: {persist_reason}")
        regime_ok = (regime_sc >= MIN_REGIME_SCORE_CORE) or (PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE)
        if entry_quality >= MIN_ENTRY_QUALITY_CORE and regime_ok:
            if regime_sc < MIN_REGIME_SCORE_CORE:
                soft_conflicts.append("LOW_REGIME_SCORE")
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
        # Pre-calculate reversal indicators to avoid undefined variable reference in fallback reasoning
        is_rev, rev_reason = detect_reversal_from_scans(symbol, verdict, confidence)

        # Priority 1: Reversal detection
        # Fix #7: reversal requires REVERSAL_MIN_CONFIDENCE.
        # Without this gate, a 70-confidence reversal call at trend-day open
        # would fire before persistence/momentum, closing profitable positions
        # on noise. REVERSAL_MIN_CONFIDENCE (default 75) is a higher bar than
        # MIN_CONFIDENCE_CORE (default 65) — you need stronger conviction to
        # call a top/bottom than to ride an existing move.
        if (is_rev
                and confidence >= REVERSAL_MIN_CONFIDENCE
                and entry_quality >= MIN_ENTRY_QUALITY_CORE):
            return _decision("TRIGGERED_CORE", "CONFIRMED_REVERSAL", rev_reason, soft_conflicts, scores)
        elif is_rev and confidence < REVERSAL_MIN_CONFIDENCE:
            soft_conflicts.append(
                f"REVERSAL_LOW_CONF({confidence}<{REVERSAL_MIN_CONFIDENCE})"
            )
            log.debug(
                "%s: reversal detected but confidence %d < REVERSAL_MIN_CONFIDENCE %d.",
                symbol, confidence, REVERSAL_MIN_CONFIDENCE,
            )

        # Priority 2: Trend persistence
        is_persistent, persist_reason = check_trend_persistence(
            symbol, verdict, confidence, ctx, broader_trend=broader_trend
        )
        regime_ok = (regime_sc >= MIN_REGIME_SCORE_CORE) or (PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE)
        if is_persistent and entry_quality >= MIN_ENTRY_QUALITY_CORE and regime_ok:
            if regime_sc < MIN_REGIME_SCORE_CORE:
                soft_conflicts.append("LOW_REGIME_SCORE")
            return _decision("TRIGGERED_CORE", "TREND_CONTINUATION",
                             persist_reason, soft_conflicts, scores)

        # Priority 3: Momentum scoring
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
        elif confidence < REVERSAL_MIN_CONFIDENCE:
            block_parts.append(f"Reversal low conf ({confidence}<{REVERSAL_MIN_CONFIDENCE})")
        if not is_persistent:
            block_parts.append(f"No persistence: {persist_reason}")
        if momentum_score < MOMENTUM_SCORE_THRESHOLD:
            block_parts.append(f"Low momentum ({momentum_score} < {MOMENTUM_SCORE_THRESHOLD})")
        if entry_quality < MIN_ENTRY_QUALITY_EXPERIMENTAL:
            block_parts.append(f"Poor entry quality ({entry_quality}/100): {'; '.join(entry_reasons)}")

        block_reason = "; ".join(block_parts) or "No qualifying condition met"

        # Priority 5: AI boost
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
        # Unknown mode — legacy fallback
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

def _decision_global(status: str, setup_type: str, reason: str,
                     soft_conflicts: list[str], scores: dict,
                     ai_verdict=None, suppress_logs: bool = False) -> dict:
    from config.runtime_config import load_runtime_config
    rconf = load_runtime_config()
    ai_decision_mode = rconf.get("live_ai_decision_mode", "advisory")
    ai_min_confidence_veto = int(rconf.get("live_ai_min_confidence_veto", 85))

    if ai_decision_mode == "full" and "ai_bias" not in scores and not ai_verdict:
        if not suppress_logs:
            log.warning("AI decision mode is 'full' but no AI verdict was provided. Demoting trade.")
        if status == "TRIGGERED_CORE":
            status = "TRIGGERED_EXPERIMENTAL"
            soft_conflicts = soft_conflicts + ["AI_NO_VERDICT_DEMOTED"]
            reason = f"Demoted from CORE due to missing AI verdict | {reason}"

    if (("ai_bias" in scores or ai_verdict) and ai_decision_mode == "full"
            and status.startswith("TRIGGERED")):
        ai_conf = scores.get('ai_confidence', 0)
        ai_agrees = scores.get('ai_agrees', False)  # fix #6: was True — veto never fired when key absent
        ai_risk = scores.get('ai_risk_rating', '')
        if not ai_agrees and ai_conf >= ai_min_confidence_veto:
            if not suppress_logs:
                log.warning(
                    "Trade VETOED by AI: %s | AI bias disagrees (conf=%d%%, risk=%s)",
                    status, ai_conf, ai_risk,
                )
            return {
                "status":         "BLOCKED",
                "setup_type":     None,
                "reason":         f"AI VETO: conf={ai_conf}% disagrees, risk={ai_risk} | was {status}: {reason}",
                "soft_conflicts": soft_conflicts + ["AI_VETOED"],
                "scores":         scores,
            }
    if not suppress_logs:
        log.info("Trade decision: %s | %s | %s", status, setup_type, reason)
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
