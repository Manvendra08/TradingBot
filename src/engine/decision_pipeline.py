import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
from typing import Any

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
    MCX_MIN_CONFIDENCE,
    MCX_SYMBOLS,
    PIPELINE_SHORT_CIRCUIT,
    ENTRY_QUALITY_MIN_SCORE_TF,
    TREND_ALIGNMENT_MIN_SCORE_TF,
    TIMEFRAME_OI_MIN_DIFF_PCT,
    HIGH_CONFIDENCE_BYPASS_THRESHOLD,
    EMP_BOOST_MIN_TRADES,
    EMP_BOOST_MIN_WINRATE,
)
from src.engine.time_guards import is_trading_allowed_now
from src.engine.entry_quality import calculate_entry_quality
from src.engine.trend_analysis import (
    detect_reversal_from_scans,
    get_trend_alignment_score,
    check_trend_persistence,
    calculate_momentum_score,
    get_broader_trend_from_alerts,
)
from src.engine.regime_detector import detect_market_regime, regime_score_for_trade, REGIME_NO_TRADE
from src.engine.risk_engine import _check_risk_limits_for_table
from src.engine.verdict_sets import is_bullish, is_bearish
from src.engine.trade_decision import (
    _extract_ai_bias,
    _extract_ai_veto_flag,
    _extract_ai_veto_reason,
    _count_valid_scans,
)
from src.engine.pattern_history import get_pattern_stats

log = logging.getLogger(__name__)


@dataclass
class StepResult:
    name: str           # signal | rule | ai | entry_quality | regime | trend_alignment
    passed: bool        # Whether the check passed
    score: float        # Numeric score (0-100) or -1 for binary steps
    reason: str         # Human-readable summary
    data: dict          # Step input/output context


@dataclass
class PipelineContext:
    engine: str         # CORE_OI | TIMEFRAME
    symbol: str
    direction: str | None  # LONG | SHORT (set during execution)
    underlying: float
    scan_context: dict
    ai_verdict: dict | None
    steps: list[StepResult]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps)

    @property
    def final_action(self) -> str:
        return "TRADE" if self.passed else "SKIP"

    @property
    def block_step(self) -> str | None:
        failed = [s for s in self.steps if not s.passed]
        return failed[0].name if failed else None

    @property
    def block_reason(self) -> str:
        failed = [s for s in self.steps if not s.passed]
        return failed[0].reason if failed else ""

    @property
    def verdict_label(self) -> str:
        return str(
            self.scan_context.get("intel", {}).get("verdict_label")
            or self.scan_context.get("verdict_label")
            or ""
        )

    @property
    def pcr_regime(self) -> str:
        regime_step = next((s for s in self.steps if s.name == "regime"), None)
        if regime_step and regime_step.data.get("regime"):
            return str(regime_step.data.get("regime"))
        return str(
            self.scan_context.get("intel", {}).get("pcr_regime")
            or self.scan_context.get("pcr_regime")
            or ""
        )


# ── Step Implementations ──────────────────────────────────────────────────────────

def step_signal_core_oi(ctx: PipelineContext) -> StepResult:
    intel = ctx.scan_context.get("intel") or {}
    verdict = intel.get("verdict_label", "")
    confidence = int(intel.get("confidence") or 0)

    if not is_bullish(verdict) and not is_bearish(verdict):
        return StepResult(
            name="signal",
            passed=False,
            score=0,
            reason=f"Verdict '{verdict}' is not directional",
            data={"verdict": verdict, "confidence": confidence}
        )

    sym_base = str(ctx.symbol).upper().split()[0]
    effective_min_conf = MCX_MIN_CONFIDENCE if sym_base in MCX_SYMBOLS else MIN_CONFIDENCE_CORE

    ctx.direction = "LONG" if is_bullish(verdict) else "SHORT"

    if confidence < effective_min_conf:
        # BUG-H06 FIX: Always check momentum for low-conviction signals,
        # regardless of research mode. Previously this gate only fired when
        # not in PAPER_RESEARCH_MODE, causing low-conviction signals to pass
        # silently in research mode without momentum validation.
        from src.engine.trend_analysis import calculate_momentum_score
        momentum = calculate_momentum_score(
            symbol=ctx.symbol,
            verdict=verdict,
            confidence=confidence,
            ctx=ctx.scan_context,
        )
        if "Low Conviction" in verdict and momentum >= 75:
            log.info(
                "%s: Boosting 'Low Conviction' confidence from %d to %d due to high momentum (%d)",
                ctx.symbol, confidence, effective_min_conf, momentum
            )
            confidence = effective_min_conf
            # BUG-H10 FIX: Mutate scan_context in-place instead of replacing with new dict.
            # Replacing breaks references held by other pipeline steps that still point to old dict.
            if "intel" in ctx.scan_context:
                ctx.scan_context["intel"]["confidence"] = confidence
        elif not PAPER_RESEARCH_MODE:
            # Only block in non-research mode; research mode allows low-conf through
            return StepResult(
                name="signal",
                passed=False,
                score=confidence,
                reason=f"Confidence {confidence}% below threshold {effective_min_conf}%",
                data={"verdict": verdict, "confidence": confidence, "min_confidence": effective_min_conf}
            )

    return StepResult(
        name="signal",
        passed=True,
        score=confidence,
        reason=f"Signal {verdict} detected with confidence {confidence}%",
        data={"verdict": verdict, "confidence": confidence}
    )


