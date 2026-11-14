"""
EIA Natural Gas Storage Report Analyzer & Inter-Report Recalibrator.

Analyzes Thursday EIA reports (20:00 IST) using the unified multi-provider LLM chain
(OmniRouter -> Claude -> Groq -> Gemini) within deep seasonal, historical, and physical
supply-demand context.

Recalibrates the persistent weekly macro stance (BULLISH_TIGHTENING / BEARISH_LOOSENING / NEUTRAL_BALANCED)
which persists in the database to guide all Natural Gas trades until the next release.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytz
from pydantic import BaseModel, Field

from config.settings import GEMINI_API_KEY
from src.alerts.telegram_dispatcher import send_text
from src.models.schema import get_conn
from src.fetchers.eia_consensus_fetcher import fetch_complete_eia_dataset, store_eia_weekly_data
from src.engine.ng_macro_context import get_seasonal_regime, classify_storage_event, format_eia_surprise

log = logging.getLogger(__name__)
_IST = pytz.timezone("Asia/Kolkata")


class EIAAnalysisVerdict(BaseModel):
    sentiment: str = Field(description="BULLISH_TIGHTENING | BEARISH_LOOSENING | DIVERGENT_TACTICAL_RELIEF | DIVERGENT_MACRO_TIGHTENING | NEUTRAL_BALANCED")
    summary: str = Field(description="Summary of physical supply-demand balance and surplus trajectory")
    weekly_outlook: str = Field(description="Actionable multi-day guidance for trades until the next weekly EIA release")
    expected_impact: str = Field(description="Expected immediate and multi-session price impact on MCX/NYMEX Natural Gas")
    key_resistance: str = Field(description="Key overhead resistance ceiling & Fibonacci retracement clusters")
    key_support: str = Field(description="Key downside support floor & moving average levels")
    markdown_telegram_message: str = Field(description="Complete Telegram formatted markdown alert with emojis")


def _get_latest_naturalgas_oi() -> dict:
    """Fetch the latest OI data for NATURALGAS from the database."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT underlying, pcr, ce_oi_change, pe_oi_change 
                FROM scan_summaries 
                WHERE symbol LIKE '%NATURALGAS%' 
                ORDER BY fetched_at DESC 
                LIMIT 1
                """
            ).fetchone()
            if row:
                return dict(row)
    except Exception as e:
        log.error("Failed to fetch NATURALGAS OI data: %s", e)
    return {}


def analyze_eia_report() -> None:
    """
    Fetch EIA consensus & official actual data, compute 5-year seasonal metrics,
    query the unified LLM chain, recalibrate weekly stance, and dispatch Telegram alert.
    """
    try:
        now_ist = datetime.now(_IST)
        today_str = now_ist.strftime("%Y-%m-%d")

        # 1. Fetch complete official EIA dataset + consensus
        eia_data = fetch_complete_eia_dataset()
        if not eia_data:
            log.warning("Could not fetch EIA data; attempting DB fallback")
            eia_data = {}

        report_date = eia_data.get("report_date") or today_str
        actual = eia_data.get("actual_bcf")
        if actual is None:
            log.warning("analyze_eia_report: EIA Actual print is not yet published. Aborting recalibration until next scan tick.")
            return

        consensus = eia_data.get("consensus_bcf")
        surprise = eia_data.get("surprise_bcf")
        five_yr_avg = eia_data.get("five_year_avg_bcf")
        surplus_bcf = eia_data.get("surplus_vs_5yr_bcf")
        surplus_pct = eia_data.get("surplus_vs_5yr_pct")
        total_storage = eia_data.get("total_storage_bcf")
        year_ago_bcf = eia_data.get("year_ago_bcf")
        seasonal_regime = eia_data.get("seasonal_regime") or "INJECTION"

        season_info = get_seasonal_regime(now_ist)

        # 2. Options context
        oi_data = _get_latest_naturalgas_oi()
        underlying = oi_data.get("underlying", "N/A")
        pcr = oi_data.get("pcr", "N/A")
        ce_oi_chg = oi_data.get("ce_oi_change", "N/A")
        pe_oi_chg = oi_data.get("pe_oi_change", "N/A")

        ce_val = 0
        pe_val = 0
        try:
            ce_val = int(ce_oi_chg) if ce_oi_chg not in ("N/A", None) else 0
            pe_val = int(pe_oi_chg) if pe_oi_chg not in ("N/A", None) else 0
        except (ValueError, TypeError):
            pass

        if pe_val > 0 and ce_val < 0:
            oi_sentiment = "BULLISH (PE put writing support + CE call unwinding)"
        elif pe_val > 0 and pe_val > ce_val:
            oi_sentiment = f"BULLISH BIAS (PE put writing +{pe_val:,} dominates CE writing +{ce_val:,})"
        elif ce_val > 0 and pe_val < 0:
            oi_sentiment = "BEARISH (CE call writing resistance + PE put unwinding)"
        elif ce_val > 0 and ce_val > pe_val:
            oi_sentiment = f"BEARISH BIAS (CE call writing +{ce_val:,} dominates PE writing +{pe_val:,})"
        elif ce_val < 0 and pe_val < 0:
            oi_sentiment = "NEUTRAL (Two-sided unwinding / squaring)"
        else:
            oi_sentiment = "NEUTRAL / BALANCED"

        # 3. Trailing consecutive below/above normal weeks
        consecutive_weeks = 0
        try:
            with get_conn() as conn:
                hist_rows = conn.execute(
                    """
                    SELECT actual_bcf, five_year_avg_bcf FROM eia_consensus 
                    WHERE actual_bcf IS NOT NULL AND report_date <= ? 
                    ORDER BY report_date DESC LIMIT 6
                    """,
                    (report_date,),
                ).fetchall()
                for hr in hist_rows:
                    a = hr["actual_bcf"]
                    f = hr["five_year_avg_bcf"] or season_info["expected_benchmark_bcf"]
                    if a is not None and f is not None:
                        if a < f:
                            consecutive_weeks += 1
                        else:
                            break
        except Exception:
            pass

        # 4. Compute deterministic baseline stance, surprise labels, and physical metrics
        actual_str = f"{actual:+.1f} Bcf"
        cons_str = f"{consensus:+.1f} Bcf" if consensus is not None else "N/A"
        fyr_str = f"{five_yr_avg:+.1f} Bcf" if five_yr_avg is not None else "N/A"
        surplus_delta_str = f"{(actual - five_yr_avg):+.1f} Bcf" if five_yr_avg is not None else "N/A"
        surplus_str = f"{surplus_bcf:+.0f} Bcf ({surplus_pct:+.1f}%)" if surplus_bcf is not None else "N/A"

        surp_str, surp_dir, surp_label = format_eia_surprise(actual, consensus)

        deterministic_stance, deterministic_summary, _ = classify_storage_event(
            actual_bcf=actual,
            consensus_bcf=consensus,
            five_year_avg_bcf=five_yr_avg,
            surplus_vs_5yr_bcf=surplus_bcf,
            surplus_vs_5yr_pct=surplus_pct,
            seasonal_regime=seasonal_regime,
        )

        prompt = f"""You are an elite commodity hedge fund energy strategist analyzing the US EIA Weekly Natural Gas Storage Report.

