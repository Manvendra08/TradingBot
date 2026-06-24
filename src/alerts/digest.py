"""Telegram scan digest builder."""
from __future__ import annotations

import json
import logging
import re
import uuid

log = logging.getLogger(__name__)
from datetime import datetime, timedelta, timezone

from src.engine.intelligence import generate_intelligence

IST = timezone(timedelta(hours=5, minutes=30))
MAX_TELEGRAM_LEN = 3900
USE_ENHANCED_TEMPLATE = True  # Toggle between old and new template
_LEGACY_DIGEST = False


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


def _esc(text: any) -> str:
    """Escapes markdown formatting characters for Telegram Markdown V1."""
    if text is None:
        return ""
    s = str(text)
    for char in ('\\', '_', '*', '`', '['):
        s = s.replace(char, f"\\{char}")
    return s


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


def _format_expiry_and_dte(expiry_str: str | None) -> tuple[str, str]:
    """Returns (formatted_expiry_str, dte_str)"""
    if not expiry_str:
        return "", ""
    try:
        exp_date = datetime.strptime(str(expiry_str).strip(), "%Y-%m-%d").date()
        today_date = datetime.now(timezone.utc).date()
        days_to_expiry = (exp_date - today_date).days
        formatted = exp_date.strftime("%d %b")
        return formatted, f"{days_to_expiry} DTE"
    except Exception:
        return str(expiry_str), ""


def _fmt_num(value, digits: int = 0) -> str:
    try:
        value = float(value)
    except Exception:
        return "N/A"
    if digits:
        return f"{value:.{digits}f}"
    return f"{value:.0f}"


def _fmt_val(value, symbol: str) -> str:
    if value is None:
        return "N/A"
    base = symbol.upper().split()[0] if symbol else ""
    digits = 2 if base in {"NATURALGAS", "SILVER"} else 0
    return _fmt_num(value, digits)


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


def _get_symbol_offset(symbol: str) -> float:
    base = _norm_symbol(symbol)
    if "NATURALGAS" in base:
        return 5.0
    elif "CRUDEOIL" in base:
        return 100.0
    elif "GOLD" in base:
        return 500.0
    elif "SILVER" in base:
        return 1000.0
    elif "BANKNIFTY" in base:
        return 100.0
    elif "MIDCP" in base:
        return 25.0
    else:
        return 50.0


def _price_label(symbol: str) -> str:
    base = _norm_symbol(symbol).split()[0] if symbol else ""
    return "Future" if base in _FUTURE_SYMBOLS else "Spot"



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


def _parse_intelligence(raw) -> dict:
    """
    Parse intelligence text into a structured dict for digest rendering.

    Phase 3: If `raw` is an IntelligenceResult, extract fields directly
    (no regex). Falls back to text parsing for legacy string input.
    """
    # ── Phase 3: structured fast-path ─────────────────────────────────────
    try:
        from src.engine.intelligence import IntelligenceResult
        if isinstance(raw, IntelligenceResult):
            return {
                "verdict":    raw.verdict_label,
                "desc":       raw.verdict_desc,
                "confidence": raw.confidence,
                "action":     raw.action_plan,
                "paper":      "",   # paper trade line not needed in digest header
                "warning":    raw.risk_note,
                "oi_note":    "",
                "bull":       [f"P{1 if s>=90 else 2 if s>=70 else 3} [{s}] {t}" for s, t in (raw.bull_forces or [])],
                "bear":       [f"P{1 if s>=90 else 2 if s>=70 else 3} [{s}] {t}" for s, t in (raw.bear_forces or [])],
                "trend":      raw.trend,
                "conflict":   "Chart conflict: 1H vs 3H signals disagree" if raw.chart_conflict else "",
            }
    except ImportError:
        pass

    # ── Legacy: parse from Telegram text string ────────────────────────────
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


def _format_trade_status(status: dict | None, is_live: bool = False) -> str:
    label_prefix = "live" if is_live else "paper"
    if not status:
        return f"• *Status:* NO ACTION | No {label_prefix} trade logic evaluated."

    action = status.get("action")
    if action == "EXECUTED":
        trade = status.get("trade", {})
        setup = status.get("setup_type", "CORE")
        opt = trade.get("option_type", "CE")
        strike = trade.get("strike")
        entry = trade.get("entry_premium") or trade.get("entry_underlying")
        sl = trade.get("sl_premium") or trade.get("sl_underlying")
        tgt = trade.get("target_premium") or trade.get("target_underlying")
        lots = status.get("lots") or trade.get("lots") or 10
        side = str(trade.get("side") or status.get("side") or "BUY").title()

        entry_str = f"{entry:.2f}" if entry is not None else "—"
        sl_str = f"{sl:.2f}" if sl is not None else "—"
        tgt_str = f"{tgt:.2f}" if tgt is not None else "—"
        strike_str = f"{strike:g}" if strike is not None else "—"

        if opt == "FUT":
            details = f"{side} FUT @ {entry_str} | SL: {sl_str} | Target: {tgt_str} (Lots: {lots})"
        else:
            details = f"{side} {strike_str} {opt} @ {entry_str} | SL: {sl_str} | Target: {tgt_str} (Lots: {lots})"
        return (
            f"• *Status:* EXECUTED ({_esc(setup)})\n"
            f"• *Details:* {_esc(details)}\n"
            f"• *Reason:* {_esc(status.get('reason', 'Signal filters passed'))}"
        )
    elif action == "CLOSED":
        trade = status.get("trade", {})
        opt = trade.get("option_type", "CE")
        strike = trade.get("strike")
        pnl = trade.get("pnl_rupees")
        if pnl is None:
            pnl = 0.0
        pnl_sign = "+" if pnl > 0 else ""
        side = str(trade.get("side") or "BUY").title()
        strike_str = f"{strike:g}" if strike is not None else "—"
        if opt == "FUT":
            details = f"{side} FUT trade closed | P&L: {pnl_sign}₹{pnl:,.2f}"
        else:
            details = f"{side} {strike_str} {opt} trade closed | P&L: {pnl_sign}₹{pnl:,.2f}"
        return (
            f"• *Status:* CLOSED\n"
            f"• *Details:* {_esc(details)}\n"
            f"• *Reason:* {_esc(status.get('reason') or trade.get('reason') or 'Exit conditions met')}"
        )
    elif action == "HELD":
        trade = status.get("trade", {})
        opt = trade.get("option_type", "CE")
        strike = trade.get("strike")
        entry = trade.get("entry_premium") or trade.get("entry_underlying")
        lots = trade.get("lots", 10)
        side = str(trade.get("side") or "BUY").title()
        strike_str = f"{strike:g}" if strike is not None else "—"
        opened_at = trade.get("opened_at")
        opened_at_str = str(opened_at)[:16].replace("T", " ") if opened_at else "—"
        if opt == "FUT":
            details = f"{side} FUT position open since {opened_at_str}"
        else:
            details = f"{side} {strike_str} {opt} open since {opened_at_str}"
        return (
            f"• *Status:* HELD ({_esc(details)})\n"
            f"• *Action:* Monitoring exits"
        )
    elif action and action.startswith("BLOCKED"):
        return f"• *Status:* BLOCKED | *Reason:* {_esc(status.get('reason', 'Filters not met'))}"
    elif action == "SKIPPED_MARKET_CLOSED":
        return f"• *Status:* SKIPPED | *Reason:* Market is currently closed"
    else:
        return f"• *Status:* NO TRADE | *Reason:* {_esc(status.get('reason', 'No directional setup'))}"

def _format_paper_trade_status(status: dict | None) -> str:
    return _format_trade_status(status, is_live=False)


