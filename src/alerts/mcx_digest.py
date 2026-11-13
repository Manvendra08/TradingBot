"""
MCX-Specific Telegram Alert Digest Builder.

Dedicated template for MCX commodity contracts (NATURALGAS, CRUDEOIL, GOLD, SILVER).
Maintains strict separation from the common NSE/BSE template:
  1. Renders active weekly EIA Macro Stance & 5-year storage buffer metrics for NATURALGAS.
  2. Enforces the strict 72% MCX confidence floor display (downgrades sub-72% verdicts to HOLD / NO-TRADE).
  3. Adheres strictly to Timeframe Role Separation (3H entry timing vs 1H exit timing, not conflicting trends).
  4. Surfaces contract rollover & prompt short-squeeze risks.
"""

from __future__ import annotations

import uuid
import textwrap
from datetime import datetime, timezone, timedelta
from typing import Any
import pytz

from src.alerts.digest import (
    _esc,
    _esc_code,
    _val,
    _humanize_age,
    _top_news,
    _format_trade_status,
    _oi_buildup_label,
)
from src.engine.ng_macro_context import get_active_ng_macro_context

MCX_CONFIDENCE_FLOOR = 72
_IST = pytz.timezone("Asia/Kolkata")


def build_mcx_timeframe_digest(payload: dict, digest_id: str = None) -> tuple[str, str]:
    """
    Renders a dedicated, commodity-aware decision pipeline digest for MCX symbols.
    """
    header = payload.get("header", {})
    multileg = payload.get("multileg") or {}
    quant = payload.get("quant_inputs") or {}
    tfss = payload.get("tfss") or {}
    tf_block = payload.get("timeframe") or {}
    ai_thesis = payload.get("ai_thesis", "")
    ai_model_name = payload.get("ai_model_name") or header.get("ai_model_name")
    exit_advice = payload.get("exit_advice")
    news_data = payload.get("news_data") or quant.get("news_data")

    sym = _val(header.get("symbol")) or "MCX"
    stime = _val(header.get("scan_time")) or ""
    expiry = _val(header.get("expiry"))
    dte = header.get("dte")
    spot = header.get("underlying") if header.get("underlying") is not None else header.get("spot")

    conf_raw = multileg.get("confidence") or header.get("confidence")
    try:
        conf_num = float(conf_raw) if conf_raw is not None else 0.0
    except (ValueError, TypeError):
        conf_num = 0.0

    is_sub_threshold = conf_num < MCX_CONFIDENCE_FLOOR

    lines: list[str] = []
    DIV = "───────────────"

    ml_action = str(multileg.get("action") or "").upper()
    has_live_books = bool(multileg.get("live_books"))
    has_legs = bool(multileg.get("legs"))
    raw_strat = str(multileg.get("strategy_type") or "")
    is_real_strategy = raw_strat and raw_strat.upper() not in ("", "NO_TRADE", "NONE")

    has_ml_activity = (
        has_legs
        or has_live_books
        or is_real_strategy
        or bool(multileg.get("closed_items"))
        or ml_action in ("ENTERED", "HOLD", "MONITORED", "ADVISORY", "REJECTED", "CONFLICT", "CLOSED")
    )

    header_trade_entered = bool(header.get("trade_entered"))
    ml_stage = str(multileg.get("decision_stage") or "").upper()
    is_blocked_by_stage = ml_stage in ("REJECTED", "BLOCKED", "NO_ACTION")

    is_entered = (
        (header_trade_entered and not is_blocked_by_stage)
        or (ml_action == "ENTERED" and has_live_books)
    )
    is_closed_cycle = bool(multileg.get("closed_items")) and not is_entered
    is_holding = (
        (ml_action in ("HOLD", "MONITORED") or (has_live_books and not is_entered))
        and not is_entered
        and not is_closed_cycle
    )
    is_advisory = ml_action == "ADVISORY"
    is_rejected = ml_action in ("REJECTED", "BLOCKED")
    is_conflict = ml_action == "CONFLICT"
    is_paused = ml_action == "BLOCKED_TRADING_PAUSED" or ml_stage == "BLOCKED_TRADING_PAUSED"

    # Header status badge enforcing the 72% MCX confidence floor
    if is_paused:
        trade_status_str = "⏸️ *PAUSED*"
    elif is_entered:
        _is_live = False
        try:
            from config.runtime_config import is_broker_trade_enabled
            _is_live = is_broker_trade_enabled()
        except Exception:
            pass
        trade_status_str = "🟢 *LIVE ENTERED*" if _is_live else "🟢 *PAPER ENTERED*"
    elif is_closed_cycle:
        trade_status_str = "🔴 *EXITED*"
    elif is_holding:
        trade_status_str = "🔵 *HOLDING*"
    elif is_sub_threshold and not is_entered:
        # Enforce MCX 72% confidence floor on status badge
        trade_status_str = f"⏸️ *SUB-THRESHOLD ({int(conf_num)}% < 72%)*"
    elif is_advisory:
        trade_status_str = "🟡 *ADVISORY*"
    elif is_rejected or is_conflict:
        trade_status_str = "🔴 *REJECTED*"
    elif has_ml_activity:
        trade_status_str = "⏸️ *STAND ASIDE*"
    else:
        trade_status_str = "⏸️ *STAND ASIDE*"

    # Format expiry & DTE
    if expiry:
        try:
            cleaned_exp = str(expiry).strip().split("T")[0].split()[0]
            exp_dt = None
            for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
                try:
                    exp_dt = datetime.strptime(cleaned_exp, fmt).date()
                    break
                except Exception:
                    continue
            if exp_dt:
                exp_fmt = exp_dt.strftime("%d %b")
                today_dt = datetime.now(_IST).date()
                calc_dte = (exp_dt - today_dt).days
                if dte is None or not (0 <= dte <= 365):
                    dte = max(0, calc_dte)
            else:
                exp_fmt = str(expiry)
        except Exception:
            exp_fmt = str(expiry)
        expiry_str = f"🗓 Expiry {exp_fmt}"
    else:
        expiry_str = "🗓 Expiry N/A"

    dte_str = f"{dte} DTE" if (dte is not None and 0 <= dte <= 365) else "N/A DTE"

    # Format spot
    if isinstance(spot, (int, float)) and spot > 0:
        spot_str = f"₹{spot:,.2f}".rstrip('0').rstrip('.')
    else:
        spot_str = f"₹{spot}" if spot not in (None, "", "N/A", 0, 0.0) else "N/A"

    stime_clean = stime.replace(" IST", "").strip() if stime else ""
    time_str = f" · {stime_clean} IST" if stime_clean else ""

    status_suffix = f"  {trade_status_str}" if trade_status_str else ""
    lines.append(f"🛢️ *{sym}*{time_str}{status_suffix}")
    nymex_str = ""
    if "NATURALGAS" in sym.upper():
        try:
            from src.fetchers.tv_nymex_streamer import get_live_nymex_quote
            lq = get_live_nymex_quote(max_age_sec=30.0)
            if lq and lq.get("price") and float(lq["price"]) > 0:
                nymex_str = f" | NYMEX ${float(lq['price']):.3f}"
        except Exception:
            pass
    lines.append(f"{expiry_str} · {dte_str} | Spot {spot_str}{nymex_str}")

    # ── 1. COMMODITY & MACRO READINGS ──
    lines.append("")
    lines.append(DIV)
    lines.append("🌍 *COMMODITY & MACRO READINGS*")

    # Natural Gas specific macro intelligence
    if "NATURALGAS" in sym.upper():
        ng_macro = payload.get("mcx_macro")
        if not ng_macro:
            try:
                ng_macro = get_active_ng_macro_context()
            except Exception:
                ng_macro = {}

        stance = ng_macro.get("macro_stance", "NEUTRAL_BALANCED")
        stance_icon = "🟢" if "BULL" in stance else ("🔴" if "BEAR" in stance else ("🟡" if "DIVERGENT" in stance else "⚪"))
        valid_until_raw = ng_macro.get("valid_until")
        valid_str = ""
        if valid_until_raw:
            try:
                v_dt = datetime.fromisoformat(str(valid_until_raw).replace("Z", "+00:00")).astimezone(_IST)
                valid_str = f" (Valid to {v_dt.strftime('%d %b')})"
            except Exception:
                pass

        stance_fmt = str(stance).replace("_", " ")
        lines.append(f"• Macro EIA Stance: {stance_icon} *{stance_fmt}*{valid_str}")

        act_bcf = ng_macro.get("actual_bcf")
        five_yr_bcf = ng_macro.get("five_year_avg_bcf")
        cons_bcf = ng_macro.get("consensus_bcf")
        if act_bcf is not None and five_yr_bcf is not None:
            delta_bcf = act_bcf - five_yr_bcf
            delta_desc = "Deficit / Surplus Erosion" if delta_bcf < 0 else "Surplus Expansion"
            lines.append(f"• Storage Delta (vs 5-Yr): {act_bcf:+.1f} Bcf Act vs {five_yr_bcf:+.1f} Bcf 5-Yr ({delta_bcf:+.1f} Bcf {delta_desc})")

        if act_bcf is not None and cons_bcf is not None:
            from src.engine.ng_macro_context import format_eia_surprise
            _, _, surp_label = format_eia_surprise(act_bcf, cons_bcf)
            lines.append(f"• Consensus Surprise: {act_bcf:+.1f} Bcf Act vs {cons_bcf:+.1f} Bcf Cons ({surp_label})")

        surplus_bcf = ng_macro.get("surplus_vs_5yr_bcf")
        surplus_pct = ng_macro.get("surplus_vs_5yr_pct")
        consec = ng_macro.get("consecutive_tightening_weeks", 0)
        streak_str = f" | {consec} below-norm builds" if consec >= 2 else ""
        if surplus_bcf is not None and surplus_pct is not None:
            lines.append(f"• Storage Buffer: {surplus_bcf:+.0f} Bcf ({surplus_pct:+.1f}% vs 5-yr norm){streak_str}")

        # Big Player Institutional Intent footprint
        try:
            from src.engine.ng_macro_context import deduce_institutional_intent
            p_chg = float(quant.get("price_change_pct") or 0.0)
            ce_c = int(quant.get("ce_oi_change") or 0)
            pe_c = int(quant.get("pe_oi_change") or 0)
            pcr_v = float(quant.get("pcr") or 1.0) if quant.get("pcr") not in (None, "N/A", "") else 1.0
            cons_d = (act_bcf - cons_bcf) if (act_bcf is not None and cons_bcf is not None) else 0.0
            inst = deduce_institutional_intent(
                spot_change_pct=p_chg,
                ce_oi_change=ce_c,
                pe_oi_change=pe_c,
                pcr=pcr_v,
                macro_stance=stance,
                consensus_delta=cons_d,
            )
            lines.append(f"• Big Player Footprint: 🏦 *{inst['label']}*")
            lines.append(f"  ↳ _{inst['trading_implication']}_")
        except Exception:
            pass

        # Commodity session regime
        ng_reg = quant.get("ng_regime")
        if ng_reg:
            ng_dev = quant.get("ng_dev_pct", 0)
            lines.append(f"• Session Regime: `{ng_reg}` (Dev `{ng_dev:+.2f}%`)")

        # Expiry rollover risk
        squeeze_risk = ng_macro.get("is_rollover_squeeze_risk", False)
        prompt_dte = ng_macro.get("prompt_dte", 15)
        if squeeze_risk:
            lines.append(f"• Expiry Rollover: ⚠️ *HIGH Short-Squeeze Risk* ({prompt_dte} DTE prompt rollover)")

    # Quantitative options flow & technicals
    oi_v = _val(quant.get("oi_verdict")) or "Neutral"
    oi_c = quant.get("oi_confidence", 0)
    pcr_val = _val(quant.get("pcr")) or "N/A"
    pain_val = _val(quant.get("max_pain")) or "N/A"
    sup_val = _val(quant.get("support")) or "N/A"
    res_val = _val(quant.get("resistance")) or "N/A"
    ce_chg = quant.get("ce_oi_change") or 0
    pe_chg = quant.get("pe_oi_change") or 0

    lines.append(f"• Session OI Flow: CE `{ce_chg:+,}` / PE `{pe_chg:+,}` | PCR `{pcr_val}` | Pain `{pain_val}`")
    lines.append(f"• Range: S `{sup_val}` / R `{res_val}`")

    # Timeframe Separation strictly observed: 3H = Entry Timing, 1H = Exit Level
    c1 = quant.get("chart_1h", "NEUTRAL")
    c3 = quant.get("chart_3h", "NEUTRAL")
    c1_mark = "🟢" if "BULL" in str(c1).upper() else ("🔴" if "BEAR" in str(c1).upper() else "⚪")
    c3_mark = "🟢" if "BULL" in str(c3).upper() else ("🔴" if "BEAR" in str(c3).upper() else "⚪")
    lines.append(f"• Timeframe Roles: 3H Entry Timing {c3_mark} `{c3}` | 1H Exit Level {c1_mark} `{c1}`")

    atm_iv = quant.get("atm_iv", 0)
    if atm_iv:
        lines.append(f"• Volatility: ATM IV `{atm_iv:.1f}%` (Range `{quant.get('iv_low',0):.1f}%` - `{quant.get('iv_high',0):.1f}%`)")

    # ── 2. COMMODITY STRUCTURE & STRATEGY SELECTION ──
    if has_ml_activity and (is_real_strategy or has_legs):
        lines.append("")
        lines.append(DIV)
        strat_type = raw_strat.replace("_", " ").upper() if raw_strat else "NO TRADE"
        strat_icon_map = {
            "BEAR CALL SPREAD": "🛡️",
            "BULL PUT SPREAD": "🛡️",
            "IRON CONDOR": "🦅",
            "SHORT STRANGLE": "⚡",
            "SHORT STRADDLE": "🎯",
            "JADE LIZARD": "🦎",
            "NO TRADE": "⏸️",
        }
        icon = strat_icon_map.get(strat_type, "📐")

        m_tag = f" ({_esc(ai_model_name)})" if ai_model_name else ""
        lines.append(f"🧠 *LLM STRUCTURE CHOICE*{m_tag}")

        if is_sub_threshold and not is_entered:
            lines.append(f"Strategy: ⏸️ *NO TRADE* (Confidence {int(conf_num)}% < 72% MCX Floor)")
        else:
            conf_str = f" ({int(conf_num)}% conviction)" if conf_num > 0 else ""
            lines.append(f"Strategy: {icon} *{_esc(strat_type)}*{conf_str}")

        legs = multileg.get("legs") or []
        if legs:
            lines.append("Leg Breakdown:")
            for leg in legs:
                side = (leg.get("side") or "SELL").upper()
                side_icon = "🔴" if side == "SELL" else "🟢"
                opt_type = (leg.get("option_type") or "").upper()
                strike_val = float(leg.get("strike") or 0.0)
                prem_val = float(leg.get("entry_premium") or leg.get("premium") or 0.0)
                delta_val = float(leg.get("delta") or 0.0)
                strike_fmt = f"{strike_val:,.0f}" if strike_val >= 1000 else f"{strike_val:.2f}".rstrip('0').rstrip('.')
                lines.append(f"  {side_icon} {side} {strike_fmt} {opt_type} @ ₹{prem_val:.2f} (Δ{delta_val:+.2f})")

        net_prem = float(multileg.get("net_premium") or 0.0)
        margin_val = float(multileg.get("margin") or 0.0)
        econ_parts = []
        if net_prem > 0:
            econ_parts.append(f"Net Prem ₹{net_prem:.2f}")
        if margin_val > 0:
            econ_parts.append(f"Margin ₹{margin_val:,.0f}")
        if econ_parts:
            lines.append(f"Economics: {' · '.join(econ_parts)}")

    # ── 2b. LIVE BOOK MONITORING & POSITION EXIT ADVICE (MCX) ──
    live_books = multileg.get("live_books") or []
    closed_items = multileg.get("closed_items") or []
    ai_exit = multileg.get("ai_exit_advice") or exit_advice

    has_current_trade = is_entered or bool(closed_items)
    should_show_books = (
        has_ml_activity
        and (has_current_trade or is_holding or bool(ai_exit))
        and (bool(closed_items) or ((is_entered or is_holding) and bool(live_books)) or bool(ai_exit))
    )
    if should_show_books:
        lines.append("")
        lines.append(DIV)
        lines.append("📈 *MULTI-LEG TRADES*")

        if closed_items:
            for c in closed_items:
                c_id = c.get("book_id") or ""
                c_st = str(c.get("strategy_type") or "").replace("_", " ").upper()
                c_pnl = float(c.get("total_pnl") or 0.0)
                c_re = c.get("reason") or "Closed"
                pnl_icon = "🟢" if c_pnl >= 0 else "🔴"
                lines.append(f"• Closed: `{_esc_code(c_id)}` ({c_st}) | {pnl_icon} P&L ₹{c_pnl:,.0f} | {_esc(c_re)}")

        if (is_entered or is_holding) and live_books:
            entered_book_id = multileg.get("book_id")
            target_books = [b for b in live_books if b.get("book_id") == entered_book_id] if (entered_book_id and is_entered) else live_books
            for b in target_books:
                b_id = b.get("book_id") or ""
                b_st = str(b.get("strategy_type") or "").replace("_", " ").upper()
                b_pnl = float(b.get("total_pnl") or 0.0)
                b_prem = float(b.get("net_premium") or 0.0)
                b_delta = float(b.get("net_delta") or 0.0)
                pnl_icon = "🟢" if b_pnl >= 0 else "🔴"

                age_suffix = ""
                if is_holding and b.get("opened_at"):
                    age = _humanize_age(b.get("opened_at"))
                    if age:
                        age_suffix = f" · entered {age} ago"

                lines.append(f"• Book: `{_esc_code(b_id)}` · {b_st}{age_suffix}")
                lines.append(f"  Net Prem ₹{b_prem:.1f}/lot | {pnl_icon} Total P&L ₹{b_pnl:,.0f} | Δ Net {b_delta:+.2f}")

                be_l = b.get("breakeven_lower")
                be_u = b.get("breakeven_upper")
                if be_l and be_u:
                    lines.append(f"  BE: ₹{be_l:,.1f} — ₹{be_u:,.1f}")

                b_legs = b.get("legs") or []
                for l in b_legs:
                    l_side = (l.get("side") or "SELL").upper()
                    l_type = (l.get("option_type") or "").upper()
                    l_strike = float(l.get("strike") or 0.0)
                    l_entry = float(l.get("entry_premium") or 0.0)
                    l_curr = float(l.get("current_premium") or l_entry)
                    s_icon = "🔴" if l_side == "SELL" else "🟢"
                    lines.append(f"    {s_icon} {l_side} {l_strike:.1f} {l_type} | Entry ₹{l_entry:.1f} → Current ₹{l_curr:.1f}")

        if ai_exit:
            ea_act = ai_exit.get("action") if isinstance(ai_exit, dict) else getattr(ai_exit, "action", None)
            if ea_act:
                ea_urg = ai_exit.get("urgency") if isinstance(ai_exit, dict) else getattr(ai_exit, "urgency", "LOW")
                ea_rea = ai_exit.get("reasoning") if isinstance(ai_exit, dict) else getattr(ai_exit, "reasoning", "")
                ea_emoji = {"HOLD": "🔵", "ADJUST": "🔄", "CLOSE": "🔴"}.get(str(ea_act).upper(), "⚙️")
                lines.append(f"• AI Exit Advice: {ea_emoji} *{_esc(ea_act)}* (Urgency: {_esc(ea_urg)})")
                if ea_rea:
                    lines.append(f"  _{_esc(ea_rea)}_")

    # ── 3. THESIS & MARKET CONTEXT ──
    thesis_text = (
        ai_thesis
        or multileg.get("thesis")
        or multileg.get("reason")
        or tfss.get("primary_reason")
        or ""
    ).strip()

    import re

    # Strip embedded duplicate news lines from synthesize_market_insight to prevent double printing
    thesis_text = re.sub(r"(?i)(?:^|\n)news:\s*[\s\S]*?(?=(?:\n[A-Z][a-z]+:|\Z))", "", thesis_text).strip()

    # Enforce 72% MCX floor in the thesis verdict (handling single or multiline wrapped verdicts)
    if is_holding:
        floor_verdict = "Verdict: 🔵 HOLD CURRENT POSITION (Monitoring active book)"
        verdict_pat = r"(?i)verdict:[\s\S]*?(?=(?:\n\n|\n[A-Z][a-z]+:|\Z))"
        if re.search(r"(?i)verdict:", thesis_text):
            thesis_text = re.sub(verdict_pat, floor_verdict, thesis_text)
        else:
            thesis_text += f"\n\n{floor_verdict}"
    elif is_sub_threshold and not is_entered:
        floor_verdict = f"Verdict: HOLD / NO-TRADE (Confidence {int(conf_num)}% < 72% MCX Floor)"
        verdict_pat = r"(?i)verdict:[\s\S]*?(?=(?:\n\n|\n[A-Z][a-z]+:|\Z))"
        if re.search(r"(?i)verdict:", thesis_text):
            thesis_text = re.sub(verdict_pat, floor_verdict, thesis_text)
        else:
            thesis_text += f"\n\n{floor_verdict}"

    if thesis_text:
        lines.append("")
        lines.append(DIV)
        if is_holding:
            lines.append("💡 *POSITION MONITORING & EXIT EVALUATION*")
        else:
            lines.append("💡 *THESIS & MARKET CONTEXT*")
        for raw_line in thesis_text.splitlines():
            if not raw_line.strip():
                continue
            for chunk in textwrap.wrap(raw_line, width=38):
                lines.append(_esc(chunk))

    # News section (single canonical rendering)
    news_line = _top_news(news_data)
    if news_line:
        lines.append("")
        lines.append(f"📰 *News:* {_esc(news_line)}")

    matrix_shadow = payload.get("matrix_shadow")
    if matrix_shadow:
        try:
            from src.alerts.matrix_alerts import matrix_digest_line
            m_line = matrix_digest_line(matrix_shadow)
            if m_line:
                lines.append("")
                lines.append(m_line)
        except Exception:
            pass

    if digest_id is None:
        digest_id = str(uuid.uuid4())[:8]

    return digest_id, "\n".join(lines)
