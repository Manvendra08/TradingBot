"""
Per-scan digest builder v4.0
Redesigned message hierarchy: ACTION FIRST → context → signals → detail.

Structure:
  [1] HEADER     — symbol, spot, time, signal count
  [2] VERDICT    — bias + confidence bar (instant read)
  [3] ACTION     — what to do RIGHT NOW (trade or wait)
  [4] LEVELS     — key S/R/MaxPain in one line
  [5] OI PULSE   — CE/PE OI totals + net flow direction
  [6] SIGNALS    — severity-tiered alert list (HIGH → MED → LOW)
  [7] CHART      — timeframe sentiments inline
  [8] BROADER    — multi-scan trend
  [9] FOOTER     — scan count + digest id

Zero-alert scans: compact single-block with diagnostics.
"""
import json
import uuid
from datetime import datetime, timezone, timedelta

from src.engine.intelligence import generate_intelligence

IST = timezone(timedelta(hours=5, minutes=30))

# ── Severity styling ─────────────────────────────────────────────────────
_SEV_BADGE = {
    "HIGH":   "🔴 HIGH",
    "MEDIUM": "🟡 MED ",
    "LOW":    "🔵 LOW ",
}
_SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

# ── Alert type short labels ──────────────────────────────────────────────
_ATYPE_SHORT = {
    "OI_SPIKE":          "OI spike",
    "OI_UNWIND":         "OI unwind",
    "BUILDUP_CLASSIFY":  "",
    "PRICE_SPIKE":       "Spot move",
    "PCR_EXTREME":       "PCR extreme",
    "PCR_SHIFT":         "PCR shift",
    "PCR_VELOCITY":      "PCR velocity",
    "IV_SPIKE":          "IV spike",
    "IV_CRUSH":          "IV crush",
    "ATM_LEG_MOVE":      "ATM leg move",
    "STRADDLE_PREMIUM":  "Straddle Δ",
    "MAX_PAIN_SHIFT":    "Max Pain shift",
    "OI_WALL_SHIFT":     "OI wall shift",
    "VOLUME_AGGRESSION": "Vol aggression",
    "OTM_UNUSUAL":       "OTM unusual",
}

# ── Verdict → display mapping ────────────────────────────────────────────
_VERDICT_STYLE = {
    "Long Buildup":      ("🟢", "BULLISH"),
    "Put Writing":       ("🟢", "BULLISH"),
    "Short Covering":    ("🟡", "CAUTIOUS BULL"),
    "OI Bias Bullish":   ("🟡", "BIAS BULL"),
    "Short Buildup":     ("🔴", "BEARISH"),
    "Call Writing":      ("🔴", "BEARISH"),
    "Long Unwinding":    ("🟠", "CAUTIOUS BEAR"),
    "OI Bias Bearish":   ("🟠", "BIAS BEAR"),
    "Sideways":          ("⚪", "NEUTRAL"),
}


def _confidence_bar(pct: int) -> str:
    """Visual confidence bar using block chars. 10 segments."""
    filled = round(pct / 10)
    return "█" * filled + "░" * (10 - filled) + f" {pct}%"


