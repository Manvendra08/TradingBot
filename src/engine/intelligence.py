"""
Bot Intelligence Engine v3.1
Combines scan context (OI totals, price movement, PCR, max pain, OI walls)
with current alerts to produce trade-actionable intelligence.

Output: Telegram-formatted markdown block appended to digest.
"""
import json
import logging
import re
from datetime import datetime, timezone
from src.models.schema import get_alert_history
from src.utils.formatting import safe_num, fmt_oi

log = logging.getLogger(__name__)


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
    x = re.sub(r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4}(FUT)?$", "", x)
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

def _price_oi_verdict(price_pct: float | None, net_oi_change: int,
                      ce_oi_change: int, pe_oi_change: int,
                      pcr: float | None = None,
                      alerts: list | None = None) -> tuple[str, str, str]:
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
        if pe_oi_change > 0 and (ce_oi_change <= 0 or abs_pe > abs_ce * 2):
            return "Long Buildup", "🟢", "Bullish — fresh longs / heavy put writing"
        if ce_oi_change < 0 and (pe_oi_change >= 0 or abs_ce > abs_pe * 2):
            return "Short Covering", "🟡", "Weak Bullish — shorts exiting"

    if price_dn:
        if ce_oi_change > 0 and (pe_oi_change <= 0 or abs_ce > abs_pe * 2):
            return "Short Buildup", "🔴", "Bearish — fresh shorts / heavy call writing"
        if pe_oi_change < 0 and (ce_oi_change >= 0 or abs_pe > abs_ce * 2):
            return "Long Unwinding", "🟠", "Weak Bearish — longs exiting"

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
            1 for a in alerts
            if a.get("severity") == "HIGH"
            and a.get("alert_type") in ("OI_SPIKE", "BUILDUP_CLASSIFY", "OTM_UNUSUAL")
            and a.get("option_type") == "CE"
        )
        high_pe_spikes = sum(
            1 for a in alerts
            if a.get("severity") == "HIGH"
            and a.get("alert_type") in ("OI_SPIKE", "BUILDUP_CLASSIFY", "OTM_UNUSUAL")
            and a.get("option_type") == "PE"
        )
        # Bullish override: PCR protective of downside + CE buildup by smart money
        if pcr >= 1.25 and high_ce_spikes >= 1:
            return "OI Bias Bullish", "🟡", "Cautious Bullish — PCR supportive, CE OI accumulating"
        # Bearish override: low PCR + PE buildup signals
        if pcr <= 0.80 and high_pe_spikes >= 1:
            return "OI Bias Bearish", "🟠", "Cautious Bearish — PCR weak, PE OI accumulating"
        # Pure PCR signal (no conflicting OI)
        if pcr >= 1.5:
            return "Put Writing", "🟢", "Bullish — heavy put writing, strong support"
        if pcr <= 0.60:
            return "Call Writing", "🔴", "Bearish — heavy call writing, strong resistance"

    return "Sideways", "⚪", "Neutral — mixed signals or rangebound"


# ── Confidence Scorer ─────────────────────────────────────────────────────

def _compute_confidence(scan_ctx: dict, alerts: list[dict],
                        parsed_chart: dict | None = None) -> tuple[int, bool]:
    """
    Score 0–100 based on signal confluence.
    Base 10, +15 for HIGH severity, +10 for PCR confluence, +10 for levels.
    """
    score = 10  # base

    price_pct = _safe(scan_ctx.get("price_change_pct"))
    ce_chg = scan_ctx.get("ce_oi_change", 0)
    pe_chg = scan_ctx.get("pe_oi_change", 0)
    pcr = _safe(scan_ctx.get("pcr"))

    # Alert severity weighting (Aggressive)
    for a in alerts:
        sev = a.get("severity", "LOW")
        if sev == "HIGH":
            score += 20
        elif sev == "MEDIUM":
            score += 10

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
    chart_conflict = False
    if parsed_chart:
        tf_1h = parsed_chart.get("1h", {}).get("sentiment")
        tf_3h = parsed_chart.get("3h", {}).get("sentiment")
        if tf_1h and tf_3h and tf_1h != tf_3h and "NEUTRAL" not in (tf_1h, tf_3h):
            chart_conflict = True
            score -= 10  # Chart conflict reduces confidence meaningfully
        elif tf_1h and tf_1h == tf_3h and tf_1h != "NEUTRAL":
            score += 15  # Confluence bonus

    # Cap: Sideways verdict should never print 90%+ confidence — contradictory
    if score > 65:
        # Re-derive verdict to check if it's sideways/neutral
        abs_ce = abs(scan_ctx.get("ce_oi_change", 0))
        abs_pe = abs(scan_ctx.get("pe_oi_change", 0))
        p_pct = (scan_ctx.get("price_change_pct") or 0)
        is_flat_price = abs(p_pct) <= 0.05
        no_dominant_oi = abs_ce > 0 and abs_pe > 0 and max(abs_ce, abs_pe) < min(abs_ce, abs_pe) * 1.5
        if is_flat_price and no_dominant_oi:
            score = min(score, 65)  # Flat price + balanced OI → cap confidence

    return min(score, 98), chart_conflict


