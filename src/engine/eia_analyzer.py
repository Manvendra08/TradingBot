import logging
import json
import subprocess
from pathlib import Path
from config.settings import GEMINI_API_KEY
from src.alerts.telegram_dispatcher import send_text
from src.models.schema import get_conn

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
SCRAPE_SCRIPT = ROOT / "tools" / "scrape_eia_report.py"

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
    """Run the EIA scraper and analyze the results using the LLM."""
    try:
        result = subprocess.run(
            ["python", str(SCRAPE_SCRIPT)], 
            capture_output=True, 
            text=True, 
            timeout=120
        )
        if result.returncode != 0:
            log.error("EIA Scrape failed: %s", result.stderr)
            return

        data = json.loads(result.stdout)
        if "error" in data:
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
                config={'temperature': 0.2}
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
