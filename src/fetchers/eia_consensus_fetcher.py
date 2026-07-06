"""
EIA Weekly Natural Gas Storage Consensus & Actual Fetcher.
Pulls calendar data from the Forex Factory calendar JSON feed.
"""

import logging
import requests
import re
from datetime import datetime, timezone
from src.models.schema import get_conn

log = logging.getLogger(__name__)

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def parse_bcf_value(val_str: str | None) -> float | None:
    """Helper to parse value strings like '87B', '-12B' to float numbers."""
    if not val_str:
        return None
    val_str = str(val_str).strip()
    # Clean the string, keep digits, minus, dot
    cleaned = re.sub(r"[^\d.\-]", "", val_str)
    if cleaned:
        try:
            return float(cleaned)
        except ValueError:
            pass
    return None

def fetch_eia_weekly_data() -> dict | None:
    """
    Fetch the EIA Natural Gas Storage event from the Forex Factory JSON feed.
    Returns parsed dict or None on failure/missing event.
    """
    try:
        log.info("Fetching economic calendar from Forex Factory JSON feed...")
        r = requests.get(FF_CALENDAR_URL, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            log.warning("Forex Factory calendar feed returned status code: %d", r.status_code)
            return None
        
        events = r.json()
        for item in events:
            title = item.get("title", "")
            country = item.get("country", "")
            
            # Identify the EIA Natural Gas Storage event
            if "Natural Gas Storage" in title and country == "USD":
                date_str = item.get("date", "")
                if not date_str:
                    continue
                # Parse report date (YYYY-MM-DD) from ISO date string
                report_date = date_str.split("T")[0]
                
                forecast_val = parse_bcf_value(item.get("forecast"))
                actual_val = parse_bcf_value(item.get("actual"))
                
                surprise_val = None
                if forecast_val is not None and actual_val is not None:
                    surprise_val = actual_val - forecast_val
                
                result = {
                    "report_date": report_date,
                    "consensus_bcf": forecast_val,
                    "actual_bcf": actual_val,
                    "surprise_bcf": surprise_val,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "source": "forexfactory"
                }
                log.info("Successfully fetched EIA weekly data for %s: forecast=%s, actual=%s", 
                         report_date, forecast_val, actual_val)
                return result
                
        log.warning("Natural Gas Storage event not found in current week's calendar")
    except Exception as e:
        log.warning("Failed to fetch EIA weekly calendar data: %s", e)
    return None

def store_eia_weekly_data(data: dict) -> None:
    """Insert or replace EIA consensus data into the SQLite database."""
    if not data or not data.get("report_date"):
        return
        
    sql = """
        INSERT OR REPLACE INTO eia_consensus
            (report_date, consensus_bcf, actual_bcf, surprise_bcf, fetched_at, source)
        VALUES
            (:report_date, :consensus_bcf, :actual_bcf, :surprise_bcf, :fetched_at, :source)
    """
    try:
        with get_conn() as conn:
            conn.execute(sql, data)
        log.info("Stored EIA data for %s in eia_consensus table", data["report_date"])
    except Exception as e:
        log.error("Failed to store EIA weekly data in DB: %s", e)

def fetch_and_store_eia_consensus() -> None:
    """Job entry point to fetch and persist EIA Natural Gas storage consensus."""
    data = fetch_eia_weekly_data()
    if data:
        store_eia_weekly_data(data)