# ── Trade Idea Generator ──────────────────────────────────────────────────

def _generate_trade_idea(verdict_label: str, scan_ctx: dict,
                         alerts: list[dict]) -> str:
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
        idea_parts.append("PCR supportive + HIGH CE OI spikes — smart money positioning")
        idea_parts.append("Strategy: Wait for trigger candle. Buy ATM CE on breakout, or Sell OTM PE if theta play.")
        if resistance:
            idea_parts.append(f"Entry trigger: Close above {resistance:.0f}")
        if support:
            idea_parts.append(f"SL zone: Below {support:.0f}")

    elif verdict_label == "OI Bias Bearish":
        idea_parts.append("📕 *Bias: Cautious Bearish (OI-driven)*")
        idea_parts.append("PCR weak + HIGH PE OI spikes — smart money positioning")
        idea_parts.append("Strategy: Wait for trigger candle. Buy ATM PE on breakdown, or Sell OTM CE if theta play.")
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
        idea_parts.append("Consider: Avoid fresh shorts — wait for OI buildup confirmation")
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
        idea_parts.append(f"⚠️ _{risk}_")

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


def _factor_priority(score: int) -> str:
    if score >= 90:
        return "P1"
    if score >= 70:
        return "P2"
    return "P3"


def _collect_forces(ctx: dict, alerts: list[dict], verdict_label: str, parsed_chart: dict) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
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

    for tf in ("1h", "3h"):
        tf_data = parsed_chart.get(tf) or {}
        sentiment = tf_data.get("sentiment", "NEUTRAL")
        if sentiment == "BULLISH":
            bull.append((70 if tf == "1h" else 78, f"{tf.upper()} chart bullish"))
        elif sentiment == "BEARISH":
            bear.append((70 if tf == "1h" else 78, f"{tf.upper()} chart bearish"))

    if verdict_label in ("Long Buildup", "Short Covering", "Put Writing", "OI Bias Bullish"):
        bull.append((88, f"Price x OI verdict: {verdict_label}"))
    elif verdict_label in ("Short Buildup", "Long Unwinding", "Call Writing", "OI Bias Bearish"):
        bear.append((88, f"Price x OI verdict: {verdict_label}"))

    bull = sorted(bull, key=lambda x: x[0], reverse=True)
    bear = sorted(bear, key=lambda x: x[0], reverse=True)
    return bull[:5], bear[:5]