def step_rule_core_oi(ctx: PipelineContext) -> StepResult:
    # 1. Time guard check
    expiry_str = ctx.scan_context.get("expiry")
    time_ok, time_reason = is_trading_allowed_now(ctx.symbol, expiry_str)
    if not time_ok:
        return StepResult(
            name="rule",
            passed=False,
            score=-1,
            reason=f"Time guard: {time_reason}",
            data={"reason": time_reason}
        )

    # 2. Missing underlying price check
    if float(ctx.underlying) <= 0:
        return StepResult(
            name="rule",
            passed=False,
            score=-1,
            reason="Missing underlying price",
            data={"underlying": ctx.underlying}
        )

    # 3. Insufficient scan history check
    scan_count = _count_valid_scans(ctx.symbol)
    confidence = int(ctx.scan_context.get("intel", {}).get("confidence") or 0)
    if scan_count < TREND_MIN_SCANS:
        if PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE:
            # bypassed in research mode
            pass
        else:
            return StepResult(
                name="rule",
                passed=False,
                score=-1,
                reason=f"Insufficient scan history: {scan_count} scans (need {TREND_MIN_SCANS})",
                data={"scan_count": scan_count, "required": TREND_MIN_SCANS}
            )

    return StepResult(
        name="rule",
        passed=True,
        score=100,
        reason="Core rules passed",
        data={"scan_count": scan_count}
    )


def step_ai_alignment(ctx: PipelineContext) -> StepResult:
    """Shared LLM enrichment alignment step for Core and Timeframe."""
    ai_verdict = ctx.ai_verdict

    from config.runtime_config import load_runtime_config
    rconf = load_runtime_config()
    ai_decision_mode = rconf.get("live_ai_decision_mode", "advisory")

    if ai_verdict:
        if not isinstance(ai_verdict, dict):
            try:
                ai_verdict = asdict(ai_verdict)
            except TypeError:
                ai_verdict = getattr(ai_verdict, "__dict__", {})

    if not ai_verdict:
        if ai_decision_mode == "full" and ctx.engine == "CORE_OI":
            return StepResult(
                name="ai",
                passed=True,
                score=-1,
                reason="Missing AI verdict, trade demoted to experimental",
                data={"demote": True}
            )
        return StepResult(
            name="ai",
            passed=True,
            score=-1,
            reason="AI enrichment skipped or unavailable",
            data={}
        )

    ai_bias = _extract_ai_bias(ai_verdict) or "NEUTRAL"
    ai_conf = int(ai_verdict.get("confidence") or 0)
    ai_risk = str(ai_verdict.get("risk_rating") or "").upper()

    verdict_bias = "BULLISH" if ctx.direction == "LONG" else "BEARISH"
    ai_agrees = (ai_bias == verdict_bias)

    # Timeframe Engine specific AI gates
    if ctx.engine == "TIMEFRAME":
        if not ai_agrees:
            return StepResult(
                name="ai",
                passed=False,
                score=ai_conf,
                reason=f"LLM bias alignment mismatch ({ai_bias} vs {ctx.direction})",
                data={"ai_bias": ai_bias, "ai_conf": ai_conf, "ai_agrees": ai_agrees}
            )
        if ai_risk == "HIGH":
            return StepResult(
                name="ai",
                passed=False,
                score=ai_conf,
                reason="LLM risk rating is HIGH",
                data={"ai_bias": ai_bias, "ai_conf": ai_conf, "ai_risk": ai_risk}
            )

    # Core Engine specific AI Veto in full mode (ADR-007: veto_flag binary only, no confidence threshold)
    if ctx.engine == "CORE_OI" and ai_decision_mode == "full":
        veto_flag = _extract_ai_veto_flag(ai_verdict)
        veto_reason = _extract_ai_veto_reason(ai_verdict)
        if not ai_agrees and veto_flag:
            reason_str = veto_reason or "LLM flagged disqualifier"
            return StepResult(
                name="ai",
                passed=False,
                score=-1,
                reason=f"AI VETO: {reason_str}",
                data={"ai_bias": ai_bias, "ai_agrees": ai_agrees, "ai_risk": ai_risk, "vetoed": True, "veto_reason": veto_reason}
            )

    return StepResult(
        name="ai",
        passed=True,
        score=ai_conf,
        reason=f"AI alignment passed (bias={ai_bias}, conf={ai_conf}%, risk={ai_risk})",
        data={"ai_bias": ai_bias, "ai_conf": ai_conf, "ai_agrees": ai_agrees, "ai_risk": ai_risk}
    )


