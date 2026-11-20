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
    from src.engine.index_contributors import (
        get_heavyweights_history,
        get_heavyweights_timeline,
        get_index_contributors,
    )

    valid_indices = ("NIFTY", "BANKNIFTY", "SENSEX")
    idx_key = index_name.upper() if index_name.upper() in valid_indices else "NIFTY"

    # 1. Fetch Timeline, History, and Full Constituent Catalog
    tl_data = get_heavyweights_timeline(idx_key, day_filter="today", force_refresh=force_refresh)
    hw_history = get_heavyweights_history(limit=5)
    hw_index_data = hw_history.get(idx_key, {})
    contrib_data = get_index_contributors(idx_key, force_refresh=force_refresh)

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

    # Identify Primary Driver & Primary Anchor Drag
    top_driver_dict = tl_data.get("top_driver") or {}
    driver_sym = top_driver_dict.get("symbol", "N/A")
    driver_pts = float(top_driver_dict.get("impact", 0.0))

    # Heavyweight top dragger (within top 10 heavyweights)
    hw_dragger_dict = tl_data.get("top_dragger") or {}
    hw_dragger_sym = hw_dragger_dict.get("symbol", "N/A")
    hw_dragger_pts = float(hw_dragger_dict.get("impact", 0.0))

    # True Index-Wide top dragger from full constituent catalog (e.g. TITAN)
    losers = contrib_data.get("losers", [])
    if losers:
        index_top_dragger = losers[0]
        true_dragger_sym = index_top_dragger.get("symbol", hw_dragger_sym)
        true_dragger_pts = float(index_top_dragger.get("point_impact", hw_dragger_pts))
        true_dragger_sector = index_top_dragger.get("sector", "Equities")
    else:
        true_dragger_sym = hw_dragger_sym
        true_dragger_pts = hw_dragger_pts
        true_dragger_sector = "Equities"

    # Calculate broader constituent contribution (remaining stocks in index)
    broader_pts = index_net_pts - hw_cum_pts

    # 3. Extract Heavyweights details from history
    hw_list = hw_index_data.get("heavyweights", [])
    driver_info = next((h for h in hw_list if h.get("symbol") == driver_sym), {})
    dragger_info = next((h for h in hw_list if h.get("symbol") == true_dragger_sym), {})

    driver_sector = driver_info.get("sector") or "Leading Constituent"
    dragger_sector = dragger_info.get("sector") or true_dragger_sector or "Lagging Constituent"
    driver_shift = float(driver_info.get("shift_pts") or 0.0)
    dragger_shift = float(dragger_info.get("shift_pts") or 0.0)

    # 5-Scan net point shift across heavyweights
    hw_cum_shift = round(sum(float(h.get("shift_pts", 0.0)) for h in hw_list), 2)

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
            f"its own sector peer <b>{true_dragger_sym}</b> ({true_dragger_pts:+.1f} pts) within {driver_sector}. "
            f"This internal friction indicates institutional pairs rotation rather than an unconstrained sector rally. "
            f"The net sector propulsion is muted at {driver_pts + true_dragger_pts:+.1f} pts, acting as a mechanical speed governor on spot."
        )
    else:
        extra_drag_note = f" (within top 10 heavyweights, <b>{hw_dragger_sym}</b> drags {hw_dragger_pts:+.1f} pts)" if hw_dragger_sym != true_dragger_sym else ""
        clash_title = f"Cross-Sector Divergence: {driver_sym} vs. {true_dragger_sym}"
        clash_desc = (
            f"Cross-sector divergence is active: <b>{driver_sym}</b> ({driver_pts:+.1f} pts, {driver_sector}) leads index propulsion, "
            f"while <b>{true_dragger_sym}</b> ({true_dragger_pts:+.1f} pts, {true_dragger_sector}) acts as the primary anchor drag{extra_drag_note}. "
            f"Capital is actively rotating out of {true_dragger_sector} and funding inflows into {driver_sector}."
        )

    # Pillar 3: Scan Delta Velocity & Momentum Shift
    if hw_scan_delta > 10.0:
        if index_net_pts < -50.0 or footprint_tone == "bearish":
            velocity_status = "Counter-Trend Bounce / 15m Short-Covering"
            velocity_tone = "neutral"
            velocity_desc = (
                f"Short-term counter-trend pulse: the latest 15-minute scan registered a <b>{hw_scan_delta:+.2f} pts</b> relief bounce from heavyweights. "
                f"However, this is pushing against a primary liquidation day ({index_net_pts:+.1f} pts net). "
                f"Treat this as intraday short-covering into overhead resistance rather than sustainable bullish breakout."
            )
        else:
            velocity_status = "Aggressive Institutional Acceleration"
            velocity_tone = "bullish"
            velocity_desc = (
                f"Momentum impulse is actively accelerating: the latest 15-minute scan injected <b>{hw_scan_delta:+.2f} pts</b> of fresh heavyweight impulse. "
                f"Pillars are pushing aggressively in unison. Short-term intraday momentum strongly favors continuation toward upper resistance."
            )
    elif hw_scan_delta < -10.0:
        if index_net_pts > 50.0 or footprint_tone == "bullish":
            velocity_status = "Intraday Pullback / 15m Mean Reversion"
            velocity_tone = "neutral"
            velocity_desc = (
                f"Temporary intraday pullback: latest 15-minute scan registered a <b>{hw_scan_delta:+.2f} pts</b> negative impulse from heavyweights. "
                f"With the primary regime bullish ({index_net_pts:+.1f} pts), this reflects profit-taking and mean-reversion toward Put Base."
            )
        else:
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
    res_val = float(opt_ctx['resistance']) if opt_ctx['resistance'] > 0 else (round((spot + 200)/50)*50 if idx_key == "NIFTY" else round((spot + 500)/100)*100)
    sup_val = float(opt_ctx['support']) if opt_ctx['support'] > 0 else (round((spot - 200)/50)*50 if idx_key == "NIFTY" else round((spot - 500)/100)*100)
    res_str = f"{res_val:,.0f}"
    sup_str = f"{sup_val:,.0f}"

    # Invalidation Check against quantitative thresholds
    impulse_invalidated = abs(hw_scan_delta) > 25.0
    spot_breached_lower = (spot < sup_val) if sup_val > 0 else False
    spot_breached_upper = (spot > res_val) if res_val > 0 else False
    is_invalidated = impulse_invalidated or spot_breached_lower or spot_breached_upper

    # Magnitude ratio check (is this a balanced tug-of-war or one-sided dominance?)
    driver_mag = abs(driver_pts)
    dragger_mag = abs(true_dragger_pts)
    mag_ratio = round(driver_mag / max(0.1, dragger_mag), 1) if dragger_mag > 0 else 99.0

    # True neutralizing conditions:
    # 1. Balanced magnitude (< 2.5:1 ratio, not 14:1)
    # 2. Contained net index move (< 150 pts or < 0.35%)
    # 3. Controlled 15m scan delta (|hw_scan_delta| <= 15.0 pts)
    is_true_neutralizing = (
        mag_ratio < 2.5
        and abs(index_net_pts) < (150.0 if idx_key != "BANKNIFTY" else 250.0)
        and abs(hw_scan_delta) <= 15.0
    )

    if is_invalidated:
        mandate_action = "THESIS INVALIDATED — STAND DOWN"
        mandate_tone = "invalidated"
        invalidation_detail = (
            f"15m scan delta impulse ({hw_scan_delta:+.2f} pts) exceeded ±25 pts threshold by {abs(hw_scan_delta)/25.0:.1f}×"
            if impulse_invalidated else
            f"Spot price ({spot:,.1f}) breached boundary ({sup_str} - {res_str})"
        )
        mandate_desc = (
            f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--red);'>Option Sellers:</span> "
            f"<b>STAND DOWN — THESIS INVALIDATED.</b> Heavyweight scan delta impulse of <b>{hw_scan_delta:+.2f} pts</b> breaches the ±25 pt volatility barrier by {abs(hw_scan_delta)/25.0:.1f}×. "
            f"Do NOT deploy delta-neutral Iron Condors or Strangles into an active heavyweight selloff or rapid acceleration. Severe gamma expansion risk active.</div>"
            f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--yellow);'>Directional Traders:</span> "
            f"Stand down. Await 15m scan impulse mean-reversion and spot stabilization near Put Base ({sup_str}). Avoid catching falling knives during active heavyweight liquidation.</div>"
        )
        invalidation = f"ACTIVE TRIP: {invalidation_detail}. Mandate frozen until scan delta returns within ±25 pts."
        conviction_score = 42
        conviction_badge = "THESIS INVALIDATED"
    elif is_true_neutralizing:
        mandate_action = "Delta-Neutral Iron Condor / Strangle Harvesting"
        mandate_tone = "neutral"
        mandate_desc = (
            f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--accent);'>Option Sellers:</span> "
            f"Balanced institutional tug-of-war validates <b>Delta-Neutral Iron Condors</b> (±0.25 delta wings). "
            f"With {driver_sym} (+{driver_pts:.1f}) and {true_dragger_sym} ({true_dragger_pts:.1f}) in close opposition ({mag_ratio:.1f}:1 ratio), realized volatility is suppressed. "
            f"Harvest pure theta decay between {sup_str} Base and {res_str} Cap.</div>"
            f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--text-dim);'>Directional Traders:</span> "
            f"Range-bound chopping zone; avoid directional futures exposure.</div>"
        )
        invalidation = f"Heavyweight scan delta exceeding ±25 pts or breach of {sup_str}/{res_str} boundaries."
        conviction_score = 85
        conviction_badge = "HIGH CONVICTION"
    elif footprint_tone == "bullish" or index_net_pts > 100.0:
        if hw_scan_delta >= -10.0:
            mandate_action = "Bull Put Spread / Put Credit Tranche Deployment"
            mandate_tone = "bullish"
            mandate_desc = (
                f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--green);'>Option Sellers:</span> "
                f"Market structure validates <b>Bull Put Spreads</b> or Jade Lizards. "
                f"Anchor short put strikes at or below Put Base ({sup_str}). Collect optimal theta with defined downside wings.</div>"
                f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--accent);'>Directional Traders:</span> "
                f"Trend day propelled by {driver_sym} (+{driver_pts:.1f} pts, {mag_ratio:.1f}× over {true_dragger_sym}). "
                f"Avoid chasing expensive OTM calls near {res_str} Call Wall; buy pullbacks toward {opt_ctx['atm_strike']:,.0f}.</div>"
            )
            invalidation = f"Heavyweight scan delta exceeding ±25 pts or breakdown below {sup_str} Put Base."
            conviction_score = 88
            conviction_badge = "HIGH CONVICTION"
        else:
            mandate_action = "Intraday Pullback in Strong Trend — Wait for Put Base Re-Test"
            mandate_tone = "neutral"
            mandate_desc = (
                f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--yellow);'>Option Sellers:</span> "
                f"Pause fresh credit entries. 15m scan delta ({hw_scan_delta:+.2f} pts) reflects active profit-taking. "
                f"Wait for impulse deceleration before writing puts at {sup_str}.</div>"
                f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--accent);'>Directional Traders:</span> "
                f"Healthy mean-reversion pullback within a dominant +{index_net_pts:.1f} pt trend. Prepare for dip-buying once 15m delta ticks positive.</div>"
            )
            invalidation = f"Heavyweight scan delta exceeding ±25 pts or breakdown below {sup_str} Base."
            conviction_score = 72
            conviction_badge = "MODERATE CONVICTION"
    elif footprint_tone == "bearish" or footprint_class.startswith("Synthetic"):
        mandate_action = "Bear Call Spreads / Range-Bound Iron Condor (Fade Rallies)"
        mandate_tone = "bearish"
        mandate_desc = (
            f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--red);'>Option Sellers:</span> "
            f"Deploy <b>Bear Call Spreads</b> or asymmetric Iron Condors with wider Put clearance. "
            f"Sell Call strikes defending {res_str} Wall. The heavyweight propping structure makes sharp false breakouts ideal shorting opportunities.</div>"
            f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--yellow);'>Directional Traders:</span> "
            f"Do NOT buy dips in mid-caps while broader breadth is bleeding.</div>"
        )
        invalidation = f"Sustained spot breakout above {res_str} with {true_dragger_sym} short-covering."
        conviction_score = 86
        conviction_badge = "HIGH CONVICTION"
    else:
        mandate_action = "Capital Preservation / Tactical Rotational Scalps"
        mandate_tone = "neutral"
        mandate_desc = (
            f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--text-dim);'>Option Sellers:</span> "
            f"Rotational containment without clean directional or volatility edge. Limit sizing to small test tranches.</div>"
            f"<div class='mandate-trader-row'><span class='mandate-trader-role' style='color:var(--text-dim);'>Directional Traders:</span> "
            f"Chop market with low follow-through. Stay nimble or stand down.</div>"
        )
        invalidation = f"Heavyweight scan delta exceeding ±25 pts or breach of {sup_str}/{res_str} boundaries."
        conviction_score = 75
        conviction_badge = "MODERATE CONVICTION"

    # Trimmed, executive headline without redundant titles
    headline = f"{footprint_class.upper()} · {driver_sym} (+{driver_pts:.0f}) vs {true_dragger_sym} ({true_dragger_pts:.0f})"

    # Determine Primary Configured LLM Model
    active_model = _get_configured_llm_model_name()

    # Proprietary Prop-Desk Executive Synthesis (Deterministic baseline)
    desk_synthesis = (
        f"Institutional flow reflects {footprint_class.lower()} with {hw_share:.1f}% concentration in top heavyweights. "
        f"{'Intra-sector standoff between ' + driver_sym + ' and ' + true_dragger_sym if is_sector_clash else 'Cross-sector divergence between ' + driver_sym + ' and ' + true_dragger_sym} "
        f"acts as a mechanical speed governor on spot propulsion. Deploy {mandate_action} anchoring between Put Base ({sup_str}) and Call Wall ({res_str})."
    )

    result = {
        "status": "ok",
        "index_name": idx_key,
        "spot": spot,
        "index_net_pts": round(index_net_pts, 2),
        "hw_cum_pts": round(hw_cum_pts, 2),
        "broader_pts": round(broader_pts, 2),
        "hw_share_pct": round(hw_share, 1),
        "latest_scan_time": latest_scan_time,
        "latest_scan_iso": tl_data.get("latest_scan_iso", ""),
        "is_stale": tl_data.get("is_stale", False),
        "stale_label": tl_data.get("stale_label", ""),
        "is_invalidated": is_invalidated,
        "regime": regime,
        "headline": headline,
        "conviction_score": conviction_score,
        "conviction_badge": conviction_badge,
        "pillars": {
            "driver": {"symbol": driver_sym, "impact": driver_pts, "sector": driver_sector, "shift": driver_shift},
            "dragger": {"symbol": true_dragger_sym, "impact": true_dragger_pts, "sector": dragger_sector, "shift": dragger_shift},
            "hw_dragger": {"symbol": hw_dragger_sym, "impact": hw_dragger_pts},
            "is_sector_clash": is_sector_clash,
            "mag_ratio": mag_ratio,
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
            "mandate_tone": mandate_tone,
            "analysis": mandate_desc,
            "invalidation": invalidation,
            "is_invalidated": is_invalidated,
            "put_base": sup_str,
            "call_wall": res_str,
            "max_pain": f"{opt_ctx['max_pain']:,.0f}",
        },
        "key_stats": {
            "hw_share": f"{hw_share:.1f}%",
            "driver": f"{driver_sym} ({driver_pts:+.1f} pts)",
            "dragger": f"{true_dragger_sym} ({true_dragger_pts:+.1f} pts)",
            "shift_velocity": f"{hw_cum_shift:+.2f} pts (5-Scan)",
            "latest_impulse": f"{hw_scan_delta:+.2f} pts Δ",
        },
        "llm_model": active_model,
        "llm_synthesis": desk_synthesis,
        "llm_action": "STAND_DOWN" if is_invalidated else ("NEUTRAL" if is_true_neutralizing else "TREND"),
        "engine_agreement": True,
        "consensus_label": "DETERMINISTIC RULE CONSENSUS",
        "consensus_tone": "agreed",
        "llm_enriched": False,
        "generated_at": datetime.now(IST).strftime("%d %b %Y, %H:%M:%S IST"),
    }

    # 6. Optional Deep Generative LLM Polish
    if enable_llm:
        try:
            import re
            from src.engine.llm_enrichment import call_llm_api
            from pydantic import BaseModel, Field

            class _LLMContributorSynthesisSchema(BaseModel):
                model_name: Optional[str] = Field(default=None, description="Model used for this response")
                institutional_thesis: str = Field(description="2-sentence senior derivatives desk synthesis of constituent propulsion and market structure")
                highest_probability_trade: str = Field(description="Specific recommended option structure and strike anchors")
                key_risk_invalidation: str = Field(description="Single most critical event or level that breaks the thesis")

            prompt = (
                f"You are a Chief Investment Officer and veteran institutional derivatives trader (15+ years experience, ₹1 Cr portfolio). "
                f"Analyze this quantitative constituent telemetry for {idx_key}:\n"
                f"- Spot Price: {spot:,.1f} ({index_net_pts:+.1f} pts)\n"
                f"- Top 10 Heavyweights: {hw_cum_pts:+.1f} pts ({hw_share:.1f}% share)\n"
                f"- Broader Index Constituents: {broader_pts:+.1f} pts\n"
                f"- Primary Driver: {driver_sym} ({driver_pts:+.1f} pts, {driver_sector})\n"
                f"- Primary Anchor: {true_dragger_sym} ({true_dragger_pts:+.1f} pts, {dragger_sector})\n"
                f"- Latest 15m Scan Impulse: {hw_scan_delta:+.2f} pts\n"
                f"- Derivatives Context: ATM {opt_ctx['atm_strike']:,.0f}, Max Pain {opt_ctx['max_pain']:,.0f}, PCR {opt_ctx['pcr']:.2f}, Support {sup_str}, Resistance {res_str}\n\n"
                f"Provide a concise, razor-sharp 2-sentence institutional synthesis on institutional mechanics and the highest-probability option structure."
            )
            llm_res = call_llm_api(
                symbol=idx_key,
                prompt=prompt,
                response_schema=_LLMContributorSynthesisSchema,
                deadline=None,
                purpose="contributor_intelligence",
            )
            if llm_res and hasattr(llm_res, "institutional_thesis") and llm_res.institutional_thesis:
                raw_thesis = llm_res.institutional_thesis.strip()
                raw_trade = getattr(llm_res, "highest_probability_trade", "").strip()
                raw_full = f"{raw_thesis} {raw_trade}".strip()

                # Parse and sanitize raw action enum tokens (e.g. trailing "NO_TRADE")
                action_match = re.search(r'\b(NO[_\s]TRADE|STAND[_\s]DOWN|WAIT|AVOID|HOLD|NEUTRAL|IRON[_\s]CONDOR|BULL[_\s]PUT|BEAR[_\s]CALL)\b', raw_full, re.IGNORECASE)
                extracted_action = action_match.group(1).upper().replace(" ", "_") if action_match else "NEUTRAL"

                # Strip raw action enum from prose to eliminate ugly trailing token leaks
                cleaned_text = re.sub(r'[\s.,;:—-]+' + r'(?:NO[_\s]TRADE|STAND[_\s]DOWN|WAIT|AVOID|HOLD)\b[\s.,;:—-]*', '.', raw_full, flags=re.IGNORECASE).strip()
                cleaned_text = re.sub(r'\.{2,}', '.', cleaned_text)

                result["llm_synthesis"] = cleaned_text
                result["llm_action"] = extracted_action
                result["llm_model"] = getattr(llm_res, "model_name", None) or active_model
                result["llm_enriched"] = True

                # Evaluate Engine Agreement / Resolution Hierarchy
                rule_stance = "STAND_DOWN" if is_invalidated else ("IRON_CONDOR" if is_true_neutralizing else ("BULL_PUT" if "Bull Put" in mandate_action else "NEUTRAL"))
                llm_stance = "STAND_DOWN" if extracted_action in ("NO_TRADE", "STAND_DOWN", "WAIT", "AVOID") else extracted_action

                if rule_stance == llm_stance or (rule_stance == "STAND_DOWN" and llm_stance == "STAND_DOWN"):
                    result["engine_agreement"] = True
                    result["consensus_label"] = "VERIFIED INSTITUTIONAL REASONING: CONSENSUS STAND DOWN" if rule_stance == "STAND_DOWN" else "VERIFIED INSTITUTIONAL REASONING"
                    result["consensus_tone"] = "agreed"
                else:
                    result["engine_agreement"] = False
                    result["consensus_label"] = f"ENGINE CONFLICT: RULES ({rule_stance}) VS AI ({extracted_action})"
                    result["consensus_tone"] = "conflict"
        except Exception as e:
            log.warning("LLM enrichment failed for %s: %s", idx_key, e)

    _AI_SUMMARY_CACHE[cache_key] = result
    return result


def _get_configured_llm_model_name() -> str:
    """Return the primary active LLM model configured in the NSEBOT pipeline."""
    import os
    try:
        from src.engine.antigravity_client import get_antigravity_client
        ag = get_antigravity_client()
        if ag and ag.get_valid_access_token():
            return "Antigravity Direct (Gemini 3.8 Flash High)"
    except Exception:
        pass

    try:
        from src.engine.llm_enrichment import _is_omnirouter_online
        omni_url = os.environ.get("OMNIROUTER_BASE_URL", "http://localhost:20128/v1")
        if _is_omnirouter_online(omni_url):
            return "OmniRouter (Claude-Models Combo)"
    except Exception:
        pass

    if os.environ.get("GEMINI_API_KEY"):
        return "Gemini SDK (2.5 Flash)"
    if os.environ.get("GROQ_API_KEY"):
        return "Groq (Qwen 3.8 27B)"
    if os.environ.get("OPENCODE_API_KEY"):
        return "OpenCode Zen (Nemotron 3.5 Lightning)"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "OpenRouter (Gemini 2.0 Flash)"
    return "Antigravity Direct (Gemini 3.8 Flash High)"
