"""Telegram scan digest builder."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from src.engine.intelligence import generate_intelligence

IST = timezone(timedelta(hours=5, minutes=30))
MAX_TELEGRAM_LEN = 3900
USE_ENHANCED_TEMPLATE = True  # Toggle between old and new template

EMOJI_GREEN = "\U0001F7E2"
EMOJI_RED = "\U0001F534"
EMOJI_YELLOW = "\U0001F7E1"
EMOJI_BLUE = "\U0001F535"
EMOJI_WHITE = "\u26AA"
EMOJI_CANDLES = "\U0001F56F\ufe0f"
_FUTURE_SYMBOLS = {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}

_SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_VERDICT_STYLE = {
    "Long Buildup": (EMOJI_GREEN, "BULLISH"),
    "Put Writing": (EMOJI_GREEN, "BULLISH"),
    "Short Covering": (EMOJI_YELLOW, "BULLISH, but chase carefully"),
    "OI Bias Bullish": (EMOJI_YELLOW, "BULLISH BIAS"),
    "Short Buildup": (EMOJI_RED, "BEARISH"),
    "Call Writing": (EMOJI_RED, "BEARISH"),
    "Long Unwinding": ("\U0001F7E0", "BEARISH, but late"),
    "OI Bias Bearish": ("\U0001F7E0", "BEARISH BIAS"),
    "Sideways": (EMOJI_WHITE, "SIDEWAYS"),
}


def _clean_text(text: str) -> str:
    out = str(text or "")
    bad = {
        "ðŸ•¯ï¸": EMOJI_CANDLES,
        "ðŸ•¯": EMOJI_CANDLES,
        "ðŸ”´": EMOJI_RED,
        "ðŸŸ¢": EMOJI_GREEN,
        "ðŸŸ¡": EMOJI_YELLOW,
        "ðŸ”µ": EMOJI_BLUE,
        "âšª": EMOJI_WHITE,
        "â€”": "-",
        "â†’": "->",
        "â†‘": "up",
        "â†“": "down",
        "â€¦": "...",
        "Â·": "·",
    }
    for k, v in bad.items():
        out = out.replace(k, v)
    return out


def _fmt_num(value, digits: int = 0) -> str:
    try:
        value = float(value)
    except Exception:
        return "N/A"
    if digits:
        return f"{value:.{digits}f}"
    return f"{value:.0f}"


def _fmt_signed(value, digits: int = 2) -> str:
    try:
        value = float(value or 0)
    except Exception:
        value = 0.0
    return f"{value:+.{digits}f}"


def _fmt_oi(value) -> str:
    try:
        value = int(value or 0)
    except Exception:
        return "0"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 100_000:
        return f"{sign}{value / 100_000:.2f}L"
    if value >= 1_000:
        return f"{sign}{value / 1_000:.1f}K"
    return f"{sign}{value}"


def _clip(value: str, limit: int = 120) -> str:
    text = _clean_text(str(value or "").replace("\n", " ").strip())
    return text[: limit - 3] + "..." if len(text) > limit else text


def _bar(pct: int) -> str:
    pct = max(0, min(100, int(pct or 0)))
    filled = round(pct / 10)
    return ("\u2588" * filled) + ("\u2591" * (10 - filled)) + f" {pct}%"


def _norm_symbol(symbol: str) -> str:
    value = str(symbol or "").upper().strip()
    value = re.sub(r"^(NSE|NFO|BSE|MCX|CDS):", "", value)
    value = value.replace("!", "")
    return re.sub(r"[^A-Z0-9]", "", value)


def _price_label(symbol: str) -> str:
    base = _norm_symbol(symbol).split()[0] if symbol else ""
    return "Fut" if base in _FUTURE_SYMBOLS else "Spot"


def _is_bullish_verdict(verdict: str) -> bool:
    return verdict in {"Long Buildup", "Put Writing", "OI Bias Bullish", "Short Covering"}


def _is_bearish_verdict(verdict: str) -> bool:
    return verdict in {"Short Buildup", "Call Writing", "OI Bias Bearish", "Long Unwinding"}


def _chart_payload_for_symbol(scan_context: dict, symbol: str) -> dict:
    chart_data = (scan_context or {}).get("chart_indicators")
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


def _candle_line(tf: str, tf_data: dict) -> str:
    sentiment = str((tf_data or {}).get("sentiment") or "NEUTRAL").upper()
    marker = EMOJI_GREEN if sentiment == "BULLISH" else (EMOJI_RED if sentiment == "BEARISH" else EMOJI_WHITE)
    start_label = ""
    raw_start = (tf_data or {}).get("bar_start_utc")
    raw_end = (tf_data or {}).get("bar_end_utc")
    if raw_start:
        try:
            start_dt = datetime.fromisoformat(str(raw_start)).astimezone(IST)
            if raw_end:
                end_dt = datetime.fromisoformat(str(raw_end)).astimezone(IST)
                start_label = f" ({start_dt.strftime('%d-%b %H:%M')}-{end_dt.strftime('%H:%M')} IST)"
            else:
                start_label = f" ({start_dt.strftime('%d-%b %H:%M')} IST)"
        except Exception:
            start_label = ""
    ohlc = (tf_data or {}).get("ohlc") or {}
    if isinstance(ohlc, dict):
        o = ohlc.get("open")
        h = ohlc.get("high")
        l = ohlc.get("low")
        c = ohlc.get("close")
        if all(v is not None for v in (o, h, l, c)):
            try:
                return f"{tf.upper()} {marker} {sentiment}{start_label} | O `{float(o):.1f}` H `{float(h):.1f}` L `{float(l):.1f}` C `{float(c):.1f}`"
            except Exception:
                pass
        if c is not None:
            try:
                return f"{tf.upper()} {marker} {sentiment}{start_label} | C `{float(c):.1f}`"
            except Exception:
                pass
    return f"{tf.upper()} {marker} {sentiment}{start_label}"


def _detail(alert: dict) -> dict:
    try:
        return json.loads(alert.get("detail_json") or "{}")
    except Exception:
        return {}


def _signal_line(alert: dict) -> str:
    d = _detail(alert)
    atype = alert.get("alert_type", "")
    strike = alert.get("strike")
    opt = alert.get("option_type") or ""
    leg = f"{_fmt_num(strike)} {opt}".strip() if strike else ""

    if atype == "BUILDUP_CLASSIFY":
        return f"{d.get('buildup_type', 'Buildup')} {leg} | OI {d.get('oi_pct', 0):+.0f}% · LTP {d.get('ltp_pct', 0):+.0f}%"
    if atype == "OI_SPIKE":
        return f"OI spike {leg} | {d.get('pct_change', 0):+.0f}%"
    if atype == "OI_UNWIND":
        return f"OI unwind {leg} | {d.get('pct_change', 0):+.0f}%"
    if atype == "ATM_LEG_MOVE":
        return f"ATM move | CE {d.get('ce_pct', 0):+.1f}% · PE {d.get('pe_pct', 0):+.1f}% -> {d.get('bias', '')}"
    if atype == "VOLUME_AGGRESSION":
        return f"{d.get('label', 'Aggressive volume')} {leg} | ratio {float(d.get('ratio') or 0):.1f}x"
    if atype == "OTM_UNUSUAL":
        return f"Far OTM activity {leg} | {d.get('pct_change', 0):+.0f}%"
    if atype == "PCR_EXTREME":
        return f"PCR {d.get('pcr', 'N/A')} | {d.get('interpretation', '')}"
    if atype == "PCR_SHIFT":
        return f"PCR shift {d.get('pcr_delta', 0):+.3f} -> {d.get('pcr', 'N/A')}"
    if atype == "PCR_VELOCITY":
        return f"PCR trend {d.get('slope', 0):+.3f}/scan | {d.get('label', '')}"
    if atype == "PRICE_SPIKE":
        return f"Spot move {d.get('pct_change', 0):+.2f}% {d.get('direction', '')}"
    if atype == "MAX_PAIN_SHIFT":
        return f"Max pain -> {_fmt_num(d.get('curr_max_pain'))} | shift {d.get('shift', 0):+.0f}"
    if atype == "STRADDLE_PREMIUM":
        return f"Straddle premium {d.get('pct_change', 0):+.1f}% | {d.get('label', '')}"
    if atype in {"IV_SPIKE", "IV_CRUSH"}:
        return f"{atype.replace('_', ' ').title()} {leg} | IV delta {d.get('iv_delta', 0):+.1f}"
    return f"{atype.replace('_', ' ').title()} {leg}".strip()


def _parse_intelligence(raw: str) -> dict:
    out = {
        "verdict": "Sideways",
        "desc": "",
        "confidence": 0,
        "action": "",
        "paper": "",
        "warning": "",
        "oi_note": "",
        "bull": [],
        "bear": [],
        "trend": "",
        "conflict": "",
    }
    section = None
    for line in _clean_text(raw or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if "*Verdict:" in text:
            out["verdict"] = text.split("*Verdict:", 1)[1].replace("*", "").strip()
            continue
        if out["verdict"] and not out["desc"] and text.startswith("_"):
            out["desc"] = text.strip("_")
            continue
        if text.startswith("Confidence:"):
            try:
                out["confidence"] = int(text.split(":", 1)[1].strip().replace("%", ""))
            except Exception:
                pass
            continue
        if "Chart conflict" in text:
            out["conflict"] = text.strip("_⚠️ ")
            continue
        if "*OI Analysis*" in text:
            section = "oi"
            continue
        if "*BULL FORCES" in text:
            section = "bull"
            continue
        if "*BEAR FORCES" in text:
            section = "bear"
            continue
        if "*TRADE STRATEGY*" in text:
            section = "trade"
            continue
        if "*PAPER TRADE" in text:
            section = "paper"
            continue
        if "*Broader Trend*" in text or "Broader Trend:" in text:
            out["trend"] = text.split(":", 1)[-1].strip("*🌊 ")
            section = None
            continue

        if section == "oi" and text.startswith("_"):
            out["oi_note"] = text.strip("_")
        elif section == "bull" and text.startswith("-"):
            out["bull"].append(text.lstrip("- "))
        elif section == "bear" and text.startswith("-"):
            out["bear"].append(text.lstrip("- "))
        elif section == "trade":
            if text.startswith("- Action Plan:"):
                out["action"] = text.split(":", 1)[1].strip()
            elif text.startswith("- Critical Warning:"):
                out["warning"] = text.split(":", 1)[1].strip()
        elif section == "paper" and text.startswith("-"):
            out["paper"] = text.lstrip("- ").strip()
    return out


def _default_action(label: str, confidence: int) -> str:
    if confidence < 55:
        return "No clean edge. Wait for confirmation."
    if "BULL" in label:
        return "Bullish bias. Buy only after price holds above ATM/resistance."
    if "BEAR" in label:
        return "Bearish bias. Sell only after rejection near ATM/support."
    return "No aggressive trade. Wait for breakout or rejection candle."


def _trend_text(trend_raw: str, verdict: str) -> str:
    trend = _clip(_clean_text(trend_raw), 120)
    if trend:
        return trend
    if verdict in {"Long Buildup", "Put Writing", "OI Bias Bullish"}:
        return f"{EMOJI_GREEN} Bullish follow-through likely"
    if verdict in {"Short Buildup", "Call Writing", "OI Bias Bearish"}:
        return f"{EMOJI_RED} Bearish follow-through likely"
    return f"{EMOJI_WHITE} Mixed - no dominant trend yet"


def _delta_color(delta: float | int | None) -> str:
    try:
        v = float(delta or 0)
    except Exception:
        v = 0.0
    if v > 0:
        return f"{EMOJI_GREEN} +{_fmt_oi(v)}"
    if v < 0:
        return f"{EMOJI_RED} {_fmt_oi(v)}"
    return f"{EMOJI_WHITE} 0"


def _fit_telegram(message: str, digest_id: str) -> str:
    msg = _clean_text(message)
    if len(msg) <= MAX_TELEGRAM_LEN:
        return msg
    clipped = msg[: MAX_TELEGRAM_LEN - 80].rsplit("\n", 1)[0]
    return f"{clipped}\n...trimmed\n_#{digest_id}_"


def build_digest(
    symbol: str,
    alerts: list[dict],
    fetched_at: str | None = None,
    scan_context: dict | None = None,
    intelligence_text: str | None = None,
    detected_count: int | None = None,
    dedup_suppressed_count: int | None = None,
) -> tuple[str, str]:
    digest_id = str(uuid.uuid4())[:8]
    try:
        dt = datetime.fromisoformat(fetched_at or "").astimezone(IST)
    except Exception:
        dt = datetime.now(IST)
    ts = dt.strftime("%H:%M IST")

    ctx = scan_context or {}
    diag = ctx.get("diagnostics", {}) if isinstance(ctx, dict) else {}
    n = len(alerts)
    px_label = _price_label(symbol)

    if not alerts:
        max_oi = float(diag.get("max_oi_delta_pct") or 0)
        max_ltp = float(diag.get("max_atm_ltp_delta_pct") or 0)
        total_detected = int(detected_count or 0)
        deduped = int(dedup_suppressed_count or 0)
        title = "No signals"
        quiet_note = "No threshold crossed. No trade needed."
        if total_detected > 0 and deduped >= total_detected:
            title = "No NEW signals"
            quiet_note = f"Detected `{total_detected}` but dedup suppressed repeats."
        msg = "\n".join([
            f"\U0001F4CA *{symbol}* | {ts} | {title}",
            f"{px_label} `{_fmt_num(ctx.get('underlying'))}` | PCR `{_fmt_num(ctx.get('pcr'), 2)}`",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{EMOJI_WHITE} *Market quiet*",
            quiet_note,
            "",
            f"OI max `{max_oi:.2f}%` | ATM LTP max `{max_ltp:.2f}%`",
            f"_#{digest_id} · all symbols enabled_",
        ])
        return digest_id, _fit_telegram(msg, digest_id)

    sorted_alerts = sorted(alerts, key=lambda a: _SEV_ORDER.get(a.get("severity", "LOW"), 2))
    high = sum(a.get("severity") == "HIGH" for a in alerts)
    med = sum(a.get("severity") == "MEDIUM" for a in alerts)
    low = sum(a.get("severity") == "LOW" for a in alerts)

    intel_raw = intelligence_text if intelligence_text is not None else generate_intelligence(symbol, alerts, scan_context=scan_context)
    intel = _parse_intelligence(intel_raw)
    emoji, label = _VERDICT_STYLE.get(intel["verdict"], (EMOJI_WHITE, "NEUTRAL"))
    confidence = int(intel["confidence"] or 0)
    action = intel["action"] or _default_action(label, confidence)
    warning = intel["warning"] or ("Chart/OI mismatch. Wait." if intel["conflict"] else "Use trigger candle + stop loss.")

    counts = " ".join(x for x in [
        f"{EMOJI_RED} {high} high" if high else "",
        f"{EMOJI_YELLOW} {med} med" if med else "",
        f"{EMOJI_BLUE} {low} low" if low else "",
    ] if x)

    lines = [
        f"\U0001F4CA *{symbol}* | {ts} | {n} signals",
        counts or "No severity count",
        f"{px_label} `{_fmt_num(ctx.get('underlying'))}` | ATM `{_fmt_num(ctx.get('atm_strike'))}` | PCR `{_fmt_num(ctx.get('pcr'), 2)}`",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{emoji} *{label}* - {_clip(intel['verdict'], 45)}",
    ]
    # Get price change values - preserve None to detect missing data
    price_change_pct = ctx.get("price_change_pct")
    price_change_points = ctx.get("price_change_points")
    
    try:
        d_spot = float(price_change_pct) if price_change_pct is not None else 0.0
    except Exception:
        d_spot = 0.0
    try:
        d_points = float(price_change_points or 0.0)
    except Exception:
        d_points = 0.0
    
    pct_digits = 3 if abs(d_spot) < 0.01 and d_spot != 0 else 2
    
    # If price_change_pct is None (no previous data), show "no prev data" instead of "flat"
    if price_change_pct is None:
        spot_delta = "no prev data"
    elif abs(d_points) < 0.05 and abs(d_spot) < 0.005:
        spot_delta = "flat"
    else:
        spot_delta = f"{_fmt_signed(d_points, 1)} (`{_fmt_signed(d_spot, pct_digits)}%`)"
    lines.append(
        f"Δ prev scan: {px_label} `{spot_delta}` | CE OI `{_fmt_oi(ctx.get('ce_oi_change', 0))}` | PE OI `{_fmt_oi(ctx.get('pe_oi_change', 0))}`"
    )
    if intel["desc"]:
        lines.append(_clip(intel["desc"], 100))
    lines.append(f"Confidence: `{_bar(confidence)}`")

    lines += [
        "",
        "\U0001F3AF *What to do*",
        f"• {_clip(action, 150)}",
        f"• {_clip(warning, 130)}",
    ]
    if intel["paper"]:
        lines.append(f"• Paper: {_clip(intel['paper'], 120)}")

    levels = []
    if ctx.get("support") is not None:
        levels.append(f"S `{_fmt_num(ctx.get('support'))}`")
    if ctx.get("resistance") is not None:
        levels.append(f"R `{_fmt_num(ctx.get('resistance'))}`")
    if ctx.get("max_pain") is not None:
        levels.append(f"MP `{_fmt_num(ctx.get('max_pain'))}`")
    if levels:
        lines += ["", "\U0001F4CD *Key levels*", " | ".join(levels)]

    chart_payload = _chart_payload_for_symbol(ctx, symbol)
    candle_lines = []
    for tf in ("1h", "3h"):
        tf_data = chart_payload.get(tf)
        if isinstance(tf_data, dict):
            candle_lines.append(_candle_line(tf, tf_data))
    if candle_lines:
        lines += ["", f"{EMOJI_CANDLES} *Candles (1H / 3H)*", *candle_lines]

    ce_oi = ctx.get("total_ce_oi", 0)
    pe_oi = ctx.get("total_pe_oi", 0)
    ce_chg = ctx.get("ce_oi_change", 0)
    pe_chg = ctx.get("pe_oi_change", 0)
    if ce_oi or pe_oi:
        lines += [
            "",
            "\U0001F9EE *OI pulse*",
            f"CE `{_fmt_oi(ce_oi)}` {_delta_color(ce_chg)} | PE `{_fmt_oi(pe_oi)}` {_delta_color(pe_chg)}",
        ]
        if intel["oi_note"]:
            lines.append(_clip(intel["oi_note"], 120))

    cap = 8 if high >= 5 else 10
    lines += ["", "\U0001F4E1 *Top signals*"]
    for alert in sorted_alerts[:cap]:
        badge = {"HIGH": EMOJI_RED, "MEDIUM": EMOJI_YELLOW, "LOW": EMOJI_BLUE}.get(alert.get("severity", "LOW"), EMOJI_BLUE)
        lines.append(f"{badge} {_clip(_signal_line(alert), 130)}")
    hidden = len(sorted_alerts) - cap
    if hidden > 0:
        lines.append(f"...and {hidden} more signals.")

    bull = _clip(intel["bull"][0], 110) if intel["bull"] else "No strong bullish factor"
    bear = _clip(intel["bear"][0], 110) if intel["bear"] else "No strong bearish factor"
    lines += ["", "\u2696\ufe0f *Balance*"]
    if _is_bullish_verdict(intel["verdict"]):
        lines += [f"{EMOJI_GREEN} Primary: {bull}", f"{EMOJI_YELLOW} Caution: {bear}"]
    elif _is_bearish_verdict(intel["verdict"]):
        lines += [f"{EMOJI_RED} Primary: {bear}", f"{EMOJI_YELLOW} Caution: {bull}"]
    else:
        lines += [f"{EMOJI_GREEN} {bull}", f"{EMOJI_RED} {bear}"]

    lines += ["", f"\U0001F30A *Trend:* {_trend_text(intel['trend'], intel['verdict'])}"]
    lines += ["", f"_#{digest_id} · {n} signals · all symbols enabled_"]
    return digest_id, _fit_telegram("\n".join(lines), digest_id)


# ═══════════════════════════════════════════════════════════════════════════
# ENHANCED TELEGRAM TEMPLATE
# ═══════════════════════════════════════════════════════════════════════════


def _calculate_confidence_score(alerts: list[dict], intel: dict, scan_context: dict, chart_payload: dict) -> int:
    """Calculate confidence score (30-100) based on signal strength and confirmations."""
    score = 50  # Base score
    
    # Primary signal strength (max +40)
    max_oi_pct = 0.0
    for alert in alerts:
        detail = _detail(alert)
        if alert.get("alert_type") in {"OI_SPIKE", "OI_UNWIND"}:
            pct = abs(float(detail.get("pct_change", 0)))
            max_oi_pct = max(max_oi_pct, pct)
    
    if max_oi_pct > 200:
        score += 40
    elif max_oi_pct > 100:
        score += 30
    elif max_oi_pct > 50:
        score += 20
    elif max_oi_pct > 30:
        score += 10
    
    # Candle confirmation (max +15)
    candles_1h = chart_payload.get("1h", {}).get("sentiment", "").upper()
    candles_3h = chart_payload.get("3h", {}).get("sentiment", "").upper()
    verdict = intel.get("verdict", "")
    
    is_bullish_verdict = _is_bullish_verdict(verdict)
    is_bearish_verdict = _is_bearish_verdict(verdict)
    
    if is_bullish_verdict:
        if candles_1h == "BULLISH" and candles_3h == "BULLISH":
            score += 15
        elif candles_1h == "BULLISH" or candles_3h == "BULLISH":
            score += 10
        elif candles_1h == "BEARISH" or candles_3h == "BEARISH":
            score -= 15  # Conflict
    elif is_bearish_verdict:
        if candles_1h == "BEARISH" and candles_3h == "BEARISH":
            score += 15
        elif candles_1h == "BEARISH" or candles_3h == "BEARISH":
            score += 10
        elif candles_1h == "BULLISH" or candles_3h == "BULLISH":
            score -= 15  # Conflict
    
    # PCR support (max +5)
    pcr = float(scan_context.get("pcr", 1.0) or 1.0)
    if is_bullish_verdict and pcr > 1.2:
        score += 5
    elif is_bearish_verdict and pcr < 0.8:
        score += 5
    
    # Multiple confirmations (+5)
    high_severity_count = sum(1 for a in alerts if a.get("severity") == "HIGH")
    if high_severity_count >= 3:
        score += 5
    
    # Mixed OI (both CE and PE buildup) reduces confidence
    ce_change = scan_context.get("ce_oi_change", 0)
    pe_change = scan_context.get("pe_oi_change", 0)
    if ce_change > 0 and pe_change > 0 and min(ce_change, pe_change) > max(ce_change, pe_change) * 0.5:
        score -= 10  # Both sides building = uncertainty
    
    return max(30, min(100, score))


def _find_key_signal(alerts: list[dict]) -> dict:
    """Find the strongest/most important signal from alerts."""
    if not alerts:
        return {}
    
    # Prioritize OI spikes/unwinds with highest percentage
    oi_alerts = [a for a in alerts if a.get("alert_type") in {"OI_SPIKE", "OI_UNWIND"}]
    if oi_alerts:
        return max(oi_alerts, key=lambda a: abs(float(_detail(a).get("pct_change", 0))))
    
    # Fall back to highest severity
    high_alerts = [a for a in alerts if a.get("severity") == "HIGH"]
    if high_alerts:
        return high_alerts[0]
    
    return alerts[0]


def _format_key_signal(alert: dict) -> str:
    """Format the key signal with emphasis."""
    if not alert:
        return "⚠️ NO DOMINANT SIGNAL\n   → Mixed signals, no clear direction"
    
    detail = _detail(alert)
    atype = alert.get("alert_type", "")
    strike = alert.get("strike")
    opt = alert.get("option_type") or ""
    leg = f"{_fmt_num(strike)} {opt}".strip() if strike else ""
    
    if atype == "OI_SPIKE":
        pct = float(detail.get("pct_change", 0))
        oi_from = detail.get("oi_from", "?")
        oi_to = detail.get("oi_to", "?")
        interpretation = "Massive resistance wall forming" if opt == "CE" else "Strong support building"
        action = "Sellers aggressively capping upside" if opt == "CE" else "Sellers confident price won't fall"
        return f"🔥 {leg}: OI SPIKE {pct:+.1f}% ({oi_from}→{oi_to})\n   → {interpretation}\n   → {action}"
    
    elif atype == "OI_UNWIND":
        pct = float(detail.get("pct_change", 0))
        interpretation = "Bulls exiting aggressively" if opt == "PE" else "Bears covering shorts"
        return f"🔥 {leg}: OI UNWINDING {pct:.1f}%\n   → {interpretation}"
    
    elif atype == "BUILDUP_CLASSIFY":
        buildup_type = detail.get("buildup_type", "")
        oi_pct = detail.get("oi_pct", 0)
        ltp_pct = detail.get("ltp_pct", 0)
        return f"🔥 {leg}: {buildup_type.upper()}\n   → OI {oi_pct:+.1f}% | LTP {ltp_pct:+.1f}%"
    
    return f"🔥 {_signal_line(alert)}"


def _build_market_structure(alerts: list[dict], verdict: str) -> str:
    """Build market structure section showing CE/PE buildups and unwinding."""
    ce_buildups = []
    pe_buildups = []
    ce_unwinds = []
    pe_unwinds = []
    
    for alert in alerts:
        detail = _detail(alert)
        atype = alert.get("alert_type", "")
        strike = alert.get("strike")
        opt = alert.get("option_type") or ""
        sev = alert.get("severity", "LOW")
        
        if not strike or not opt:
            continue
        
        leg = f"{_fmt_num(strike)} {opt}"
        pct = float(detail.get("pct_change", 0))
        sev_tag = f"[{sev[:3]}]"
        
        if atype == "OI_SPIKE":
            if opt == "CE":
                ce_buildups.append(f"• {leg}: {pct:+.1f}% {sev_tag}")
            else:
                pe_buildups.append(f"• {leg}: {pct:+.1f}% {sev_tag}")
        elif atype == "OI_UNWIND":
            if opt == "CE":
                ce_unwinds.append(f"• {leg}: {pct:.1f}% {sev_tag}")
            else:
                pe_unwinds.append(f"• {leg}: {pct:.1f}% {sev_tag}")
        elif atype == "BUILDUP_CLASSIFY":
            buildup_type = detail.get("buildup_type", "")
            if "Buildup" in buildup_type and opt == "CE":
                ce_buildups.append(f"• {leg}: {buildup_type} {sev_tag}")
            elif "Buildup" in buildup_type and opt == "PE":
                pe_buildups.append(f"• {leg}: {buildup_type} {sev_tag}")
            elif "Unwinding" in buildup_type and opt == "CE":
                ce_unwinds.append(f"• {leg}: {buildup_type} {sev_tag}")
            elif "Unwinding" in buildup_type and opt == "PE":
                pe_unwinds.append(f"• {leg}: {buildup_type} {sev_tag}")
    
    lines = []
    
    # Show relevant sections based on verdict
    is_bearish = _is_bearish_verdict(verdict)
    is_bullish = _is_bullish_verdict(verdict)
    
    if is_bearish:
        if ce_buildups:
            lines.append("📉 CE Buildup (Bearish):")
            lines.extend(ce_buildups[:4])
        if pe_unwinds:
            lines.append("\n📉 PE Unwinding (Bearish):")
            lines.extend(pe_unwinds[:4])
    elif is_bullish:
        if pe_buildups:
            lines.append("📈 PE Buildup (Bullish):")
            lines.extend(pe_buildups[:4])
        if ce_unwinds:
            lines.append("\n📈 CE Unwinding (Bullish):")
            lines.extend(ce_unwinds[:4])
    else:
        # Neutral - show both sides
        if ce_buildups:
            lines.append("CE Buildup:")
            lines.extend(ce_buildups[:3])
        if pe_buildups:
            lines.append("\nPE Buildup:")
            lines.extend(pe_buildups[:3])
    
    if not lines:
        return "No significant OI structure changes"
    
    # Add interpretation
    lines.append("\n📊 What This Means:")
    if is_bearish:
        lines.append("→ CE buildup = Sellers expect price to stay below resistance")
        if pe_unwinds:
            lines.append("→ PE unwinding = Bulls giving up, expecting downside")
        lines.append("→ Combined signal = Strong bearish pressure")
    elif is_bullish:
        lines.append("→ PE buildup = Sellers expect price to stay above support")
        if ce_unwinds:
            lines.append("→ CE unwinding = Bears closing shorts, expecting upside")
        lines.append("→ Combined signal = Bullish bias")
    else:
        lines.append("→ Mixed signals = Range-bound expectation")
        lines.append("→ No clear breakout signal yet")
    
    return "\n".join(lines)


def _build_trading_plan(verdict: str, confidence: int, scan_context: dict, intel: dict) -> str:
    """Build actionable trading plan section."""
    lines = []
    
    support = scan_context.get("support")
    resistance = scan_context.get("resistance")
    atm = scan_context.get("atm_strike")
    
    is_bearish = _is_bearish_verdict(verdict)
    is_bullish = _is_bullish_verdict(verdict)
    
    # Recommended actions
    lines.append("✅ Recommended:")
    if is_bearish and resistance:
        lines.append(f"• Sell {_fmt_num(resistance)} CE / {_fmt_num(resistance + 50)} CE (collect premium at resistance)")
        if support:
            lines.append(f"• Sell {_fmt_num(support)} PE (if spot holds above {_fmt_num(support)})")
    elif is_bullish and support:
        lines.append(f"• Sell {_fmt_num(support)} PE / {_fmt_num(support - 50)} PE (collect premium at support)")
        if resistance:
            lines.append(f"• Buy {_fmt_num(resistance)} CE (if spot breaks above {_fmt_num(resistance)} with volume)")
    else:
        if atm:
            lines.append(f"• Range trading: Sell {_fmt_num(atm + 50)} CE + {_fmt_num(atm - 50)} PE (iron condor)")
        lines.append("• Wait for breakout confirmation before directional trades")
    
    # Avoid actions
    lines.append("\n❌ Avoid:")
    if is_bearish:
        lines.append("• Buying PEs (unwinding suggests more fall)")
        if resistance:
            lines.append(f"• Buying CEs above {_fmt_num(resistance)} (strong resistance)")
    elif is_bullish:
        lines.append("• Buying CEs (buildup suggests upside capped)")
        if support:
            lines.append(f"• Buying PEs below {_fmt_num(support)} (strong support)")
    else:
        lines.append("• Directional bets (low confidence)")
        lines.append("• Aggressive positions (mixed signals)")
    
    # Risk management
    lines.append("\n⚠️ Risk Management:")
    if is_bearish and resistance:
        lines.append(f"• Stop: If spot breaks above {_fmt_num(resistance + 50)} with volume")
        if support:
            lines.append(f"• Target: {_fmt_num(support)} support, then {_fmt_num(support - 50)}")
    elif is_bullish and support:
        lines.append(f"• Stop: If spot breaks below {_fmt_num(support - 50)} decisively")
        if resistance:
            lines.append(f"• Target: {_fmt_num(resistance)} resistance, then {_fmt_num(resistance + 50)}")
    else:
        if resistance and support:
            lines.append(f"• Stop: If spot breaks {_fmt_num(resistance)} or {_fmt_num(support)} decisively")
        lines.append("• Strategy: Neutral strategies only (straddle/strangle)")
    
    if confidence < 60:
        lines.append("• Caution: Low confidence = smaller position size")
    
    # Add conflict warning if exists
    if intel.get("conflict"):
        lines.append(f"• ⚠️ {intel['conflict']}")
    
    return "\n".join(lines)


def _build_confirmation_section(chart_payload: dict, scan_context: dict, verdict: str) -> str:
    """Build confirmation signals section."""
    lines = []
    
    # Candles
    candles_1h = chart_payload.get("1h", {}).get("sentiment", "NEUTRAL").upper()
    candles_3h = chart_payload.get("3h", {}).get("sentiment", "NEUTRAL").upper()
    
    arrow_1h = "▲" if candles_1h == "BULLISH" else ("▼" if candles_1h == "BEARISH" else "→")
    arrow_3h = "▲" if candles_3h == "BULLISH" else ("▼" if candles_3h == "BEARISH" else "→")
    
    conflict = ""
    is_bearish = _is_bearish_verdict(verdict)
    is_bullish = _is_bullish_verdict(verdict)
    
    if is_bullish and (candles_1h == "BEARISH" or candles_3h == "BEARISH"):
        conflict = " ⚠️ CONFLICT"
    elif is_bearish and (candles_1h == "BULLISH" or candles_3h == "BULLISH"):
        conflict = " ⚠️ CONFLICT"
    
    lines.append(f"Candles: 1H {candles_1h} {arrow_1h} | 3H {candles_3h} {arrow_3h}{conflict}")
    
    # OI Bias
    ce_change = scan_context.get("ce_oi_change", 0)
    pe_change = scan_context.get("pe_oi_change", 0)
    
    if ce_change > 0 and pe_change < 0:
        oi_bias = "CE buildup + PE unwinding → BEARISH"
    elif pe_change > 0 and ce_change < 0:
        oi_bias = "PE buildup + CE unwinding → BULLISH"
    elif ce_change > 0 and pe_change > 0:
        oi_bias = "Both CE & PE buildup → NEUTRAL"
    else:
        oi_bias = "Mixed OI changes → NEUTRAL"
    
    lines.append(f"OI Bias: {oi_bias}")
    
    return "\n".join(lines)


def _build_bottom_line(verdict: str, confidence: int, key_signal_alert: dict, scan_context: dict) -> str:
    """Build concise bottom line summary."""
    is_bearish = _is_bearish_verdict(verdict)
    is_bullish = _is_bullish_verdict(verdict)
    
    # Setup description
    if confidence >= 75:
        setup = "Strong"
    elif confidence >= 60:
        setup = "Moderate"
    else:
        setup = "Weak"
    
    direction = "bearish" if is_bearish else ("bullish" if is_bullish else "neutral")
    
    # Key level
    resistance = scan_context.get("resistance")
    support = scan_context.get("support")
    
    if is_bearish and resistance:
        key_level = f"{_fmt_num(resistance)} as key resistance"
    elif is_bullish and support:
        key_level = f"{_fmt_num(support)} as key support"
    else:
        key_level = "range-bound"
    
    # Trade suggestion
    if is_bearish and resistance:
        trade = f"Sell CEs at {_fmt_num(resistance)}-{_fmt_num(resistance + 50)}"
        watch = f"Watch {_fmt_num(support)} support - break = accelerated fall" if support else "Watch for breakdown"
    elif is_bullish and support:
        trade = f"Sell PEs at {_fmt_num(support)}-{_fmt_num(support - 50)}"
        watch = f"Watch {_fmt_num(resistance)} resistance - break = rally" if resistance else "Watch for breakout"
    else:
        trade = "Range trading or wait for breakout"
        watch = "Patience is key"
    
    # Confidence note
    if confidence < 60:
        conf_note = f"Low confidence ({confidence}%) = avoid directional trades"
    else:
        conf_note = f"Multiple confirmations"
    
    return f"{setup} {direction} setup with {key_level}. {conf_note}. Trade: {trade}. {watch}."


def build_enhanced_digest(
    symbol: str,
    alerts: list[dict],
    fetched_at: str | None = None,
    scan_context: dict | None = None,
    intelligence_text: str | None = None,
    detected_count: int | None = None,
    dedup_suppressed_count: int | None = None,
) -> tuple[str, str]:
    """Build enhanced telegram digest with improved structure and clarity."""
    digest_id = str(uuid.uuid4())[:8]
    try:
        dt = datetime.fromisoformat(fetched_at or "").astimezone(IST)
    except Exception:
        dt = datetime.now(IST)
    ts = dt.strftime("%d %b, %H:%M")
    
    ctx = scan_context or {}
    n = len(alerts)
    px_label = _price_label(symbol)
    
    # Handle no signals case
    if not alerts:
        return build_digest(symbol, alerts, fetched_at, scan_context, intelligence_text, detected_count, dedup_suppressed_count)
    
    # Generate intelligence
    intel_raw = intelligence_text if intelligence_text is not None else generate_intelligence(symbol, alerts, scan_context=scan_context)
    intel = _parse_intelligence(intel_raw)
    verdict = intel.get("verdict", "Sideways")
    emoji, label = _VERDICT_STYLE.get(verdict, (EMOJI_WHITE, "NEUTRAL"))
    
    # Get chart data
    chart_payload = _chart_payload_for_symbol(ctx, symbol)
    
    # Calculate confidence
    confidence = _calculate_confidence_score(alerts, intel, ctx, chart_payload)
    
    # Build confidence bar
    filled = round(confidence / 10)
    conf_bar = ("\u2588" * filled) + ("\u2591" * (10 - filled))
    
    # Find key signal
    key_signal_alert = _find_key_signal(alerts)
    key_signal_formatted = _format_key_signal(key_signal_alert)
    
    # Get price change
    price_change_pct = ctx.get("price_change_pct")
    price_change_points = ctx.get("price_change_points")
    
    try:
        d_spot = float(price_change_pct) if price_change_pct is not None else 0.0
    except Exception:
        d_spot = 0.0
    try:
        d_points = float(price_change_points or 0.0)
    except Exception:
        d_points = 0.0
    
    pct_digits = 3 if abs(d_spot) < 0.01 and d_spot != 0 else 2
    
    if price_change_pct is None:
        spot_delta = "no prev data"
    elif abs(d_points) < 0.05 and abs(d_spot) < 0.005:
        spot_delta = "flat"
    else:
        spot_delta = f"{_fmt_signed(d_points, 1)} (`{_fmt_signed(d_spot, pct_digits)}%`)"
    
    # Build sections
    market_structure = _build_market_structure(alerts, verdict)
    trading_plan = _build_trading_plan(verdict, confidence, ctx, intel)
    confirmation = _build_confirmation_section(chart_payload, ctx, verdict)
    bottom_line = _build_bottom_line(verdict, confidence, key_signal_alert, ctx)
    
    # Build levels section
    levels_parts = []
    if ctx.get("support"):
        levels_parts.append(f"Support: `{_fmt_num(ctx.get('support'))}`")
    if ctx.get("resistance"):
        levels_parts.append(f"Resistance: `{_fmt_num(ctx.get('resistance'))}`")
    levels_section = " | ".join(levels_parts) if levels_parts else "No key levels identified"
    
    # Build message
    lines = [
        f"📊 {symbol} | {ts} | {n} signals",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"{emoji} {label} - {_clip(verdict, 45)}",
        f"Confidence: {conf_bar} {confidence}%",
        "",
        f"{px_label} `{_fmt_num(ctx.get('underlying'))}` | ATM `{_fmt_num(ctx.get('atm_strike'))}` | PCR `{_fmt_num(ctx.get('pcr'), 2)}`",
        f"Δ prev scan: {px_label} `{spot_delta}` | CE OI `{_fmt_oi(ctx.get('ce_oi_change', 0))}` | PE OI `{_fmt_oi(ctx.get('pe_oi_change', 0))}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "⚡ KEY SIGNAL",
        "",
        key_signal_formatted,
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 MARKET STRUCTURE",
        "",
        market_structure,
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "🎯 TRADING PLAN",
        "",
        trading_plan,
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📍 KEY LEVELS",
        "",
        levels_section,
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📈 CONFIRMATION SIGNALS",
        "",
        confirmation,
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "💡 BOTTOM LINE",
        "",
        bottom_line,
        "",
        f"_#{digest_id} · {n} signals_",
    ]
    
    message = "\n".join(lines)
    return digest_id, _fit_telegram(message, digest_id)


# Wrapper to choose between old and new template
def build_digest_wrapper(
    symbol: str,
    alerts: list[dict],
    fetched_at: str | None = None,
    scan_context: dict | None = None,
    intelligence_text: str | None = None,
    detected_count: int | None = None,
    dedup_suppressed_count: int | None = None,
) -> tuple[str, str]:
    """Wrapper that chooses between old and enhanced template based on config."""
    if USE_ENHANCED_TEMPLATE:
        return build_enhanced_digest(symbol, alerts, fetched_at, scan_context, intelligence_text, detected_count, dedup_suppressed_count)
    else:
        return build_digest(symbol, alerts, fetched_at, scan_context, intelligence_text, detected_count, dedup_suppressed_count)