REPORT DATA (Lower 48):
- Release Date: {report_date}
- Actual Net Change: {actual_str}
- Consensus Expectation: {cons_str}
- Surprise vs Consensus: {surp_label}
- 5-Year Historical Average Net Change: {fyr_str}
- 5-Year Historical Delta (Actual - 5YrAvg): {surplus_delta_str}
- Total Working Gas in Storage: {total_storage or 'N/A'} Bcf
- Surplus/Deficit vs 5-Year Average: {surplus_str}
- Trailing Tightening Streak: {consecutive_weeks} consecutive below-normal builds

DETERMINISTIC REGIME GROUND TRUTH:
- Baseline Calibrated Stance: {deterministic_stance}
- Fundamental Summary: {deterministic_summary}

SEASONAL & MACRO CONTEXT:
- Seasonal Regime: {seasonal_regime} Season (Calendar Week {season_info['calendar_week']})
- Core Fundamental Rule: {season_info['rule_description']}
- Market Dynamics: 
  * Storage Surprise = Actual - Consensus.
  * If Actual < Consensus, surprise is negative, which means LESS gas stored than expected (or deeper draw) -> TIGHTER balance -> BULLISH BEAT. Under NO circumstances classify a negative surprise as Bearish!
  * If Actual > Consensus, surprise is positive, which means MORE gas stored than expected (or shallower draw) -> LOOSER balance -> BEARISH MISS.
  * If actual build beat consensus (under-build) but storage remains in a multi-week surplus vs 5-year average (+75 Bcf surplus), the regime is DIVERGENT_TACTICAL_RELIEF (tactical short-covering bounce into macro surplus), NEVER BEARISH_LOOSENING.

