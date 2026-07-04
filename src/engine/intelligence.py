"""
Bot Intelligence Engine v4.0
Combines scan context (OI totals, price movement, PCR, max pain, OI walls)
with current alerts to produce trade-actionable intelligence.

Phase 3 refactor: generate_intelligence() now returns an IntelligenceResult
dataclass natively. generate_intelligence_structured() reads fields directly
from the dataclass — zero regex parsing anywhere.

Output: Telegram-formatted markdown block appended to digest.
"""

import json
import logging
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

from src.engine.paper_plan import build_paper_trade_plan, format_paper_plan
from src.engine.verdict_sets import is_bearish, is_bullish
from src.models.schema import get_alert_history
from src.utils.formatting import fmt_oi, safe_num

log = logging.getLogger(__name__)


def _ctx_copy(ctx: dict) -> dict:
    """Copy a context dict, safely discarding any non-string keys that would crash ** unpacking."""
    return {k: v for k, v in ctx.items() if isinstance(k, str)}


# ── Structured result ──────────────────────────────────────────────────────


@dataclass
class IntelligenceResult:
    """
    Structured output from generate_intelligence().
    All downstream consumers (paper_trading, scan_summary, pipeline)
    read fields directly — no regex parsing needed.
    """

    symbol: str
    verdict_label: str  # e.g. "Long Buildup"
    verdict_emoji: str  # e.g. "🟢"
    verdict_desc: str  # e.g. "Bullish — fresh longs"
    bias: str  # "BULLISH" | "BEARISH" | "NEUTRAL"
    confidence: int  # 0-100
    chart_conflict: bool  # 1H vs 3H disagree
    trend: str  # broader trend label
    bull_forces: list[tuple[int, str]] = field(default_factory=list)
    bear_forces: list[tuple[int, str]] = field(default_factory=list)
    action_plan: str = ""
    risk_note: str = ""
    telegram_text: str = ""  # full Telegram-formatted message
    expiry: str = ""
    days_to_expiry: int = -1
    trade_decision: Any = None

    # Convenience: dict-like access for backward-compat with callers that do intel["key"]
    def __getitem__(self, key: str):
        return getattr(self, key)

    def keys(self):
        return [f.name for f in fields(self)]

    def __iter__(self):
        return iter(self.keys())

    def values(self):
        return (getattr(self, k) for k in self.keys())

    def items(self):
        return ((k, getattr(self, k)) for k in self.keys())

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def __str__(self) -> str:
        """Backward-compat: str(intel) returns the Telegram text."""
        return self.telegram_text

    def __contains__(self, item: str) -> bool:
        """Backward-compat: 'text' in intel checks the Telegram text."""
        return item in self.telegram_text


# ── Helpers ────────────────────────────────────────────────────────────────


def _safe(val, default=0):
    return safe_num(val, default)


def _norm_symbol(s: str | None) -> str:
    """Normalize chart/option-chain symbols for loose matching."""
    if not s:
        return ""
    x = str(s).upper().strip()
    x = re.sub(r"^(NSE|NFO|BSE|MCX|CDS):", "", x)
    x = x.replace("!", "")
    x = re.sub(
        r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4}(FUT)?$", "", x
    )
    return re.sub(r"[^A-Z0-9]", "", x)


def _select_chart_payload(chart_data, symbol: str) -> dict:
    """Accept direct timeframe payload or symbol-keyed chart cache."""
    if not isinstance(chart_data, dict):
        return {}
    tf_keys = {"1h", "3h", "4h", "1d", "15m", "30m", "5m"}
    if any(str(k).lower() in tf_keys for k in chart_data.keys()):
        return chart_data

    target = _norm_symbol(symbol)
    for key, value in chart_data.items():
        if isinstance(value, dict) and _norm_symbol(key) == target:
            return value
    return {}


def _tf_sort_key(tf: str) -> int:
    order = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "3h": 180, "4h": 240, "1d": 1440}
    return order.get(str(tf).lower(), 99999)


# ── Price × OI Matrix ─────────────────────────────────────────────────────
# This is the core F&O analysis framework:
#   Price ↑ + OI ↑ = Long Buildup  (Bullish)
#   Price ↑ + OI ↓ = Short Covering (Weak Bullish — shorts exiting, not fresh buying)
#   Price ↓ + OI ↑ = Short Buildup  (Bearish)
#   Price ↓ + OI ↓ = Long Unwinding (Weak Bearish — longs exiting, not fresh selling)


