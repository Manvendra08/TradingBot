import logging
import json
import sys
from pathlib import Path
from pydantic import BaseModel, Field
from config.settings import GEMINI_API_KEY
from src.alerts.telegram_dispatcher import send_text
from src.models.schema import get_conn

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


class EIAAnalysisVerdict(BaseModel):
    sentiment: str = Field(description="BULLISH | BEARISH | NEUTRAL")
    summary: str = Field(description="Summary of EIA Natural Gas Storage Report")
    expected_impact: str = Field(description="Expected price impact on MCX Natural Gas")
    markdown_telegram_message: str = Field(description="Complete Telegram formatted markdown alert with emojis")


def _get_latest_naturalgas_oi():
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


def analyze_eia_report():
    """Fetch EIA consensus/actual data and analyze using the unified LLM chain.
    
    Uses Forex Factory and official EIA HTTP endpoints first (fast, reliable),
    falling back to cached DB and scraper if needed. LLM analysis is routed
    through the unified multi-provider chain (OmniRouter -> Claude -> Groq -> Gemini).
    """
    try:
        data = None
        from datetime import datetime
        import pytz
        now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
        today_str = now_ist.strftime("%Y-%m-%d")

        # 1. Fast HTTP fetch via Forex Factory JSON feed
        try:
            from src.fetchers.eia_consensus_fetcher import fetch_eia_weekly_data, store_eia_weekly_data
            ff_data = fetch_eia_weekly_data()
            if ff_data:
                store_eia_weekly_data(ff_data)
        except Exception as e:
            log.warning("Forex Factory EIA fetch failed: %s", e)

        # 2. Check DB consensus & official EIA fallback for actual release
        consensus = None
        actual = None
        release_date = today_str
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT report_date, consensus_bcf, actual_bcf FROM eia_consensus ORDER BY report_date DESC LIMIT 1"
                ).fetchone()
                if row:
                    release_date = row["report_date"] or today_str
                    if row["consensus_bcf"] is not None:
                        consensus = float(row["consensus_bcf"])
                    if row["actual_bcf"] is not None:
                        actual = float(row["actual_bcf"])
        except Exception as e:
            log.warning("Failed to query eia_consensus table: %s", e)

        if actual is None:
            try:
                from src.engine.ng_eia_strategy import fetch_eia_actual_fallback
                actual = fetch_eia_actual_fallback()
                if actual is not None and consensus is not None:
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE eia_consensus SET actual_bcf=?, surprise_bcf=? WHERE report_date=?",
                            (actual, actual - consensus, release_date),
                        )
            except Exception as e:
                log.warning("Official EIA fallback scrape failed: %s", e)

        surprise_str = f"{(actual - consensus):+.1f} Bcf" if (actual is not None and consensus is not None) else "N/A"
        data = {
            "release_date": release_date,
            "actual": f"{actual:.1f} Bcf" if actual is not None else "Pending / N/A",
            "forecast": f"{consensus:.1f} Bcf" if consensus is not None else "N/A",
            "previous": "N/A",
            "surprise": surprise_str,
        }

        oi_data = _get_latest_naturalgas_oi()
        underlying = oi_data.get("underlying", "N/A")
        pcr = oi_data.get("pcr", "N/A")
        ce_oi_chg = oi_data.get("ce_oi_change", "N/A")
        pe_oi_chg = oi_data.get("pe_oi_change", "N/A")

        prompt = f"""You are an expert commodities trader analyzing the US Natural Gas EIA storage report.

REPORT DATA:
- Release Date: {data.get('release_date')}
- Actual: {data.get('actual')}
- Forecast: {data.get('forecast')}
- Previous: {data.get('previous')}
- Surprise (Actual - Forecast): {data.get('surprise')}

CONTEXT (MCX NATURALGAS):
- Current Underlying Price: {underlying}
- PCR: {pcr}
- CE OI Change: {ce_oi_chg}
- PE OI Change: {pe_oi_chg}

RULES:
1. Compare Actual vs Forecast. A draw (or smaller build) than forecast is BULLISH. A larger build than forecast is BEARISH.
2. Contextualize with the recent OI changes. Do the options markets agree with the EIA data?
3. Provide a clear, actionable summary of the sentiment and expected price impact on MCX Natural Gas.
4. Format markdown_telegram_message with emojis, bold headers, and concise bullet points.
"""

        # 3. Call Unified Multi-Provider LLM Chain
        analysis_msg = None
        try:
            from src.engine.llm_enrichment import _call_llm_api
            result: EIAAnalysisVerdict | None = _call_llm_api(
                symbol="NATURALGAS",
                prompt=prompt,
                response_schema=EIAAnalysisVerdict,
                purpose="eia_analysis",
            )
            if result and hasattr(result, "markdown_telegram_message") and result.markdown_telegram_message:
                analysis_msg = result.markdown_telegram_message
            elif result and hasattr(result, "summary"):
                analysis_msg = (
                    f"🛢️ *EIA Natural Gas Report Analysis*\n\n"
                    f"*Sentiment*: {getattr(result, 'sentiment', 'NEUTRAL')}\n"
                    f"*Actual*: {data.get('actual')} | *Forecast*: {data.get('forecast')} ({data.get('surprise')})\n\n"
                    f"{getattr(result, 'summary', '')}\n\n"
                    f"*Impact*: {getattr(result, 'expected_impact', '')}"
                )
        except Exception as e:
            log.warning("Primary LLM chain for EIA failed, trying fallback: %s", e)

        # 4. Fallback if primary LLM chain failed
        if not analysis_msg:
            # Deterministic quantitative analysis
            bias = "NEUTRAL"
            if actual is not None and consensus is not None:
                diff = actual - consensus
                if diff <= -2.0:
                    bias = "BULLISH (Smaller Build / Draw)"
                elif diff >= 2.0:
                    bias = "BEARISH (Larger Build)"
                else:
                    bias = "IN-LINE (Neutral)"

            analysis_msg = (
                f"🛢️ *EIA Natural Gas Storage Report*\n\n"
                f"• *Release Date*: `{data.get('release_date')}`\n"
                f"• *Actual*: `{data.get('actual')}`\n"
                f"• *Forecast*: `{data.get('forecast')}`\n"
                f"• *Surprise*: `{data.get('surprise')}`\n\n"
                f"📊 *Fundamental Bias*: **{bias}**\n"
                f"• Spot Price: ₹{underlying}\n"
                f"• PCR: {pcr} | CE ΔOI: {ce_oi_chg:,} | PE ΔOI: {pe_oi_chg:,}"
            )

        # 5. Dispatch to Telegram
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