CURRENT MCX/LOCAL MARKET STATUS:
- Spot / Underlying Price: ₹{underlying}
- PCR: {pcr} | CE ΔOI: {ce_oi_chg} | PE ΔOI: {pe_oi_chg}
- Options Flow Ground Truth: {oi_sentiment}

TASK:
1. Provide qualitative hedge fund commentary, expected immediate price impact on MCX/NYMEX, and multi-day weekly outlook.
2. Identify specific technical resistance and support levels.
3. Your sentiment output MUST align with the deterministic physical regime: '{deterministic_stance}'.
"""

        verdict_obj: EIAAnalysisVerdict | None = None
        try:
            import time
            from src.engine.llm_enrichment import _call_llm_api
            result: EIAAnalysisVerdict | None = _call_llm_api(
                symbol="NATURALGAS",
                prompt=prompt,
                response_schema=EIAAnalysisVerdict,
                deadline=time.time() + 90.0,
                purpose="eia_analysis",
            )
            if result and hasattr(result, "weekly_outlook") and result.weekly_outlook:
                verdict_obj = result
        except Exception as e:
            log.warning("Primary LLM chain for EIA failed, using deterministic fallback: %s", e)

        # 5. Deterministic Stance Enforcement (LLM cannot override physical reality)
        stance_to_store = deterministic_stance
        if verdict_obj and verdict_obj.sentiment == deterministic_stance:
            summary_to_store = f"{verdict_obj.summary} | Outlook: {verdict_obj.weekly_outlook}"
        elif verdict_obj:
            log.warning("LLM proposed sentiment '%s' which conflicts with deterministic stance '%s'. Enforcing deterministic.",
                        verdict_obj.sentiment, deterministic_stance)
            summary_to_store = f"{deterministic_summary} | Outlook: {verdict_obj.weekly_outlook}"
        else:
            summary_to_store = deterministic_summary

        key_levels_to_store = {}
        if verdict_obj:
            key_levels_to_store = {
                "resistance": verdict_obj.key_resistance,
                "support": verdict_obj.key_support,
            }

        # 6. Format Authoritative Telegram Alert
        if surp_dir == "BULLISH":
            if stance_to_store == "DIVERGENT_TACTICAL_RELIEF":
                header_icon = "⚡"
                header_title = f"EIA Natural Gas Report: Tactical Relief Beat ({surp_str})"
            else:
                header_icon = "📈"
                header_title = f"EIA Natural Gas Report: Bullish Surprise ({surp_str})"
        elif surp_dir == "BEARISH":
            if stance_to_store == "DIVERGENT_MACRO_TIGHTENING":
                header_icon = "⚠️"
                header_title = f"EIA Natural Gas Report: Macro Tightening ({surp_str})"
            else:
                header_icon = "📉"
                header_title = f"EIA Natural Gas Report: Bearish Surprise ({surp_str})"
        else:
            header_icon = "📊"
            header_title = "EIA Natural Gas Storage Report: In-Line"

        outlook_text = verdict_obj.weekly_outlook if verdict_obj else "Monitor technical levels for continuation or exhaustion."
        impact_text = verdict_obj.expected_impact if verdict_obj else "Tactical repricing on headline surprise."
        res_text = verdict_obj.key_resistance if verdict_obj and verdict_obj.key_resistance else "₹310.0, ₹315.0"
        sup_text = verdict_obj.key_support if verdict_obj and verdict_obj.key_support else "₹300.0, ₹295.0"

        surplus_line = f"• *Surplus vs 5-Yr Avg*: `{surplus_bcf:+.0f} Bcf ({surplus_pct:+.1f}%)`\n" if surplus_bcf is not None else ""

        alert_msg = (
            f"{header_icon} *{header_title}* {header_icon}\n\n"
            f"📅 *Release Date*: `{report_date}`\n\n"
            f"📊 *Key Data*:\n"
            f"• *Actual Net Change*: `{actual_str}`\n"
            f"• *Consensus*: `{cons_str}`\n"
            f"• *Surprise*: `{surp_label}`\n"
            f"• *5-Year Average*: `{fyr_str}` (Delta: `{surplus_delta_str}`)\n"
            f"• *Total Storage*: `{total_storage or 'N/A'}` Bcf\n"
            f"{surplus_line}\n"
            f"📈 *Market Dynamics*:\n"
            f"• Spot Price: ₹{underlying} | PCR: {pcr}\n"
            f"• Options Flow: {oi_sentiment}\n\n"
            f"🎯 *Trading Strategy*:\n"
            f"• Stance: **{stance_to_store}**\n"
            f"• Resistance: {res_text}\n"
            f"• Support: {sup_text}\n"
            f"• Outlook: {outlook_text}\n"
            f"• Expected Impact: {impact_text}\n\n"
            f"⚠️ *Physical Balance Context*:\n"
            f"{deterministic_summary}"
        )

        # 7. Persist calibrated weekly stance into eia_consensus table (DST & holiday-aware)
        from src.engine.time_guards import get_eia_schedule
        eia_sched = get_eia_schedule(now_ist)
        next_release_dt = eia_sched["scheduled_release_dt"] + timedelta(days=7)
        valid_until_str = next_release_dt.astimezone(timezone.utc).isoformat()

        persisted_data = {
            "report_date": report_date,
            "consensus_bcf": consensus,
            "actual_bcf": actual,
            "surprise_bcf": surprise,
            "five_year_avg_bcf": five_yr_avg,
            "five_year_total_bcf": eia_data.get("five_year_total_bcf"),
            "surplus_vs_5yr_bcf": surplus_bcf,
            "surplus_vs_5yr_pct": surplus_pct,
            "total_storage_bcf": total_storage,
            "year_ago_bcf": year_ago_bcf,
            "pct_change_yrago": eia_data.get("pct_change_yrago"),
            "prior_week_revised_bcf": None,
            "prior_week_revision_flag": eia_data.get("prior_week_revision_flag", 0),
            "seasonal_regime": seasonal_regime,
            "macro_stance": stance_to_store,
            "stance_summary": summary_to_store,
            "key_levels_json": json.dumps(key_levels_to_store) if key_levels_to_store else None,
            "valid_until": valid_until_str,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "eia_analyzer_recalibration",
        }
        store_eia_weekly_data(persisted_data)
        log.info("Persisted weekly EIA recalibration: stance=%s, valid_until=%s", stance_to_store, valid_until_str)

        # 8. Dispatch alert
        send_text(alert_msg)
        log.info("EIA Report Analysis dispatched to Telegram.")

    except Exception as e:
        log.error("analyze_eia_report failed: %s", e, exc_info=True)


if __name__ == "__main__":
    from config.logging_config import configure_logging
    configure_logging()
    analyze_eia_report()