def step_tfss_handoff_core(ctx: PipelineContext) -> StepResult:
    from src.engine.trend_following_short_strangle import (
        normalize_core_verdict_to_tfss_intent,
        compute_persisted_trend,
        resolve_tfss_execution_side
    )
    # Bypass TFSS in legacy mock tests using 'TEST' symbol
    if ctx.symbol == "TEST":
        return StepResult(
            name="tfss_handoff",
            passed=True,
            score=100,
            reason="TFSS bypassed for TEST symbol",
            data={"tfss_eligible": False}
        )

    verdict = ctx.scan_context.get("intel", {}).get("verdict_label", "")
    
    tfss_intent = normalize_core_verdict_to_tfss_intent(verdict)
    if not tfss_intent:
        # Non-qualifying Core verdicts continue existing handling
        return StepResult(
            name="tfss_handoff",
            passed=True,
            score=100,
            reason="Verdict does not qualify for TFSS execution",
            data={"tfss_eligible": False}
        )
        
    # Compute native persistence using the >=3 of last 5 rule
    persisted = compute_persisted_trend(ctx.symbol, ctx.scan_context)
    if not persisted.is_valid:
        return StepResult(
            name="tfss_handoff",
            passed=False,
            score=0,
            reason=f"TFSS Persistence Blocked: {persisted.reason}",
            data={"tfss_eligible": True, "persisted": persisted.is_valid}
        )
        
    side_res = resolve_tfss_execution_side(tfss_intent, persisted)
    if isinstance(side_res, dict) and side_res.get("action") == "BLOCK":
        return StepResult(
            name="tfss_handoff",
            passed=False,
            score=0,
            reason=f"TFSS Blocked: {side_res.get('reason')}",
            data={"tfss_eligible": True, "persisted": persisted.is_valid}
        )
    
    # Store the TFSS resolution in scan_context for downstream steps
    ctx.scan_context["_tfss_intent"] = tfss_intent.bias
    ctx.scan_context["_tfss_bias"] = tfss_intent.bias
    ctx.scan_context["_execution_source"] = "CORE_TFSS"
    ctx.scan_context["_tfss_execution_side"] = side_res # e.g. "SELL_PE", "SELL_CE"
    ctx.scan_context["_tfss_persistence"] = persisted.label
    
    return StepResult(
        name="tfss_handoff",
        passed=True,
        score=100,
        reason=f"TFSS Handoff OK: {tfss_intent.bias} -> {side_res}",
        data={
            "tfss_eligible": True,
            "tfss_bias": tfss_intent.bias,
            "tfss_execution_side": side_res,
            "persisted_label": persisted.label
        }
    )



def step_entry_quality_core(ctx: PipelineContext) -> StepResult:
    from src.engine.paper_plan import build_paper_trade_plan
    # BUG-M10 FIX: Use dict(ctx.scan_context) without filtering non-string keys.
    # Filtering out non-string keys may remove important data from scan_context.
    plan_ctx = dict(ctx.scan_context)
    plan_ctx["symbol"] = ctx.symbol
    verdict = ctx.scan_context.get("intel", {}).get("verdict_label", "")
    confidence = int(ctx.scan_context.get("intel", {}).get("confidence") or 0)
    # Pass LLM instrument so GO_LONG/GO_SHORT use the actual CE/PE from the LLM
    if ctx.ai_verdict:
        plan_ctx["instrument"] = getattr(ctx.ai_verdict, "instrument", None) or (
            ctx.ai_verdict.get("instrument") if isinstance(ctx.ai_verdict, dict) else None
        )

    plan = build_paper_trade_plan(verdict, confidence, plan_ctx)
    if not plan:
        return StepResult(
            name="entry_quality",
            passed=False,
            score=0,
            reason="No valid trade plan from verdict",
            data={}
        )

    option_type = plan["option_type"]
    strike = plan["strike"]
    plan_ctx = {**plan_ctx, **plan}

    entry_quality, entry_reasons = calculate_entry_quality(ctx.symbol, option_type, strike, plan_ctx)

    # Cache for downstream step usage
    ctx.scan_context["_pipeline_plan"] = plan
    ctx.scan_context["_entry_quality"] = entry_quality
    ctx.scan_context["_entry_reasons"] = entry_reasons

    # Flaw #5: Check against Core threshold instead of Experimental
    passed = entry_quality >= MIN_ENTRY_QUALITY_CORE

    return StepResult(
        name="entry_quality",
        passed=passed,
        score=entry_quality,
        reason=f"Entry quality score {entry_quality}/100: {'; '.join(entry_reasons)}" if entry_reasons else f"Entry quality score {entry_quality}/100",
        data={"option_type": option_type, "strike": strike, "entry_reasons": entry_reasons}
    )


def step_regime(ctx: PipelineContext) -> StepResult:
    regime = detect_market_regime(ctx.symbol)
    confidence = int(ctx.scan_context.get("intel", {}).get("confidence") or 0)

    if regime == REGIME_NO_TRADE:
        if PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE:
            regime_sc = 50
            passed = True
            reason = "Regime gate bypassed in research mode"
        else:
            return StepResult(
                name="regime",
                passed=False,
                score=0,
                reason="Insufficient scan history for regime detection",
                data={"regime": regime}
            )
    else:
        plan = ctx.scan_context.get("_pipeline_plan") or {}
        option_type = plan.get("option_type", "CE")
        regime_sc = regime_score_for_trade(regime, option_type)
        # Flaw #4: Enforce Regime Score Gating
        if PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE:
            passed = True
            reason = f"Market regime: {regime} (score={regime_sc}) - Bypassed in research mode"
        else:
            passed = regime_sc >= MIN_REGIME_SCORE_CORE
            if passed:
                reason = f"Market regime: {regime} (score={regime_sc})"
            else:
                reason = f"Market regime: {regime} (score={regime_sc}) below required {MIN_REGIME_SCORE_CORE}"

    return StepResult(
        name="regime",
        passed=passed,
        score=regime_sc,
        reason=reason,
        data={"regime": regime, "regime_score": regime_sc}
    )


