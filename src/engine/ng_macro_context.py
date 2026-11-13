"""
Natural Gas Macro Context & Inter-Report Intelligence Module.

Maintains persistent, seasonally aware fundamental intelligence derived from
US EIA Natural Gas Storage Reports, 5-year historical averages, prompt-month
expiration cycles, and physical supply-demand dynamics.

Persists stance between weekly Thursday releases (20:00 IST) to calibrate
all Natural Gas trading decisions across Parity, Momentum, and LLM enrichment.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any
import pytz

from src.models.schema import get_conn

log = logging.getLogger("nsebot.ng_macro_context")
_IST = pytz.timezone("Asia/Kolkata")

# 52-week Lower 48 historical 5-year average weekly change benchmarks (approximate Bcf).
# Positive = Injection (Build); Negative = Withdrawal (Draw).
# Used as fallback if historical database row is unpopulated.
_TYPICAL_5YR_WEEKLY_CHANGE = {
    # Winter Withdrawal (Weeks 1 - 13: Jan - Mar)
    1: -160.0, 2: -180.0, 3: -200.0, 4: -190.0, 5: -170.0,
    6: -150.0, 7: -130.0, 8: -110.0, 9: -90.0, 10: -70.0,
    11: -50.0, 12: -35.0, 13: -20.0,
    # Shoulder / Early Injection (Weeks 14 - 17: Apr)
    14: 15.0, 15: 35.0, 16: 55.0, 17: 70.0,
    # Peak Injection (Weeks 18 - 26: May - Jun)
    18: 85.0, 19: 95.0, 20: 100.0, 21: 105.0, 22: 100.0,
    23: 95.0, 24: 90.0, 25: 85.0, 26: 80.0,
    # Mid-Summer AC Power Burn (Weeks 27 - 35: Jul - Aug)
    27: 65.0, 28: 55.0, 29: 45.0, 30: 35.0, 31: 30.0,
    32: 35.0, 33: 40.0, 34: 45.0, 35: 55.0,
    # Late Summer / Early Autumn Pre-Winter Fill (Weeks 36 - 43: Sep - Oct)
    36: 65.0, 37: 75.0, 38: 76.0, 39: 80.0, 40: 85.0,
    41: 80.0, 42: 70.0, 43: 60.0,
    # Shoulder Transition to Withdrawal (Weeks 44 - 47: Nov)
    44: 35.0, 45: 10.0, 46: -15.0, 47: -35.0,
    # Early Winter Withdrawal (Weeks 48 - 52: Dec)
    48: -60.0, 49: -90.0, 50: -125.0, 51: -150.0, 52: -160.0,
}


def get_seasonal_regime(dt: datetime | None = None) -> dict[str, Any]:
    """
    Determines the US Natural Gas seasonal regime and expected storage behavior.
    - Injection Season: April 1 - October 31 (Builds are normal and expected)
    - Withdrawal Season: November 1 - March 31 (Draws are normal and expected)
    - Shoulder Months: April and October (High weather and rollover volatility)
    """
    if dt is None:
        dt = datetime.now(_IST)

    month = dt.month
    cal_week = dt.isocalendar()[1]

    is_injection = 4 <= month <= 10
    is_withdrawal = not is_injection
    is_shoulder = month in (4, 10)

    regime = "INJECTION" if is_injection else "WITHDRAWAL"
    expected_benchmark = _TYPICAL_5YR_WEEKLY_CHANGE.get(cal_week, 50.0 if is_injection else -100.0)

    if is_injection:
        rule_desc = (
            f"INJECTION SEASON (Apr-Oct): Weekly inventory additions are standard operational procedure. "
            f"An injection on its own is NOT bearish. A build below expectations or below the 5-year average "
            f"(~{expected_benchmark:+.0f} Bcf norm for Week {cal_week}) is structurally BULLISH because it erodes the pre-winter buffer."
        )
    else:
        rule_desc = (
            f"WITHDRAWAL SEASON (Nov-Mar): Weekly inventory draws are standard heating season procedure. "
            f"A draw on its own is NOT bullish. A draw smaller than expectations or below the 5-year average "
            f"(~{expected_benchmark:+.0f} Bcf norm for Week {cal_week}) is structurally BEARISH because winter demand is underperforming."
        )

    return {
        "regime": regime,
        "is_shoulder": is_shoulder,
        "calendar_week": cal_week,
        "expected_benchmark_bcf": expected_benchmark,
        "rule_description": rule_desc,
    }


def format_eia_surprise(
    actual_bcf: float | None,
    consensus_bcf: float | None,
) -> tuple[str, str, str]:
    """
    Format and classify the EIA storage surprise vs market consensus.

    Returns:
        (surprise_str, direction, label)
        - surprise_str: e.g. "-4.0 Bcf"
        - direction: "BULLISH", "BEARISH", or "NEUTRAL"
        - label: e.g. "-4.0 Bcf (Bullish Beat - Smaller Build than Expected)"

    Commodity Storage Principle:
    Surprise = Actual - Consensus
    - actual < consensus (surprise < 0):
      -> Injections: Build was smaller than expected (Under-build). Less gas in storage.
      -> Withdrawals: Draw was deeper than expected (Larger draw). More gas drawn.
      -> In ALL cases: Tighter supply-demand balance -> BULLISH BEAT.
    - actual > consensus (surprise > 0):
      -> Injections: Build was larger than expected (Over-build). More gas in storage.
      -> Withdrawals: Draw was shallower than expected (Smaller draw). Less gas drawn.
      -> In ALL cases: Looser supply-demand balance -> BEARISH MISS.
    """
    if actual_bcf is None or consensus_bcf is None:
        return "N/A", "NEUTRAL", "N/A (Pending)"

    surprise_bcf = round(actual_bcf - consensus_bcf, 1)
    is_build = actual_bcf >= 0

    if surprise_bcf <= -1.0:
        flow_desc = "Smaller Build" if is_build else "Deeper Draw"
        direction = "BULLISH"
        label = f"{surprise_bcf:+.1f} Bcf (Bullish Beat - {flow_desc} than Expected)"
    elif surprise_bcf >= 1.0:
        flow_desc = "Larger Build" if is_build else "Shallower Draw"
        direction = "BEARISH"
        label = f"{surprise_bcf:+.1f} Bcf (Bearish Miss - {flow_desc} than Expected)"
    else:
        direction = "NEUTRAL"
        label = f"{surprise_bcf:+.1f} Bcf (In-Line with Consensus)"

    return f"{surprise_bcf:+.1f} Bcf", direction, label


def classify_storage_event(
    actual_bcf: float,
    consensus_bcf: float | None,
    five_year_avg_bcf: float | None,
    surplus_vs_5yr_bcf: float | None = None,
    surplus_vs_5yr_pct: float | None = None,
    seasonal_regime: str = "INJECTION",
) -> tuple[str, str, dict[str, Any]]:
    """
    Classifies an EIA storage print using a rigorous dual-benchmark framework:
    1. Expectation Surprise: Actual vs Consensus (Immediate intraday price driver)
    2. Structural Balance: Actual vs 5-Year Historical Average (Macro physical buffer)

    Returns: (stance, summary, metrics_dict)
    Supported Stances:
      - BULLISH_TIGHTENING: Both 5-Yr and Consensus confirm physical deficit / tightening
      - BEARISH_LOOSENING: Both 5-Yr and Consensus confirm physical surplus / inventory glut
      - DIVERGENT_TACTICAL_RELIEF: Consensus beat (under-build / deeper draw) amidst broader 5-yr surplus
      - DIVERGENT_MACRO_TIGHTENING: Consensus miss (over-build / shallower draw) amidst broader 5-yr deficit
      - NEUTRAL_BALANCED: In-line print within normal noise thresholds
    """
    consensus_delta = (actual_bcf - consensus_bcf) if consensus_bcf is not None else 0.0
    five_yr_delta = (actual_bcf - five_year_avg_bcf) if five_year_avg_bcf is not None else 0.0

    is_build = actual_bcf >= 0
    flow_word = "build" if is_build else "draw"

    stance = "NEUTRAL_BALANCED"
    summary_parts = []

    # Consensus comparison (Expectations)
    # consensus_delta < 0 is ALWAYS a Bullish Beat (under-build or deeper draw)
    # consensus_delta > 0 is ALWAYS a Bearish Miss (over-build or shallower draw)
    is_consensus_beat = consensus_delta <= -2.0
    is_consensus_miss = consensus_delta >= 2.0

    # 5-Year comparison (Physical benchmark)
    # five_yr_delta < 0 is tightening (build < 5yr avg or draw > 5yr avg)
    # five_yr_delta > 0 is loosening (build > 5yr avg or draw < 5yr avg)
    is_5yr_tightening = five_yr_delta <= -8.0
    is_5yr_loosening = five_yr_delta >= 8.0

    if is_5yr_tightening and is_consensus_beat:
        stance = "BULLISH_TIGHTENING"
        if is_build:
            summary_parts.append(
                f"Dual Bullish Alignment: Storage build of {actual_bcf:+.1f} Bcf is {abs(five_yr_delta):.1f} Bcf below 5-yr norm "
                f"({five_year_avg_bcf:+.1f} Bcf) and beat consensus by {abs(consensus_delta):.1f} Bcf ({consensus_bcf:+.1f} Bcf est). "
                f"High-conviction inventory erosion tightening physical balances."
            )
        else:
            summary_parts.append(
                f"Dual Bullish Alignment: Storage draw of {actual_bcf:+.1f} Bcf was {abs(five_yr_delta):.1f} Bcf deeper than 5-yr norm "
                f"({five_year_avg_bcf:+.1f} Bcf) and beat consensus by {abs(consensus_delta):.1f} Bcf ({consensus_bcf:+.1f} Bcf est). "
                f"Intense heating demand depleting inventories."
            )
    elif is_5yr_loosening and is_consensus_miss:
        stance = "BEARISH_LOOSENING"
        if is_build:
            summary_parts.append(
                f"Dual Bearish Alignment: Storage build of {actual_bcf:+.1f} Bcf exceeded 5-yr norm ({five_year_avg_bcf:+.1f} Bcf) "
                f"by +{five_yr_delta:.1f} Bcf and exceeded consensus by +{consensus_delta:.1f} Bcf. Strong physical supply expanding storage buffer."
            )
        else:
            summary_parts.append(
                f"Dual Bearish Alignment: Storage draw of {actual_bcf:+.1f} Bcf was {five_yr_delta:+.1f} Bcf shallower than 5-yr norm "
                f"({five_year_avg_bcf:+.1f} Bcf) and missed consensus by +{consensus_delta:.1f} Bcf. Weak heating demand leaving bloated inventories."
            )
    elif is_5yr_loosening and is_consensus_beat:
        stance = "DIVERGENT_TACTICAL_RELIEF"
        if is_build:
            summary_parts.append(
                f"Tactical Relief into Macro Surplus: Build of {actual_bcf:+.1f} Bcf beat consensus ({consensus_bcf:+.1f} Bcf) "
                f"by {abs(consensus_delta):.1f} Bcf (under-build) triggering intraday short-covering bids, but remains +{five_yr_delta:.1f} Bcf above the 5-yr average "
                f"({five_year_avg_bcf:+.1f} Bcf), preserving the structural multi-week surplus."
            )
        else:
            summary_parts.append(
                f"Tactical Relief into Macro Surplus: Draw of {actual_bcf:+.1f} Bcf beat consensus ({consensus_bcf:+.1f} Bcf) "
                f"by {abs(consensus_delta):.1f} Bcf (deeper draw) sparking short-covering bids, but remains shallower than 5-yr norm by +{five_yr_delta:.1f} Bcf, "
                f"preserving the broader surplus."
            )
    elif is_5yr_tightening and is_consensus_miss:
        stance = "DIVERGENT_MACRO_TIGHTENING"
        if is_build:
            summary_parts.append(
                f"Intraday Pullback into Structural Tightness: Build of {actual_bcf:+.1f} Bcf missed consensus by +{consensus_delta:.1f} Bcf "
                f"sparking headline selling, but physical injection remains {abs(five_yr_delta):.1f} Bcf below 5-yr average, maintaining macro support."
            )
        else:
            summary_parts.append(
                f"Intraday Pullback into Structural Tightness: Draw of {actual_bcf:+.1f} Bcf missed consensus by +{consensus_delta:.1f} Bcf "
                f"sparking headline selling, but draw remains {abs(five_yr_delta):.1f} Bcf deeper than 5-yr norm, maintaining macro deficit."
            )
    elif abs(five_yr_delta) >= 14.0:
        stance = "BEARISH_LOOSENING" if five_yr_delta > 0 else "BULLISH_TIGHTENING"
        summary_parts.append(f"Storage {flow_word} of {actual_bcf:+.1f} Bcf dominated by massive 5-year divergence ({five_yr_delta:+.1f} Bcf).")
    else:
        stance = "NEUTRAL_BALANCED"
        summary_parts.append(f"Storage {flow_word} of {actual_bcf:+.1f} Bcf is roughly in-line with seasonal norms ({five_year_avg_bcf:+.1f} Bcf).")

    if surplus_vs_5yr_pct is not None and surplus_vs_5yr_bcf is not None:
        summary_parts.append(
            f"National working gas sits at {surplus_vs_5yr_pct:+.1f}% vs the 5-year historical average ({surplus_vs_5yr_bcf:+.0f} Bcf surplus/deficit)."
        )

    summary = " ".join(summary_parts)
    metrics = {
        "actual_bcf": actual_bcf,
        "consensus_bcf": consensus_bcf,
        "five_year_avg_bcf": five_year_avg_bcf,
        "consensus_delta_bcf": consensus_delta,
        "five_year_delta_bcf": five_yr_delta,
        "surplus_vs_5yr_bcf": surplus_vs_5yr_bcf,
        "surplus_vs_5yr_pct": surplus_vs_5yr_pct,
        "seasonal_regime": seasonal_regime,
        "macro_stance": stance,
    }
    return stance, summary, metrics


def deduce_institutional_intent(
    spot_change_pct: float = 0.0,
    ce_oi_change: int = 0,
    pe_oi_change: int = 0,
    pcr: float = 1.0,
    macro_stance: str = "NEUTRAL_BALANCED",
    consensus_delta: float = 0.0,
) -> dict[str, Any]:
    """
    Deduce what institutional desks and market makers are doing by cross-referencing:
    Price Direction x Strike-Level OI Flow x EIA Macro Regime.
    """
    tot_oi_chg = ce_oi_change + pe_oi_change

    if spot_change_pct >= 0.4:
        # Price is rising
        if ce_oi_change < -500 or (ce_oi_change < 0 and pe_oi_change <= 0):
            return {
                "intent_code": "SHORT_COVERING_SQUEEZE",
                "label": "Short-Covering Squeeze (Weak-Hand Panic)",
                "rationale": "Trapped short sellers are covering into the pop; net open interest is draining rather than accumulating.",
                "trading_implication": "Fragile fuel; do not chase longs. Watch overhead resistance for exhaustion / short re-entry.",
            }
        elif pe_oi_change > 1000 and pe_oi_change >= ce_oi_change * 1.5:
            return {
                "intent_code": "AGGRESSIVE_PUT_FLOOR",
                "label": "Institutional Put Floor Defense (Long Buildup)",
                "rationale": "Smart money is actively writing aggressive put premium, anchoring a new price floor.",
                "trading_implication": "High-probability dip buying; bullish put spreads favored above support.",
            }
        elif ce_oi_change > 1200 and ce_oi_change >= pe_oi_change * 1.5:
            return {
                "intent_code": "CALL_RESISTANCE_FADE",
                "label": "Institutional Call Fade (Selling Strength)",
                "rationale": "Desks are selling massive call premium into retail buying candles at key resistance strikes.",
                "trading_implication": "Upside is heavily capped by call walls; bear call spreads favored at resistance.",
            }
        else:
            return {
                "intent_code": "MODERATE_LONG_INFLOW",
                "label": "Moderate Long Accumulation",
                "rationale": "Steady spot uptick supported by bilateral participation.",
                "trading_implication": "Trend following with tight trailing stops.",
            }

    elif spot_change_pct <= -0.4:
        # Price is falling
        if pe_oi_change < -500 or (pe_oi_change < 0 and ce_oi_change <= 0):
            return {
                "intent_code": "LONG_LIQUIDATION",
                "label": "Long Liquidation (Stale Bulls Exiting)",
                "rationale": "Long positions are squaring off without aggressive fresh short commitment.",
                "trading_implication": "Support may hold once long liquidation subsides.",
            }
        elif ce_oi_change > 1000 and ce_oi_change >= pe_oi_change * 1.5:
            return {
                "intent_code": "HEAVY_SHORT_BUILDUP",
                "label": "Aggressive Institutional Short Buildup",
                "rationale": "Heavy call writing pushing price down; institutional supply hammering bids.",
                "trading_implication": "Strong bearish trend pressure; bear call spreads or put debit spreads favored.",
            }
        else:
            return {
                "intent_code": "MODERATE_SHORT_PRESSURE",
                "label": "Moderate Short Pressure",
                "rationale": "Spot drifting downward under general supply.",
                "trading_implication": "Respect the downtrend; avoid fighting downside momentum.",
            }
    else:
        # Rangebound / pin
        if tot_oi_chg > 2000 and abs(ce_oi_change - pe_oi_change) < 1000:
            return {
                "intent_code": "CONDOR_WRITING_PIN",
                "label": "Institutional Condor / Strangle Pinning",
                "rationale": "Desks are writing both CE and PE strikes simultaneously, locking spot around Max Pain.",
                "trading_implication": "Ideal rangebound volatility selling; Iron Condors favored.",
            }
        return {
            "intent_code": "NEUTRAL_STANDOFF",
            "label": "Institutional Standoff (Low Conviction)",
            "rationale": "Equilibrium between buyers and sellers with no definitive institutional commitment.",
            "trading_implication": "Stand aside or trade well-defined boundaries.",
        }


def get_active_ng_macro_context() -> dict[str, Any]:
    """
    Returns the active Natural Gas macro context persisting between weekly releases.
    Includes:
      - Latest EIA print and historical benchmarks
      - Active macro stance (BULLISH_TIGHTENING / BEARISH_LOOSENING / NEUTRAL_BALANCED)
      - Consecutive below/above normal week trend
      - Prompt-month rollover / expiry squeeze indicators
      - Technical Fibonacci & key moving average anchors
      - Validity timestamp (ends next Thursday 20:00 IST)
    """
    now_ist = datetime.now(_IST)
    season = get_seasonal_regime(now_ist)

    macro_data: dict[str, Any] = {
        "report_date": None,
        "actual_bcf": None,
        "consensus_bcf": None,
        "surprise_bcf": None,
        "five_year_avg_bcf": season["expected_benchmark_bcf"],
        "surplus_vs_5yr_bcf": None,
        "surplus_vs_5yr_pct": None,
        "total_storage_bcf": None,
        "year_ago_bcf": None,
        "prior_week_revised_bcf": None,
        "prior_week_revision_flag": 0,
        "seasonal_regime": season["regime"],
        "is_shoulder": season["is_shoulder"],
        "macro_stance": "NEUTRAL_BALANCED",
        "stance_summary": season["rule_description"],
        "consecutive_tightening_weeks": 0,
        "prompt_dte": 15,
        "is_rollover_squeeze_risk": False,
        "key_levels": {},
        "valid_until": None,
        "source": "cache/db",
    }

    try:
        with get_conn() as conn:
            # Query last 6 weeks of EIA releases to track trends
            rows = conn.execute(
                """
                SELECT report_date, consensus_bcf, actual_bcf, surprise_bcf,
                       five_year_avg_bcf, five_year_total_bcf, surplus_vs_5yr_bcf,
                       surplus_vs_5yr_pct, total_storage_bcf, year_ago_bcf, pct_change_yrago,
                       prior_week_revised_bcf, prior_week_revision_flag, seasonal_regime,
                       macro_stance, stance_summary, key_levels_json, valid_until
                FROM eia_consensus
                WHERE actual_bcf IS NOT NULL
                ORDER BY report_date DESC
                LIMIT 6
                """
            ).fetchall()

            if rows:
                latest = dict(rows[0])
                macro_data["report_date"] = latest.get("report_date")
                macro_data["actual_bcf"] = latest.get("actual_bcf")
                macro_data["consensus_bcf"] = latest.get("consensus_bcf")
                macro_data["surprise_bcf"] = latest.get("surprise_bcf")
                macro_data["five_year_avg_bcf"] = latest.get("five_year_avg_bcf") or season["expected_benchmark_bcf"]
                macro_data["surplus_vs_5yr_bcf"] = latest.get("surplus_vs_5yr_bcf")
                macro_data["surplus_vs_5yr_pct"] = latest.get("surplus_vs_5yr_pct")
                macro_data["total_storage_bcf"] = latest.get("total_storage_bcf")
                macro_data["year_ago_bcf"] = latest.get("year_ago_bcf")
                macro_data["prior_week_revised_bcf"] = latest.get("prior_week_revised_bcf")
                macro_data["prior_week_revision_flag"] = latest.get("prior_week_revision_flag") or 0
                macro_data["seasonal_regime"] = latest.get("seasonal_regime") or season["regime"]
                macro_data["macro_stance"] = latest.get("macro_stance") or "NEUTRAL_BALANCED"
                if latest.get("stance_summary"):
                    macro_data["stance_summary"] = latest.get("stance_summary")
                macro_data["valid_until"] = latest.get("valid_until")

                if latest.get("key_levels_json"):
                    try:
                        macro_data["key_levels"] = json.loads(latest["key_levels_json"])
                    except Exception:
                        pass

                # Calculate consecutive below-normal or above-normal weeks
                consecutive_below = 0
                for r in rows:
                    act = r["actual_bcf"]
                    f_avg = r["five_year_avg_bcf"] or season["expected_benchmark_bcf"]
                    if act is not None and f_avg is not None:
                        if act < f_avg:
                            consecutive_below += 1
                        else:
                            break
                macro_data["consecutive_tightening_weeks"] = consecutive_below
    except Exception as e:
        log.warning("Failed to load active NG macro context from DB: %s", e)

    # Calculate DTE to prompt MCX / NYMEX futures contract
    try:
        from config.symbol_classes import get_futures_expiry
        exp_str = get_futures_expiry("NATURALGAS", now_ist.date())
        if exp_str:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - now_ist.date()).days
            macro_data["prompt_dte"] = dte
            # In natural gas, front month contract expiration triggers major liquidity rollover and short squeezes
            macro_data["is_rollover_squeeze_risk"] = dte <= 5
    except Exception:
        pass

    return macro_data


def format_ng_macro_for_llm(context: dict[str, Any] | None = None) -> str:
    """
    Formats the Natural Gas macro dossier for injection into LLM prompts.
    Provides the LLM with deep fundamental awareness of EIA dynamics,
    seasonal norms, and key technical boundaries.
    """
    if context is None:
        context = get_active_ng_macro_context()

    rep_date = context.get("report_date") or "Latest Release"
    actual = context.get("actual_bcf")
    consensus = context.get("consensus_bcf")
    five_yr = context.get("five_year_avg_bcf")
    surplus_bcf = context.get("surplus_vs_5yr_bcf")
    surplus_pct = context.get("surplus_vs_5yr_pct")
    stance = context.get("macro_stance", "NEUTRAL_BALANCED")
    summary = context.get("stance_summary", "")
    consec = context.get("consecutive_tightening_weeks", 0)
    dte = context.get("prompt_dte", 15)
    squeeze_risk = context.get("is_rollover_squeeze_risk", False)
    season_reg = context.get("seasonal_regime", "INJECTION")

    lines = [
        "NATURAL GAS FUNDAMENTAL & EIA MACRO INTELLIGENCE:",
        f"  • Active Weekly Stance: **{stance}** (Persisting until next Thursday EIA release)",
        f"  • Seasonal Regime: {season_reg} SEASON — Builds are expected & normal; below-average builds are bullish.",
    ]

    if actual is not None:
        act_str = f"{actual:+.1f} Bcf"
        cons_str = f"{consensus:+.1f} Bcf" if consensus is not None else "N/A"
        fyr_str = f"{five_yr:+.1f} Bcf" if five_yr is not None else "N/A"
        lines.append(f"  • EIA Storage Print ({rep_date}): Actual {act_str} | Consensus {cons_str} | 5-Yr Avg {fyr_str}")
        if consensus is not None:
            c_delta = actual - consensus
            c_label = "BULLISH BEAT (Under-build / Demand Surge)" if c_delta < 0 else "BEARISH MISS (Over-build)"
            lines.append(f"  ? Consensus Surprise: {c_delta:+.1f} Bcf ({c_label})")
        if five_yr is not None:
            f_delta = actual - five_yr
            f_label = "STRUCTURAL TIGHTENING / Buffer Erosion" if f_delta < 0 else "SURPLUS EXPANSION"
            lines.append(f"  ? 5-Year Norm Delta: {f_delta:+.1f} Bcf ({f_label})")

    if surplus_bcf is not None and surplus_pct is not None:
        lines.append(f"  • National Storage Buffer: {surplus_bcf:+.0f} Bcf ({surplus_pct:+.1f}% vs 5-year average)")

    if consec >= 2:
        lines.append(f"  • Structural Trend: {consec} consecutive below-normal builds (persistent national surplus erosion)")

    lines.append(f"  • Contract Rollover: {dte} DTE on prompt contract (Expiry Squeeze Risk: {'HIGH - Front-month short covering underway' if squeeze_risk else 'NORMAL'})")

    if summary:
        lines.append(f"  • Macro Perspective: {summary}")

    lines.append(
        "  • Inter-Report Execution Guidance: Do NOT treat raw storage increases as bearish during injection season. "
        "Align intraday momentum with the active weekly stance; avoid fighting structural tightening rallies."
    )

    return "\n".join(lines)
