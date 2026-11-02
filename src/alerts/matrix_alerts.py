"""
Telegram alert and digest templates for Decision Matrix v1.0.
Pure string formatters; enforces Telegram Markdown V1 escaping and length constraints.
"""

from typing import Any
from src.alerts.digest import _esc, _esc_code

TELEGRAM_MAX_LEN = 4096


def _fit_msg(text: str, max_chars: int = TELEGRAM_MAX_LEN) -> str:
    """Clamp message to Telegram maximum length without breaking halfway."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def matrix_digest_line(shadow_data: dict[str, Any]) -> str:
    """
    Produce a concise <=3 line digest entry representing the decision matrix verdict.
    Format:
    🧭 Matrix v1.0.0 · NEUTRAL · IV MID (IVP 48.0) → IRON CONDOR · AGG
    [MCX]: NYMEX 1H BULL · OI NEUTRAL · agree ✓
    [Shadow]: 👁 SHADOW · would: IRON CONDOR · actual: SHORT_STRANGLE
    """
    if not isinstance(shadow_data, dict):
        return ""

    mode = str(shadow_data.get("mode", "shadow")).lower()
    version = str(shadow_data.get("config_version", "1.0.0"))
    direction = str(shadow_data.get("direction", "NEUTRAL"))
    vol_bucket = str(shadow_data.get("vol_bucket", "HIGH"))
    ivp = shadow_data.get("ivp")
    ivp_str = f"IVP {ivp:.1f}" if isinstance(ivp, (int, float)) else "IVP N/A"

    rec_struct = shadow_data.get("recommended_structure")
    profile = str(shadow_data.get("delta_profile", "aggressive"))[:3].upper()

    reason = shadow_data.get("reason", "OK")

    lines: list[str] = []

    prefix = "👁 SHADOW · " if mode == "shadow" else ""

    rec_struct_fmt = str(rec_struct).replace("_", " ") if rec_struct else ""
    if rec_struct:
        line1 = f"🧭 {prefix}Matrix v{_esc(version)} · {_esc(direction)} · IV {_esc(vol_bucket)} ({_esc(ivp_str)}) → *{_esc(rec_struct_fmt)}* · {_esc(profile)}"
    else:
        line1 = f"⛔ {prefix}Matrix v{_esc(version)} · {_esc(direction)} · IV {_esc(vol_bucket)} ({_esc(ivp_str)}) → *NO TRADE* ({_esc(reason)})"
    lines.append(line1)

    # Line 2: NYMEX agreement for MCX
    nymex_sym = shadow_data.get("nymex_symbol")
    nymex_trend = shadow_data.get("nymex_trend")
    if nymex_sym and nymex_trend:
        agree_icon = "agree ✓" if shadow_data.get("nymex_agrees") else "diverge ✗"
        line2 = f"   ↳ NYMEX 1H {_esc(nymex_trend)} · OI {_esc(direction)} · {_esc(agree_icon)}"
        lines.append(line2)

    # Line 3: Shadow comparison if different
    if mode == "shadow":
        actual = shadow_data.get("actual_llm_structure") or shadow_data.get("actual_core_verdict") or "None"
        actual_fmt = str(actual).replace("_", " ")
        if rec_struct != actual:
            lines.append(f"   ↳ shadow diff: would *{_esc(rec_struct_fmt)}* vs actual `{_esc_code(actual_fmt)}`")

    return _fit_msg("\n".join(lines))


def format_entry(
    plan: dict[str, Any],
    config_version: str = "1.0.0",
    is_shadow: bool = False,
    verbosity: str = "compact",
) -> str:
    """
    Format a multi-leg entry notification:
    structure · expiry · cfg version · profile
    legs with strike/Δ/premium
    net credit, credit/wing %, max loss, stop, target
    capital cap % used, lots, AI line
    """
    prefix = "👁 [SHADOW ENTRY]" if is_shadow else "🚀 [ENTRY]"
    symbol = plan.get("symbol", "")
    structure = plan.get("strategy") or plan.get("structure") or "SPREAD"
    expiry = plan.get("expiry", "")
    profile = plan.get("delta_profile", "aggressive").upper()

    lines = [
        f"{prefix} *{_esc(symbol)}* {_esc(structure)} · {_esc(expiry)}",
        f"⚙️ Matrix v{_esc(config_version)} · Profile: {_esc(profile)}",
    ]

    legs = plan.get("legs", [])
    if legs:
        leg_strs = []
        for leg in legs:
            action = leg.get("action", "SELL")
            opt_type = leg.get("option_type", "CE")
            strike = leg.get("strike", 0)
            delta = leg.get("delta")
            delta_str = f"Δ{delta:.2f}" if isinstance(delta, (int, float)) else ""
            premium = leg.get("premium", 0.0)
            leg_strs.append(f"{action[0]} {strike}{opt_type} {delta_str} @ ₹{premium:.1f}")
        lines.append("🦵 " + " | ".join(leg_strs))

    net_credit = plan.get("net_credit", 0.0)
    credit_ratio = plan.get("credit_to_width_pct") or plan.get("credit_ratio")
    ratio_str = f" ({credit_ratio:.1f}% wing)" if isinstance(credit_ratio, (int, float)) else ""
    max_loss = plan.get("max_loss", 0.0)
    lines.append(f"💰 Net Credit: ₹{net_credit:.1f}{ratio_str} | Max Loss: ₹{max_loss:.1f}")

    stop_str = plan.get("stop_description") or f"₹{plan.get('stop_loss', 0.0):.1f}"
    target_str = plan.get("target_description") or f"₹{plan.get('target', 0.0):.1f}"
    lines.append(f"🎯 Target: {target_str} | 🛑 Stop: {stop_str}")

    lots = plan.get("lots", 1)
    cap_pct = plan.get("capital_cap_pct")
    cap_str = f" | Cap Used: {cap_pct:.1f}%" if isinstance(cap_pct, (int, float)) else ""
    lines.append(f"📦 Lots: {lots}{cap_str}")

    ai_verdict = plan.get("ai_verdict") or plan.get("ai_assessment")
    if ai_verdict:
        ai_action = plan.get("ai_action", "agree")
        if ai_action == "veto":
            ai_line = f"✋ AI veto: {_esc(str(ai_verdict))}"
        elif ai_action == "size_cut":
            ai_line = f"✂️ AI size ½: {_esc(str(ai_verdict))}"
        else:
            ai_line = f"✓ AI agrees: {_esc(str(ai_verdict))}"
        lines.append(ai_line)

    return _fit_msg("\n".join(lines))


def format_exit(
    exit_data: dict[str, Any],
    is_shadow: bool = False,
    verbosity: str = "compact",
) -> str:
    """
    Format trade exit alert with standardized exit codes:
    HARD_RISK | TESTED_SIDE | EVENT | EXPIRY | REGIME_FLIP | TARGET | DEAD_TRADE | AI_TIGHTEN
    """
    prefix = "👁 [SHADOW EXIT]" if is_shadow else "🏁 [EXIT]"
    symbol = exit_data.get("symbol", "")
    structure = exit_data.get("strategy") or exit_data.get("structure") or "POSITION"
    reason_code = exit_data.get("reason_code", "TARGET")
    pnl = exit_data.get("pnl_rupees", 0.0)
    pnl_pct = exit_data.get("pnl_credit_pct")
    pnl_pct_str = f" ({pnl_pct:+.1f}% credit)" if isinstance(pnl_pct, (int, float)) else ""
    hold_time = exit_data.get("hold_time_str") or exit_data.get("hold_duration", "")

    pnl_icon = "🟢" if pnl >= 0 else "🔴"

    lines = [
        f"{prefix} *{_esc(symbol)}* {_esc(structure)}",
        f"🏷 Trigger: *{_esc(reason_code)}*",
        f"{pnl_icon} P&L: ₹{pnl:+,.1f}{pnl_pct_str} | Hold: {_esc(str(hold_time))}",
    ]

    detail = exit_data.get("trigger_detail")
    if detail:
        lines.append(f"🔍 Detail: {_esc(str(detail))}")

    lines.append("⚡ Order: SELL legs closed first")
    return _fit_msg("\n".join(lines))


def format_skip(skip_data: dict[str, Any]) -> str:
    """Format skipped trade reason."""
    symbol = skip_data.get("symbol", "")
    reason = skip_data.get("reason", "No trade")
    override = skip_data.get("override_rule")
    override_str = f" · Override: {_esc(override)}" if override else ""
    return _fit_msg(f"⛔ *SKIP {_esc(symbol)}*: {_esc(reason)}{override_str}")


def format_config_event(event_type: str, details: str, config_version: str = "1.0.0") -> str:
    """Format hot-reload or config validation alert."""
    if event_type.upper() == "INVALID":
        return _fit_msg(f"⚠️ *Matrix cfg INVALID* ({_esc(details)}) → defaults active")
    elif event_type.upper() == "RELOADED":
        return _fit_msg(f"⚙️ *Matrix cfg v{_esc(config_version)} loaded* · changed: {_esc(details)}")
    return _fit_msg(f"ℹ️ *Matrix cfg*: {_esc(details)}")