def build_digest(
    symbol: str,
    alerts: list[dict],
    fetched_at: str | None = None,
    scan_context: dict | None = None,
    intelligence_text: str | None = None,
    detected_count: int | None = None,
    dedup_suppressed_count: int | None = None,
    digest_id: str | None = None,
    paper_trade_status: dict | None = None,
    live_trade_status: dict | None = None,
    llm_verdict: dict | None = None,
    exit_advice: any = None,
) -> tuple[str, str]:
    if not _LEGACY_DIGEST:
        log.warning("Legacy build_digest called but disabled. Redirecting to build_llm_consolidated_digest.")
        return build_llm_consolidated_digest(
            symbol, alerts, fetched_at, scan_context,
            detected_count, dedup_suppressed_count, digest_id,
            paper_trade_status, live_trade_status, llm_verdict,
            exit_advice=exit_advice
        )
    if digest_id is None:
        digest_id = str(uuid.uuid4())[:8]
    try:
        dt = datetime.fromisoformat(fetched_at or "").astimezone(IST)
    except Exception:
        dt = datetime.now(IST)
    ts = dt.strftime("%H:%M IST")

    ctx = scan_context or {}
    _base_sym0 = symbol.upper().strip().split()[0]
    _is_mcx0 = _base_sym0 in {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}
    expiry_val = ctx.get("futures_expiry") if (_is_mcx0 and ctx.get("futures_expiry")) else ctx.get("expiry")
    exp_fmt, dte_lbl = _format_expiry_and_dte(expiry_val)
    header_extra = f" (Exp: {exp_fmt} | {dte_lbl})" if exp_fmt and dte_lbl else (f" (Exp: {exp_fmt})" if exp_fmt else "")

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
            
        ai_part = ""
        if llm_verdict:
            action = llm_verdict.get("action") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "action", "")
            conf = llm_verdict.get("confidence") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "confidence", 0)
            instrument = llm_verdict.get("instrument") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "instrument", "")
            entry_trigger = llm_verdict.get("entry_trigger") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "entry_trigger", "")
            stop_loss = llm_verdict.get("stop_loss") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "stop_loss", "")
            target_1 = llm_verdict.get("target_1") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "target_1", "")
            thesis = llm_verdict.get("thesis") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "thesis", "")
            risk = llm_verdict.get("risk_rating") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "risk_rating", "")
            
            action_emoji = {"GO_LONG": "🟢", "GO_SHORT": "🔴", "NO_TRADE": "⚪"}.get(action, "❓")
            ai_part = (
                f"\n{action_emoji} *AI Trade Plan* ({action}, {conf}%)\n"
                f"📋 Contract: {_esc(instrument)}\n"
                f"🎯 Entry: {_esc(entry_trigger)}\n"
                f"🛑 SL: {_esc(stop_loss)} | T1: {_esc(target_1)}\n"
                f"💡 {_esc(thesis)}\n"
                f"⚠️ Risk: {_esc(risk)}\n"
            )

        msg = "\n".join([
            f"\U0001F4CA *{symbol}*{header_extra} | {ts} | {title}",
            f"{'━' * 20}",
            f"{px_label} `{_fmt_num(ctx.get('underlying'))}` | PCR `{_fmt_num(ctx.get('pcr'), 2)}`",
            f"{EMOJI_WHITE} *Market quiet*",
            quiet_note,
            ai_part,
            f"OI max `{max_oi:.2f}%` | ATM LTP max `{max_ltp:.2f}%`",
            f"_#{digest_id} · all symbols enabled_",
            f"{'━' * 20}",
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
        f"\U0001F4CA *{symbol}*{header_extra} | {ts} | {n} signals",
        f"{'━' * 20}",
        counts or "No severity count",
        f"{px_label} `{_fmt_num(ctx.get('underlying'))}` | ATM `{_fmt_num(ctx.get('atm_strike'))}` | PCR `{_fmt_num(ctx.get('pcr'), 2)}`",
        f"{emoji} *{_esc(label)}* - {_esc(_clip(intel['verdict'], 45))}",
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
        lines.append(_esc(_clip(intel["desc"], 100)))
    lines.append(f"Confidence: `{_bar(confidence)}`")
 
    lines += [
        "",
        "\U0001F3AF *What to do*",
        f"• {_esc(_clip(action, 150))}",
        f"• {_esc(_clip(warning, 130))}",
    ]
    if intel["paper"]:
        lines.append(f"• Paper: {_esc(_clip(intel['paper'], 120))}")

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
            lines.append(_esc(_clip(intel["oi_note"], 120)))

    cap = 8 if high >= 5 else 10
    lines += ["", "\U0001F4E1 *Top signals*"]
    for alert in sorted_alerts[:cap]:
        badge = {"HIGH": EMOJI_RED, "MEDIUM": EMOJI_YELLOW, "LOW": EMOJI_BLUE}.get(alert.get("severity", "LOW"), EMOJI_BLUE)
        lines.append(f"{badge} {_esc(_clip(_signal_line(alert), 130))}")
    hidden = len(sorted_alerts) - cap
    if hidden > 0:
        lines.append(f"...and {hidden} more signals.")

    bull = _clip(intel["bull"][0], 110) if intel["bull"] else "No strong bullish factor"
    bear = _clip(intel["bear"][0], 110) if intel["bear"] else "No strong bearish factor"
    lines += ["", "\u2696\ufe0f *Balance*"]
    if _is_bullish_verdict(intel["verdict"]):
        lines += [f"{EMOJI_GREEN} Primary: {_esc(bull)}", f"{EMOJI_YELLOW} Caution: {_esc(bear)}"]
    elif _is_bearish_verdict(intel["verdict"]):
        lines += [f"{EMOJI_RED} Primary: {_esc(bear)}", f"{EMOJI_YELLOW} Caution: {_esc(bull)}"]
    else:
        lines += [f"{EMOJI_GREEN} {_esc(bull)}", f"{EMOJI_RED} {_esc(bear)}"]
 
    lines += ["", f"\U0001F30A *Trend:* {_esc(_trend_text(intel['trend'], intel['verdict']))}"]
    if llm_verdict:
        # Support both new (action-oriented) and old (bias-oriented) schemas
        action = llm_verdict.get("action") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "action", "")
        conf = llm_verdict.get("confidence") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "confidence", 0)
        instrument = llm_verdict.get("instrument") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "instrument", "")
        entry_trigger = llm_verdict.get("entry_trigger") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "entry_trigger", "")
        stop_loss = llm_verdict.get("stop_loss") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "stop_loss", "")
        target_1 = llm_verdict.get("target_1") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "target_1", "")
        thesis = llm_verdict.get("thesis") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "thesis", "")
        risk = llm_verdict.get("risk_rating") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "risk_rating", "")
        invalidation = llm_verdict.get("invalidation") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "invalidation", "")

        action_emoji = {"GO_LONG": EMOJI_GREEN, "GO_SHORT": EMOJI_RED, "NO_TRADE": EMOJI_WHITE}.get(action, EMOJI_YELLOW)
        lines += [
            "",
            f"{action_emoji} *AI Trade Plan* ({_esc(action)}, {conf}%)",
            f"📋 {_esc(instrument)}",
            f"🎯 Entry: {_esc(entry_trigger)}",
            f"🛑 SL: {_esc(stop_loss)} | T1: {_esc(target_1)}",
            f"💡 {_esc(thesis)}",
            f"⚠️ Risk: {_esc(risk)} | Invalidation: {_esc(invalidation)}",
        ]
    if paper_trade_status:
        lines += ["", "🤖 *PAPER TRADE STATUS*", _format_paper_trade_status(paper_trade_status)]
    if live_trade_status:
        lines += ["", "🟢 *LIVE/SHADOW TRADE STATUS*", _format_trade_status(live_trade_status, is_live=True)]
    lines += ["", f"_#{digest_id} · {n} signals · all symbols enabled_"]
    lines += [f"{'━' * 20}"]
    return digest_id, _fit_telegram("\n".join(lines), digest_id)



# ═══════════════════════════════════════════════════════════════════════════
# ENHANCED TELEGRAM TEMPLATE  (redesigned)
# ═══════════════════════════════════════════════════════════════════════════

DIVIDER = "\u2500" * 20  # single canonical divider


def _verdict_bias(verdict: str) -> str:
    """Map verdict -> one canonical bias string. Single source of truth."""
    if _is_bullish_verdict(verdict):
        return "BULLISH"
    if _is_bearish_verdict(verdict):
        return "BEARISH"
    return "NEUTRAL"


def _oi_flow_read(ce_change, pe_change) -> tuple[str, str]:
    """
    Interpret CE/PE OI deltas. Returns (bias, human_text).
    Handles all four quadrants INCLUDING both-negative (both unwinding),
    which the old code wrongly collapsed to 'NEUTRAL'.
    """
    try:
        ce = float(ce_change or 0)
        pe = float(pe_change or 0)
    except Exception:
        ce = pe = 0.0

    EPS = 1.0  # ignore noise around zero
    ce_up, ce_dn = ce > EPS, ce < -EPS
    pe_up, pe_dn = pe > EPS, pe < -EPS

    if pe_up and ce_dn:
        return "BULLISH", "PE buildup + CE unwinding"
    if ce_up and pe_dn:
        return "BEARISH", "CE buildup + PE unwinding"
    if ce_up and pe_up:
        # whoever builds more wins; near-equal = genuine standoff
        if pe > ce * 1.5:
            return "BULLISH", "Both building, PE-heavy"
        if ce > pe * 1.5:
            return "BEARISH", "Both building, CE-heavy"
        return "NEUTRAL", "Both CE & PE buildup"
    if ce_dn and pe_dn:
        # BOTH UNWINDING — direction = whoever exits faster
        if pe < ce * 1.5:  # pe more negative
            return "BEARISH", "Both unwinding, PE exits faster"
        if ce < pe * 1.5:
            return "BULLISH", "Both unwinding, CE exits faster"
        return "NEUTRAL", "Both sides unwinding"
    return "NEUTRAL", "No decisive OI flow"


