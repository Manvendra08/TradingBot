"""Telegram scan digest builder."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from src.engine.intelligence import generate_intelligence

IST = timezone(timedelta(hours=5, minutes=30))
MAX_TELEGRAM_LEN = 3900

EMOJI_GREEN = "\U0001F7E2"
EMOJI_RED = "\U0001F534"
EMOJI_YELLOW = "\U0001F7E1"
EMOJI_BLUE = "\U0001F535"
EMOJI_WHITE = "\u26AA"
EMOJI_CANDLES = "\U0001F56F\ufe0f"

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
            f"Spot `{_fmt_num(ctx.get('underlying'))}` | PCR `{_fmt_num(ctx.get('pcr'), 2)}`",
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
        f"Spot `{_fmt_num(ctx.get('underlying'))}` | ATM `{_fmt_num(ctx.get('atm_strike'))}` | PCR `{_fmt_num(ctx.get('pcr'), 2)}`",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{emoji} *{label}* - {_clip(intel['verdict'], 45)}",
    ]
    try:
        d_spot = float(ctx.get("price_change_pct") or 0.0)
    except Exception:
        d_spot = 0.0
    try:
        d_points = float(ctx.get("price_change_points") or 0.0)
    except Exception:
        d_points = 0.0
    pct_digits = 3 if abs(d_spot) < 0.01 and d_spot != 0 else 2
    
    # If price_change_pct is None (no previous data), show "no prev data" instead of "flat"
    if ctx.get("price_change_pct") is None:
        spot_delta = "no prev data"
    elif abs(d_points) < 0.05 and abs(d_spot) < 0.005:
        spot_delta = "flat"
    else:
        spot_delta = f"{_fmt_signed(d_points, 1)} (`{_fmt_signed(d_spot, pct_digits)}%`)"
    lines.append(
        f"Δ prev scan: Spot `{spot_delta}` | CE OI `{_fmt_oi(ctx.get('ce_oi_change', 0))}` | PE OI `{_fmt_oi(ctx.get('pe_oi_change', 0))}`"
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
    lines += ["", "\u2696\ufe0f *Balance*", f"{EMOJI_GREEN} {bull}", f"{EMOJI_RED} {bear}"]

    lines += ["", f"\U0001F30A *Trend:* {_trend_text(intel['trend'], intel['verdict'])}"]
    lines += ["", f"_#{digest_id} · {n} signals · all symbols enabled_"]
    return digest_id, _fit_telegram("\n".join(lines), digest_id)