def _one_liner(alert: dict) -> str:
    """Compact single-line signal description."""
    atype  = alert.get("alert_type", "")
    detail = {}
    try:
        detail = json.loads(alert.get("detail_json") or "{}")
    except Exception:
        pass

    strike     = alert.get("strike")
    opt_type   = alert.get("option_type", "")
    strike_tag = f" {int(strike)} {opt_type}".rstrip() if strike else ""

    if atype == "BUILDUP_CLASSIFY":
        label = detail.get("buildup_type", "Buildup")
        oi_p  = detail.get("oi_pct", 0)
        ltp_p = detail.get("ltp_pct", 0)
        return f"{label}{strike_tag}  OI {oi_p:+.0f}% · LTP {ltp_p:+.0f}%"

    if atype in ("OI_SPIKE", "OI_UNWIND"):
        pct = detail.get("pct_change", 0)
        label = "↑ OI spike" if atype == "OI_SPIKE" else "↓ OI unwind"
        return f"{label}{strike_tag}  {pct:+.0f}%"

    if atype == "PRICE_SPIKE":
        pct  = detail.get("pct_change", 0)
        dire = detail.get("direction", "")
        return f"Spot {pct:+.2f}% {dire}"

    if atype == "PCR_SHIFT":
        delta = detail.get("pcr_delta", 0)
        pcr   = detail.get("pcr", "?")
        dire  = "bear flip" if delta < 0 else "bull flip"
        return f"PCR {delta:+.3f} → {pcr}  ({dire})"

    if atype == "PCR_EXTREME":
        pcr   = detail.get("pcr", "?")
        interp = detail.get("interpretation", "")
        return f"PCR {pcr} — {interp}"

    if atype == "PCR_VELOCITY":
        label = detail.get("label", "")
        slope = detail.get("slope", 0)
        return f"PCR trend {slope:+.3f}/scan  {label}"

    if atype == "IV_SPIKE":
        iv_d = detail.get("iv_delta", 0)
        return f"IV spike{strike_tag}  +{iv_d:.1f}pts  event hedge"

    if atype == "IV_CRUSH":
        iv_d = detail.get("iv_delta", 0)
        return f"IV crush{strike_tag}  {iv_d:.1f}pts  vol decay"

    if atype == "ATM_LEG_MOVE":
        bias = detail.get("bias", "")
        ce_p = detail.get("ce_pct", 0)
        pe_p = detail.get("pe_pct", 0)
        return f"CE {ce_p:+.1f}% · PE {pe_p:+.1f}%  → {bias}"

    if atype == "STRADDLE_PREMIUM":
        pct   = detail.get("pct_change", 0)
        label = detail.get("label", "")
        return f"Straddle {pct:+.1f}%  {label}"

    if atype == "MAX_PAIN_SHIFT":
        shift = detail.get("shift", 0)
        curr  = detail.get("curr_max_pain", "?")
        return f"MaxPain → {curr}  ({shift:+.0f}pts)"

    if atype == "OI_WALL_SHIFT":
        chg = detail.get("changes", {})
        parts = []
        for side, v in chg.items():
            parts.append(f"{side.capitalize()} wall {v['prev']}→{v['curr']}")
        return "  ".join(parts) if parts else "OI wall moved"

    if atype == "VOLUME_AGGRESSION":
        label = detail.get("label", "")
        ratio = detail.get("ratio", 0)
        return f"{label}{strike_tag}  ratio {ratio:.1f}x"

    if atype == "OTM_UNUSUAL":
        pct = detail.get("pct_change", 0)
        return f"Far-OTM{strike_tag}  +{pct:.0f}%  watch tail"

    return f"{_ATYPE_SHORT.get(atype, atype)}{strike_tag}"