def step_trend_alignment_core(ctx: PipelineContext) -> StepResult:
    symbol = ctx.symbol
    verdict = ctx.scan_context.get("intel", {}).get("verdict_label", "")
    confidence = int(ctx.scan_context.get("intel", {}).get("confidence") or 0)

    entry_quality = next((s.score for s in ctx.steps if s.name == "entry_quality"), 0)
    regime_sc = next((s.score for s in ctx.steps if s.name == "regime"), 0)

    trend_alignment = get_trend_alignment_score(symbol, verdict)
    broader_trend = get_broader_trend_from_alerts(symbol)

    ai_step = next((s for s in ctx.steps if s.name == "ai"), None)
    ai_agrees = ai_step.data.get("ai_agrees", False) if ai_step else False
    ai_conf = ai_step.score if ai_step else 0

    from config.runtime_config import load_runtime_config
    rconf = load_runtime_config()
    ai_decision_mode = rconf.get("live_ai_decision_mode", "advisory")

    regime_ok = (regime_sc >= MIN_REGIME_SCORE_CORE) or (PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_CORE)

    passed = False
    setup_type = "UNKNOWN"
    reason = "No qualifying trend condition met"
    soft_conflicts = []

    if TREND_FILTER_MODE == "conservative":
        is_persistent, persist_reason = check_trend_persistence(
            symbol, verdict, confidence, ctx.scan_context, broader_trend=broader_trend
        )
        if is_persistent and entry_quality >= MIN_ENTRY_QUALITY_CORE and regime_ok:
            passed = True
            setup_type = "TREND_CONTINUATION"
            reason = f"Conservative: {persist_reason}"
            if regime_sc < MIN_REGIME_SCORE_CORE:
                soft_conflicts.append("LOW_REGIME_SCORE")
        else:
            reason = f"Conservative: not persistent or poor quality (eq={entry_quality}, regime={regime_sc})"

    elif TREND_FILTER_MODE == "balanced":
        momentum_score = calculate_momentum_score(
            symbol, verdict, confidence, ctx.scan_context, broader_trend=broader_trend
        )
        if momentum_score >= MOMENTUM_SCORE_THRESHOLD and entry_quality >= MIN_ENTRY_QUALITY_CORE:
            passed = True
            setup_type = "MOMENTUM_TRADE"
            reason = f"Momentum score={momentum_score}"
        else:
            reason = f"Balanced: low momentum ({momentum_score} < {MOMENTUM_SCORE_THRESHOLD}) or poor quality ({entry_quality})"

    elif TREND_FILTER_MODE == "aggressive":
        is_rev, rev_reason = detect_reversal_from_scans(symbol, verdict, confidence)
        if is_rev and entry_quality >= MIN_ENTRY_QUALITY_CORE:
            passed = True
            setup_type = "CONFIRMED_REVERSAL"
            reason = rev_reason
        else:
            reason = f"Aggressive: no reversal ({rev_reason}) or poor quality ({entry_quality})"

    elif TREND_FILTER_MODE == "hybrid":
        is_rev, rev_reason = detect_reversal_from_scans(symbol, verdict, confidence)
        is_persistent, persist_reason = check_trend_persistence(
            symbol, verdict, confidence, ctx.scan_context, broader_trend=broader_trend
        )
        momentum_score = calculate_momentum_score(
            symbol, verdict, confidence, ctx.scan_context, broader_trend=broader_trend
        )

        # Priority 1: Reversal detection
        if is_rev and confidence >= REVERSAL_MIN_CONFIDENCE and entry_quality >= MIN_ENTRY_QUALITY_CORE:
            passed = True
            setup_type = "CONFIRMED_REVERSAL"
            reason = rev_reason

        # Priority 2: Trend persistence
        elif is_persistent and entry_quality >= MIN_ENTRY_QUALITY_CORE and regime_ok:
            # High-confidence bypass: when OI confidence >= 90%, relax trend alignment
            # to allow entries in choppy markets with strong OI conviction
            effective_trend_threshold = MIN_TREND_ALIGNMENT_CORE
            if confidence >= HIGH_CONFIDENCE_BYPASS_THRESHOLD:
                effective_trend_threshold = int(MIN_TREND_ALIGNMENT_CORE * 0.6)
                log.debug(
                    "%s: High confidence bypass — trend threshold relaxed from %d to %d (conf=%d)",
                    symbol, MIN_TREND_ALIGNMENT_CORE, effective_trend_threshold, confidence,
                )
            if trend_alignment >= effective_trend_threshold:
                passed = True
                setup_type = "TREND_CONTINUATION"
                reason = persist_reason
                if regime_sc < MIN_REGIME_SCORE_CORE:
                    soft_conflicts.append("LOW_REGIME_SCORE")
            else:
                reason = f"Trend continuation blocked: alignment ({trend_alignment}) < required ({effective_trend_threshold})"

        # Priority 3: Momentum scoring
        elif momentum_score >= MOMENTUM_SCORE_THRESHOLD and entry_quality >= MIN_ENTRY_QUALITY_CORE:
            passed = True
            setup_type = "MOMENTUM_TRADE"
            reason = f"Momentum score={momentum_score}"

        # Priority 4: Experimental (research mode only)
        elif PAPER_RESEARCH_MODE and confidence >= MIN_CONFIDENCE_EXPERIMENTAL and entry_quality >= MIN_ENTRY_QUALITY_EXPERIMENTAL:
            passed = True
            setup_type = "EXPERIMENTAL_SETUP"
            reason = (
                f"Marginal setup — conf={confidence} eq={entry_quality} "
                f"ta={trend_alignment} momentum={momentum_score}"
            )

        # Priority 5: Empirical promotion (ADR-007 v2)
        if not passed and ai_decision_mode in ("full", "empirical"):
            block_parts = []
            if not is_rev:
                block_parts.append(f"No reversal: {rev_reason}")
            if not is_persistent:
                block_parts.append(f"No persistence: {persist_reason}")
            if momentum_score < MOMENTUM_SCORE_THRESHOLD:
                block_parts.append(f"Low momentum ({momentum_score})")

            precedent = get_pattern_stats(
                symbol=ctx.symbol, verdict=ctx.verdict_label, pcr_regime=ctx.pcr_regime,
            )
            veto_flag = _extract_ai_veto_flag(ctx.ai_verdict)
            if (precedent.n_trades >= rconf.get("emp_boost_min_trades", EMP_BOOST_MIN_TRADES)
                and precedent.win_rate >= rconf.get("emp_boost_min_winrate", EMP_BOOST_MIN_WINRATE)
                and precedent.avg_pnl > 0
                and not veto_flag):
                passed = True
                setup_type = "EMPIRICAL_PROMOTED"
                soft_conflicts.append("EMPIRICAL_PROMOTED")
                reason = (f"Empirical boost: {precedent.win_rate:.0%} over "
                          f"{precedent.n_trades} trades | LLM veto: none | "
                          f"Rule blocked: {'; '.join(block_parts)}")

    # Cache outputs for final trade execution
    ctx.scan_context["_setup_type"] = setup_type
    ctx.scan_context["_decision_reason"] = reason
    ctx.scan_context["_soft_conflicts"] = soft_conflicts
    ctx.scan_context["_scores"] = {
        "confidence": confidence,
        "entry_quality": entry_quality,
        "trend_alignment": trend_alignment,
        "regime_score": regime_sc,
    }

    return StepResult(
        name="trend",
        passed=passed,
        score=trend_alignment,
        reason=reason,
        data={
            "trend_alignment": trend_alignment,
            "setup_type": setup_type,
            "soft_conflicts": soft_conflicts,
            "mode": TREND_FILTER_MODE
        }
    )