def _net_oi_delta(alerts: list[dict], scan_context: dict) -> tuple[int, int]:
    """Net CE / PE OI change. Prefer scan_context, fall back to summing alerts."""
    ce = scan_context.get("ce_oi_change")
    pe = scan_context.get("pe_oi_change")
    if ce is not None and pe is not None:
        try:
            return int(ce), int(pe)
        except Exception:
            pass
    ce_net = pe_net = 0
    for a in alerts:
        d = _detail(a)
        prev, curr = d.get("prev_oi"), d.get("curr_oi")
        if prev and curr is not None:
            delta = int(curr) - int(prev)
            if (a.get("option_type") or "") == "CE":
                ce_net += delta
            else:
                pe_net += delta
    return ce_net, pe_net


def _classify_regime(alerts: list[dict], scan_context: dict, verdict: str) -> tuple[str, str, bool]:
    """
    Returns (regime, banner_text, no_trade).
    Backstop for the engine: catches mass two-sided unwinding even if a verdict
    leaks through. Also honors a non-directional verdict as NO-TRADE.
    """
    oi = [a for a in alerts if a.get("alert_type") in {"OI_SPIKE", "OI_UNWIND"}]
    ce_net, pe_net = _net_oi_delta(alerts, scan_context)

    if oi:
        unwind = sum(1 for a in oi if a.get("alert_type") == "OI_UNWIND")
        ratio = unwind / len(oi)
        both_shrink = ce_net < 0 and pe_net < 0
        if ratio >= 0.7 and both_shrink and len(oi) >= 6:
            return ("SQUARING",
                    f"POSITION SQUARING - both sides exiting ({unwind}/{len(oi)} unwinds)",
                    True)

    if _verdict_bias(verdict) == "NEUTRAL":
        oi_bias, _ = _oi_flow_read(ce_net, pe_net)
        if oi_bias != "NEUTRAL":
            return ("NO_EDGE", f"NO DIRECTIONAL EDGE - conflicting signals ({oi_bias} flow)", True)
        return ("NO_EDGE", "NO DIRECTIONAL EDGE - mixed / rangebound", True)

    return ("TRADEABLE", "", False)


def _calculate_signal_strength(alerts: list[dict], intel: dict, scan_context: dict, chart_payload: dict) -> int:
    """
    Heuristic 30-100 score. NOT a probability. Rendered as a 'signal strength'
    label, never as a confidence percentage, because it is uncalibrated.
    """
    score = 50

    max_oi_pct = 0.0
    for alert in alerts:
        detail = _detail(alert)
        if alert.get("alert_type") in {"OI_SPIKE", "OI_UNWIND"}:
            max_oi_pct = max(max_oi_pct, abs(float(detail.get("pct_change", 0))))
    if max_oi_pct > 200:
        score += 40
    elif max_oi_pct > 100:
        score += 30
    elif max_oi_pct > 50:
        score += 20
    elif max_oi_pct > 30:
        score += 10

    candles_1h = chart_payload.get("1h", {}).get("sentiment", "").upper()
    candles_3h = chart_payload.get("3h", {}).get("sentiment", "").upper()
    verdict = intel.get("verdict", "")
    is_bull = _is_bullish_verdict(verdict)
    is_bear = _is_bearish_verdict(verdict)

    if is_bull:
        if candles_1h == "BULLISH" and candles_3h == "BULLISH":
            score += 15
        elif candles_1h == "BULLISH" or candles_3h == "BULLISH":
            score += 10
        elif candles_1h == "BEARISH" or candles_3h == "BEARISH":
            score -= 15
    elif is_bear:
        if candles_1h == "BEARISH" and candles_3h == "BEARISH":
            score += 15
        elif candles_1h == "BEARISH" or candles_3h == "BEARISH":
            score += 10
        elif candles_1h == "BULLISH" or candles_3h == "BULLISH":
            score -= 15

    pcr = float(scan_context.get("pcr", 1.0) or 1.0)
    if is_bull and pcr > 1.2:
        score += 5
    elif is_bear and pcr < 0.8:
        score += 5

    if sum(1 for a in alerts if a.get("severity") == "HIGH") >= 3:
        score += 5

    # OI flow agreeing with verdict adds, disagreeing subtracts
    oi_bias, _ = _oi_flow_read(scan_context.get("ce_oi_change", 0), scan_context.get("pe_oi_change", 0))
    vbias = _verdict_bias(verdict)
    if oi_bias != "NEUTRAL" and vbias != "NEUTRAL":
        score += 8 if oi_bias == vbias else -12

    return max(30, min(100, score))


def _strength_label(score: int) -> str:
    if score >= 75:
        return "STRONG"
    if score >= 60:
        return "MODERATE"
    if score >= 45:
        return "WEAK"
    return "VERY WEAK"


def _find_key_signal(alerts: list[dict]) -> dict:
    if not alerts:
        return {}
    oi_alerts = [a for a in alerts if a.get("alert_type") in {"OI_SPIKE", "OI_UNWIND"}]
    if oi_alerts:
        return max(oi_alerts, key=lambda a: abs(float(_detail(a).get("pct_change", 0))))
    high_alerts = [a for a in alerts if a.get("severity") == "HIGH"]
    if high_alerts:
        return high_alerts[0]
    return alerts[0]


def _key_signal_supports_verdict(alert: dict, verdict: str) -> bool:
    """Does the headline signal actually agree with the verdict bias?"""
    if not alert:
        return False
    atype = alert.get("alert_type", "")
    opt = alert.get("option_type") or ""
    vbias = _verdict_bias(verdict)
    sig_bias = "NEUTRAL"
    if atype == "OI_UNWIND":
        sig_bias = "BULLISH" if opt == "CE" else "BEARISH"   # CE unwind=bull, PE unwind=bear
    elif atype == "OI_SPIKE":
        sig_bias = "BEARISH" if opt == "CE" else "BULLISH"   # CE buildup=bear, PE buildup=bull
    return sig_bias == vbias and sig_bias != "NEUTRAL"


def _format_key_signal(alert: dict, verdict: str = "") -> str:
    if not alert:
        return "No dominant signal -> mixed, no clear direction"
    detail = _detail(alert)
    atype = alert.get("alert_type", "")
    strike = alert.get("strike")
    opt = alert.get("option_type") or ""
    leg = f"{_fmt_num(strike)} {opt}".strip() if strike else ""

    if atype == "OI_SPIKE":
        pct = float(detail.get("pct_change", 0))
        prev_oi = _fmt_oi(detail.get("prev_oi", 0)) if detail.get("prev_oi") else "?"
        curr_oi = _fmt_oi(detail.get("curr_oi", 0)) if detail.get("curr_oi") else "?"
        interp = "Resistance wall forming" if opt == "CE" else "Support building"
        body = f"{leg}: OI SPIKE {pct:+.1f}% ({prev_oi}->{curr_oi})\n   -> {interp}"
    elif atype == "OI_UNWIND":
        pct = float(detail.get("pct_change", 0))
        interp = "Bulls exiting" if opt == "PE" else "Bears covering shorts"
        body = f"{leg}: OI UNWINDING {pct:.1f}%\n   -> {interp}"
    elif atype == "BUILDUP_CLASSIFY":
        body = f"{leg}: {detail.get('buildup_type','').upper()}\n   -> OI {detail.get('oi_pct',0):+.1f}% | LTP {detail.get('ltp_pct',0):+.1f}%"
    else:
        body = _signal_line(alert)

    # Bug 4 fix: flag when headline signal contradicts the verdict
    if verdict and not _key_signal_supports_verdict(alert, verdict):
        body += "\n   \u26A0\ufe0f Note: headline signal does not confirm the verdict"
    return body


