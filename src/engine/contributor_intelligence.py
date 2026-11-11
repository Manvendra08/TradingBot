"""
contributor_intelligence.py
---------------------------
Institutional Pro Trader AI Intelligence Engine for Index Contributors.

Analyzes index constituent point propulsion, heavyweight concentration,
intraday scan delta impulses, and derivative structure alignment to deliver
senior executive market intelligence for an institutional derivatives desk.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional

import pytz

from src.models.schema import get_read_conn

log = logging.getLogger("contributor_intelligence")
IST = pytz.timezone("Asia/Kolkata")

# Memory cache for fast responses: keyed by (index_name, latest_scan_time)
_AI_SUMMARY_CACHE: Dict[str, Dict[str, Any]] = {}


def generate_pro_trader_intelligence(
    index_name: str = "NIFTY",
    force_refresh: bool = False,
    enable_llm: bool = False
) -> Dict[str, Any]:
    """
    Generate an institutional, Pro Trader intelligence briefing for the given index.

    Combines:
      - Heavyweight cumulative point impact vs. index spot movement
      - Constituent Breadth Divergence (Heavyweight Share %)
      - Pillar dispersion & Intra-sector clash (e.g., HDFC Bank vs. ICICI Bank)
      - 15-minute scan delta impulse & 5-scan shift velocity
      - Option chain alignment (PCR, Max Pain, Put Base, Call Wall)
      - Actionable desk execution playbook for a ₹1 Cr derivatives portfolio

    Returns structured JSON with 4 core intelligence blocks.
    """
    from src.engine.index_contributors import get_heavyweights_history, get_heavyweights_timeline

    valid_indices = ("NIFTY", "BANKNIFTY", "SENSEX")
    idx_key = index_name.upper() if index_name.upper() in valid_indices else "NIFTY"

    # 1. Fetch Timeline and History
    tl_data = get_heavyweights_timeline(idx_key, day_filter="today", force_refresh=force_refresh)
    hw_history = get_heavyweights_history(limit=5)
    hw_index_data = hw_history.get(idx_key, {})

    latest_scan_time = tl_data.get("latest_scan_time") or "Recent"
    cache_key = f"{idx_key}_{latest_scan_time}_{enable_llm}"

    if not force_refresh and cache_key in _AI_SUMMARY_CACHE:
        return _AI_SUMMARY_CACHE[cache_key]

    # 2. Extract Key Quantitative Drivers
    spot = float(tl_data.get("latest_spot_price") or 0.0)
    index_net_pts = float(tl_data.get("latest_index_net_points") or 0.0)
    hw_cum_pts = float(tl_data.get("latest_hw_cumulative_points") or 0.0)
    hw_scan_delta = float(tl_data.get("latest_hw_scan_delta") or 0.0)
    divergence_pts = float(tl_data.get("divergence_points") or 0.0)
    hw_share = float(tl_data.get("hw_share_pct") or 50.0)
    regime = tl_data.get("regime") or "Range-Bound Consolidation"

    top_driver_dict = tl_data.get("top_driver") or {}
    driver_sym = top_driver_dict.get("symbol", "N/A")
    driver_pts = float(top_driver_dict.get("impact", 0.0))

    top_dragger_dict = tl_data.get("top_dragger") or {}
    dragger_sym = top_dragger_dict.get("symbol", "N/A")
    dragger_pts = float(top_dragger_dict.get("impact", 0.0))

    # Calculate broader constituent contribution (remaining stocks in index)
    broader_pts = index_net_pts - hw_cum_pts

    # 3. Extract Heavyweights details from history
    hw_list = hw_index_data.get("heavyweights", [])
    driver_info = next((h for h in hw_list if h.get("symbol") == driver_sym), {})
    dragger_info = next((h for h in hw_list if h.get("symbol") == dragger_sym), {})

    driver_sector = driver_info.get("sector") or "Leading Constituent"
    dragger_sector = dragger_info.get("sector") or "Lagging Constituent"
    driver_shift = float(driver_info.get("shift_pts") or 0.0)
    dragger_shift = float(dragger_info.get("shift_pts") or 0.0)

    # Check for same-sector clash (e.g. HDFC Bank vs ICICI Bank in Financial Services)
    is_sector_clash = (
        driver_sector != "Leading Constituent"
        and dragger_sector != "Lagging Constituent"
        and (driver_sector.lower() in dragger_sector.lower() or dragger_sector.lower() in driver_sector.lower())
    )

    # 4. Fetch Derivative Alignment Context from scan_summaries
    opt_ctx = {
        "pcr": 1.0,
        "atm_strike": round(spot / 50.0) * 50.0 if idx_key == "NIFTY" else round(spot / 100.0) * 100.0,
        "max_pain": round(spot / 50.0) * 50.0 if idx_key == "NIFTY" else round(spot / 100.0) * 100.0,
        "support": 0.0,
        "resistance": 0.0,
        "verdict": "Neutral",
        "confidence": 50,
    }
    try:
        with get_read_conn() as conn:
            row = conn.execute(
                """
                SELECT underlying, atm_strike, max_pain, pcr, support, resistance, verdict_label, confidence
                FROM scan_summaries
                WHERE symbol = ?
                ORDER BY fetched_at DESC
                LIMIT 1
                """,
                (idx_key,)
            ).fetchone()
            if row:
                opt_ctx["pcr"] = float(row[3] or 1.0)
                opt_ctx["max_pain"] = float(row[2] or opt_ctx["atm_strike"])
                opt_ctx["support"] = float(row[4] or 0.0)
                opt_ctx["resistance"] = float(row[5] or 0.0)
                opt_ctx["verdict"] = row[6] or "Neutral"
                opt_ctx["confidence"] = int(row[7] or 50)
                if row[1]:
                    opt_ctx["atm_strike"] = float(row[1])
    except Exception as e:
        log.warning("Could not fetch derivative context for %s: %s", idx_key, e)

    # 5. Build Quant Analysis Pillars

    # Pillar 1: Institutional Market Footprint (Breadth vs. Heavyweight Shield)
    if index_net_pts > 0 and hw_cum_pts > 0 and broader_pts > 0:
        footprint_class = "Broad Market Genuine Accumulation"
        footprint_tone = "bullish"
        footprint_desc = (
            f"Genuine institutional capital inflow confirmed across both mega-caps and broader market. "
            f"Index spot gain of {index_net_pts:+.1f} pts is propelled {hw_share:.1f}% by the top 10 heavyweights ({hw_cum_pts:+.1f} pts) "
            f"and supported by {broader_pts:+.1f} pts from the broader constituents. This is NOT an artificial heavyweight prop; "
            f"institutional accumulation exhibits genuine multi-sector participation."
        )
    elif index_net_pts <= 0 and hw_cum_pts > 0:
        footprint_class = "Synthetic Heavyweight Propping (Distribution Shield)"
        footprint_tone = "bearish"
        footprint_desc = (
            f"High-risk institutional divergence detected. Mega-caps are being actively bid ({hw_cum_pts:+.1f} pts from top heavyweights) "
            f"to mask intense underlying liquidation across broader constituents ({broader_pts:+.1f} pts drag). "
            f"Smart money frequently deploys this 'heavyweight shield' to offload mid-tier holdings before letting the index adjust downward."
        )
    elif index_net_pts > 0 and hw_cum_pts <= 0:
        footprint_class = "Stealth Broad-Market Accumulation"
        footprint_tone = "bullish"
        footprint_desc = (
            f"Stealth accumulation in play. Mega-caps are consolidating or being trimmed ({hw_cum_pts:+.1f} pts), "
            f"yet intense bottom-up buying across broader constituents ({broader_pts:+.1f} pts) is lifting the index {index_net_pts:+.1f} pts. "
            f"Once heavyweights find equilibrium, the index possesses substantial upside spring."
        )
    elif index_net_pts < 0 and hw_cum_pts < 0:
        footprint_class = "Broad-Based Institutional Liquidation"
        footprint_tone = "bearish"
        footprint_desc = (
            f"Systemic risk-off liquidation confirmed. The index drop of {index_net_pts:+.1f} pts is uniformly distributed between "
            f"heavyweights ({hw_cum_pts:+.1f} pts, {hw_share:.1f}% share) and the broader basket ({broader_pts:+.1f} pts). "
            f"No major heavyweight pillar is providing institutional floor defense."
        )
    else:
        footprint_class = "Rotational Range Equilibrium"
        footprint_tone = "neutral"
        footprint_desc = (
            f"Index spot ({spot:,.1f}) is locked in tight rotational containment ({index_net_pts:+.1f} pts). "
            f"Heavyweight propulsion ({hw_cum_pts:+.1f} pts) is almost exactly neutralized by broader market counter-flows ({broader_pts:+.1f} pts)."
        )

    # Pillar 2: Titan Clash & Sector Rotation
    if is_sector_clash:
        clash_title = f"Intra-Sector Clash: {driver_sector} Duel"
        clash_desc = (
            f"Epic intra-sector standoff: <b>{driver_sym}</b> ({driver_pts:+.1f} pts) is locked in direct opposition with "
            f"its own sector peer <b>{dragger_sym}</b> ({dragger_pts:+.1f} pts) within {driver_sector}. "
            f"This internal friction indicates institutional pairs rotation rather than an unconstrained sector rally. "
            f"The net sector propulsion is muted at {driver_pts + dragger_pts:+.1f} pts, acting as a mechanical speed governor on spot."
        )
    else:
        clash_title = f"Cross-Sector Divergence: {driver_sym} vs. {dragger_sym}"
        clash_desc = (
            f"Cross-sector divergence is active: <b>{driver_sym}</b> ({driver_pts:+.1f} pts, {driver_sector}) leads index propulsion, "
            f"while <b>{dragger_sym}</b> ({dragger_pts:+.1f} pts, {dragger_sector}) acts as the primary anchor drag. "
            f"Capital is actively rotating out of {dragger_sector} and funding inflows into {driver_sector}."
        )

    # Pillar 3: Scan Delta Velocity & Momentum Shift
    if hw_scan_delta > 10.0:
        velocity_status = "Aggressive Institutional Acceleration"
        velocity_tone = "bullish"
        velocity_desc = (
            f"Momentum impulse is actively accelerating: the latest 15-minute scan injected <b>{hw_scan_delta:+.2f} pts</b> of fresh heavyweight impulse. "
            f"Pillars are pushing aggressively in unison. Short-term intraday momentum strongly favors continuation toward upper resistance."
        )
    elif hw_scan_delta < -10.0:
        velocity_status = "Sharp Intraday Reversal / Profit Taking"
        velocity_tone = "bearish"
        velocity_desc = (
            f"Abrupt momentum deceleration: latest 15-minute scan registered a sharp <b>{hw_scan_delta:+.2f} pts</b> negative impulse from heavyweights. "
            f"Pillars are encountering aggressive profit-taking. High probability of an afternoon mean-reversion pull toward Max Pain."
        )
    elif hw_scan_delta < 0.0 and driver_shift < -5.0:
        velocity_status = "Session Fatigue & Leader Deceleration"
        velocity_tone = "neutral"
        velocity_desc = (
            f"Intraday exhaustion alert: latest scan delta ticked <b>{hw_scan_delta:+.2f} pts</b>, while primary driver {driver_sym} "
            f"has surrendered {driver_shift:+.1f} pts over the last 5 scans. The morning buying climax is fading; market is entering afternoon theta compression."
        )
    else:
        velocity_status = "Controlled Scan Impulse"
        velocity_tone = "neutral"
        velocity_desc = (
            f"Scan delta is ticking at a controlled <b>{hw_scan_delta:+.2f} pts</b>. Heavyweights are holding recent intraday gains without runaway acceleration or aggressive dumping."
        )

    # Pillar 4: Desk Execution Mandate (₹1 Cr Portfolio Playbook)
    res_str = f"{opt_ctx['resistance']:,.0f}" if opt_ctx['resistance'] > 0 else f"{round((spot + 200)/50)*50:,.0f}"
    sup_str = f"{opt_ctx['support']:,.0f}" if opt_ctx['support'] > 0 else f"{round((spot - 200)/50)*50:,.0f}"

    if footprint_tone == "bullish" and hw_scan_delta >= -5.0:
        mandate_action = "Bull Put Spread / Put Credit Tranche Deployment"
        mandate_desc = (
            f"<b>Option Sellers:</b> Market structure validates <b>Bull Put Spreads</b> or Jade Lizards. "
            f"Anchor short put strikes at or below Put Base ({sup_str}). Collect optimal theta with defined wings. "
            f"<b>Directional Traders:</b> Avoid chasing expensive OTM calls near {res_str} Call Wall; "
            f"intra-sector friction ({dragger_sym}) caps parabolic breakout potential. Trade pullbacks toward {opt_ctx['atm_strike']:,.0f}."
        )
        invalidation = f"Invalidation: Spot breakdown below {sup_str} or {driver_sym} flipping to negative scan delta."
    elif footprint_tone == "bearish" or footprint_class.startswith("Synthetic"):
        mandate_action = "Bear Call Spreads / Range-Bound Iron Condor (Fade Rallies)"
        mandate_desc = (
            f"<b>Option Sellers:</b> Deploy <b>Bear Call Spreads</b> or asymmetric Iron Condors with wider Put clearance. "
            f"Sell Call strikes defending {res_str} Wall. The heavyweight propping structure makes sharp false breakouts ideal shorting opportunities. "
            f"<b>Directional Traders:</b> Do NOT buy dips in mid-caps while broader breadth is bleeding."
        )
        invalidation = f"Invalidation: Sustained spot breakout above {res_str} with {dragger_sym} short-covering."
    else:
        mandate_action = "Delta-Neutral Iron Condor / Strangle Harvesting"
        mandate_desc = (
            f"<b>Option Sellers:</b> Ideal environment for <b>Delta-Neutral Iron Condors</b> (±0.25 delta wings). "
            f"With {driver_sym} and {dragger_sym} neutralizing each other, realized volatility is suppressed. "
            f"Harvest pure theta decay between {sup_str} Base and {res_str} Cap. "
            f"<b>Directional Traders:</b> Range-bound chopping zone; avoid directional futures exposure."
        )
        invalidation = f"Invalidation: Heavyweight scan delta exceeding ±25 pts or breach of {sup_str}/{res_str} boundaries."

    # Conviction Calculation
    base_conv = 80
    if abs(hw_share - 50) > 15:
        base_conv += 5
    if abs(index_net_pts) > 100:
        base_conv += 4
    if opt_ctx.get("confidence", 50) >= 60:
        base_conv += 3
    conviction_score = min(94, max(74, base_conv))

    headline = f"{idx_key} PRO TRADER DOSSIER: {footprint_class.upper()} WITH {driver_sym} PULLING VS {dragger_sym} DRAGGING"

    result = {
        "status": "ok",
        "index_name": idx_key,
        "spot": spot,
        "index_net_pts": round(index_net_pts, 2),
        "hw_cum_pts": round(hw_cum_pts, 2),
        "broader_pts": round(broader_pts, 2),
        "hw_share_pct": round(hw_share, 1),
        "latest_scan_time": latest_scan_time,
        "regime": regime,
        "headline": headline,
        "conviction_score": conviction_score,
        "conviction_badge": "HIGH CONVICTION" if conviction_score >= 85 else "MODERATE CONVICTION",
        "pillars": {
            "driver": {"symbol": driver_sym, "impact": driver_pts, "sector": driver_sector, "shift": driver_shift},
            "dragger": {"symbol": dragger_sym, "impact": dragger_pts, "sector": dragger_sector, "shift": dragger_shift},
            "is_sector_clash": is_sector_clash,
        },
        "footprint": {
            "classification": footprint_class,
            "tone": footprint_tone,
            "analysis": footprint_desc,
        },
        "conflict": {
            "title": clash_title,
            "analysis": clash_desc,
        },
        "velocity": {
            "status": velocity_status,
            "scan_delta": round(hw_scan_delta, 2),
            "tone": velocity_tone,
            "analysis": velocity_desc,
        },
        "playbook": {
            "mandate_action": mandate_action,
            "analysis": mandate_desc,
            "invalidation": invalidation,
            "put_base": sup_str,
            "call_wall": res_str,
            "max_pain": f"{opt_ctx['max_pain']:,.0f}",
        },
        "key_stats": {
            "hw_share": f"{hw_share:.1f}%",
            "driver": f"{driver_sym} ({driver_pts:+.1f} pts)",
            "dragger": f"{dragger_sym} ({dragger_pts:+.1f} pts)",
            "breadth_divergence": f"{divergence_pts:+.1f} pts",
            "latest_impulse": f"{hw_scan_delta:+.2f} pts Δ",
        },
        "llm_enriched": False,
        "generated_at": datetime.now(IST).strftime("%d %b %Y, %H:%M:%S IST"),
    }

    # 6. Optional LLM Polish
    if enable_llm:
        try:
            from src.engine.llm_enrichment import call_llm_chain
            prompt = (
                f"You are a Chief Investment Officer and veteran institutional derivatives trader (15+ years experience, ₹1 Cr portfolio). "
                f"Analyze this quantitative constituent telemetry for {idx_key}:\n"
                f"- Spot Price: {spot:,.1f} ({index_net_pts:+.1f} pts)\n"
                f"- Top 10 Heavyweights: {hw_cum_pts:+.1f} pts ({hw_share:.1f}% share)\n"
                f"- Broader Index Constituents: {broader_pts:+.1f} pts\n"
                f"- Primary Driver: {driver_sym} ({driver_pts:+.1f} pts, {driver_sector})\n"
                f"- Primary Anchor: {dragger_sym} ({dragger_pts:+.1f} pts, {dragger_sector})\n"
                f"- Latest 15m Scan Impulse: {hw_scan_delta:+.2f} pts\n"
                f"- Derivatives Context: ATM {opt_ctx['atm_strike']:,.0f}, Max Pain {opt_ctx['max_pain']:,.0f}, PCR {opt_ctx['pcr']:.2f}, Support {sup_str}, Resistance {res_str}\n\n"
                f"Provide a concise, razor-sharp 2-sentence institutional synthesis on institutional mechanics and the highest-probability option structure."
            )
            llm_resp = call_llm_chain(
                prompt=prompt,
                schema=None,
                symbol=idx_key,
                system_prompt="You are a veteran institutional prop desk head. Be ultra-dense, direct, and actionable. Zero retail fluff.",
                max_tokens=300
            )
            if llm_resp and isinstance(llm_resp, str) and len(llm_resp) > 30:
                result["llm_synthesis"] = llm_resp.strip()
                result["llm_enriched"] = True
        except Exception as e:
            log.warning("LLM enrichment skipped for %s: %s", idx_key, e)

    _AI_SUMMARY_CACHE[cache_key] = result
    return result