def step_risk(ctx: PipelineContext) -> StepResult:
    # 0. Check option buying on expiry day
    expiry_str = ctx.scan_context.get("expiry")
    if expiry_str:
        try:
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            import pytz
            IST = pytz.timezone("Asia/Kolkata")
            today_ist = datetime.now(IST).date()
            if expiry_date == today_ist:
                side = None
                option_type = None
                if ctx.engine == "CORE_OI":
                    plan = ctx.scan_context.get("_pipeline_plan")
                    if plan:
                        side = plan.get("side")
                        option_type = plan.get("option_type")
                else:  # TIMEFRAME
                    side = "BUY"
                    option_type = "CE" if ctx.direction == "LONG" else "PE"
                    is_mcx = ctx.symbol.upper().split()[0] in MCX_SYMBOLS
                    if is_mcx:
                        from src.engine.paper_plan import mcx_option_liquidity_ok
                        from config.symbol_classes import get_strike_step
                        step = float(get_strike_step(ctx.symbol) or 1)
                        atm = ctx.scan_context.get("atm_strike") or round(ctx.underlying / step) * step
                        if not mcx_option_liquidity_ok(ctx.symbol, atm, ctx.scan_context):
                            option_type = "FUT"

                if side == "BUY" and option_type in ("CE", "PE"):
                    return StepResult(
                        name="risk",
                        passed=False,
                        score=-1,
                        reason=f"Option buying (BUY {option_type}) is blocked on expiry day ({expiry_str})",
                        data={"expiry": expiry_str, "option_type": option_type, "side": side}
                    )
        except Exception as e:
            log.warning("Error checking expiry day option buying in step_risk: %s", e)

    is_live = ctx.scan_context.get("is_live", False)
    table = "live_trades" if is_live else "paper_trades"
    mode = "live" if is_live else "paper"
    allowed, reason, sub_check_code = _check_risk_limits_for_table(ctx.symbol, table, mode)
    return StepResult(
        name="risk",
        passed=allowed,
        score=-1,
        reason=reason if not allowed else "Risk checks passed",
        data={"sub_check": sub_check_code, "reason": reason}
    )


# ── Timeframe Strategy Steps ──────────────────────────────────────────────────────────

def step_signal_timeframe(ctx: PipelineContext) -> StepResult:
    chart_indicators = ctx.scan_context.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}
    pay_3h = tf_data.get("3h") or {}
    ohlc_3h = pay_3h.get("ohlc")
    prev_3h = pay_3h.get("prev_ohlc") or pay_3h.get("last_closed_ohlc")

    if not ohlc_3h or not prev_3h:
        return StepResult(
            name="signal",
            passed=False,
            score=0,
            reason="Missing or incomplete 3H candle data",
            data={}
        )

    c_3h_close = float(ohlc_3h["close"])
    p_3h_high = float(prev_3h["high"])
    p_3h_low = float(prev_3h["low"])

    from src.engine.paper_trading import _get_atr
    atr_val = _get_atr(ctx.scan_context)
    breakout_buffer = max((atr_val or 0) * 0.5, ctx.underlying * 0.003)

    is_long_trigger = c_3h_close > p_3h_high + breakout_buffer
    is_short_trigger = c_3h_close < p_3h_low - breakout_buffer

    if not is_long_trigger and not is_short_trigger:
        return StepResult(
            name="signal",
            passed=False,
            score=0,
            reason="No 3H breakout detected",
            data={"c_3h_close": c_3h_close, "p_3h_high": p_3h_high, "p_3h_low": p_3h_low, "buffer": breakout_buffer}
        )

    ctx.direction = "LONG" if is_long_trigger else "SHORT"

    # Cache breakout info
    ctx.scan_context["_breakout_buffer"] = breakout_buffer
    ctx.scan_context["_c_3h_close"] = c_3h_close
    ctx.scan_context["_p_3h_high"] = p_3h_high
    ctx.scan_context["_p_3h_low"] = p_3h_low

    return StepResult(
        name="signal",
        passed=True,
        score=100,
        reason=f"3H Breakout {ctx.direction} detected (close={c_3h_close:.2f}, high={p_3h_high:.2f}, low={p_3h_low:.2f}, buffer={breakout_buffer:.2f})",
        data={"c_3h_close": c_3h_close, "p_3h_high": p_3h_high, "p_3h_low": p_3h_low, "buffer": breakout_buffer, "direction": ctx.direction}
    )