def _build_market_structure(alerts: list[dict], verdict: str) -> str:
    """CE/PE buildup & unwinding. Boilerplate 'What This Means' removed."""
    ce_b, pe_b, ce_u, pe_u = [], [], [], []
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
        tag = f"[{sev.title()}]"
        if atype == "OI_SPIKE":
            (ce_b if opt == "CE" else pe_b).append(f"\u2022 {leg}: {pct:+.1f}% {tag}")
        elif atype == "OI_UNWIND":
            (ce_u if opt == "CE" else pe_u).append(f"\u2022 {leg}: {pct:.1f}% {tag}")
        elif atype == "BUILDUP_CLASSIFY":
            bt = detail.get("buildup_type", "")
            if "Buildup" in bt:
                (ce_b if opt == "CE" else pe_b).append(f"\u2022 {leg}: {bt} {tag}")
            elif "Unwinding" in bt:
                (ce_u if opt == "CE" else pe_u).append(f"\u2022 {leg}: {bt} {tag}")

    lines = []
    is_bear = _is_bearish_verdict(verdict)
    is_bull = _is_bullish_verdict(verdict)
    if is_bear:
        if ce_b:
            lines += ["Call (CE) Activity:", *ce_b[:4]]
        if pe_u:
            lines += ["Put (PE) Unwinding:", *pe_u[:4]]
    elif is_bull:
        if pe_b:
            lines += ["Put (PE) Activity:", *pe_b[:4]]
        if ce_u:
            lines += ["Call (CE) Unwinding:", *ce_u[:4]]
    else:
        if ce_b:
            lines += ["Call (CE) Activity:", *ce_b[:3]]
        if pe_b:
            lines += ["Put (PE) Activity:", *pe_b[:3]]
    return "\n".join(lines) if lines else "No significant OI structure changes"


def _build_trading_plan(symbol: str, verdict: str, strength: int, scan_context: dict, intel: dict) -> str:
    """
    Plan with explicit entry/stop/target levels. Consolidated for brevity.
    """
    lines = []
    px = _price_label(symbol).lower()
    offset = _get_symbol_offset(symbol)
    fmt = lambda v: _fmt_val(v, symbol)
    support = scan_context.get("support")
    resistance = scan_context.get("resistance")
    atm = scan_context.get("atm_strike")
    is_bear = _is_bearish_verdict(verdict)
    is_bull = _is_bullish_verdict(verdict)

    # 1. Recommended
    rec = []
    if is_bear and resistance:
        rec.append(f"\u2022 Sell {fmt(resistance)} CE / {fmt(resistance + offset)} CE (premium at resistance)")
        if support:
            rec.append(f"\u2022 Sell {fmt(support)} PE only if {px} holds above {fmt(support)}")
    elif is_bull and support:
        rec.append(f"\u2022 Sell {fmt(support)} PE / {fmt(support - offset)} PE (premium at support)")
        if resistance:
            rec.append(f"\u2022 Buy {fmt(resistance)} CE only if {px} breaks {fmt(resistance)} with volume")
    else:
        if atm:
            rec.append(f"\u2022 Sell {fmt(atm + offset)} CE + {fmt(atm - offset)} PE")
        rec.append("\u2022 Wait for breakout confirmation")
    if rec:
        lines.append("Recommended:")
        lines.extend(rec)

    # 2. Avoid
    avoid = []
    if is_bear:
        avoid.append("\u2022 Buying CEs (trend is downward)")
        if support:
            avoid.append(f"\u2022 Selling PEs below {fmt(support)}")
    elif is_bull:
        avoid.append("\u2022 Buying PEs (trend is upward)")
        if resistance:
            avoid.append(f"\u2022 Selling CEs above {fmt(resistance)}")
    else:
        avoid.append("\u2022 Directional bets (no clear edge)")
    if avoid:
        lines.append("\nAvoid:")
        lines.extend(avoid)

    # 3. Levels
    lvls = []
    if is_bear and resistance:
        lvls.append(f"\u2022 Stop: {px} closes above {fmt(resistance + offset)}")
        if support:
            lvls.append(f"\u2022 Target: {fmt(support)}, then {fmt(support - offset)}")
    elif is_bull and support:
        # Bug 2 fix: stop is support - offset, NOT an arbitrary far level
        lvls.append(f"\u2022 Stop: {px} closes below {fmt(support - offset)}")
        if resistance:
            lvls.append(f"\u2022 Target: {fmt(resistance)}, then {fmt(resistance + offset)}")
    else:
        if resistance and support:
            lvls.append(f"\u2022 Invalidation: break of {fmt(resistance)} or {fmt(support)}")
    if lvls:
        lines.append("\nLevels:")
        lines.extend(lvls)

    extra_notes = []
    if strength < 60:
        extra_notes.append("\u2022 Low signal strength -> reduce size")
    if intel.get("conflict"):
        extra_notes.append(f"\u2022 \u26A0\ufe0f {intel['conflict']}")
    if extra_notes:
        lines.append("")
        lines.extend(extra_notes)

    return "\n".join(lines)


def _build_confirmation_section(chart_payload: dict, scan_context: dict, verdict: str) -> str:
    """Candles + OI flow. Uses the SAME _oi_flow_read as the strength calc."""
    lines = []
    c1 = chart_payload.get("1h", {}).get("sentiment", "NEUTRAL").upper()
    c3 = chart_payload.get("3h", {}).get("sentiment", "NEUTRAL").upper()

    def arrow(s):
        if s == "BULLISH":
            return f"{EMOJI_GREEN} \u25B2"
        if s == "BEARISH":
            return f"{EMOJI_RED} \u25BC"
        return f"{EMOJI_WHITE} \u2192"

    vbias = _verdict_bias(verdict)
    conflict = ""
    if vbias == "BULLISH" and "BEARISH" in (c1, c3):
        conflict = " \u26A0\ufe0f CONFLICT"
    elif vbias == "BEARISH" and "BULLISH" in (c1, c3):
        conflict = " \u26A0\ufe0f CONFLICT"
    lines.append(f"\u2022 *Candles:* 1H {c1} {arrow(c1)} | 3H {c3} {arrow(c3)}{conflict}")

    oi_bias, oi_text = _oi_flow_read(scan_context.get("ce_oi_change", 0), scan_context.get("pe_oi_change", 0))
    if oi_bias == vbias and vbias != "NEUTRAL":
        agree_lbl = "*[agrees with verdict]*"
    elif oi_bias != "NEUTRAL" and vbias != "NEUTRAL" and oi_bias != vbias:
        agree_lbl = f"*[disagrees with verdict]*"
    else:
        agree_lbl = ""
    
    agree_part = f" {agree_lbl}" if agree_lbl else ""
    lines.append(f"\u2022 *OI Flow:* {oi_text} -> {oi_bias}{agree_part}")
    return "\n".join(lines)


def _build_bottom_line(symbol: str, verdict: str, strength: int, key_signal_alert: dict, scan_context: dict) -> str:
    is_bear = _is_bearish_verdict(verdict)
    is_bull = _is_bullish_verdict(verdict)
    offset = _get_symbol_offset(symbol)
    setup = _strength_label(strength).capitalize()
    direction = "bearish" if is_bear else ("bullish" if is_bull else "neutral")
    resistance = scan_context.get("resistance")
    support = scan_context.get("support")
    fmt = lambda v: _fmt_val(v, symbol)

    if is_bear and resistance:
        key_level = f"{fmt(resistance)} as resistance"
        trade = f"Sell CEs {fmt(resistance)}-{fmt(resistance + offset)}"
        watch = f"Break of {fmt(support)} = accelerated fall" if support else "Watch for breakdown"
    elif is_bull and support:
        key_level = f"{fmt(support)} as support"
        trade = f"Sell PEs {fmt(support)}-{fmt(support - offset)}"
        watch = f"Break of {fmt(resistance)} = rally" if resistance else "Watch for breakout"
    else:
        key_level = "range-bound"
        trade = "Range trade or wait"
        watch = "Patience"

    note = f"Low strength ({strength}/100) - avoid directional" if strength < 60 else "Multiple confirmations"
    return f"{setup} {direction} setup, {key_level}. {note}. Trade: {trade}. {watch}."


