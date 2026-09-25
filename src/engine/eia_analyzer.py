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
from src.engine.ng_macro_context import get_seasonal_regime, classify_storage_event

log = logging.getLogger(__name__)
_IST = pytz.timezone("Asia/Kolkata")


class EIAAnalysisVerdict(BaseModel):
    sentiment: str = Field(description="BULLISH_TIGHTENING | BEARISH_LOOSENING | NEUTRAL_BALANCED")
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

        # 4. Prompt Construction with complete seasonal awareness
        actual_str = f"{actual:+.1f} Bcf" if actual is not None else "Pending / N/A"
        cons_str = f"{consensus:+.1f} Bcf" if consensus is not None else "N/A"
        surp_str = f"{surprise:+.1f} Bcf" if surprise is not None else "N/A"
        fyr_str = f"{five_yr_avg:+.1f} Bcf" if five_yr_avg is not None else "N/A"
        surplus_delta_str = f"{(actual - five_yr_avg):+.1f} Bcf" if (actual is not None and five_yr_avg is not None) else "N/A"

        prompt = f"""You are an elite commodity hedge fund energy strategist analyzing the US EIA Weekly Natural Gas Storage Report.

REPORT DATA (Lower 48):
- Release Date: {report_date}
- Actual Net Change: {actual_str}
- Consensus Expectation: {cons_str}
- Surprise vs Consensus: {surp_str}
- 5-Year Historical Average Net Change: {fyr_str}
- 5-Year Historical Delta (Actual - 5YrAvg): {surplus_delta_str}
- Total Working Gas in Storage: {total_storage or 'N/A'} Bcf
- Surplus/Deficit vs 5-Year Average: {surplus_bcf:+.0f} Bcf ({surplus_pct:+.1f}%) if surplus_bcf else 'N/A'
- Trailing Tightening Streak: {consecutive_weeks} consecutive below-normal builds

SEASONAL & MACRO CONTEXT:
- Seasonal Regime: {seasonal_regime} Season (Calendar Week {season_info['calendar_week']})
- Core Fundamental Rule: {season_info['rule_description']}
- Market Dynamics: 
  * In Injection Season (Apr-Oct), an injection on its own is NOT bearish. A build below expectations or below the 5-year average tightens the pre-winter buffer (BULLISH_TIGHTENING).
  * Check for front-month contract expiry rollover and short-covering triggers.
  * Check for supply curtailments (Lower 48 production drops) and export LNG feedgas demand.

CURRENT MCX/LOCAL MARKET STATUS:
- Spot / Underlying Price: ₹{underlying}
- PCR: {pcr} | CE ΔOI: {ce_oi_chg} | PE ΔOI: {pe_oi_chg}
- Options Flow Ground Truth: {oi_sentiment}

TASK:
1. Determine the calibrated macro stance: BULLISH_TIGHTENING, BEARISH_LOOSENING, or NEUTRAL_BALANCED.
2. Formulate a multi-day trading perspective that guides trades until NEXT Thursday's report resets the view.
3. Identify specific technical ceiling (resistance) and floor (support) levels.
4. Generate an executive Telegram message with emojis, bold headers, and dense bullet points.
"""

        analysis_msg = None
        verdict_obj: EIAAnalysisVerdict | None = None
        try:
            from src.engine.llm_enrichment import _call_llm_api
            result: EIAAnalysisVerdict | None = _call_llm_api(
                symbol="NATURALGAS",
                prompt=prompt,
                response_schema=EIAAnalysisVerdict,
                purpose="eia_analysis",
            )
            if result and hasattr(result, "markdown_telegram_message") and result.markdown_telegram_message:
                verdict_obj = result
                analysis_msg = result.markdown_telegram_message
        except Exception as e:
            log.warning("Primary LLM chain for EIA failed, trying fallback: %s", e)

        # 5. Deterministic fallback if LLM chain failed
        if not verdict_obj or not analysis_msg:
            deterministic_stance, deterministic_summary, _ = classify_storage_event(
                actual_bcf=actual if actual is not None else 0.0,
                consensus_bcf=consensus,
                five_year_avg_bcf=five_yr_avg,
                surplus_vs_5yr_bcf=surplus_bcf,
                surplus_vs_5yr_pct=surplus_pct,
                seasonal_regime=seasonal_regime,
            )
            analysis_msg = (
                f"🛢️ *EIA Natural Gas Storage Report*\n\n"
                f"• *Release Date*: `{report_date}`\n"
                f"• *Actual Net Change*: `{actual_str}`\n"
                f"• *Consensus*: `{cons_str}` ({surp_str})\n"
                f"• *5-Year Benchmark*: `{fyr_str}` (Delta: `{surplus_delta_str}`)\n"
                f"• *Total Storage*: `{total_storage or 'N/A'}` Bcf ({surplus_pct:+.1f}% vs 5-yr norm)\n\n"
                f"📊 *Weekly Calibrated Stance*: **{deterministic_stance}**\n"
                f"{deterministic_summary}\n\n"
                f"• Spot Price: ₹{underlying} | PCR: {pcr}\n"
                f"• Options Flow: {oi_sentiment}"
            )
            stance_to_store = deterministic_stance
            summary_to_store = deterministic_summary
            key_levels_to_store = {}
        else:
            stance_to_store = verdict_obj.sentiment
            summary_to_store = f"{verdict_obj.summary} | Outlook: {verdict_obj.weekly_outlook}"
            key_levels_to_store = {
                "resistance": verdict_obj.key_resistance,
                "support": verdict_obj.key_support,
            }

        # 6. Persist calibrated weekly stance into eia_consensus table
        days_to_next_thu = (3 - now_ist.weekday()) % 7
        if days_to_next_thu == 0:
            days_to_next_thu = 7
        next_thu = (now_ist + timedelta(days=days_to_next_thu)).replace(hour=20, minute=0, second=0, microsecond=0)
        valid_until_str = next_thu.astimezone(timezone.utc).isoformat()

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

        # 7. Dispatch alert
        if not analysis_msg.startswith("🛢️"):
            analysis_msg = f"🛢️ *EIA Natural Gas Report Analysis*\n\n{analysis_msg}"
        send_text(analysis_msg)
        log.info("EIA Report Analysis dispatched to Telegram.")

    except Exception as e:
        log.error("analyze_eia_report failed: %s", e, exc_info=True)


if __name__ == "__main__":
    from config.logging_config import configure_logging
    configure_logging()
    analyze_eia_report()