def _price_oi_verdict(
    price_pct: float | None,
    net_oi_change: int,
    ce_oi_change: int,
    pe_oi_change: int,
    pcr: float | None = None,
    alerts: list | None = None,
) -> tuple[str, str, str]:
    """
    Returns (verdict_label, emoji, trade_bias).
    Uses price × OI matrix as primary signal, with PCR and alert-level
    OI spikes as secondary override when price is flat.

    Fix v3.2: Flat price no longer defaults to Sideways when PCR or
    strike-level OI activity indicates clear directional positioning.
    """
    alerts = alerts or []
    p_pct = price_pct or 0
    price_up = p_pct > 0.05
    price_dn = p_pct < -0.05

    abs_ce = abs(ce_oi_change)
    abs_pe = abs(pe_oi_change)
    max_chg = max(abs_ce, abs_pe)

    # ── PRIMARY: Directional price + OI ────────────────────────────────────
    if price_up:
        # Check for contradictory Bearish options flow even though price is up
        if ce_oi_change > 0:
            is_contradictory = False
            if pe_oi_change > 0 and ce_oi_change > pe_oi_change * 1.5:
                is_contradictory = True
            elif pe_oi_change <= 0:
                has_ce_spike = any(
                    a.get("severity") == "HIGH"
                    and a.get("alert_type")
                    in ("OI_SPIKE", "BUILDUP_CLASSIFY", "OTM_UNUSUAL")
                    and a.get("option_type") == "CE"
                    for a in alerts
                )
                if (pcr is not None and pcr <= 0.85) or has_ce_spike:
                    is_contradictory = True
            if is_contradictory:
                return (
                    "Call Writing",
                    "🔴",
                    "Bearish — resistance building / call writing dominant despite price tick",
                )

        if pe_oi_change > 0 and (ce_oi_change <= 0 or abs_pe > abs_ce * 2):
            return "Long Buildup", "🟢", "Bullish — fresh longs / heavy put writing"
        if ce_oi_change < 0 and (pe_oi_change >= 0 or abs_ce > abs_pe * 2):
            return "Short Covering", "🟡", "Weak Bullish — shorts exiting"
        if ce_oi_change > 0 and pe_oi_change <= 0:
            # Symmetrically, only treat as Long Buildup if PCR isn't highly bearish
            if pcr is not None and pcr <= 0.85:
                pass
            else:
                return "Long Buildup", "🟢", "Bullish — fresh longs / call buying"

        # Only return the default directional verdict if the price move is substantial (>0.15%)
        # or if the options flow is completely flat/minor. Otherwise, fall through to check other indicators.
        if p_pct > 0.15 or (ce_oi_change == 0 and pe_oi_change == 0):
            return "Long Buildup", "🟢", "Bullish — upward price trend dominant"

    if price_dn:
        # Check for contradictory Bullish options flow even though price is down
        if pe_oi_change > 0:
            is_contradictory = False
            if ce_oi_change > 0 and pe_oi_change > ce_oi_change * 1.5:
                is_contradictory = True
            elif ce_oi_change <= 0:
                has_pe_spike = any(
                    a.get("severity") == "HIGH"
                    and a.get("alert_type")
                    in ("OI_SPIKE", "BUILDUP_CLASSIFY", "OTM_UNUSUAL")
                    and a.get("option_type") == "PE"
                    for a in alerts
                )
                if (pcr is not None and pcr >= 1.25) or has_pe_spike:
                    is_contradictory = True
            if is_contradictory:
                return (
                    "Put Writing",
                    "🟢",
                    "Bullish — support building / put writing dominant despite price dip",
                )

        if ce_oi_change > 0 and (pe_oi_change <= 0 or abs_ce > abs_pe * 2):
            return "Short Buildup", "🔴", "Bearish — fresh shorts / heavy call writing"
        if pe_oi_change < 0 and (ce_oi_change >= 0 or abs_pe > abs_ce * 2):
            return "Long Unwinding", "🟠", "Weak Bearish — longs exiting"
        if pe_oi_change > 0 and ce_oi_change <= 0:
            if pcr is not None and pcr >= 1.25:
                pass
            else:
                return "Short Buildup", "🔴", "Bearish — fresh shorts / put buying"
        # Both building but PE heavily dominates: price dip is noise / short-term pullback.
        # Symmetric to price_up line 131 (abs_pe > abs_ce * 2 → Long Buildup).
        if pe_oi_change > 0 and ce_oi_change > 0 and abs_pe > abs_ce * 3:
            return (
                "Put Writing",
                "🟢",
                "Cautious Bullish — PE-heavy buildup despite price dip",
            )
        # PCR extreme override: check even when price is slightly down.
        # Prevents a tiny -0.05% dip from masking a PCR 2.0+ or 0.5- regime.
        if pcr is not None:
            high_ce_spikes_dn = sum(
                1
                for a in alerts
                if a.get("severity") == "HIGH"
                and a.get("alert_type")
                in ("OI_SPIKE", "BUILDUP_CLASSIFY", "OTM_UNUSUAL")
                and a.get("option_type") == "CE"
            )
            if pcr >= 1.5 and high_ce_spikes_dn >= 1:
                return (
                    "OI Bias Bullish",
                    "🟡",
                    "Cautious Bullish — PCR very high, CE accumulating despite dip",
                )

        # Only return the default directional verdict if the price move is substantial (<-0.15%)
        # or if the options flow is completely flat/minor. Otherwise, fall through to check other indicators.
        if p_pct < -0.15 or (ce_oi_change == 0 and pe_oi_change == 0):
            return "Short Buildup", "🔴", "Bearish — downward price trend dominant"

    # ── SECONDARY: Flat price — check OI dominance (relaxed ratio) ─────────
    if max_chg > 0:
        if pe_oi_change > 0 and (ce_oi_change <= 0 or abs_pe > abs_ce * 1.5):
            return "Put Writing", "🟢", "Bullish — support building"
        if ce_oi_change > 0 and (pe_oi_change <= 0 or abs_ce > abs_pe * 1.5):
            return "Call Writing", "🔴", "Bearish — resistance building"

    # ── TERTIARY: PCR override when price is flat but sentiment is clear ────
    # PCR > 1.3 with HIGH CE OI spikes = smart money positioning bullish
    # PCR < 0.7 with HIGH PE OI spikes = smart money positioning bearish
    if pcr is not None:
        high_ce_spikes = sum(
            1
            for a in alerts
            if a.get("severity") == "HIGH"
            and a.get("alert_type") in ("OI_SPIKE", "BUILDUP_CLASSIFY", "OTM_UNUSUAL")
            and a.get("option_type") == "CE"
        )
        high_pe_spikes = sum(
            1
            for a in alerts
            if a.get("severity") == "HIGH"
            and a.get("alert_type") in ("OI_SPIKE", "BUILDUP_CLASSIFY", "OTM_UNUSUAL")
            and a.get("option_type") == "PE"
        )
        # Bullish override: PCR protective of downside + CE buildup by smart money
        if pcr >= 1.25 and high_ce_spikes >= 1:
            return (
                "OI Bias Bullish",
                "🟡",
                "Cautious Bullish — PCR supportive, CE OI accumulating",
            )
        # Bearish override: low PCR + PE buildup signals
        if pcr <= 0.80 and high_pe_spikes >= 1:
            return (
                "OI Bias Bearish",
                "🟠",
                "Cautious Bearish — PCR weak, PE OI accumulating",
            )
        # Pure PCR signal — but ONLY when the relevant side is actually BUILDING.
        # High PCR while PE is unwinding is NOT put writing; it is stale OI + exit.
        if pcr >= 1.5 and pe_oi_change > 0:
            return "Put Writing", "🟢", "Bullish — heavy put writing, strong support"
        if pcr <= 0.60 and ce_oi_change > 0:
            return (
                "Call Writing",
                "🔴",
                "Bearish — heavy call writing, strong resistance",
            )

    # Mass two-sided unwinding = position squaring / expiry, NOT a setup.
    if ce_oi_change < 0 and pe_oi_change < 0:
        return "Sideways", "⚪", "Neutral — both sides unwinding (squaring/expiry)"

    return "Sideways", "⚪", "Neutral — mixed signals or rangebound"


# ── Confidence Scorer ─────────────────────────────────────────────────────

# ── Alert Direction Classifier ─────────────────────────────────────────────


def _get_alert_direction(a: dict) -> str:
    """Classifies an alert as BULLISH, BEARISH, or NEUTRAL."""
    atype = a.get("alert_type", "")
    ot = a.get("option_type", "")

    detail = {}
    try:
        detail_raw = a.get("detail_json") or "{}"
        detail = (
            json.loads(detail_raw)
            if isinstance(detail_raw, str)
            else (detail_raw or {})
        )
    except Exception:
        pass

    if atype == "BUILDUP_CLASSIFY":
        bt = detail.get("buildup_type", "")
        if "Long Buildup" in bt or "Short Covering" in bt:
            return "BULLISH"
        if "Short Buildup" in bt or "Long Unwinding" in bt:
            return "BEARISH"

    if atype == "OI_SPIKE":
        if ot == "PE":
            return "BULLISH"
        if ot == "CE":
            return "BEARISH"

    if atype == "OI_UNWIND":
        if ot == "CE":
            return "BULLISH"
        if ot == "PE":
            return "BEARISH"

    if atype == "OTM_UNUSUAL":
        if ot == "PE":
            return "BULLISH"
        if ot == "CE":
            return "BEARISH"

    if atype == "ATM_LEG_MOVE":
        bias = str(detail.get("bias") or "")
        if "Bullish" in bias:
            return "BULLISH"
        if "Bearish" in bias:
            return "BEARISH"

    if atype == "VOLUME_AGGRESSION":
        if ot == "PE":
            return "BULLISH"
        if ot == "CE":
            return "BEARISH"

    if atype == "PRICE_SPIKE":
        direction = str(detail.get("direction") or "").upper()
        if direction == "UP":
            return "BULLISH"
        if direction == "DOWN":
            return "BEARISH"

    if atype == "PCR_VELOCITY":
        direction = str(detail.get("direction") or "").lower()
        if direction == "rising":
            return "BULLISH"
        if direction == "falling":
            return "BEARISH"

    if atype == "PCR_SHIFT":
        pcr_delta = float(detail.get("pcr_delta") or 0)
        if pcr_delta > 0:
            return "BULLISH"
        if pcr_delta < 0:
            return "BEARISH"

    return "NEUTRAL"