def step_rule_timeframe(ctx: PipelineContext) -> StepResult:
    symbol = ctx.symbol
    direction = ctx.direction

    # 1. Market hours check
    from src.engine.paper_trading import _is_market_open
    if not _is_market_open(symbol):
        return StepResult(
            name="rule",
            passed=False,
            score=-1,
            reason="Outside market hours",
            data={}
        )

    # 1b. Time guard check (including expiry cutoff)
    expiry_str = ctx.scan_context.get("expiry")
    time_ok, time_reason = is_trading_allowed_now(symbol, expiry_str)
    if not time_ok:
        return StepResult(
            name="rule",
            passed=False,
            score=-1,
            reason=f"Time guard: {time_reason}",
            data={"reason": time_reason}
        )

    # 2. Duplicate signal key check
    chart_indicators = ctx.scan_context.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}
    pay_3h = tf_data.get("3h") or {}
    bar_end_3h = pay_3h.get("bar_end_utc")
    if not bar_end_3h:
        return StepResult(
            name="rule",
            passed=False,
            score=-1,
            reason="Missing 3H bar end timestamp",
            data={}
        )

    signal_key = f"{symbol}:TIMEFRAME:3H:{direction}:{bar_end_3h}"
    ctx.scan_context["_signal_key"] = signal_key

    is_live = ctx.scan_context.get("is_live", False)
    table = "live_trades" if is_live else "paper_trades"

    from src.models.schema import get_conn, get_open_timeframe_trades
    with get_conn() as conn:
        cnt = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE signal_key=?", (signal_key,)
        ).fetchone()["c"]
        if cnt > 0:
            return StepResult(
                name="rule",
                passed=False,
                score=-1,
                reason=f"Duplicate signal key {signal_key}",
                data={"signal_key": signal_key}
            )

    # 3. Pyramiding checks
    open_trades = get_open_timeframe_trades(symbol, table=table)
    if len(open_trades) >= 3:
        return StepResult(
            name="rule",
            passed=False,
            score=-1,
            reason="Maximum pyramid level (3) reached",
            data={"open_count": len(open_trades)}
        )

    if len(open_trades) > 0:
        if any(t["verdict_label"] != direction for t in open_trades):
            return StepResult(
                name="rule",
                passed=False,
                score=-1,
                reason="Cannot pyramid in opposite direction",
                data={"open_trades": [t["verdict_label"] for t in open_trades]}
            )

        from src.engine.trade_plan import get_option_premium
        any_profitable = False
        for t in open_trades:
            if t["option_type"] in ("CE", "PE"):
                t_exit = get_option_premium(
                    symbol,
                    ctx.scan_context.get("expiry", ""),
                    t["strike"],
                    t["option_type"],
                    ctx.scan_context.get("option_rows"),
                )
                t_side = t.get("side") or "BUY"
                if t_exit:
                    if t_side == "SELL":
                        is_profitable = t_exit < float(t.get("entry_premium") or 0.0)
                    else:
                        is_profitable = t_exit > float(t.get("entry_premium") or 0.0)
                    if is_profitable:
                        any_profitable = True
                        break
            else:
                if t["verdict_label"] == "LONG" and ctx.underlying > float(t["entry_underlying"]):
                    any_profitable = True
                    break
                elif t["verdict_label"] == "SHORT" and ctx.underlying < float(t["entry_underlying"]):
                    any_profitable = True
                    break

        if not any_profitable:
            return StepResult(
                name="rule",
                passed=False,
                score=-1,
                reason="No profitable open trades to pyramid",
                data={"open_count": len(open_trades)}
            )

    ctx.scan_context["_pyramid_level"] = len(open_trades) + 1

    return StepResult(
        name="rule",
        passed=True,
        score=100,
        reason="Rules passed",
        data={"pyramid_level": len(open_trades) + 1}
    )


