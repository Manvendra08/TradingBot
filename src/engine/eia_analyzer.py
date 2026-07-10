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
        # BUG-L01: Direct module import instead of subprocess
        scrape_module_path = ROOT / "tools" / "scrape_eia_report.py"
        if not scrape_module_path.exists():
            log.error("EIA scraper script not found at %s", scrape_module_path)
            return
        
        # Add tools directory to sys.path temporarily for import
        tools_dir = str(ROOT / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        
        try:
            import scrape_eia_report as scrape_module
            # Call the scraper's main function directly
            if hasattr(scrape_module, 'scrape_eia_report'):
                data = scrape_module.scrape_eia_report()
            elif hasattr(scrape_module, 'main'):
                data = scrape_module.main()
            else:
                log.error("EIA scraper has no callable entry point")
                return
        except Exception as scrape_err:
            log.error("EIA Scrape failed: %s", scrape_err)
            return
        finally:
            # Clean up sys.path
            if tools_dir in sys.path:
                sys.path.remove(tools_dir)
        
        if not data:
            log.error("EIA Scraper returned no data")
            return
        
        if isinstance(data, dict) and "error" in data:
            log.error("EIA Scraper returned error: %s", data["error"])
            return

        oi_data = _get_latest_naturalgas_oi()
        
        prompt = f"""
You are an expert commodities trader analyzing the US Natural Gas EIA storage report.

REPORT DATA:
- Release Date: {data.get("release_date")}
- Actual: {data.get("actual")}
- Forecast: {data.get("forecast")}
- Previous: {data.get("previous")}

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