# ── Confidence Scorer ─────────────────────────────────────────────────────


def _compute_confidence(
    scan_ctx: dict,
    alerts: list[dict],
    parsed_chart: dict | None = None,
    verdict_label: str | None = None,
) -> tuple[int, bool]:
    """
    Score 0–100 based on signal confluence.
    Base 10, +15 for HIGH severity, +10 for PCR confluence, +10 for levels.
    """
    score = 10  # base

    price_pct = _safe(scan_ctx.get("price_change_pct"))
    ce_chg = scan_ctx.get("ce_oi_change", 0)
    pe_chg = scan_ctx.get("pe_oi_change", 0)
    pcr = _safe(scan_ctx.get("pcr"))

    # Determine verdict bias if label provided
    verdict_bias = "NEUTRAL"
    if verdict_label:
        if is_bullish(verdict_label):
            verdict_bias = "BULLISH"
        elif is_bearish(verdict_label):
            verdict_bias = "BEARISH"

    # Alert severity weighting (Aggressive).
    # Unwinding alerts are EXITS, not fresh conviction — weight them at ~40%.
    for a in alerts:
        sev = a.get("severity", "LOW")
        is_unwind = a.get("alert_type") == "OI_UNWIND" or (
            a.get("alert_type") == "BUILDUP_CLASSIFY"
            and "Unwinding" in (a.get("detail_json") or "")
        )

        weight = 8 if is_unwind else 20
        if sev == "MEDIUM":
            weight = 4 if is_unwind else 10
        elif sev == "LOW":
            weight = 0

        if weight > 0:
            alert_dir = _get_alert_direction(a)
            if verdict_bias != "NEUTRAL" and alert_dir != "NEUTRAL":
                if alert_dir == verdict_bias:
                    score += weight
                else:
                    score -= weight  # subtract points when alerts contradict
            else:
                score += weight

    # PCR confirmation for directional momentum
    if pcr and pcr < 0.75 and price_pct and price_pct > 0:
        score += 15  # Strong Bullish Confluence
    elif pcr and pcr > 1.25 and price_pct and price_pct < 0:
        score += 15  # Strong Bearish Confluence

    # OI wall proximity (Support/Resistance respect)
    underlying = _safe(scan_ctx.get("underlying"))
    support = _safe(scan_ctx.get("support"))
    resistance = _safe(scan_ctx.get("resistance"))
    if underlying and support and resistance:
        total_range = resistance - support
        if total_range > 0:
            dist_to_support = underlying - support
            # Bouncing off support?
            if dist_to_support < total_range * 0.15 and price_pct and price_pct > 0:
                score += 15
            dist_to_resistance = resistance - underlying
            # Rejecting from resistance?
            if dist_to_resistance < total_range * 0.15 and price_pct and price_pct < 0:
                score += 15

    # Max pain gravity
    max_pain = _safe(scan_ctx.get("max_pain"))
    if underlying and max_pain:
        mp_dist_pct = abs(underlying - max_pain) / underlying * 100
        if mp_dist_pct < 0.4:
            score += 10

    # Chart timeframe conflict detection
    # Detects when 1H and 3H have opposite non-NEUTRAL sentiments
    chart_conflict = False

    if isinstance(parsed_chart, dict):
        # Extract 1h and 3h sentiments
        h1_sent = parsed_chart.get("1h", {}).get("sentiment", "NEUTRAL")
        h3_sent = parsed_chart.get("3h", {}).get("sentiment", "NEUTRAL")

        # Conflict: both non-NEUTRAL and opposite directions
        if h1_sent != "NEUTRAL" and h3_sent != "NEUTRAL" and h1_sent != h3_sent:
            chart_conflict = True

        # Chart alignment boost: +10 if chart sentiment matches verdict direction
        if verdict_bias != "NEUTRAL":
            for tf_key in sorted(parsed_chart.keys()):
                tf_sent = parsed_chart[tf_key].get("sentiment", "NEUTRAL")
                if (verdict_bias == "BULLISH" and tf_sent == "BULLISH") or (
                    verdict_bias == "BEARISH" and tf_sent == "BEARISH"
                ):
                    score += 10
                    break

    alert_types = [a.get("alert_type") for a in alerts]
    if alerts and alert_types.count("VOLUME_AGGRESSION") / max(len(alerts), 1) >= 0.70:
        directional_types = {
            "BUILDUP_CLASSIFY",
            "OI_SPIKE",
            "OI_UNWIND",
            "ATM_LEG_MOVE",
        }
        directional_count = sum(1 for t in alert_types if t in directional_types)
        if directional_count <= max(2, len(alerts) * 0.15):
            score = min(score, 88)

    # Cap: Sideways verdict should never print 90%+ confidence — contradictory
    if score > 65:
        # Re-derive verdict to check if it's sideways/neutral
        abs_ce = abs(scan_ctx.get("ce_oi_change", 0))
        abs_pe = abs(scan_ctx.get("pe_oi_change", 0))
        p_pct = scan_ctx.get("price_change_pct") or 0
        is_flat_price = abs(p_pct) <= 0.05
        no_dominant_oi = (
            abs_ce > 0
            and abs_pe > 0
            and max(abs_ce, abs_pe) < min(abs_ce, abs_pe) * 1.5
        )
        if is_flat_price and no_dominant_oi:
            score = min(score, 65)  # Flat price + balanced OI → cap confidence

    # Squaring guard: if most alerts are unwinds AND both sides shrinking,
    # this is not a high-conviction directional scan. Cap hard.
    if alerts:
        unwinds = sum(
            1
            for a in alerts
            if a.get("alert_type") == "OI_UNWIND"
            or (
                a.get("alert_type") == "BUILDUP_CLASSIFY"
                and "Unwinding" in (a.get("detail_json") or "")
            )
        )
        unwind_ratio = unwinds / len(alerts)
        both_shrinking = ce_chg < 0 and pe_chg < 0
        if unwind_ratio >= 0.7 and both_shrinking:
            score = min(score, 45)

    return min(max(score, 0), 98), chart_conflict