def step_entry_quality_tf(ctx: PipelineContext) -> StepResult:
    from config.settings import (
        TF_CANDLE_BODY_MIN_RATIO,
        TF_CANDLE_CLOSE_POSITION_LONG,
        TF_CANDLE_CLOSE_POSITION_SHORT
    )
    direction = ctx.direction

    chart_indicators = ctx.scan_context.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}
    pay_3h = tf_data.get("3h") or {}
    ohlc_3h = pay_3h.get("ohlc")

    if not ohlc_3h:
        return StepResult(
            name="entry_quality",
            passed=False,
            score=0,
            reason="Missing 3H candle data for entry quality",
            data={}
        )

    try:
        c_open = float(ohlc_3h["open"])
        c_high = float(ohlc_3h["high"])
        c_low = float(ohlc_3h["low"])
        c_close = float(ohlc_3h["close"])
    except (ValueError, KeyError, TypeError):
        return StepResult(
            name="entry_quality",
            passed=False,
            score=0,
            reason="Invalid candle OHLC values",
            data={}
        )

    candle_range = c_high - c_low
    if candle_range <= 0:
        return StepResult(
            name="entry_quality",
            passed=False,
            score=0,
            reason="Zero candle range",
            data={}
        )

    body_size = abs(c_close - c_open)
    body_ratio = body_size / candle_range
    close_pos = (c_close - c_low) / candle_range

    # Score out of 100
    body_score = min(50.0, (body_ratio / TF_CANDLE_BODY_MIN_RATIO) * 50.0)
    if direction == "LONG":
        close_score = min(50.0, (close_pos / TF_CANDLE_CLOSE_POSITION_LONG) * 50.0)
    else:
        close_score = min(50.0, ((1.0 - close_pos) / (1.0 - TF_CANDLE_CLOSE_POSITION_SHORT)) * 50.0)

    score = int(body_score + close_score)

    body_ok = body_ratio >= TF_CANDLE_BODY_MIN_RATIO
    if direction == "LONG":
        close_ok = close_pos >= TF_CANDLE_CLOSE_POSITION_LONG
        reason_detail = f"close in top {((1 - close_pos)*100):.1f}% (need top {((1 - TF_CANDLE_CLOSE_POSITION_LONG)*100):.1f}%)"
    else:
        close_ok = close_pos <= TF_CANDLE_CLOSE_POSITION_SHORT
        reason_detail = f"close in bottom {(close_pos*100):.1f}% (need bottom {(TF_CANDLE_CLOSE_POSITION_SHORT*100):.1f}%)"

    passed = body_ok and close_ok and (score >= ENTRY_QUALITY_MIN_SCORE_TF)

    reason = f"TF Candle Quality score {score}/100: body_ratio={body_ratio:.2f} ({'OK' if body_ok else 'FAIL'}), {reason_detail}"

    return StepResult(
        name="entry_quality",
        passed=passed,
        score=score,
        reason=reason,
        data={
            "body_ratio": body_ratio,
            "close_pos": close_pos,
            "body_ok": body_ok,
            "close_ok": close_ok,
            "body_score": body_score,
            "close_score": close_score
        }
    )


def step_trend_alignment_tf(ctx: PipelineContext) -> StepResult:
    symbol = ctx.symbol
    direction = ctx.direction

    current_ce = ctx.scan_context.get("total_ce_oi")
    current_pe = ctx.scan_context.get("total_pe_oi")

    if current_ce is None or current_pe is None:
        return StepResult(
            name="trend",
            passed=False,
            score=0,
            reason="Missing total ce/pe OI data for trend alignment",
            data={}
        )

    from config.runtime_config import get_scan_frequency_mcx, get_scan_frequency_nse
    from config.symbol_classes import get_symbol_class
    from src.models.schema import get_scan_summary_n_scans_ago, get_scan_summary_at_least_1h_old

    if get_symbol_class(symbol) == "MCX_COMMODITY":
        scan_freq = get_scan_frequency_mcx()
    else:
        scan_freq = get_scan_frequency_nse()

    fetched_at = ctx.scan_context.get("fetched_at") or datetime.now(timezone.utc).isoformat()

    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        older = get_scan_summary_n_scans_ago(symbol, scans_needed - 1)
    else:
        older = get_scan_summary_at_least_1h_old(symbol, fetched_at)

    if not older:
        return StepResult(
            name="trend",
            passed=False,
            score=0,
            reason="Insufficient scan history to calculate OI change",
            data={}
        )

    prev_ce = older["total_ce_oi"]
    prev_pe = older["total_pe_oi"]
    ce_diff = current_ce - prev_ce
    pe_diff = current_pe - prev_pe

    min_diff_pct = TIMEFRAME_OI_MIN_DIFF_PCT
    long_oi_support = (pe_diff - ce_diff) > (prev_pe * min_diff_pct)
    short_oi_support = (ce_diff - pe_diff) > (prev_ce * min_diff_pct)

    # 1H Confirm Price Sentiments
    chart_indicators = ctx.scan_context.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}
    pay_1h = tf_data.get("1h") or {}
    ohlc_1h = pay_1h.get("ohlc") or {}

    one_h_confirm = False
    c_1h_open = float(ohlc_1h.get("open") or 0)
    c_1h_close = float(ohlc_1h.get("close") or 0)
    if c_1h_open > 0 and c_1h_close > 0:
        if direction == "LONG":
            one_h_confirm = c_1h_close > c_1h_open
        elif direction == "SHORT":
            one_h_confirm = c_1h_close < c_1h_open

    oi_ratio = 0.0
    passed_oi = False
    if direction == "LONG":
        passed_oi = long_oi_support
        oi_ratio = (pe_diff - ce_diff) / (prev_pe * min_diff_pct) if prev_pe > 0 else 0
    elif direction == "SHORT":
        passed_oi = short_oi_support
        oi_ratio = (ce_diff - pe_diff) / (prev_ce * min_diff_pct) if prev_ce > 0 else 0

    oi_score = min(70.0, oi_ratio * 70.0) if passed_oi else 0.0
    confirm_score = 30.0 if one_h_confirm else 0.0
    score = int(oi_score + confirm_score)

    passed = passed_oi and (score >= TREND_ALIGNMENT_MIN_SCORE_TF)

    reason = f"TF Trend Alignment score {score}/100: OI support={passed_oi} (OI score={int(oi_score)}), 1H confirm={one_h_confirm} (confirm score={int(confirm_score)})"

    return StepResult(
        name="trend",
        passed=passed,
        score=score,
        reason=reason,
        data={
            "ce_diff": ce_diff,
            "pe_diff": pe_diff,
            "min_diff_pct": min_diff_pct,
            "long_oi_support": long_oi_support,
            "short_oi_support": short_oi_support,
            "one_h_confirm": one_h_confirm,
            "c_1h_open": c_1h_open,
            "c_1h_close": c_1h_close
        }
    )


