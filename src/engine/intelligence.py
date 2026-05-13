"""
Bot Intelligence Engine v3.0
Combines scan context (OI totals, price movement, PCR, max pain, OI walls)
with current alerts to produce trade-actionable intelligence.

Output: Telegram-formatted markdown block appended to digest.
"""
import json
import logging
import re
from datetime import datetime, timezone
from src.models.schema import get_alert_history

log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def _safe(val, default=0):
    """Safely convert numeric-ish values, including comma-formatted strings."""
    try:
        if val is None:
            return default
        if isinstance(val, str):
            s = val.replace(",", "").strip()
            if not s or s in {"—", "-", "--", "NA", "N/A", "null", "None"}:
                return default
            val = s
        n = float(val)
        return n if n == n else default  # NaN guard
    except (TypeError, ValueError):
        return default


def _fmt_oi(n: int | float | str | None) -> str:
    n = _safe(n, 0)
    if abs(n) >= 1e7:
        return f"{n / 1e7:.1f}Cr"
    if abs(n) >= 1e5:
        return f"{n / 1e5:.1f}L"
    if abs(n) >= 1e3:
        return f"{n / 1e3:.1f}K"
    return str(int(n)) if float(n).is_integer() else f"{n:.1f}"


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

def _price_oi_verdict(price_pct: float | None, net_oi_change: int) -> tuple[str, str, str]:
    """
    Returns (verdict_label, emoji, trade_bias).
    net_oi_change = total OI change across CE+PE combined.
    """
    price_up = (price_pct or 0) > 0.05   # >0.05% to avoid noise
    price_dn = (price_pct or 0) < -0.05
    oi_up = net_oi_change > 0
    oi_dn = net_oi_change < 0

    if price_up and oi_up:
        return "Long Buildup", "🟢", "Bullish — fresh longs entering"
    if price_up and oi_dn:
        return "Short Covering", "🟡", "Weak Bullish — shorts exiting, not fresh buying"
    if price_dn and oi_up:
        return "Short Buildup", "🔴", "Bearish — fresh shorts entering"
    if price_dn and oi_dn:
        return "Long Unwinding", "🟠", "Weak Bearish — longs exiting, not fresh selling"

    # Flat price
    if oi_up:
        return "Range Expansion", "⚪", "Neutral — OI building, breakout likely"
    if oi_dn:
        return "Range Contraction", "⚪", "Neutral — OI exiting, low-conviction zone"
    return "Sideways", "⚪", "Neutral — no clear signal"


# ── Confidence Scorer ─────────────────────────────────────────────────────

def _compute_confidence(scan_ctx: dict, alerts: list[dict]) -> int:
    """
    Score 0–100 based on signal confluence.
    Higher = more factors agreeing on direction.
    """
    score = 30  # base

    price_pct = _safe(scan_ctx.get("price_change_pct"))
    ce_chg = scan_ctx.get("ce_oi_change", 0)
    pe_chg = scan_ctx.get("pe_oi_change", 0)
    pcr = _safe(scan_ctx.get("pcr"))

    # Alert severity weighting
    for a in alerts:
        sev = a.get("severity", "LOW")
        if sev == "HIGH":
            score += 8
        elif sev == "MEDIUM":
            score += 4

    # PCR confirmation
    if pcr and pcr < 0.7 and price_pct and price_pct > 0:
        score += 10  # low PCR + price up = bullish confluence
    elif pcr and pcr > 1.3 and price_pct and price_pct < 0:
        score += 10  # high PCR + price down = bearish confluence

    # OI wall proximity
    underlying = _safe(scan_ctx.get("underlying"))
    support = _safe(scan_ctx.get("support"))
    resistance = _safe(scan_ctx.get("resistance"))
    if underlying and support and resistance:
        total_range = resistance - support
        if total_range > 0:
            # Near support = bullish bias confirmation if price rising
            dist_to_support = underlying - support
            if dist_to_support < total_range * 0.2 and price_pct and price_pct > 0:
                score += 8
            # Near resistance = bearish bias confirmation if price falling
            dist_to_resistance = resistance - underlying
            if dist_to_resistance < total_range * 0.2 and price_pct and price_pct < 0:
                score += 8

    # Max pain gravity
    max_pain = _safe(scan_ctx.get("max_pain"))
    if underlying and max_pain:
        mp_dist_pct = abs(underlying - max_pain) / underlying * 100
        if mp_dist_pct < 0.5:
            score += 5  # near max pain = high probability zone

    return min(score, 95)  # cap at 95, never 100


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

    if verdict_label == "Long Buildup":
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
    elif pcr <= 0.85:
        bear.append((85, f"PCR weak ({pcr:.2f})"))
    else:
        bull.append((50, f"PCR neutral ({pcr:.2f})"))
        bear.append((50, f"PCR neutral ({pcr:.2f})"))

    if price_pct >= 0.35:
        bull.append((80, f"Spot momentum +{price_pct:.2f}%"))
    elif price_pct <= -0.35:
        bear.append((80, f"Spot momentum {price_pct:.2f}%"))

    if pe_oi_change > 0 and ce_oi_change <= 0:
        bull.append((75, "Put writing visible"))
    if ce_oi_change > 0 and pe_oi_change <= 0:
        bear.append((75, "Call writing visible"))
    if ce_oi_change > 0 and pe_oi_change > 0:
        bull.append((55, "Both-side OI build"))
        bear.append((55, "Both-side OI build"))

    for tf in ("1h", "3h"):
        tf_data = parsed_chart.get(tf) or {}
        sentiment = tf_data.get("sentiment", "NEUTRAL")
        if sentiment == "BULLISH":
            bull.append((70 if tf == "1h" else 78, f"{tf.upper()} chart bullish"))
        elif sentiment == "BEARISH":
            bear.append((70 if tf == "1h" else 78, f"{tf.upper()} chart bearish"))

    if verdict_label in ("Long Buildup", "Short Covering"):
        bull.append((88, f"Price x OI verdict: {verdict_label}"))
    elif verdict_label in ("Short Buildup", "Long Unwinding"):
        bear.append((88, f"Price x OI verdict: {verdict_label}"))

    bull = sorted(bull, key=lambda x: x[0], reverse=True)
    bear = sorted(bear, key=lambda x: x[0], reverse=True)
    return bull[:5], bear[:5]