def _paper_trade_idea(verdict_label: str, ctx: dict) -> str:
    atm = int(_safe(ctx.get("atm_strike"), 0))
    support = int(_safe(ctx.get("support"), 0))
    resistance = int(_safe(ctx.get("resistance"), 0))

    if verdict_label == "OI Bias Bullish":
        if atm:
            return (
                f"PAPER: Buy {atm} CE on close above {resistance or atm + 5} "
                f"| SL below {support or atm - 5} | Partial exit at ATM+1 strike"
            )
        return "Wait for breakout candle above resistance — enter CE on confirmation"

    if verdict_label == "OI Bias Bearish":
        if atm:
            return (
                f"PAPER: Buy {atm} PE on close below {support or atm - 5} "
                f"| SL above {resistance or atm + 5} | Partial exit at ATM-1 strike"
            )
        return "Wait for breakdown candle below support — enter PE on confirmation"

    if verdict_label == "Long Buildup":
        if atm:
            return f"Buy {atm} CE (paper) | SL below {support or atm - 1} | Target near {resistance or atm + 5}"
        return "Buy FUT (paper) on pullback | strict SL"
    if verdict_label == "Short Buildup":
        if atm:
            return f"Buy {atm} PE (paper) | SL above {resistance or atm + 1} | Target near {support or atm - 5}"
        return "Sell FUT (paper) on rise-fail | strict SL"
    if verdict_label == "Short Covering":
        if atm:
            return f"Avoid fresh CE buys | sell OTM PE (paper) only with hedge | watch {resistance or atm + 5}"
        return "Trail longs only; no fresh entry"
    if verdict_label == "Long Unwinding":
        if atm:
            return f"Avoid fresh PE buys | sell OTM CE (paper) only with hedge | watch {support or atm - 5}"
        return "Trail shorts only; no fresh entry"
    if verdict_label == "Put Writing":
        if atm:
            return f"Sell {atm} PE (paper) | SL below {support or atm - 1} | Target near {resistance or atm + 5}"
        return "Sell PE (paper) with hedge | strict SL"
    if verdict_label == "Call Writing":
        if atm:
            return f"Sell {atm} CE (paper) | SL above {resistance or atm + 1} | Target near {support or atm - 5}"
        return "Sell CE (paper) with hedge | strict SL"
    if "Expansion" in verdict_label:
        return "Breakout watch (paper): enter on range break + OI confirmation"
    if "Contraction" in verdict_label:
        return "Range play (paper): fade extremes or short premium with hedge"
    return "No clean edge (paper): wait for confirmation"


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

    for h in merged:
        row = dict(h) if not isinstance(h, dict) else h
        atype = row.get("alert_type", "")
        ot = row.get("option_type", "")
        detail = {}
        try:
            detail_raw = row.get("detail_json") or "{}"
            detail = json.loads(detail_raw) if isinstance(detail_raw, str) else (detail_raw or {})
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

    # Decision logic
    bull_score = long_buildups + short_covers + oi_spikes_pe
    bear_score = short_buildups + long_unwinds + oi_spikes_ce

    if bear_score >= 8 and bear_score > bull_score * 2:
        return "🔴 Strong Bearish Trend — persistent call writing + short buildup"
    if bear_score >= 5 and bear_score > bull_score * 1.5:
        return "🟠 Mild Bearish — resistance building, sellers active"
    if bull_score >= 8 and bull_score > bear_score * 2:
        return "🟢 Strong Bullish Trend — persistent put writing + long buildup"
    if bull_score >= 5 and bull_score > bear_score * 1.5:
        return "🟡 Mild Bullish — support building, buyers active"
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
            result["supertrend"] = {"sentiment": sentiment, "raw_name": item.get("name")}
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