def build_enhanced_digest(
    symbol: str,
    alerts: list[dict],
    fetched_at: str | None = None,
    scan_context: dict | None = None,
    intelligence_text: str | None = None,
    detected_count: int | None = None,
    dedup_suppressed_count: int | None = None,
    digest_id: str | None = None,
    paper_trade_status: dict | None = None,
    live_trade_status: dict | None = None,
    llm_verdict: dict | None = None,
    exit_advice: any = None,
) -> tuple[str, str]:
    if digest_id is None:
        digest_id = str(uuid.uuid4())[:8]
    try:
        dt = datetime.fromisoformat(fetched_at or "").astimezone(IST)
    except Exception:
        dt = datetime.now(IST)
    ts = dt.strftime("%d %b, %H:%M")

    ctx = scan_context or {}
    _base_sym1 = symbol.upper().strip().split()[0]
    _is_mcx1 = _base_sym1 in {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}
    expiry_val = ctx.get("futures_expiry") if (_is_mcx1 and ctx.get("futures_expiry")) else ctx.get("expiry")
    exp_fmt, dte_lbl = _format_expiry_and_dte(expiry_val)
    header_extra = f" (Exp: {exp_fmt} | {dte_lbl})" if exp_fmt and dte_lbl else (f" (Exp: {exp_fmt})" if exp_fmt else "")

    n = len(alerts)
    px_label = _price_label(symbol)
 
    if not alerts:
        return build_digest(symbol, alerts, fetched_at, scan_context, intelligence_text, detected_count, dedup_suppressed_count, digest_id=digest_id, paper_trade_status=paper_trade_status, live_trade_status=live_trade_status, llm_verdict=llm_verdict, exit_advice=exit_advice)
 
    intel_raw = intelligence_text if intelligence_text is not None else generate_intelligence(symbol, alerts, scan_context=scan_context)
    intel = _parse_intelligence(intel_raw)
    verdict = intel.get("verdict", "Sideways")
    emoji, label = _VERDICT_STYLE.get(verdict, (EMOJI_WHITE, "NEUTRAL"))
 
    chart_payload = _chart_payload_for_symbol(ctx, symbol)
    strength = _calculate_signal_strength(alerts, intel, ctx, chart_payload)
    s_label = _strength_label(strength)
    filled = round(strength / 10)
    s_bar = ("\u2588" * filled) + ("\u2591" * (10 - filled))
 
    key_signal_alert = _find_key_signal(alerts)
    key_signal_formatted = _format_key_signal(key_signal_alert, verdict)
 
    # price delta (unchanged logic)
    pcp = ctx.get("price_change_pct")
    pcpt = ctx.get("price_change_points")
    try:
        d_spot = float(pcp) if pcp is not None else 0.0
    except Exception:
        d_spot = 0.0
    try:
        d_points = float(pcpt or 0.0)
    except Exception:
        d_points = 0.0
    pct_digits = 3 if abs(d_spot) < 0.01 and d_spot != 0 else 2
    if pcp is None:
        spot_delta = "no prev data"
    elif abs(d_points) < 0.05 and abs(d_spot) < 0.005:
        spot_delta = "flat"
    else:
        spot_delta = f"{_fmt_signed(d_points, 1)} (`{_fmt_signed(d_spot, pct_digits)}%`)"
 
    # ── Decision
    def sec(title, body):
        return ["", title, body]

    spot_delta_clean = spot_delta.replace("`", "")
    spot_delta_str = f" ({spot_delta_clean})" if spot_delta != "flat" and spot_delta != "no prev data" else ""
    oi_unwind_note = ""
    ce_net, pe_net = _net_oi_delta(alerts, ctx)
    if ce_net < 0 and pe_net < 0:
        oi_unwind_note = " (Both Unwinding)"
    elif ce_net > 0 and pe_net > 0:
        oi_unwind_note = " (Both Building)"

    # ── Header (always) ────────────────────────────────────────────────────
    spot_val = _fmt_val(ctx.get('underlying'), symbol)
    atm_val = _fmt_val(ctx.get('atm_strike'), symbol)
    pcr_val = _fmt_num(ctx.get('pcr'), 2)
    lines = [
        f"\U0001F4CA *{symbol}*{header_extra} | {ts}",
        f"{px_label} `{spot_val}{spot_delta_str}` | ATM `{atm_val}` | PCR `{pcr_val}`",
        f"Net OI \u0394: CE `{_fmt_oi(ce_net)}` | PE `{_fmt_oi(pe_net)}`{oi_unwind_note}",
        DIVIDER,
    ]
 
    # ── Decision gate: regime backstop + verdict ──────────────────────────
    regime, banner_text, no_trade = _classify_regime(alerts, ctx, verdict)
    vbias = _verdict_bias(verdict)
 
    c1 = chart_payload.get("1h", {}).get("sentiment", "NEUTRAL").upper()
    c3 = chart_payload.get("3h", {}).get("sentiment", "NEUTRAL").upper()
    has_conflict = (vbias == "BULLISH" and "BEARISH" in (c1, c3)) or (vbias == "BEARISH" and "BULLISH" in (c1, c3))
 
    confirmation = _compress_fallback_section(_build_confirmation_section(chart_payload, ctx, verdict))
 
    if no_trade:
        # NO-TRADE banner. Plan suppressed — no fake entries on squaring.
        lines += [
            f"\U0001F6D1 *NO TRADE* - {banner_text}",
            "Stand aside. No directional edge this scan.",
            "",
            f"{px_label} `{spot_val}` | ATM `{atm_val}` | PCR `{pcr_val}`",
            f"Net OI flow: CE `{_fmt_oi(ce_net)}` | PE `{_fmt_oi(pe_net)}`",
        ]
        if vbias != "NEUTRAL":
            lines.append(f"\u26A0\ufe0f Engine verdict was {label} ({strength}/100) - not confirmed by flow")
        lines += sec("\u26A1 *WHY NO TRADE*", _build_no_trade_reason(regime, ce_net, pe_net, verdict, strength))
        lines += sec("\U0001F4CA *BIGGEST MOVES*", _build_biggest_moves(alerts))
        lines += sec("\U0001F4C8 *CONFIRMATION*", confirmation)
        if paper_trade_status:
            lines += sec("🤖 *PAPER TRADE STATUS*", _format_paper_trade_status(paper_trade_status))
        if live_trade_status:
            lines += sec("🟢 *LIVE/SHADOW TRADE STATUS*", _format_trade_status(live_trade_status, is_live=True))
        lines += ["", f"_#{digest_id}_", DIVIDER]
        return digest_id, _fit_telegram("\n".join(lines), digest_id)

    # ── TRADEABLE path ─────────────────────────────────────────────────────
    market_structure = _compress_fallback_section(_build_market_structure(alerts, verdict))
    trading_plan = _compress_fallback_section(_build_trading_plan(symbol, verdict, strength, ctx, intel))
    bottom_line = _to_caveman(_build_bottom_line(symbol, verdict, strength, key_signal_alert, ctx))

    levels_parts = []
    if ctx.get("support"):
        levels_parts.append(f"Support `{_fmt_val(ctx.get('support'), symbol)}`")
    if ctx.get("resistance"):
        levels_parts.append(f"Resistance `{_fmt_val(ctx.get('resistance'), symbol)}`")
    levels_section = " | ".join(levels_parts) if levels_parts else "None identified"

    if "Put Writing" in verdict:
        trade_word = "SELL PE"
    elif "Call Writing" in verdict:
        trade_word = "SELL CE"
    elif vbias == "BULLISH":
        trade_word = "BUY CE"
    elif vbias == "BEARISH":
        trade_word = "BUY PE"
    else:
        trade_word = "WAIT"

    trade_text = f"{emoji} *TRADE: {trade_word}* - {_esc(label)}"
    lines += [
        trade_text,
        f"`Signal strength: {s_bar} {strength}/100 ({s_label.upper()})`",
    ]
    if has_conflict:
        lines.append(f"`\u26A0\ufe0f Chart timeframe conflict (1H vs 3H) - size down`")

    # Combine key levels and key signal into one section
    key_signal_clean = key_signal_formatted.replace("\n", " ").strip()
    key_signal_clean = key_signal_clean.replace(
        "   \u26A0\ufe0f Note: headline signal does not confirm the verdict",
        " *[⚠️ Disagrees with verdict]*"
    )

    signals_levels_body = [
        f"\u2022 *Key Levels:* {levels_section}",
        f"\u2022 *Headline:* {_esc(key_signal_clean)}"
    ]
    lines += sec("\u26A1 *KEY SIGNALS & LEVELS*", "\n".join(signals_levels_body))
    lines += sec("\U0001F3AF *TRADING PLAN*", trading_plan)
    
    if market_structure and market_structure != "No significant OI structure changes":
        lines += sec("\U0001F4CA *MARKET STRUCTURE*", market_structure)
        
    lines += sec("\U0001F4C8 *CONFIRMATION*", confirmation)
    lines += sec("\U0001F4A1 *BOTTOM LINE*", bottom_line)
    if llm_verdict:
        bias = llm_verdict.get("bias") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "bias", "")
        conf = llm_verdict.get("confidence") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "confidence", 0)
        strat = llm_verdict.get("strategy") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "strategy", "")
        strike = llm_verdict.get("strike_selection") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "strike_selection", "")
        reason = llm_verdict.get("reasoning") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "reasoning", "")
        ai_body = [
            f"\u2022 *Bias:* {_esc(bias)} ({conf}%)",
            f"\u2022 *Strategy:* {_esc(strat)}",
            f"\u2022 *Target:* {_esc(strike)}",
            f"\u2022 *Reasoning:* _{_esc(reason)}_"
        ]
        lines += sec("🧠 *AI VERDICT*", "\n".join(ai_body))
    if paper_trade_status:
        lines += sec("🤖 *PAPER TRADE STATUS*", _format_paper_trade_status(paper_trade_status))
    if live_trade_status:
        lines += sec("🟢 *LIVE/SHADOW TRADE STATUS*", _format_trade_status(live_trade_status, is_live=True))
    lines += ["", f"_#{digest_id}_", DIVIDER]

    return digest_id, _fit_telegram("\n".join(lines), digest_id)