def _paper_trade_idea(verdict_label: str, ctx: dict) -> str:
    atm = int(_safe(ctx.get("atm_strike"), 0))
    support = int(_safe(ctx.get("support"), 0))
    resistance = int(_safe(ctx.get("resistance"), 0))

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
    return "No clean edge (paper): wait for breakout + OI confirmation"


# ── Broader Trend from History ─────────────────────────────────────────────

def _compute_broader_trend(symbol: str, alerts: list[dict]) -> str:
    """
    Analyze last 50 alerts for the symbol to determine multi-scan trend.
    """
    history = get_alert_history(symbol, limit=50)
    if not history:
        return "Insufficient history — first scan"

    # Count buildup types from BUILDUP_CLASSIFY alerts
    long_buildups = 0
    short_buildups = 0
    long_unwinds = 0
    short_covers = 0
    oi_spikes_ce = 0
    oi_spikes_pe = 0

    for h in history:
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
    verdict_label, verdict_emoji, verdict_desc = _price_oi_verdict(price_pct, net_oi_change)

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
            # Boost confidence for 1H/3H chart + OC alignment
            if tf in ("1h", "3h") and effective_sentiment:
                if verdict_label == "Long Buildup" and effective_sentiment == "BULLISH":
                    confidence = min(confidence + 10, 95)
                    log.debug("[intel] Chart confluence +10%% | %s %s BULLISH aligns Long Buildup", symbol, tf)
                elif verdict_label == "Short Buildup" and effective_sentiment == "BEARISH":
                    confidence = min(confidence + 10, 95)
                    log.debug("[intel] Chart confluence +10%% | %s %s BEARISH aligns Short Buildup", symbol, tf)

    # ── Build Message ──────────────────────────────────────────────────────
    msg = [f"🤖 *Bot Intelligence | {symbol}*"]

    # Market Stance
    msg.append(f"")
    msg.append(f"{verdict_emoji} *Verdict: {verdict_label}*")
    msg.append(f"_{verdict_desc}_")
    msg.append(f"Confidence: {confidence}%")

    # OI Analysis
    if total_ce_oi or total_pe_oi:
        msg.append(f"")
        msg.append(f"📊 *OI Analysis*")
        ce_arrow = "↑" if ce_oi_change > 0 else ("↓" if ce_oi_change < 0 else "→")
        pe_arrow = "↑" if pe_oi_change > 0 else ("↓" if pe_oi_change < 0 else "→")
        ce_chg_str = f"+{_fmt_oi(ce_oi_change)}" if ce_oi_change >= 0 else f"-{_fmt_oi(abs(ce_oi_change))}"
        pe_chg_str = f"+{_fmt_oi(pe_oi_change)}" if pe_oi_change >= 0 else f"-{_fmt_oi(abs(pe_oi_change))}"
        msg.append(f"CE OI: `{_fmt_oi(total_ce_oi)}` {ce_arrow} ({ce_chg_str})")
        msg.append(f"PE OI: `{_fmt_oi(total_pe_oi)}` {pe_arrow} ({pe_chg_str})")

        # Dominant writer interpretation
        if ce_oi_change > 0 and pe_oi_change > 0:
            dominant = "Both sides adding — big move brewing"
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
    spot_str = f"Spot: `{underlying:.0f}`" if underlying else "Spot: N/A"
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
    if verdict_label in ("Long Buildup", "Short Covering"):
        action_plan = "Trail SL on longs. Avoid blind chase."
    elif verdict_label in ("Short Buildup", "Long Unwinding"):
        action_plan = "Trail SL on shorts. Avoid panic entry."
    else:
        action_plan = "No aggressive trade. Wait trigger candle."
    msg.append(f"- Action Plan: {action_plan}")
    risk_note = _generate_risk_note(verdict_label, ctx) or "Low conviction zone."
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