# ── Trade Idea Generator ──────────────────────────────────────────────────


def _generate_trade_idea(
    verdict_label: str,
    scan_ctx: dict,
    alerts: list[dict],
    parsed_chart: dict | None = None,
) -> str:
    """
    Produce a specific, actionable trade suggestion.
    Always advisory — never commanding.
    """
    underlying = _safe(scan_ctx.get("underlying"))
    atm = _safe(scan_ctx.get("atm_strike"))
    support = _safe(scan_ctx.get("support"))
    resistance = _safe(scan_ctx.get("resistance"))
    max_pain = _safe(scan_ctx.get("max_pain"))
    straddle = _safe(scan_ctx.get("straddle_premium"))
    pcr = _safe(scan_ctx.get("pcr"))

    # Check for IV spike in current alerts — affects strategy choice
    has_iv_spike = any(a.get("alert_type") == "IV_SPIKE" for a in alerts)
    has_iv_crush = any(a.get("alert_type") == "IV_CRUSH" for a in alerts)

    idea_parts = []



    if verdict_label == "OI Bias Bullish":
        idea_parts.append("📗 *Bias: Cautious Bullish (OI-driven)*")
        idea_parts.append(
            "PCR supportive + HIGH CE OI spikes — smart money positioning"
        )
        idea_parts.append(
            "Strategy: Wait for trigger candle. Buy ATM CE on breakout, or Sell OTM PE if theta play."
        )
        if resistance:
            idea_parts.append(f"Entry trigger: Close above {resistance:.0f}")
        if support:
            idea_parts.append(f"SL zone: Below {support:.0f}")

    elif verdict_label == "OI Bias Bearish":
        idea_parts.append("📕 *Bias: Cautious Bearish (OI-driven)*")
        idea_parts.append("PCR weak + HIGH PE OI spikes — smart money positioning")
        idea_parts.append(
            "Strategy: Wait for trigger candle. Buy ATM PE on breakdown, or Sell OTM CE if theta play."
        )
        if support:
            idea_parts.append(f"Entry trigger: Close below {support:.0f}")
        if resistance:
            idea_parts.append(f"SL zone: Above {resistance:.0f}")

    elif verdict_label == "Long Buildup":
        idea_parts.append("📗 *Bias: Bullish*")
        if has_iv_crush:
            idea_parts.append("Consider: Buy ATM/OTM CE (IV low, cheaper entry)")
        elif has_iv_spike:
            idea_parts.append("Consider: Bull Call Spread (IV high, cap vega risk)")
        else:
            idea_parts.append("Consider: Buy CE near ATM or Sell PE at support")
        if support:
            idea_parts.append(f"SL zone: Below {support:.0f}")

    elif verdict_label == "Short Buildup":
        idea_parts.append("📕 *Bias: Bearish*")
        if has_iv_crush:
            idea_parts.append("Consider: Buy ATM/OTM PE (IV low, cheaper entry)")
        elif has_iv_spike:
            idea_parts.append("Consider: Bear Put Spread (IV high, cap vega risk)")
        else:
            idea_parts.append("Consider: Buy PE near ATM or Sell CE at resistance")
        if resistance:
            idea_parts.append(f"SL zone: Above {resistance:.0f}")

    elif verdict_label == "Short Covering":
        idea_parts.append("📒 *Bias: Cautious Bullish*")
        idea_parts.append("Rally driven by exit, not fresh buying")
        idea_parts.append("Consider: Avoid fresh longs — trail existing positions")
        if resistance:
            idea_parts.append(f"Watch resistance: {resistance:.0f}")

    elif verdict_label == "Long Unwinding":
        idea_parts.append("📙 *Bias: Cautious Bearish*")
        idea_parts.append("Decline from long exit, not aggressive shorts")
        idea_parts.append(
            "Consider: Avoid fresh shorts — wait for OI buildup confirmation"
        )
        if support:
            idea_parts.append(f"Watch support: {support:.0f}")

    elif "Expansion" in verdict_label:
        idea_parts.append("📘 *Bias: Breakout Expected*")
        if straddle > 0 and atm:
            idea_parts.append(f"Straddle premium: {straddle:.0f}")
            idea_parts.append("Consider: Long Straddle/Strangle if expecting big move")
        if support and resistance:
            idea_parts.append(f"Range: {support:.0f}–{resistance:.0f}")

    elif "Contraction" in verdict_label:
        idea_parts.append("📘 *Bias: Range Decay*")
        idea_parts.append("Consider: Short Straddle/Strangle (collect premium decay)")
        if support and resistance:
            idea_parts.append(f"Expected range: {support:.0f}–{resistance:.0f}")

    else:
        idea_parts.append("📘 *Bias: Neutral — Wait & Watch*")
        idea_parts.append("No clear edge — sit on hands or scalp only")

    # Risk note
    risk = _generate_risk_note(verdict_label, scan_ctx)
    if risk:
        idea_parts.append(
            f"⚠️ _{risk}_"
        )  # Keep this, but ensure it's not redundant with confidence-based advice

    return "\n".join(idea_parts)


def _generate_risk_note(verdict: str, ctx: dict) -> str:
    """One-line risk invalidation note."""
    support = _safe(ctx.get("support"))
    resistance = _safe(ctx.get("resistance"))
    max_pain = _safe(ctx.get("max_pain"))
    underlying = _safe(ctx.get("underlying"))

    if verdict == "Long Buildup":
        if support:
            return f"Thesis invalid if spot breaks below {support:.0f}"
        return "Thesis invalid if OI unwinds sharply"

    if verdict == "Short Buildup":
        if resistance:
            return f"Thesis invalid if spot breaks above {resistance:.0f}"
        return "Thesis invalid if short covering triggers"

    if verdict == "Short Covering":
        return "Caution: Rally may stall once short covering exhausts"

    if verdict == "Long Unwinding":
        return "Caution: Decline may pause once weak longs exit"

    if verdict == "Put Writing":
        return "Caution: Support weakens if put writing exits"

    if verdict == "Call Writing":
        return "Caution: Resistance weakens if call writing exits"

    if "Expansion" in verdict:
        return "Breakout direction unclear — wait for confirmation candle"

    return ""


def _compute_dynamic_action_plan(
    v_label: str, tf_1h: str | None, tf_3h: str | None, conflict: bool
) -> str:
    """Compute action plan dynamically based on verdict."""
    if v_label in ("Long Buildup", "Short Covering", "Put Writing"):
        return "Trail SL on longs. Avoid blind chase."
    elif v_label in ("Short Buildup", "Long Unwinding", "Call Writing"):
        return "Trail SL on shorts. Avoid panic entry."
    elif v_label == "OI Bias Bullish":
        return "Buy CE on breakout confirmation."
    elif v_label == "OI Bias Bearish":
        return "Buy PE on breakdown confirmation."

    return "No aggressive trade. Wait for clean setup."