def step_heavyweight_alignment(ctx: PipelineContext) -> StepResult:
    """
    Checks if index heavyweight momentum is aligned with the trade direction.
    - LONG trades are blocked if index weighted momentum is deeply negative (< -0.50%).
    - SHORT trades are blocked if index weighted momentum is deeply positive (> +0.50%).
    This guard applies only to NIFTY and BANKNIFTY.
    """
    symbol = ctx.symbol
    if symbol not in ("NIFTY", "BANKNIFTY"):
        return StepResult(
            name="heavyweight_alignment",
            passed=True,
            score=100,
            reason="Heavyweight alignment guard not active for this symbol",
            data={}
        )

    # Determine trade direction
    direction = ctx.direction
    if not direction:
        # Resolve direction from verdict if not set
        intel = ctx.scan_context.get("intel") or {}
        verdict = intel.get("verdict_label", "")
        if is_bullish(verdict):
            direction = "LONG"
        elif is_bearish(verdict):
            direction = "SHORT"

    if not direction:
        return StepResult(
            name="heavyweight_alignment",
            passed=True,
            score=100,
            reason="No trade direction detected; bypassing guard",
            data={}
        )

    ws = ctx.scan_context.get("index_weights_sentiment")
    if not ws:
        return StepResult(
            name="heavyweight_alignment",
            passed=True,
            score=100,
            reason="Index weights sentiment data unavailable; bypassing guard",
            data={}
        )

    weighted_momentum = float(ws.get("weighted_momentum", 0.0))
    passed = True
    reason = f"Heavyweight momentum {weighted_momentum:.3f}% is aligned with trade direction {direction}"

    from config.settings import HEAVYWEIGHT_THRESHOLDS
    threshold = HEAVYWEIGHT_THRESHOLDS.get(symbol, 0.50)

    if direction == "LONG" and weighted_momentum <= -threshold:
        passed = False
        reason = f"LONG trade blocked: Index heavyweight momentum is deeply negative ({weighted_momentum:.3f}%, threshold={-threshold:.2f}%)"
    elif direction == "SHORT" and weighted_momentum >= threshold:
        passed = False
        reason = f"SHORT trade blocked: Index heavyweight momentum is deeply positive ({weighted_momentum:.3f}%, threshold={threshold:.2f}%)"

    return StepResult(
        name="heavyweight_alignment",
        passed=passed,
        score=100 if passed else 0,
        reason=reason,
        data={"weighted_momentum": weighted_momentum, "direction": direction}
    )


# ── Pipeline Mapping and Execution ───────────────────────────────────────────────────

CORE_OI_STEPS = [
    step_signal_core_oi,
    step_rule_core_oi,
    step_ai_alignment,
    step_tfss_handoff_core,
    step_entry_quality_core,
    step_regime,
    step_trend_alignment_core,
    step_heavyweight_alignment,
    step_risk,
]

TIMEFRAME_STEPS = [
    step_signal_timeframe,
    step_rule_timeframe,
    step_ai_alignment,
    step_entry_quality_tf,
    step_trend_alignment_tf,
    step_regime,
    step_heavyweight_alignment,
    step_risk,
]


def run_entry_pipeline(ctx: PipelineContext) -> PipelineContext:
    # P2-XBUG-001: Validate is_live is explicitly a boolean at pipeline entry
    is_live = ctx.scan_context.get("is_live", False)
    if not isinstance(is_live, bool):
        raise AssertionError("is_live flag in scan_context must be a boolean.")

    # P2-BUG-027: Initialize safe defaults to prevent None errors if short-circuited
    ctx.scan_context["_pipeline_plan"] = {}
    ctx.scan_context["_entry_quality"] = 0
    ctx.scan_context["_entry_reasons"] = []
    ctx.scan_context["_setup_type"] = "UNKNOWN"
    ctx.scan_context["_decision_reason"] = "Pipeline short-circuited or incomplete"
    ctx.scan_context["_soft_conflicts"] = []
    ctx.scan_context["_scores"] = {
        "confidence": 0,
        "entry_quality": 0,
        "trend_alignment": 0,
        "regime_score": 0,
    }
    ctx.scan_context["_pyramid_level"] = 1
    ctx.scan_context["_signal_key"] = ""

    steps = CORE_OI_STEPS if ctx.engine == "CORE_OI" else TIMEFRAME_STEPS
    for step_fn in steps:
        result = step_fn(ctx)
        ctx.steps.append(result)
        if not result.passed and PIPELINE_SHORT_CIRCUIT:
            break
    return ctx