def _extract_intel_fields(intel: str) -> dict:
    """
    Parse the intelligence block into named fields for structured rendering.
    Returns dict with keys: verdict, confidence, conflict, oi_analysis,
    levels, chart_lines, bull_forces, bear_forces, action, paper_trade,
    broader_trend, signal_count.
    """
    fields = {
        "verdict": "", "verdict_desc": "", "confidence": 0,
        "conflict": False, "conflict_text": "",
        "oi_lines": [], "oi_note": "",
        "level_lines": [], "straddle": "",
        "chart_lines": [],
        "bull_forces": [], "bear_forces": [],
        "action_bias": "", "action_plan": "", "critical_warning": "",
        "paper_trade": "",
        "broader_trend": "",
        "signal_count": 0,
    }
    if not intel:
        return fields

    lines = intel.splitlines()
    section = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Verdict line:  "🟢 *Verdict: Long Buildup*"
        if "*Verdict:" in stripped:
            v = stripped.split("*Verdict:")[-1].replace("*", "").strip()
            fields["verdict"] = v
            continue

        # Verdict description (italic line after verdict)
        if fields["verdict"] and stripped.startswith("_") and fields["verdict_desc"] == "":
            fields["verdict_desc"] = stripped.strip("_")
            continue

        # Confidence
        if stripped.startswith("Confidence:"):
            try:
                fields["confidence"] = int(stripped.split(":")[1].strip().replace("%", ""))
            except Exception:
                pass
            continue

        # Chart conflict
        if "Chart conflict" in stripped:
            fields["conflict"] = True
            fields["conflict_text"] = stripped.strip("_⚠️ ")
            continue

        # Section headers
        if "*OI Analysis*" in stripped:
            section = "oi"
            continue
        if "*Key Levels*" in stripped:
            section = "levels"
            continue
        if "*Chart Status*" in stripped:
            section = "chart"
            continue
        if "*BULL FORCES" in stripped:
            section = "bull"
            continue
        if "*BEAR FORCES" in stripped:
            section = "bear"
            continue
        if "*TRADE STRATEGY*" in stripped:
            section = "trade"
            continue
        if "*PAPER TRADE" in stripped:
            section = "paper"
            continue
        if "*Broader Trend*" in stripped or "Broader Trend:" in stripped:
            rest = stripped.split(":", 1)[-1].strip() if ":" in stripped else stripped
            fields["broader_trend"] = rest.strip("*🌊 ")
            section = None
            continue
        if stripped.startswith("_Based on"):
            try:
                fields["signal_count"] = int(stripped.split("Based on ")[1].split(" signal")[0])
            except Exception:
                pass
            continue

        # Collect section lines
        if section == "oi":
            if stripped.startswith("_") and stripped.endswith("_"):
                fields["oi_note"] = stripped.strip("_")
            else:
                fields["oi_lines"].append(stripped)
        elif section == "levels":
            if "Straddle" in stripped:
                fields["straddle"] = stripped
            else:
                fields["level_lines"].append(stripped)
        elif section == "chart":
            fields["chart_lines"].append(stripped)
        elif section == "bull":
            if stripped.startswith("-"):
                fields["bull_forces"].append(stripped.lstrip("- "))
        elif section == "bear":
            if stripped.startswith("-"):
                fields["bear_forces"].append(stripped.lstrip("- "))
        elif section == "trade":
            if stripped.startswith("- Bias:"):
                fields["action_bias"] = stripped.split("- Bias:", 1)[1].strip()
            elif stripped.startswith("- Action Plan:"):
                fields["action_plan"] = stripped.split("- Action Plan:", 1)[1].strip()
            elif stripped.startswith("- Critical Warning:"):
                fields["critical_warning"] = stripped.split("- Critical Warning:", 1)[1].strip()
        elif section == "paper":
            if stripped.startswith("-"):
                fields["paper_trade"] = stripped.lstrip("- ").strip()

    return fields