def _factor_priority(score: int) -> str:
    if score >= 90:
        return "P1"
    if score >= 70:
        return "P2"
    return "P3"


def _collect_forces(
    ctx: dict, alerts: list[dict], verdict_label: str, parsed_chart: dict
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    bull: list[tuple[int, str]] = []
    bear: list[tuple[int, str]] = []

    pcr = _safe(ctx.get("pcr"), 0)
    price_pct = _safe(ctx.get("price_change_pct"), 0)
    ce_oi_change = _safe(ctx.get("ce_oi_change"), 0)
    pe_oi_change = _safe(ctx.get("pe_oi_change"), 0)

    if pcr >= 1.15:
        bull.append((85, f"PCR supportive ({pcr:.2f})"))
    elif pcr > 0 and pcr <= 0.85:
        bear.append((85, f"PCR weak ({pcr:.2f})"))

    if price_pct >= 0.35:
        bull.append((80, f"Spot momentum +{price_pct:.2f}%"))
    elif price_pct <= -0.35:
        bear.append((80, f"Spot momentum {price_pct:.2f}%"))

    if pe_oi_change > 0 and ce_oi_change <= 0:
        bull.append((75, "Put writing visible"))
    if ce_oi_change > 0 and pe_oi_change <= 0:
        bear.append((75, "Call writing visible"))

    if verdict_label in (
        "Long Buildup",
        "Short Covering",
        "Put Writing",
        "OI Bias Bullish",
    ):
        bull.append((88, f"Price x OI verdict: {verdict_label}"))
    elif verdict_label in (
        "Short Buildup",
        "Long Unwinding",
        "Call Writing",
        "OI Bias Bearish",
    ):
        bear.append((88, f"Price x OI verdict: {verdict_label}"))

    bull = sorted(bull, key=lambda x: x[0], reverse=True)
    bear = sorted(bear, key=lambda x: x[0], reverse=True)
    return bull[:5], bear[:5]


def _paper_trade_idea(verdict_label: str, ctx: dict) -> str:
    confidence = int(_safe(ctx.get("confidence"), 0))
    plan = build_paper_trade_plan(verdict_label, confidence, ctx)
    if plan:
        msg = format_paper_plan(plan)
        if ctx.get("days_to_expiry") == 0:
            msg += "\n⚠️ Expiry Day: strictly intraday."
        elif ctx.get("days_to_expiry") in (1, 2):
            msg += "\n⚠️ Low DTE: prefer closer strikes or short/hedged structures."
        return msg
    if verdict_label in {"Put Writing", "Call Writing"}:
        return "No auto paper trade: option writing is not enabled"
    if verdict_label == "Short Covering":
        return "No auto paper trade: short covering is trail-only"
    if verdict_label == "Long Unwinding":
        return "No auto paper trade: long unwinding is trail-only"
    if "Expansion" in verdict_label:
        return "No auto paper trade: breakout direction unconfirmed"
    if "Contraction" in verdict_label:
        return "No auto paper trade: range decay needs hedged strategy"
    return "No auto paper trade: wait for cleaner alignment"


# ── Broader Trend from History ─────────────────────────────────────────────


def _compute_broader_trend(symbol: str, alerts: list[dict]) -> str:
    """
    Analyze last 50 alerts for the symbol to determine multi-scan trend.
    """
    history = get_alert_history(symbol, limit=50)
    merged = list(history or [])
    if alerts:
        merged.extend(alerts)
    if not merged:
        return "Insufficient history - first scan"

    # Count buildup types from BUILDUP_CLASSIFY alerts
    long_buildups = 0
    short_buildups = 0
    long_unwinds = 0
    short_covers = 0
    oi_spikes_ce = 0
    oi_spikes_pe = 0
    vol_aggr_ce = 0
    vol_aggr_pe = 0
    atm_bull = 0
    atm_bear = 0

    for h in merged:
        row = dict(h) if not isinstance(h, dict) else h
        atype = row.get("alert_type", "")
        ot = row.get("option_type", "")
        detail = {}
        try:
            detail_raw = row.get("detail_json") or "{}"
            detail = (
                json.loads(detail_raw)
                if isinstance(detail_raw, str)
                else (detail_raw or {})
            )
        except Exception:
            pass

        if atype == "BUILDUP_CLASSIFY":
            bt = detail.get("buildup_type", "")
            if "Long Buildup" in bt:
                long_buildups += 1
            elif "Short Buildup" in bt:
                short_buildups += 1
            elif "Long Unwinding" in bt:
                long_unwinds += 1
            elif "Short Covering" in bt:
                short_covers += 1

        if atype == "OI_SPIKE":
            if ot == "CE":
                oi_spikes_ce += 1
            else:
                oi_spikes_pe += 1
        if atype == "VOLUME_AGGRESSION":
            if ot == "CE":
                vol_aggr_ce += 1
            elif ot == "PE":
                vol_aggr_pe += 1
        if atype == "ATM_LEG_MOVE":
            bias = str(detail.get("bias") or "")
            if "Bullish" in bias:
                atm_bull += 1
            elif "Bearish" in bias:
                atm_bear += 1

    # Decision logic
    bull_score = long_buildups + short_covers + oi_spikes_pe + vol_aggr_pe + atm_bull
    bear_score = short_buildups + long_unwinds + oi_spikes_ce + vol_aggr_ce + atm_bear
    active_flow = vol_aggr_ce + vol_aggr_pe

    if bear_score >= 8 and bear_score > bull_score * 2:
        return "🔴 Strong Bearish Trend — persistent call writing + short buildup"
    if bear_score >= 5 and bear_score > bull_score * 1.5:
        return "🟠 Mild Bearish — resistance building, sellers active"
    if bull_score >= 8 and bull_score > bear_score * 2:
        return "🟢 Strong Bullish Trend — persistent put writing + long buildup"
    if bull_score >= 5 and bull_score > bear_score * 1.5:
        return "🟡 Mild Bullish — support building, buyers active"
    if active_flow >= 10:
        return "⚪ High Activity — aggressive flow on both sides"
    if oi_spikes_ce > 3 and oi_spikes_pe > 3 and abs(oi_spikes_ce - oi_spikes_pe) <= 2:
        return "⚪ Rangebound — balanced OI activity on both sides"
    if bull_score + bear_score < 3:
        return "⚪ Low Activity — insufficient signals for trend"

    return "⚪ Mixed — no dominant trend yet"


# ── Public API ─────────────────────────────────────────────────────────────


def _parse_chart_indicators(raw_indicators) -> dict:
    """
    Normalise the scraper's list-of-objects indicator format into a lookup dict.

    Scraper (tv_content.js v15) stores indicators as:
        [{"name": "SuperTrend 10,3", "sentiment": "BULLISH"}, ...]

    We convert this to:
        {"supertrend": {"sentiment": "BULLISH"}, "rsi": {"value": None}, ...}

    Also accepts the legacy dict format transparently.
    """
    if isinstance(raw_indicators, dict):
        return raw_indicators  # already in legacy format — pass through

    result: dict = {}
    if not isinstance(raw_indicators, list):
        return result

    for item in raw_indicators:
        if not isinstance(item, dict):
            continue
        raw_name = (item.get("name") or "").lower().strip()
        sentiment = item.get("sentiment", "NEUTRAL")
        value = item.get("value")  # numeric value if present (e.g. RSI=67.2)

        if "supertrend" in raw_name:
            result["supertrend"] = {
                "sentiment": sentiment,
                "raw_name": item.get("name"),
            }
        elif "rsi" in raw_name:
            result["rsi"] = value  # plain float expected downstream
        elif "macd" in raw_name:
            result["macd"] = {"sentiment": sentiment}
        elif "ema" in raw_name or "sma" in raw_name:
            result.setdefault("ma", {"sentiment": sentiment})
        else:
            # Generic fallback — store by normalised key
            key = raw_name.replace(" ", "_")[:20]
            if key:
                result[key] = {"sentiment": sentiment}

    return result


def _get_trading_mode_indicator() -> str:
    try:
        from config.settings import PAPER_RESEARCH_MODE, TREND_FILTER_MODE

        mode_emoji = {
            "conservative": "🛡️",
            "balanced": "⚖️",
            "aggressive": "⚡",
            "hybrid": "🎯",
        }.get(TREND_FILTER_MODE, "📊")
        research_tag = " [RESEARCH]" if PAPER_RESEARCH_MODE else ""
        return f"_{mode_emoji} Mode: {TREND_FILTER_MODE.title()}{research_tag}_"
    except Exception:
        return ""


def generate_intelligence(
    symbol: str,
    current_alerts: list[dict],
    scan_context: dict | None = None,
    ai_verdict=None,
) -> "IntelligenceResult":
    """
    Analyzes scan context + current alerts to generate trade-actionable intelligence.

    Phase 3: Returns IntelligenceResult dataclass natively.
    All structured fields (verdict_label, confidence, bias, chart_conflict, trend)
    are set directly — no regex parsing needed downstream.

    Backward-compat: callers that used the return value as a plain string
    should switch to result.telegram_text. The IntelligenceResult.__str__
    returns telegram_text for any remaining legacy str() usage.
    """
    current_alerts = current_alerts or []

    ctx = scan_context or {}
    underlying = _safe(ctx.get("underlying"))
    price_pct = ctx.get("price_change_pct")
    total_ce_oi = ctx.get("total_ce_oi", 0)
    total_pe_oi = ctx.get("total_pe_oi", 0)
    ce_oi_change = ctx.get("ce_oi_change", 0)
    pe_oi_change = ctx.get("pe_oi_change", 0)
    net_oi_change = ce_oi_change + pe_oi_change
    pcr = ctx.get("pcr")
    max_pain = ctx.get("max_pain")
    support = ctx.get("support")
    resistance = ctx.get("resistance")
    straddle = _safe(ctx.get("straddle_premium"))
    expiry = ctx.get("expiry", "")

    days_to_expiry = -1
    if expiry:
        try:
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            today_date = datetime.now(timezone.utc).date()
            days_to_expiry = (exp_date - today_date).days
        except Exception:
            pass

    # ── Price × OI Verdict ─────────────────────────────────────────────────
    verdict_label, verdict_emoji, verdict_desc = _price_oi_verdict(
        price_pct,
        net_oi_change,
        ce_oi_change,
        pe_oi_change,
        pcr=pcr,
        alerts=current_alerts,
    )

    # ── Chart Confluence Confidence Boost ──────────────────────────────────
    # Must run BEFORE building msg so the printed Confidence% is accurate.
    chart_data = _select_chart_payload(ctx.get("chart_indicators"), symbol)
    parsed_chart: dict[str, dict] = {}  # tf -> {"sentiment", "indicators_dict"}
    if isinstance(chart_data, dict):
        for tf, tf_data in chart_data.items():
            if not isinstance(tf_data, dict):
                continue
            pane_sentiment = tf_data.get("sentiment", "NEUTRAL")
            ind_dict = _parse_chart_indicators(tf_data.get("indicators", []))
            # SuperTrend overrides pane sentiment if present
            st = ind_dict.get("supertrend", {})
            effective_sentiment = st.get("sentiment") if st else pane_sentiment
            parsed_chart[tf] = {
                "sentiment": effective_sentiment,
                "ind_dict": ind_dict,
                "ohlc": tf_data.get("ohlc"),
                "updated_at": tf_data.get("updated_at"),
            }

    # ── Compute confidence (with chart confluence boost) ──────────────────────
    confidence, chart_conflict = _compute_confidence(
        ctx, current_alerts, parsed_chart=parsed_chart, verdict_label=verdict_label
    )

    # ── Build Message ──────────────────────────────────────────────────────
    # H5 fix: apply ALL confidence ceilings in one pass after the confluence boost.
    # Previously only volume-dominant (88) and chart-conflict (85) were re-applied
    # here; flat-price (65) and squaring-guard (45) caps from _compute_confidence
    # were lost when the boost pushed above them.
    alert_types = [a.get("alert_type") for a in current_alerts]
    ceiling = 98  # absolute max

    # Volume-dominant cap: mostly volume aggression, few directional signals
    if (
        current_alerts
        and alert_types.count("VOLUME_AGGRESSION") / max(len(current_alerts), 1) >= 0.70
    ):
        ceiling = min(ceiling, 88)

    # Chart conflict ceiling removed: see _compute_confidence notes.
    # Preserved: volume-dominant cap and flat-price/balanced-OI cap.

    # Flat price + balanced OI cap: no clear directional edge
    abs_ce = abs(ctx.get("ce_oi_change", 0))
    abs_pe = abs(ctx.get("pe_oi_change", 0))
    p_pct = ctx.get("price_change_pct") or 0
    is_flat_price = abs(p_pct) <= 0.05
    no_dominant_oi = (
        abs_ce > 0 and abs_pe > 0 and max(abs_ce, abs_pe) < min(abs_ce, abs_pe) * 1.5
    )
    if is_flat_price and no_dominant_oi:
        ceiling = min(ceiling, 65)

    # Squaring guard cap: mostly unwinds + both sides shrinking
    if current_alerts:
        ce_chg = ctx.get("ce_oi_change", 0)
        pe_chg = ctx.get("pe_oi_change", 0)
        unwinds = sum(
            1
            for a in current_alerts
            if a.get("alert_type") == "OI_UNWIND"
            or (
                a.get("alert_type") == "BUILDUP_CLASSIFY"
                and "Unwinding" in (a.get("detail_json") or "")
            )
        )
        unwind_ratio = unwinds / len(current_alerts)
        both_shrinking = (ce_chg < 0) and (pe_chg < 0)
        if unwind_ratio >= 0.7 and both_shrinking:
            ceiling = min(ceiling, 45)

    confidence = min(confidence, ceiling)

    # Derive bias from verdict (no regex — direct set membership)
    bias = (
        "BULLISH"
        if is_bullish(verdict_label)
        else ("BEARISH" if is_bearish(verdict_label) else "NEUTRAL")
    )

    if confidence < 50:
        telegram_text = "\n".join(
            [
                f"🤖 *Bot Intelligence | {symbol}*",
                # Add trading mode indicator here as well for consistency
                _get_trading_mode_indicator(),
                "",
                "⚪ *Verdict: Low Conviction*",
                "_No actionable edge — wait for alignment_",
                f"Confidence: {confidence}%",
            ]
        )
        return IntelligenceResult(
            symbol=symbol,
            verdict_label="Low Conviction",
            verdict_emoji="⚪",
            verdict_desc="No actionable edge",
            bias="NEUTRAL",
            confidence=confidence,
            chart_conflict=chart_conflict,
            trend="",
            telegram_text=telegram_text,
            expiry=expiry,
            days_to_expiry=days_to_expiry,
        )

    msg = [f"🤖 *Bot Intelligence | {symbol}*"]

    # Add trading mode indicator
    try:
        from config.settings import PAPER_RESEARCH_MODE, TREND_FILTER_MODE

        mode_emoji = {
            "conservative": "🛡️",
            "balanced": "⚖️",
            "aggressive": "⚡",
            "hybrid": "🎯",
        }.get(TREND_FILTER_MODE, "📊")
        research_tag = " [RESEARCH]" if PAPER_RESEARCH_MODE else ""
        msg.append(f"_{mode_emoji} Mode: {TREND_FILTER_MODE.title()}{research_tag}_")
    except Exception:
        pass

    # Market Stance
    msg.append(f"")
    msg.append(f"{verdict_emoji} *Verdict: {verdict_label}*")
    msg.append(f"_{verdict_desc}_")
    msg.append(f"Confidence: {confidence}%")
    if chart_conflict:
        msg.append(
            "💡 _1H vs 3H candles diverge — potential entry timing signal (not a conflict for OI trades)_"
        )

    # OI Analysis
    if total_ce_oi or total_pe_oi:
        msg.append(f"")
        msg.append(f"📊 *OI Analysis*")
        ce_arrow = "↑" if ce_oi_change > 0 else ("↓" if ce_oi_change < 0 else "→")
        pe_arrow = "↑" if pe_oi_change > 0 else ("↓" if pe_oi_change < 0 else "→")
        ce_chg_str = (
            f"+{fmt_oi(ce_oi_change)}"
            if ce_oi_change >= 0
            else f"-{fmt_oi(abs(ce_oi_change))}"
        )
        pe_chg_str = (
            f"+{fmt_oi(pe_oi_change)}"
            if pe_oi_change >= 0
            else f"-{fmt_oi(abs(pe_oi_change))}"
        )
        msg.append(f"CE OI: `{fmt_oi(total_ce_oi)}` {ce_arrow} ({ce_chg_str})")
        msg.append(f"PE OI: `{fmt_oi(total_pe_oi)}` {pe_arrow} ({pe_chg_str})")

        # Dominant writer interpretation
        if ce_oi_change > 0 and pe_oi_change > 0:
            smaller = min(abs(ce_oi_change), abs(pe_oi_change))
            larger = max(abs(ce_oi_change), abs(pe_oi_change))
            if larger > 0 and (smaller / larger) >= 0.25:
                dominant = "Both sides adding — volatility expansion"
            else:
                dominant = "One-sided heavy build — skewed positioning"
        elif ce_oi_change > 0 and pe_oi_change <= 0:
            dominant = "Writers adding calls — capping upside"
        elif pe_oi_change > 0 and ce_oi_change <= 0:
            dominant = "Writers adding puts — supporting downside"
        elif ce_oi_change < 0 and pe_oi_change < 0:
            dominant = "Both sides exiting — conviction dropping"
        else:
            dominant = "Balanced"
        msg.append(f"_{dominant}_")

    # Key Levels
    msg.append(f"")
    msg.append(f"📍 *Key Levels*")
    chart_spot = None
    if "1h" in parsed_chart:
        ohlc = parsed_chart.get("1h", {}).get("ohlc") or {}
        chart_spot = _safe(ohlc.get("close")) if ohlc else None
    spot_val = chart_spot or underlying
    is_commodity = symbol.upper().split()[0] in {
        "NATURALGAS",
        "CRUDEOIL",
        "GOLD",
        "SILVER",
    }
    lbl = "Future" if is_commodity else "Spot"
    fmt = ".2f" if is_commodity else ".0f"
    spot_str = f"{lbl}: `{spot_val:{fmt}}`" if spot_val else f"{lbl}: N/A"
    parts = [spot_str]
    if pcr is not None:
        parts.append(f"PCR: `{pcr:.2f}`")
    msg.append(" | ".join(parts))

    level_parts = []
    if support:
        level_parts.append(f"Support: `{support:.0f}`")
    if resistance:
        level_parts.append(f"Resistance: `{resistance:.0f}`")
    if max_pain:
        level_parts.append(f"MaxPain: `{max_pain:.0f}`")
    if level_parts:
        msg.append(" | ".join(level_parts))

    if straddle > 0:
        msg.append(f"ATM Straddle: `{straddle:.0f}` pts")

    # ── Chart Status ───────────────────────────────────────────────────────
    # parsed_chart was built earlier (before confidence print) to allow boost.
    if parsed_chart:
        msg.append(f"")
        msg.append(f"📉 *Chart Status*")
        for tf in sorted(
            parsed_chart.keys(), key=_tf_sort_key
        ):  # deterministic timeframe order
            entry = parsed_chart[tf]
            sentiment = entry["sentiment"]
            ind_dict = entry["ind_dict"]
            rsi = ind_dict.get("rsi")  # float or None
            st = ind_dict.get("supertrend", {})

            tf_upper = tf.upper()
            if sentiment == "BULLISH":
                s_emoji = "🟢"
            elif sentiment == "BEARISH":
                s_emoji = "🔴"
            else:
                s_emoji = "⚪"

            tf_parts = [f"*{tf_upper}*: {s_emoji} {sentiment}"]

            ohlc = entry.get("ohlc")
            if ohlc and isinstance(ohlc, dict):
                o = _safe(ohlc.get("open"), None)
                h = _safe(ohlc.get("high"), None)
                l = _safe(ohlc.get("low"), None)
                c = _safe(ohlc.get("close"), None)
                if c is not None:
                    if o is not None and h is not None and l is not None:
                        # Momentum tag: price near high/low
                        m_tag = (
                            " 🔥"
                            if h and c >= h * 0.998
                            else (" ❄️" if l and c <= l * 1.002 else "")
                        )
                        price_str = f"🕯️ O:{o:.1f} H:{h:.1f} L:{l:.1f} C:{c:.1f}{m_tag}"
                    else:
                        price_str = f"🕯️ C:{c:.1f}"
                    tf_parts.append(price_str)

            if st:
                tf_parts.append("(ST)")

            if rsi is not None:
                try:
                    rsi_val = float(rsi)
                    rsi_note = (
                        " OB" if rsi_val > 70 else (" OS" if rsi_val < 30 else "")
                    )
                    tf_parts.append(f"RSI `{rsi_val:.1f}`{rsi_note}")
                except (TypeError, ValueError):
                    pass

            msg.append(" | ".join(tf_parts))

    bull_forces, bear_forces = _collect_forces(
        ctx, current_alerts, verdict_label, parsed_chart
    )

    msg.append("")
    msg.append("*BULL FORCES (Criticality Order)*")
    if bull_forces:
        for score, text in bull_forces:
            msg.append(f"- {_factor_priority(score)} [{score}] {text}")
    else:
        msg.append("- P3 [40] No strong bullish factor")

    msg.append("")
    msg.append("*BEAR FORCES (Criticality Order)*")
    if bear_forces:
        for score, text in bear_forces:
            msg.append(f"- {_factor_priority(score)} [{score}] {text}")
    else:
        msg.append("- P3 [40] No strong bearish factor")

    msg.append("")
    msg.append("*TRADE STRATEGY*")
    msg.append(f"- Bias: {verdict_desc}")
    action_plan = _compute_dynamic_action_plan(
        verdict_label, None, None, chart_conflict
    )
    msg.append(f"- Action Plan: {action_plan}")
    risk_note = _generate_risk_note(verdict_label, ctx)
    if risk_note:
        msg.append(f"- Critical Warning: {risk_note}")

    msg.append("")
    msg.append("*PAPER TRADE (Specific)*")
    paper_ctx = _ctx_copy(ctx)
    paper_ctx.update(
        symbol=symbol, confidence=confidence, days_to_expiry=days_to_expiry
    )
    msg.append(f"- {_paper_trade_idea(verdict_label, paper_ctx)}")

    # ── Phase 2-4: Decision Engine Status ──────────────────────────────────
    # Show trade decision analysis inline
    try:
        from src.engine.risk_engine import check_risk_limits
        from src.engine.trade_decision import make_trade_decision

        decision = None
        decision_ctx = _ctx_copy(ctx)
        decision_ctx.update(symbol=symbol, expiry=ctx.get("expiry", ""))
        decision = make_trade_decision(
            symbol,
            {
                "verdict_label": verdict_label,
                "confidence": confidence,
                "chart_conflict": chart_conflict,
            },
            decision_ctx,
            ai_verdict=ai_verdict,
            suppress_logs=False,
        )

        risk_ok, risk_reason = check_risk_limits(symbol)

        msg.append("")
        msg.append("🎯 *TRADE DECISION ENGINE*")

        # Decision status with emoji
        status = decision.get("status", "BLOCKED")
        if status == "TRIGGERED_CORE":
            status_emoji = "✅"
            status_text = "APPROVED (Core Setup)"
        elif status == "TRIGGERED_EXPERIMENTAL":
            status_emoji = "🧪"
            status_text = "APPROVED (Experimental)"
        else:
            status_emoji = "❌"
            status_text = "BLOCKED"

        msg.append(f"Status: {status_emoji} *{status_text}*")

        # Setup type
        setup_type = decision.get("setup_type")
        if setup_type:
            setup_emoji = {
                "CONFIRMED_REVERSAL": "🔄",
                "TREND_CONTINUATION": "📈",
                "MOMENTUM_TRADE": "⚡",
                "EXPERIMENTAL_SETUP": "🧪",
            }.get(setup_type, "📊")
            msg.append(f"Setup: {setup_emoji} {setup_type.replace('_', ' ').title()}")

        # Scores (compact format)
        scores = decision.get("scores", {})
        if scores:
            score_parts = []
            if "confidence" in scores:
                score_parts.append(f"Conf:{scores['confidence']}%")
            if "entry_quality" in scores:
                eq = scores["entry_quality"]
                eq_emoji = "🟢" if eq >= 70 else ("🟡" if eq >= 50 else "🔴")
                score_parts.append(f"EQ:{eq_emoji}{eq}")
            if "trend_alignment" in scores:
                ta = scores["trend_alignment"]
                ta_emoji = "🟢" if ta >= 70 else ("🟡" if ta >= 50 else "🔴")
                score_parts.append(f"TA:{ta_emoji}{ta}")
            if "regime_score" in scores:
                rs = scores["regime_score"]
                rs_emoji = "🟢" if rs >= 70 else ("🟡" if rs >= 50 else "🔴")
                score_parts.append(f"Reg:{rs_emoji}{rs}")
            if "momentum_score" in scores:
                ms = scores["momentum_score"]
                ms_emoji = "🟢" if ms >= 75 else ("🟡" if ms >= 60 else "🔴")
                score_parts.append(f"Mom:{ms_emoji}{ms}")

            if score_parts:
                msg.append(f"Scores: {' | '.join(score_parts)}")

        # Decision reason (compact)
        reason = decision.get("reason", "")
        if reason and len(reason) < 100:
            msg.append(f"_{reason}_")

        # Risk engine status
        if not risk_ok:
            msg.append(f"⚠️ Risk Block: {risk_reason}")

        # Soft conflicts
        soft_conflicts = decision.get("soft_conflicts", [])
        if soft_conflicts:
            conflict_text = ", ".join(soft_conflicts)
            msg.append(f"⚠️ Conflicts: {conflict_text}")

    except Exception as e:
        log.warning("Failed to add decision engine status to telegram: %s", e)

    # Broader Trend
    trend = _compute_broader_trend(symbol, current_alerts)
    msg.append(f"")
    msg.append(f"🌊 *Broader Trend:* {trend}")

    msg.append(f"")
    msg.append(f"_Based on {len(current_alerts)} signals this scan_")

    return IntelligenceResult(
        symbol=symbol,
        verdict_label=verdict_label,
        verdict_emoji=verdict_emoji,
        verdict_desc=verdict_desc,
        bias=bias,
        confidence=confidence,
        chart_conflict=chart_conflict,
        trend=trend,
        bull_forces=bull_forces,
        bear_forces=bear_forces,
        action_plan=action_plan,
        risk_note=risk_note,
        telegram_text="\n".join(msg),
        expiry=expiry,
        days_to_expiry=days_to_expiry,
        trade_decision=decision,
    )


def generate_intelligence_structured(
    symbol: str,
    current_alerts: list[dict],
    scan_context: dict | None = None,
    ai_verdict=None,
) -> "IntelligenceResult":
    """
    Phase 3: Returns IntelligenceResult directly from generate_intelligence().
    Zero regex parsing. All fields are set natively inside generate_intelligence().

    Backward-compat: callers that used intel["verdict_label"] etc. still work
    because IntelligenceResult implements __getitem__ and get().
    """
    return generate_intelligence(
        symbol, current_alerts, scan_context, ai_verdict=ai_verdict
    )