def _build_no_trade_reason(regime: str, ce_net: int, pe_net: int, verdict: str, strength: int) -> str:
    out = []
    if regime == "SQUARING":
        out.append("\u2022 Both CE & PE OI shrinking = position squaring / expiry behaviour")
        if ce_net and pe_net:
            faster = "PE" if abs(pe_net) > abs(ce_net) else "CE"
            out.append(f"\u2022 {faster} unwound faster - no fresh directional conviction")
        out.append("\u2022 Unwinding is exit flow, not a new setup")
    else:
        oi_bias, _ = _oi_flow_read(ce_net, pe_net)
        vbias = _verdict_bias(verdict)
        if vbias == "NEUTRAL" and oi_bias != "NEUTRAL":
            out.append(f"\u2022 Conflicting signals - OI Flow is {oi_bias} but engine verdict is neutral")
            out.append("\u2022 Wait for chart direction and OI flow to align")
        else:
            out.append("\u2022 Mixed / rangebound OI - no dominant side")
            out.append("\u2022 Wait for one-sided buildup before acting")
    if _verdict_bias(verdict) != "NEUTRAL":
        out.append(f"\u2022 Verdict label not backed by OI flow ({strength}/100)")
    return "\n".join(out)


def _build_biggest_moves(alerts: list[dict], cap: int = 4) -> str:
    oi = [a for a in alerts if a.get("alert_type") in {"OI_SPIKE", "OI_UNWIND"}]
    oi.sort(key=lambda a: abs(float(_detail(a).get("pct_change", 0))), reverse=True)
    lines = []
    for a in oi[:cap]:
        d = _detail(a)
        pct = float(d.get("pct_change", 0))
        leg = f"{_fmt_num(a.get('strike'))} {a.get('option_type','')}".strip()
        mark = EMOJI_RED if pct < 0 else EMOJI_GREEN
        kind = "unwind" if a.get("alert_type") == "OI_UNWIND" else "spike"
        lines.append(f"{mark} {leg} {kind} {pct:+.1f}%")
    extra = len(oi) - cap
    if extra > 0:
        lines.append(f" ...{extra} more")
    return "\n".join(lines) if lines else "No OI moves"


def _to_caveman(text: str) -> str:
    if not text:
        return ""
    reps = {
        r"\bthe\b": "",
        r"\ba\b": "",
        r"\ban\b": "",
        r"\bjust\b": "",
        r"\breally\b": "",
        r"\bbasically\b": "",
        r"\bactually\b": "",
        r"\bsimply\b": "",
        r"\bplease\b": "",
        r"\bshould\b": "",
        r"\bwould\b": "",
        r"\bcould\b": "",
        r"\bvery\b": "",
        r"\bwill\b": "",
        r"\bis\b": "",
        r"\bare\b": "",
        r"\bwas\b": "",
        r"\bwere\b": "",
        r"\bbe\b": "",
        r"\bbeen\b": "",
        r"\bhave\b": "",
        r"\bhas\b": "",
        r"\bhad\b": "",
        r"\bwith\b": "",
        r"\bfor\b": "",
        r"\bfrom\b": "",
        r"\bby\b": "",
        r"\bat\b": "",
        r"\bon\b": "",
        r"\bof\b": "",
        r"\bto\b": "",
        r"\band\b": "",
        r"\bbut\b": "",
        r"\bthat\b": "",
        r"\bthis\b": "",
        r"\bthese\b": "",
        r"\bthose\b": "",
        r"\bbecause\b": "",
        r"\bsince\b": "",
        r"\btherefore\b": "",
        r"\bthus\b": "",
        r"\bhence\b": "",
        r"\bso\b": "",
        r"\bdatabase\b": "DB",
        r"\bauthentication\b": "auth",
        r"\bconfiguration\b": "config",
        r"\brequest\b": "req",
        r"\bresponse\b": "res",
        r"\bfunction\b": "fn",
        r"\bimplementation\b": "impl",
        r"\bstrategy\b": "strat",
        r"\bstop loss\b": "SL",
        r"\bstop-loss\b": "SL",
        r"\bconfidence\b": "conf",
        r"\bunderlying\b": "und",
        r"\bsetup\b": "stp",
        r"\bsupport\b": "supp",
        r"\bresistance\b": "res",
        r"\btarget\b": "tgt",
        r"\bleads to\b": "→",
        r"\bresults in\b": "→",
        r"\bthen\b": "→",
        r"\bgoes to\b": "→",
    }
    out = text
    for pattern, replacement in reps.items():
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _format_trade_status_caveman(status: dict | None, is_live: bool = False) -> str:
    if not status:
        return "No action"
    action = status.get("action")
    if action == "EXECUTED":
        trade = status.get("trade", {})
        opt = trade.get("option_type", "CE")
        strike = trade.get("strike")
        entry = trade.get("entry_premium") or trade.get("entry_underlying")
        sl = trade.get("sl_premium") or trade.get("sl_underlying")
        tgt = trade.get("target_premium") or trade.get("target_underlying")
        side = str(trade.get("side") or status.get("side") or "BUY").upper()
        
        entry_str = f"{entry:.1f}" if entry is not None else "—"
        sl_str = f"{sl:.1f}" if sl is not None else "—"
        tgt_str = f"{tgt:.1f}" if tgt is not None else "—"
        strike_str = f"{strike:g}" if strike is not None else "—"
        
        if opt == "FUT":
            return f"Exec {side} FUT @ {entry_str} | SL {sl_str} | Tgt {tgt_str}"
        return f"Exec {side} {strike_str}{opt} @ {entry_str} | SL {sl_str} | Tgt {tgt_str}"
    elif action == "CLOSED":
        trade = status.get("trade", {})
        opt = trade.get("option_type", "CE")
        strike = trade.get("strike")
        pnl = trade.get("pnl_rupees") or 0.0
        pnl_sign = "+" if pnl > 0 else ""
        side = str(trade.get("side") or "BUY").upper()
        strike_str = f"{strike:g}" if strike is not None else "—"
        if opt == "FUT":
            return f"Closed {side} FUT | PnL: {pnl_sign}₹{pnl:.1f}"
        return f"Closed {side} {strike_str}{opt} | PnL: {pnl_sign}₹{pnl:.1f}"
    elif action == "HELD":
        trade = status.get("trade", {})
        opt = trade.get("option_type", "CE")
        strike = trade.get("strike")
        side = str(trade.get("side") or "BUY").upper()
        strike_str = f"{strike:g}" if strike is not None else "—"
        if opt == "FUT":
            return f"Held {side} FUT"
        return f"Held {side} {strike_str}{opt}"
    elif action and action.startswith("BLOCKED"):
        return f"Blocked: {status.get('reason', 'Filters')}"
    elif action == "SKIPPED_MARKET_CLOSED":
        return "Skipped: market closed"
    else:
        return f"No trade: {status.get('reason', 'No setup')}"


def _compress_fallback_section(text: str) -> str:
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        compressed = _to_caveman(line)
        if compressed:
            lines.append(compressed)
    return "\n".join(lines)


# ── Redesigned premium alert helpers ────────────────────────────────────────

def _arrow_candle(s: str) -> str:
    return "▲" if s == "BULLISH" else ("▼" if s == "BEARISH" else "→")


def _risk_badge(risk: str) -> str:
    r = str(risk).upper()
    if r in ("HIGH", "EXTREME"):
        return "🔴"
    if r == "MEDIUM":
        return "🟡"
    return "🟢"