def build_digest(symbol: str, alerts: list[dict],
                 fetched_at: str | None = None,
                 scan_context: dict | None = None) -> tuple[str, str]:
    """
    Returns (digest_id, markdown_text) for one symbol scan.
    Redesigned v4.0: ACTION-FIRST hierarchy.
    """
    digest_id = str(uuid.uuid4())[:8]

    try:
        dt = datetime.fromisoformat(fetched_at or "").astimezone(IST)
    except Exception:
        dt = datetime.now(IST)
    ts = dt.strftime("%H:%M IST")

    n = len(alerts)
    _order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    sorted_alerts = sorted(alerts, key=lambda a: _order.get(a.get("severity", "LOW"), 2))

    # ── ZERO-ALERT SCAN ───────────────────────────────────────────────────
    diag = (scan_context or {}).get("diagnostics", {})
    if not alerts:
        oi_thresh  = diag.get("oi_threshold", 40)
        ltp_thresh = diag.get("ltp_threshold", 8)
        pcr_delta  = diag.get("pcr_delta")
        pcr_text   = "PCR Δ n/a" if pcr_delta is None else (
            f"PCR Δ {abs(pcr_delta):.2f}"
        )
        max_oi  = float(diag.get("max_oi_delta_pct") or 0)
        max_ltp = float(diag.get("max_atm_ltp_delta_pct") or 0)

        ctx      = scan_context or {}
        spot     = ctx.get("underlying", "")
        pcr_val  = ctx.get("pcr", "")
        spot_str = f"  Spot `{spot:.0f}`" if spot else ""
        pcr_str  = f"  PCR `{pcr_val:.2f}`" if pcr_val else ""

        lines = [
            f"⚫ *{symbol}*  |  {ts}  |  No signals",
            f"{'─' * 30}",
            f"📭 *Market quiet — nothing actionable*",
            f"",
            f"🔎 Scan thresholds not breached:",
            f"  OI Δ max `{max_oi:.2f}%` < `{oi_thresh}%`",
            f"  ATM LTP Δ `{max_ltp:.2f}%` < `{ltp_thresh}%`",
            f"  {pcr_text}",
        ]
        if spot_str or pcr_str:
            lines += [f"", f"📍 Market Pulse:{spot_str}{pcr_str}"]
        lines += [f"", f"_Next scan in progress…_"]
        return digest_id, "\n".join(lines)

    # ── GENERATE INTELLIGENCE ─────────────────────────────────────────────
    intel_raw = generate_intelligence(symbol, alerts, scan_context=scan_context)
    f = _extract_intel_fields(intel_raw)

    verdict        = f["verdict"] or "Sideways"
    verdict_desc   = f["verdict_desc"]
    confidence     = f["confidence"]
    v_emoji, v_label = _VERDICT_STYLE.get(verdict, ("⚪", "NEUTRAL"))
    conf_bar       = _confidence_bar(confidence)

    ctx            = scan_context or {}
    spot           = ctx.get("underlying", 0)
    pcr_val        = ctx.get("pcr", 0)
    support        = ctx.get("support", 0)
    resistance     = ctx.get("resistance", 0)
    max_pain       = ctx.get("max_pain", 0)
    atm            = ctx.get("atm_strike", 0)
    ce_oi          = ctx.get("total_ce_oi", 0)
    pe_oi          = ctx.get("total_pe_oi", 0)
    ce_chg         = ctx.get("ce_oi_change", 0)
    pe_chg         = ctx.get("pe_oi_change", 0)

    high_count   = sum(1 for a in alerts if a.get("severity") == "HIGH")
    medium_count = sum(1 for a in alerts if a.get("severity") == "MEDIUM")
    low_count    = sum(1 for a in alerts if a.get("severity") == "LOW")

    # ── HELPERS ───────────────────────────────────────────────────────────
    def _fmt_oi(v) -> str:
        try:
            v = int(v)
            if v >= 100_000: return f"{v/100_000:.2f}L"
            if v >= 1_000:   return f"{v/1_000:.1f}K"
            return str(v)
        except Exception:
            return str(v)

    def _oi_arrow(chg) -> str:
        return "↑" if chg > 0 else ("↓" if chg < 0 else "→")

    def _chg_str(chg) -> str:
        try:
            chg = int(chg)
            return f"+{_fmt_oi(chg)}" if chg >= 0 else f"-{_fmt_oi(abs(chg))}"
        except Exception:
            return "?"

    # ── BUILD MESSAGE ─────────────────────────────────────────────────────
    lines = []

    # ═══════════════════════════════════════════════════════
    # [1] HEADER
    # ═══════════════════════════════════════════════════════
    spot_str  = f"`{spot:.0f}`" if spot else "N/A"
    sig_badge = f"🔴×{high_count}" if high_count else ""
    if medium_count:
        sig_badge += (" " if sig_badge else "") + f"🟡×{medium_count}"
    if low_count:
        sig_badge += (" " if sig_badge else "") + f"🔵×{low_count}"

    lines.append(f"📊 *{symbol}*  ·  {ts}  ·  {n} signal{'s' if n != 1 else ''}  {sig_badge}")
    lines.append(f"Spot {spot_str}  |  ATM `{int(atm)}`  |  PCR `{pcr_val:.2f}`")
    lines.append("━" * 28)

    # ═══════════════════════════════════════════════════════
    # [2] VERDICT + CONFIDENCE  ← most important, reads first
    # ═══════════════════════════════════════════════════════
    lines.append(f"{v_emoji} *{v_label}*  —  {verdict}")
    if verdict_desc:
        lines.append(f"_{verdict_desc}_")
    lines.append(f"Conf: `{conf_bar}`")
    if f["conflict"]:
        lines.append(f"⚠️ _{f['conflict_text']}_")

    # ═══════════════════════════════════════════════════════
    # [3] ACTION BLOCK  ← second most important
    # ═══════════════════════════════════════════════════════
    lines.append("")
    lines.append("🎯 *ACTION*")
    if f["action_plan"]:
        lines.append(f"  {f['action_plan']}")
    if f["paper_trade"] and "wait" not in f["paper_trade"].lower():
        lines.append(f"  📋 _{f['paper_trade']}_")
    elif f["paper_trade"]:
        lines.append(f"  📋 _{f['paper_trade']}_")
    if f["critical_warning"]:
        lines.append(f"  ⛔ _{f['critical_warning']}_")

    # ═══════════════════════════════════════════════════════
    # [4] KEY LEVELS  ← reference before entering
    # ═══════════════════════════════════════════════════════
    level_parts = []
    if support:    level_parts.append(f"S `{support:.0f}`")
    if resistance: level_parts.append(f"R `{resistance:.0f}`")
    if max_pain:   level_parts.append(f"MP `{max_pain:.0f}`")
    if level_parts:
        lines.append("")
        lines.append("📍 " + "  |  ".join(level_parts))

    # ═══════════════════════════════════════════════════════
    # [5] OI PULSE  ← smart money footprint
    # ═══════════════════════════════════════════════════════
    if ce_oi or pe_oi:
        lines.append("")
        lines.append("🧮 *OI Pulse*")
        lines.append(
            f"  CE `{_fmt_oi(ce_oi)}` {_oi_arrow(ce_chg)} ({_chg_str(ce_chg)})"
            f"   PE `{_fmt_oi(pe_oi)}` {_oi_arrow(pe_chg)} ({_chg_str(pe_chg)})"
        )
        if f["oi_note"]:
            lines.append(f"  _{f['oi_note']}_")

    # ═══════════════════════════════════════════════════════
    # [6] SIGNALS  ← what triggered this alert
    # ═══════════════════════════════════════════════════════
    lines.append("")
    lines.append("📡 *Signals*")
    for a in sorted_alerts:
        sev   = a.get("severity", "LOW")
        badge = _SEV_BADGE.get(sev, "🔵 LOW ")
        text  = _one_liner(a)
        lines.append(f"  `{badge}` {text}")

    # ═══════════════════════════════════════════════════════
    # [7] CHART PULSE  ← quick timeframe read
    # ═══════════════════════════════════════════════════════
    if f["chart_lines"]:
        lines.append("")
        lines.append("📉 *Chart*")
        for cl in f["chart_lines"]:
            lines.append(f"  {cl}")

    # ═══════════════════════════════════════════════════════
    # [8] FORCES SUMMARY  ← bull vs bear in one glance
    # ═══════════════════════════════════════════════════════
    if f["bull_forces"] or f["bear_forces"]:
        lines.append("")
        bull_top = f["bull_forces"][0] if f["bull_forces"] else "None"
        bear_top = f["bear_forces"][0] if f["bear_forces"] else "None"
        lines.append("⚖️ *Forces*")
        lines.append(f"  🟢 {bull_top}")
        lines.append(f"  🔴 {bear_top}")
        # Show remaining forces if more than 1 each
        if len(f["bull_forces"]) > 1 or len(f["bear_forces"]) > 1:
            remaining_bull = f["bull_forces"][1:3]
            remaining_bear = f["bear_forces"][1:3]
            for b in remaining_bull:
                lines.append(f"  🟢 {b}")
            for b in remaining_bear:
                lines.append(f"  🔴 {b}")

    # ═══════════════════════════════════════════════════════
    # [9] BROADER TREND + FOOTER
    # ═══════════════════════════════════════════════════════
    if f["broader_trend"]:
        lines.append("")
        lines.append(f"🌊 *Trend:* {f['broader_trend']}")

    lines.append("")
    lines.append(f"_#{digest_id}  ·  {n} signal{'s' if n != 1 else ''} this scan_")

    return digest_id, "\n".join(lines)
