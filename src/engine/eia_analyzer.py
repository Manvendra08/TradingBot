import logging
import json
import sys
from pathlib import Path
from config.settings import GEMINI_API_KEY
from src.alerts.telegram_dispatcher import send_text
from src.models.schema import get_conn

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]

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
    """Run the EIA scraper and analyze the results using the LLM.
    
    BUG-L01 FIX: Import the scraper module directly instead of using subprocess.
    This is more efficient (no process spawn overhead) and provides better error
    handling through Python exceptions rather than stderr parsing.
    """
    try:
        data = None
        # BUG-L01: Direct module import instead of subprocess
        scrape_module_path = ROOT / "tools" / "scrape_eia_report.py"
        if scrape_module_path.exists():
            tools_dir = str(ROOT / "tools")
            if tools_dir not in sys.path:
                sys.path.insert(0, tools_dir)
            try:
                import scrape_eia_report as scrape_module
                if hasattr(scrape_module, 'scrape_eia'):
                    data = scrape_module.scrape_eia()
                elif hasattr(scrape_module, 'scrape_eia_report'):
                    data = scrape_module.scrape_eia_report()
                elif hasattr(scrape_module, 'main'):
                    data = scrape_module.main()
            except Exception as scrape_err:
                log.warning("EIA Scrape via investing.com failed: %s", scrape_err)
            finally:
                if tools_dir in sys.path:
                    sys.path.remove(tools_dir)

        # Check if data is valid; if missing or has error, fallback to DB consensus and official EIA fallback
        if not data or not isinstance(data, dict) or "error" in data:
            log.info("Investing.com scraper unavailable (%s); falling back to DB consensus and EIA fallback.", data.get("error") if isinstance(data, dict) else "no data")
            from datetime import datetime
            import pytz
            now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
            today_str = now_ist.strftime("%Y-%m-%d")
            
            consensus = None
            actual = None
            release_date = today_str
            with get_conn() as conn:
                row = conn.execute("SELECT report_date, consensus_bcf, actual_bcf FROM eia_consensus ORDER BY report_date DESC LIMIT 1").fetchone()
                if row:
                    release_date = row["report_date"] or today_str
                    if row["consensus_bcf"] is not None:
                        consensus = float(row["consensus_bcf"])
                    if row["actual_bcf"] is not None:
                        actual = float(row["actual_bcf"])
            
            if actual is None:
                from src.engine.ng_eia_strategy import fetch_eia_actual_fallback
                actual = fetch_eia_actual_fallback()
                if actual is not None and consensus is not None:
                    with get_conn() as conn:
                        conn.execute("UPDATE eia_consensus SET actual_bcf=?, surprise_bcf=? WHERE report_date=?", (actual, actual - consensus, release_date))
            
            data = {
                "release_date": release_date,
                "actual": f"{actual} Bcf" if actual is not None else "N/A",
                "forecast": f"{consensus} Bcf" if consensus is not None else "N/A",
                "previous": "N/A",
                "surprise": f"{(actual - consensus):+.1f} Bcf" if (actual is not None and consensus is not None) else "N/A"
            }

        oi_data = _get_latest_naturalgas_oi()
        
        prompt = f"""
You are an expert commodities trader analyzing the US Natural Gas EIA storage report.

REPORT DATA:
- Release Date: {data.get("release_date")}
- Actual: {data.get("actual")}
- Forecast: {data.get("forecast")}
- Previous: {data.get("previous")}
- Surprise (Actual - Forecast): {data.get("surprise", "N/A")}

CONTEXT (MCX NATURALGAS):
- Current Underlying Price: {oi_data.get('underlying', 'N/A')}
- PCR: {oi_data.get('pcr', 'N/A')}
- CE OI Change: {oi_data.get('ce_oi_change', 'N/A')}
- PE OI Change: {oi_data.get('pe_oi_change', 'N/A')}

RULES:
1. Compare Actual vs Forecast. A draw (or smaller build) than forecast is BULLISH. A larger build than forecast is BEARISH.
2. Contextualize with the recent OI changes. Do the options markets agree with the EIA data?
3. Provide a clear, actionable summary of the sentiment and expected price impact on MCX Natural Gas.
4. Keep it concise, punchy, and formatted for a Telegram alert (use emojis).
"""

        try:
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY)
            
            response = client.models.generate_content(
                model='gemini-2.5-pro',
                contents=prompt,
                config={
                    'temperature': 0.2,
                    'http_options': {'timeout': 15.0}
                }
            )
            
            analysis = response.text.strip()
            
            # Dispatch to Telegram
            msg = f"🛢️ *EIA Natural Gas Report Analysis*\n\n{analysis}"
            send_text(msg)
            log.info("EIA Report Analysis dispatched to Telegram.")
            
        except Exception as e:
            log.error("LLM EIA Analysis failed: %s", e)
            
    except Exception as e:
        log.error("analyze_eia_report failed: %s", e)

if __name__ == "__main__":
    analyze_eia_report()