def _bot_action_block(
    paper_trade_status: dict | None,
    live_trade_status: dict | None,
    conflict_tag: str | None = None,
) -> list[str]:
    """Compact single-line summary of what the BOT actually did this scan."""
    lines: list[str] = []

    def _render(status: dict | None, label: str) -> str | None:
        if not status:
            return None
        action = (status.get("action") or "").upper()
        trade = status.get("trade") or {}
        opt = trade.get("option_type", "")
        strike = trade.get("strike")
        side = str(trade.get("side") or status.get("side") or "BUY").upper()
        entry = trade.get("entry_premium") or trade.get("entry_underlying")
        sl = trade.get("sl_premium") or trade.get("sl_underlying")
        tgt = trade.get("target_premium") or trade.get("target_underlying")
        pnl = trade.get("pnl_rupees") or 0.0
        reason = status.get("reason", "")

        # Normalize HOLD/HELD/HELD_PENDING depending on trade activity
        if action in ("HOLD", "HELD"):
            if trade and ("id" in trade or "option_type" in trade or "strike" in trade):
                action = "HELD"
            else:
                action = "NO_TRADE"

        strike_str = f"{strike:g}" if strike is not None else ""
        instrument = f"{strike_str}{opt}".strip() or "FUT"

        if action == "EXECUTED":
            entry_s = f"@{entry:.1f}" if entry is not None else ""
            sl_s = f"SL {sl:.1f}" if sl is not None else ""
            tgt_s = f"T {tgt:.1f}" if tgt is not None else ""
            parts = [p for p in [entry_s, sl_s, tgt_s] if p]
            warn = " ⚠️ (chart conflict)" if conflict_tag else ""
            return f"✅ {label} ENTERED{warn}: {side} {instrument} {' | '.join(parts)}"
        elif action == "CLOSED":
            pnl_str = f"+₹{pnl:.0f}" if pnl >= 0 else f"-₹{abs(pnl):.0f}"
            icon = "🟢" if pnl >= 0 else "🔴"
            close_reason = status.get("close_reason") or reason or ""
            close_tag = f" ({close_reason})" if close_reason else ""
            return f"{icon} {label} CLOSED: {side} {instrument}{close_tag} → PnL {pnl_str}"
        elif action == "HELD":
            live_pnl = trade.get("live_pnl_rupees")
            cmp = trade.get("current_price") or trade.get("live_price")
            pnl_part = f" | Live PnL: {'+'if (live_pnl or 0)>=0 else ''}₹{(live_pnl or 0):.0f}" if live_pnl is not None else ""
            cmp_part = f" | CMP {cmp:.1f}" if cmp is not None else ""
            # E2: Show position age so a stale held position isn't read as a new signal
            age_part = ""
            try:
                opened_at = trade.get("opened_at")
                if opened_at:
                    from datetime import datetime as _dt, timezone as _tz
                    opened_dt = _dt.fromisoformat(str(opened_at).replace("Z", "+00:00"))
                    age_min = int((_dt.now(_tz.utc) - opened_dt).total_seconds() / 60)
                    if age_min < 60:
                        age_part = f" | entered {age_min}m ago"
                    else:
                        age_part = f" | entered {age_min // 60}h {age_min % 60}m ago"
            except Exception:
                pass
            return f"📊 {label} HOLDING: {side} {instrument}{cmp_part}{pnl_part}{age_part}"
        elif action == "HELD_PENDING":
            return f"⏳ {label} PENDING: {side} {instrument} (Waiting for fill)"
        elif action and action.startswith("BLOCKED"):
            return f"🚫 {label} BLOCKED: {reason or 'filter'}"
        elif action == "SKIPPED_MARKET_CLOSED":
            return f"⏸ {label} SKIPPED: market closed"
        elif action == "NO_TRADE":
            return f"⬜ {label} NO TRADE: {reason or 'no valid plan'}"
        else:
            return f"⬜ {label}: {reason or action or 'no action'}"

    paper_line = _render(paper_trade_status, "Paper")
    live_line = _render(live_trade_status, "Live")
    if paper_line:
        lines.append(paper_line)
    if live_line:
        lines.append(live_line)
    return lines


def _conflict_tag(bias_upper: str, c1: str, c3: str) -> str:
    """
    Return a short chart-timing note when candles diverge from the OI verdict.
    Per design: for OI-based trades, 1H opposing a completed 3H trend is an
    entry-timing signal (potential pullback entry), not a warning. Only flag when
    BOTH timeframes oppose the verdict (genuine conflict, not a timing opportunity).
    """
    if "BULL" in bias_upper or "LONG" in bias_upper:
        if c1 == "BEARISH" and c3 == "BEARISH":
            return " ⚠️ Both TFs bearish vs OI bull"
        if "BEARISH" in (c1, c3):
            return " 💡 1H pullback in 3H trend"
    if "BEAR" in bias_upper or "SHORT" in bias_upper:
        if c1 == "BULLISH" and c3 == "BULLISH":
            return " ⚠️ Both TFs bullish vs OI bear"
        if "BULLISH" in (c1, c3):
            return " 💡 1H bounce in 3H downtrend"
    return ""


def _oi_summary_line(ce_net: int, pe_net: int) -> str:
    """One-line OI flow read for the market pulse section."""
    _, oi_text = _oi_flow_read(ce_net, pe_net)
    ce_fmt = _fmt_oi(ce_net)
    pe_fmt = _fmt_oi(pe_net)
    return f"CE `{ce_fmt}` | PE `{pe_fmt}` — {oi_text}"


