"""
Per-scan digest builder.
Groups all anomalies for one symbol+scan into a single Telegram message
with severity tiers + Bot Intelligence block.
"""
import uuid
from datetime import datetime, timezone, timedelta

from src.engine.intelligence import generate_intelligence

IST = timezone(timedelta(hours=5, minutes=30))

_SEV_EMOJI = {"HIGH": "🔥", "MEDIUM": "⚠️", "LOW": "ℹ️"}
_SEV_LABEL = {"HIGH": "HIGH", "MEDIUM": "MED ", "LOW": "LOW "}

_ATYPE_SHORT = {
    "OI_SPIKE":          "OI spike",
    "OI_UNWIND":         "OI unwind",
    "BUILDUP_CLASSIFY":  "",          # uses buildup_type from detail
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


def _one_liner(alert: dict) -> str:
    import json
    atype  = alert.get("alert_type", "")
    detail = {}
    try:
        detail = json.loads(alert.get("detail_json") or "{}")
    except Exception:
        pass

    strike     = alert.get("strike")
    opt_type   = alert.get("option_type", "")
    strike_tag = f" at {int(strike)} {opt_type}".rstrip() if strike else ""

    if atype == "BUILDUP_CLASSIFY":
        label = detail.get("buildup_type", "Buildup")
        oi_p  = detail.get("oi_pct", 0)
        ltp_p = detail.get("ltp_pct", 0)
        return f"{label}{strike_tag} (OI {oi_p:+.0f}%, LTP {ltp_p:+.0f}%)"

    if atype in ("OI_SPIKE", "OI_UNWIND"):
        pct = detail.get("pct_change", 0)
        return f"{_ATYPE_SHORT[atype]}{strike_tag} ({pct:+.0f}%)"

    if atype == "PRICE_SPIKE":
        pct = detail.get("pct_change", 0)
        dire = detail.get("direction", "")
        return f"Spot {pct:+.2f}% {dire} — momentum signal"

    if atype == "PCR_SHIFT":
        delta = detail.get("pcr_delta", 0)
        pcr   = detail.get("pcr", "?")
        dire  = "bearish flip" if delta < 0 else "bullish flip"
        return f"PCR shift {delta:+.3f} → {pcr} ({dire})"

    if atype == "PCR_EXTREME":
        pcr  = detail.get("pcr", "?")
        interp = detail.get("interpretation", "")
        return f"PCR={pcr} — {interp}"

    if atype == "PCR_VELOCITY":
        label = detail.get("label", "")
        slope = detail.get("slope", 0)
        return f"PCR trend {slope:+.3f}/scan — {label}"

    if atype == "IV_SPIKE":
        iv_d = detail.get("iv_delta", 0)
        return f"ATM IV +{iv_d:.1f}pts{strike_tag} — event/panic hedge"

    if atype == "IV_CRUSH":
        iv_d = detail.get("iv_delta", 0)
        return f"ATM IV {iv_d:.1f}pts{strike_tag} — vol crush"

    if atype == "ATM_LEG_MOVE":
        bias  = detail.get("bias", "")
        ce_p  = detail.get("ce_pct", 0)
        pe_p  = detail.get("pe_pct", 0)
        return f"CE {ce_p:+.1f}% / PE {pe_p:+.1f}% → {bias}"

    if atype == "STRADDLE_PREMIUM":
        pct  = detail.get("pct_change", 0)
        label = detail.get("label", "")
        return f"Straddle {pct:+.1f}% — {label}"

    if atype == "MAX_PAIN_SHIFT":
        shift = detail.get("shift", 0)
        curr  = detail.get("curr_max_pain", "?")
        return f"Max Pain → {curr} ({shift:+.0f} pts)"

    if atype == "OI_WALL_SHIFT":
        chg = detail.get("changes", {})
        parts = []
        for side, v in chg.items():
            parts.append(f"{side.capitalize()} wall {v['prev']}→{v['curr']}")
        return " | ".join(parts) if parts else "OI wall moved"

    if atype == "VOLUME_AGGRESSION":
        label = detail.get("label", "")
        ratio = detail.get("ratio", 0)
        return f"{label}{strike_tag} (ratio={ratio:.1f})"

    if atype == "OTM_UNUSUAL":
        pct = detail.get("pct_change", 0)
        return f"Far-OTM activity{strike_tag} +{pct:.0f}%"

    return f"{_ATYPE_SHORT.get(atype, atype)}{strike_tag}"


def build_digest(symbol: str, alerts: list[dict],
                 fetched_at: str | None = None,
                 scan_context: dict | None = None) -> tuple[str, str]:
    """
    Returns (digest_id, markdown_text) for one symbol scan.
    digest_id is attached to each alert so they can be grouped later.
    scan_context — enriched metadata from anomaly_detector for intelligence.
    """
    digest_id = str(uuid.uuid4())[:8]

    # Timestamp
    try:
        dt = datetime.fromisoformat(fetched_at or "").astimezone(IST)
    except Exception:
        dt = datetime.now(IST)
    ts = dt.strftime("%H:%M") + " IST"

    n = len(alerts)
    # Sort: HIGH first, then MEDIUM, then LOW
    _order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    sorted_alerts = sorted(alerts, key=lambda a: _order.get(a.get("severity", "LOW"), 2))

    lines = [f"📊 *{symbol}* | {ts} | {n} signal{'s' if n != 1 else ''}"]
    diag = (scan_context or {}).get("diagnostics", {})
    if not alerts and diag:
        pcr_delta = diag.get("pcr_delta")
        pcr_text = "PCR Δ n/a" if pcr_delta is None else f"PCR Δ {abs(pcr_delta):.3f} < {diag.get('pcr_shift_threshold', 0.25)}"
        oi_thresh = diag.get("oi_threshold", 40)
        ltp_thresh = diag.get("ltp_threshold", 8)
        lines.append(
            f"ℹ️ `SCAN` No alert: max OI Δ {float(diag.get('max_oi_delta_pct') or 0):.2f}% < {oi_thresh}, "
            f"ATM LTP Δ {float(diag.get('max_atm_ltp_delta_pct') or 0):.2f}% < {ltp_thresh}, {pcr_text}"
        )

    # ── Quick Chart Telemetry removed (now in intelligence section) ────────────────
    for a in sorted_alerts:
        sev   = a.get("severity", "LOW")
        emoji = _SEV_EMOJI.get(sev, "ℹ️")
        label = _SEV_LABEL.get(sev, "LOW ")
        text  = _one_liner(a)
        lines.append(f"{emoji} `{label}` {text}")

    # Bot Intelligence block (now receives scan context)
    intel = generate_intelligence(symbol, alerts, scan_context=scan_context)
    if intel:
        lines.append("")
        lines.extend(intel.splitlines())

    return digest_id, "\n".join(lines)