def generate_intelligence(symbol: str, current_alerts: list[dict],
                          scan_context: dict | None = None) -> str:
    """
    Analyzes scan context + current alerts to generate trade-actionable intelligence.
    Returns Telegram-formatted markdown string.
    """
    current_alerts = current_alerts or []
    if not current_alerts and not symbol:
        return ""

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

    # ── Price × OI Verdict ─────────────────────────────────────────────────
    verdict_label, verdict_emoji, verdict_desc = _price_oi_verdict(
        price_pct, net_oi_change, ce_oi_change, pe_oi_change,
        pcr=pcr, alerts=current_alerts,
    )

    # ── Confidence (base) ──────────────────────────────────────────────────
    confidence = _compute_confidence(ctx, current_alerts)

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

    # Chart-only directional fallback:
    # If OI/price is neutral but both 1H and 3H agree directionally,
    # promote verdict to cautious bias so bot decision reflects candle confluence.
    tf_1h = (parsed_chart.get("1h") or {}).get("sentiment")
    tf_3h = (parsed_chart.get("3h") or {}).get("sentiment")
    if verdict_label == "Sideways" and tf_1h and tf_1h == tf_3h:
        if tf_1h == "BULLISH":
            verdict_label = "OI Bias Bullish"
            verdict_emoji = "🟡"
            verdict_desc = "Cautious Bullish — 1H and 3H candle sentiment aligned bullish"
        elif tf_1h == "BEARISH":
            verdict_label = "OI Bias Bearish"
            verdict_emoji = "🟠"
            verdict_desc = "Cautious Bearish — 1H and 3H candle sentiment aligned bearish"
    # ── Re-compute confidence with chart context ───────────────────────────────
    confidence, chart_conflict = _compute_confidence(ctx, current_alerts, parsed_chart)

    # Chart confluence boost for matching directional verdicts
    for tf, entry in parsed_chart.items():
        if tf not in ("1h", "3h"):
            continue
        effective_sentiment = entry.get("sentiment")
        if verdict_label in ("Long Buildup", "Put Writing") and effective_sentiment == "BULLISH":
            confidence = min(confidence + 12, 95)
            log.debug("[intel] Chart confluence +10%% | %s %s BULLISH aligns Long Buildup", symbol, tf)
        elif verdict_label in ("Short Buildup", "Call Writing") and effective_sentiment == "BEARISH":
            confidence = min(confidence + 12, 95)
            log.debug("[intel] Chart confluence +10%% | %s %s BEARISH aligns Short Buildup", symbol, tf)

    # ── Build Message ──────────────────────────────────────────────────────
    if confidence < 50:
        return "\n".join([
            f"🤖 *Bot Intelligence | {symbol}*",
            "⚪ *Verdict: Low Conviction*",
            "_No actionable edge — wait for alignment_",
            f"Confidence: {confidence}%",
        ])

    msg = [f"🤖 *Bot Intelligence | {symbol}*"]

    # Market Stance
    msg.append(f"")
    msg.append(f"{verdict_emoji} *Verdict: {verdict_label}*")
    msg.append(f"_{verdict_desc}_")
    msg.append(f"Confidence: {confidence}%")
    if chart_conflict:
        msg.append("⚠️ _Chart conflict: 1H vs 3H signals disagree — reduce size, wait alignment_")

    # OI Analysis
    if total_ce_oi or total_pe_oi:
        msg.append(f"")
        msg.append(f"📊 *OI Analysis*")
        ce_arrow = "↑" if ce_oi_change > 0 else ("↓" if ce_oi_change < 0 else "→")
        pe_arrow = "↑" if pe_oi_change > 0 else ("↓" if pe_oi_change < 0 else "→")
        ce_chg_str = f"+{fmt_oi(ce_oi_change)}" if ce_oi_change >= 0 else f"-{fmt_oi(abs(ce_oi_change))}"
        pe_chg_str = f"+{fmt_oi(pe_oi_change)}" if pe_oi_change >= 0 else f"-{fmt_oi(abs(pe_oi_change))}"
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
    spot_str = f"Spot: `{spot_val:.0f}`" if spot_val else "Spot: N/A"
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
        for tf in sorted(parsed_chart.keys(), key=_tf_sort_key):  # deterministic timeframe order
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
                        m_tag = " 🔥" if h and c >= h * 0.998 else (" ❄️" if l and c <= l * 1.002 else "")
                        price_str = f"🕯️ O:{o:.1f} H:{h:.1f} L:{l:.1f} C:{c:.1f}{m_tag}"
                    else:
                        price_str = f"🕯️ C:{c:.1f}"
                    tf_parts.append(price_str)

            if st:
                tf_parts.append("(ST)")

            if rsi is not None:
                try:
                    rsi_val = float(rsi)
                    rsi_note = " OB" if rsi_val > 70 else (" OS" if rsi_val < 30 else "")
                    tf_parts.append(f"RSI `{rsi_val:.1f}`{rsi_note}")
                except (TypeError, ValueError):
                    pass

            msg.append(" | ".join(tf_parts))

    bull_forces, bear_forces = _collect_forces(ctx, current_alerts, verdict_label, parsed_chart)

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
    if verdict_label in ("Long Buildup", "Short Covering", "Put Writing"):
        action_plan = "Trail SL on longs. Avoid blind chase."
    elif verdict_label in ("Short Buildup", "Long Unwinding", "Call Writing"):
        action_plan = "Trail SL on shorts. Avoid panic entry."
    elif verdict_label == "OI Bias Bullish":
        action_plan = "Wait for 1H trigger candle above resistance. Buy CE on breakout confirmation."
    elif verdict_label == "OI Bias Bearish":
        action_plan = "Wait for 1H trigger candle below support. Buy PE on breakdown confirmation."
    else:
        action_plan = "No aggressive trade. Wait trigger candle."
    msg.append(f"- Action Plan: {action_plan}")
    risk_note = _generate_risk_note(verdict_label, ctx)
    if risk_note:
        msg.append(f"- Critical Warning: {risk_note}")

    msg.append("")
    msg.append("*PAPER TRADE (Specific)*")
    msg.append(f"- {_paper_trade_idea(verdict_label, ctx)}")

    # Broader Trend
    trend = _compute_broader_trend(symbol, current_alerts)
    msg.append(f"")
    msg.append(f"🌊 *Broader Trend:* {trend}")

    msg.append(f"")
    msg.append(f"_Based on {len(current_alerts)} signals this scan_")

    return "\n".join(msg)