def build_llm_consolidated_digest(
    symbol: str,
    alerts: list[dict],
    fetched_at: str | None = None,
    scan_context: dict | None = None,
    detected_count: int | None = None,
    dedup_suppressed_count: int | None = None,
    digest_id: str | None = None,
    paper_trade_status: dict | None = None,
    live_trade_status: dict | None = None,
    llm_verdict: dict | None = None,
    exit_advice: any = None,
) -> tuple[str, str]:
    """Premium Bloomberg-style alert: BOT action first, then market context."""
    if digest_id is None:
        digest_id = str(uuid.uuid4())[:8]
    try:
        dt = datetime.fromisoformat(fetched_at or "").astimezone(IST)
    except Exception:
        dt = datetime.now(IST)
    ts = dt.strftime("%d %b, %H:%M")

    ctx = scan_context or {}
    # For MCX commodity symbols, the underlying IS the futures price — show futures expiry in header.
    # For NSE index symbols, show the option chain expiry (more relevant for option traders).
    _base_sym = symbol.upper().strip().split()[0]
    _is_mcx = _base_sym in {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}
    if _is_mcx and ctx.get("futures_expiry"):
        expiry_val = ctx.get("futures_expiry")
    else:
        expiry_val = ctx.get("expiry")
    exp_fmt, dte_lbl = _format_expiry_and_dte(expiry_val)
    header_extra = f" | {exp_fmt} | {dte_lbl}" if exp_fmt and dte_lbl else (f" | {exp_fmt}" if exp_fmt else "")

    spot_val   = _fmt_val(ctx.get("underlying"), symbol)
    atm_val    = _fmt_val(ctx.get("atm_strike"), symbol)
    pcr_val    = _fmt_num(ctx.get("pcr"), 2)
    px_label   = _price_label(symbol)

    # Price delta
    pcp  = ctx.get("price_change_pct")
    pcpt = ctx.get("price_change_points")
    try:    d_spot   = float(pcp)  if pcp  is not None else 0.0
    except: d_spot   = 0.0
    try:    d_points = float(pcpt or 0.0)
    except: d_points = 0.0
    pct_digits = 3 if abs(d_spot) < 0.01 and d_spot != 0 else 2
    if pcp is None:
        spot_delta_str = ""
    elif abs(d_points) < 0.05 and abs(d_spot) < 0.005:
        spot_delta_str = ""
    else:
        spot_delta_str = f" ({_fmt_signed(d_points, 1)}, {_fmt_signed(d_spot, pct_digits)}%)"

    # LLM fields
    def gv(key, default=""):
        if isinstance(llm_verdict, dict):
            return llm_verdict.get(key, default)
        return getattr(llm_verdict, key, default) if llm_verdict else default

    bias       = gv("bias") or gv("action")
    conf       = gv("confidence", 0)
    strat      = gv("strategy") or gv("instrument")
    strike_sel = gv("strike_selection") or gv("entry_premium_range")
    reason     = gv("reasoning") or gv("thesis")
    risk       = gv("risk_rating")
    exit_adv   = gv("exit_advice")
    news       = gv("news_synthesis")

    entry_trigger = gv("entry_trigger")
    stop_loss     = gv("stop_loss")
    target_1      = gv("target_1")
    target_2      = gv("target_2")
    risk_reward   = gv("risk_reward")
    invalidation  = gv("invalidation")

    bias_upper = str(bias).upper().strip() if bias else "NEUTRAL"
    is_bull  = "BULL" in bias_upper or "LONG"  in bias_upper
    is_bear  = "BEAR" in bias_upper or "SHORT" in bias_upper
    is_no_trade = "NO_TRADE" in bias_upper or "NEUTRAL" in bias_upper

    verdict_emoji = "🟢" if is_bull else ("🔴" if is_bear else "⚪")
    risk_icon  = _risk_badge(str(risk)) if risk else ""

    # Candles
    chart_payload = _chart_payload_for_symbol(ctx, symbol)
    c1 = chart_payload.get("1h", {}).get("sentiment", "NEUTRAL").upper()
    c3 = chart_payload.get("3h", {}).get("sentiment", "NEUTRAL").upper()
    conflict_tag = _conflict_tag(bias_upper, c1, c3)

    # OI
    ce_net, pe_net = _net_oi_delta(alerts, ctx)

    # Key levels
    sup_val = ctx.get("support")
    res_val = ctx.get("resistance")
    levels_str = " | ".join(filter(None, [
        f"S:{_fmt_val(sup_val, symbol)}" if sup_val else "",
        f"R:{_fmt_val(res_val, symbol)}" if res_val else "",
    ])) or "—"

    # ── Signals summary
    high_cnt = sum(1 for a in alerts if a.get("severity") == "HIGH")
    med_cnt  = sum(1 for a in alerts if a.get("severity") == "MEDIUM")
    low_cnt  = sum(1 for a in alerts if a.get("severity") == "LOW")
    sev_parts = " ".join(x for x in [
        f"🔴{high_cnt} HIGH" if high_cnt else "",
        f"🟡{med_cnt} MED"  if med_cnt  else "",
        f"🔵{low_cnt} LOW"  if low_cnt  else "",
    ] if x)

    sorted_alerts = sorted(alerts, key=lambda a: _SEV_ORDER.get(a.get("severity", "LOW"), 2))
    top_signals: list[str] = []
    for alert in sorted_alerts[:3]:
        badge = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}.get(alert.get("severity", "LOW"), "🔵")
        top_signals.append(f"{badge} {_to_caveman(_signal_line(alert))}")

    # ═══════════════════════════════════════════
    # SECTION 1 — HEADER  (always visible)
    # ═══════════════════════════════════════════
    lines: list[str] = [
        f"📊 *{symbol}*{header_extra} | {ts}",
        f"{px_label} `{spot_val}{spot_delta_str}` | ATM `{atm_val}` | PCR `{pcr_val}`",
        DIVIDER,
    ]

    # ═══════════════════════════════════════════
    # SECTION 2 — AI VERDICT / EXIT ADVICE
    # ═══════════════════════════════════════════
    ea_action = None
    if exit_advice:
        ea_action = exit_advice.get("action") if isinstance(exit_advice, dict) else getattr(exit_advice, "action", None)

    if ea_action:
        ea_urgency = exit_advice.get("urgency") if isinstance(exit_advice, dict) else getattr(exit_advice, "urgency", "")
        ea_reasoning = exit_advice.get("reasoning") if isinstance(exit_advice, dict) else getattr(exit_advice, "reasoning", "")
        ea_new_sl = exit_advice.get("new_sl_premium") if isinstance(exit_advice, dict) else getattr(exit_advice, "new_sl_premium", None)
        ea_new_target = exit_advice.get("new_target_premium") if isinstance(exit_advice, dict) else getattr(exit_advice, "new_target_premium", None)

        action_emoji = {"HOLD": "⚪", "TRAIL_SL": "🟡", "CLOSE_EARLY": "🔴", "EXTEND_TARGET": "🔵"}.get(ea_action, "⚪")
        urgency_badge = {"LOW": "🟢 LOW", "MEDIUM": "🟡 MED", "HIGH": "🔴 HIGH"}.get(ea_urgency.upper(), ea_urgency)

        verdict_line = f"{action_emoji} *AI EXIT ADVISOR: {ea_action}* (Urgency: {urgency_badge})"
        lines.append(verdict_line)
        lines.append(f"  *Reasoning:* _{_esc(ea_reasoning)}_")
        if ea_new_sl is not None:
            lines.append(f"  *New SL Premium:* `{_fmt_num(ea_new_sl, 2)}`")
        if ea_new_target is not None:
            lines.append(f"  *New Target Premium:* `{_fmt_num(ea_new_target, 2)}`")
    else:
        conf_bar_filled = round(int(conf) / 10) if conf else 0
        conf_bar = ("█" * conf_bar_filled) + ("░" * (10 - conf_bar_filled))
        verdict_line = f"{verdict_emoji} *AI: {_esc(bias_upper)}* `{conf}% | {conf_bar}`"
        if risk_icon:
            verdict_line += f" {risk_icon} {_esc(str(risk).upper())}"
        lines.append(verdict_line)

        if conflict_tag:
            lines.append(f"  ⚠️ *WARNING:* AI direction conflicts with chart candles (1H {c1} | 3H {c3}) — treat this setup with extra caution")

        if strat and not is_no_trade:
            if strike_sel:
                lines.append(f"  *Instrument:* {_esc(strat)} | *Premium:* `{_esc(strike_sel)}`")
            else:
                lines.append(f"  *Instrument:* {_esc(strat)}")

            setup_parts = []
            if risk_reward:
                setup_parts.append(f"R:R: `{_esc(risk_reward)}`")
            if stop_loss:
                setup_parts.append(f"SL: `{_esc(stop_loss)}`")
            if target_1:
                setup_parts.append(f"T1: `{_esc(target_1)}`")
            if target_2:
                setup_parts.append(f"T2: `{_esc(target_2)}`")

            if setup_parts:
                lines.append(f"  *Setup:* {' | '.join(setup_parts)}")

            if entry_trigger:
                lines.append(f"  *Trigger:* _{_esc(entry_trigger)}_")

            if invalidation:
                lines.append(f"  *Invalidation:* _{_esc(invalidation)}_")

        if reason:
            lines.append(f"  *Thesis:* _{_esc(str(reason).strip())}_")

        if exit_adv and not is_no_trade:
            lines.append(f"  *Exit Signal:* {_esc(str(exit_adv).strip())}")

    if news and str(news).lower() not in ("no news data", "", "none"):
        lines.append(f"  *News:* {_esc(str(news).strip())}")

    lines.append("")

    # ═══════════════════════════════════════════
    # SECTION 3 — MARKET PULSE
    # ═══════════════════════════════════════════
    lines.append("📈 *MARKET PULSE*")
    lines.append(f"  OI Δ: {_oi_summary_line(ce_net, pe_net)}")
    lines.append(f"  Candles: 1H {c1} {_arrow_candle(c1)} | 3H {c3} {_arrow_candle(c3)}")
    lines.append(f"  Levels: {levels_str}")

    if sev_parts:
        lines.append(f"  Alerts: {sev_parts}")
    if top_signals:
        for sig in top_signals:
            lines.append(f"  {sig}")

    lines.append("")

    # ═══════════════════════════════════════════
    # SECTION 4 — BOT ACTION  (what happened)
    # ═══════════════════════════════════════════
    bot_lines = _bot_action_block(paper_trade_status, live_trade_status, conflict_tag=conflict_tag)
    if bot_lines:
        lines.append("🤖 *BOT ACTION*")
        for bl in bot_lines:
            lines.append(f"  {bl}")
    else:
        lines.append("🤖 *BOT ACTION*  No action this scan")

    lines.append("")
    lines.append(f"_#{digest_id}_")
    lines.append(DIVIDER)

    return digest_id, _fit_telegram("\n".join(lines), digest_id)


def build_digest_wrapper(
    symbol: str,
    alerts: list[dict],
    fetched_at: str | None = None,
    scan_context: dict | None = None,
    intelligence_text: str | None = None,
    detected_count: int | None = None,
    dedup_suppressed_count: int | None = None,
    digest_id: str | None = None,
    paper_trade_status: dict | None = None,
    live_trade_status: dict | None = None,
    llm_verdict: dict | None = None,
    exit_advice: any = None,
) -> tuple[str, str]:
    if llm_verdict or exit_advice:
        return build_llm_consolidated_digest(
            symbol, alerts, fetched_at, scan_context,
            detected_count, dedup_suppressed_count, digest_id,
            paper_trade_status, live_trade_status, llm_verdict,
            exit_advice=exit_advice
        )
    if USE_ENHANCED_TEMPLATE:
        return build_enhanced_digest(
            symbol, alerts, fetched_at, scan_context, intelligence_text,
            detected_count, dedup_suppressed_count, digest_id=digest_id,
            paper_trade_status=paper_trade_status,
            live_trade_status=live_trade_status,
            llm_verdict=None,
            exit_advice=exit_advice,
        )
    return build_digest(
        symbol, alerts, fetched_at, scan_context, intelligence_text,
        detected_count, dedup_suppressed_count, digest_id=digest_id,
        paper_trade_status=paper_trade_status,
        live_trade_status=live_trade_status,
        llm_verdict=None,
        exit_advice=exit_advice,
    )
