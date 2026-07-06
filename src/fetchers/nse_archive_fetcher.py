import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone

from config.settings import NSE_HEADERS
from src.fetchers.nse_fetcher import NSEPublicFetcher
from src.models.schema import get_conn

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def fetch_and_store_fii_positioning() -> bool:
    """
    Fetches daily FII/DII cash provisional data and participant-wise F&O OI.
    Parses the data and stores it in the `fii_positioning` table.
    Should be called daily after 19:15 IST.
    """
    now_ist = datetime.now(IST)
    # Check if today's data is already fetched
    today_str = now_ist.strftime("%Y-%m-%d")
    
    with get_conn() as conn:
        row = conn.execute(
            "SELECT report_date FROM fii_positioning WHERE report_date = ?", (today_str,)
        ).fetchone()
        if row:
            log.info("FII/DII positioning for %s already exists. Skipping.", today_str)
            return True

    fetcher = NSEPublicFetcher()
    fetcher._warm_session()
    
    # 1. Fetch FII/DII Cash Provisional
    fii_cash_net = 0.0
    dii_cash_net = 0.0
    data_date_str = None
    try:
        cash_url = "https://www.nseindia.com/api/fiidiiTradeReact"
        resp = fetcher.session.get(cash_url, headers=NSE_HEADERS, timeout=15)
        resp.raise_for_status()
        cash_data = resp.json()
        
        for item in cash_data:
            cat = item.get("category", "")
            net = float(item.get("netValue", "0").replace(",", ""))
            if not data_date_str:
                data_date_str = item.get("date")
            if cat == "DII":
                dii_cash_net = net
            elif cat == "FII/FPI" or cat == "FII":
                fii_cash_net = net
    except Exception as e:
        log.error("Failed to fetch FII/DII cash data: %s", e)
        # We can continue and try to fetch F&O data even if cash fails

    # 2. Fetch Participant-wise F&O OI CSV
    if data_date_str:
        try:
            dt = datetime.strptime(data_date_str, "%d-%b-%Y")
            csv_date_str = dt.strftime("%d%m%Y")
            today_str = dt.strftime("%Y-%m-%d")
        except:
            csv_date_str = now_ist.strftime("%d%m%Y")
    else:
        csv_date_str = now_ist.strftime("%d%m%Y")
        
    csv_url = f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{csv_date_str}.csv"
    
    fii_long = 0
    fii_short = 0
    client_long = 0
    client_short = 0
    
    try:
        csv_resp = fetcher.session.get(csv_url, headers=NSE_HEADERS, timeout=15)
        csv_resp.raise_for_status()
        
        # Parse CSV
        reader = csv.reader(io.StringIO(csv_resp.text))
        headers_found = False
        for row in reader:
            if not row:
                continue
            if row[0].strip() == "Client Type":
                headers_found = True
                continue
            if headers_found:
                ctype = row[0].strip()
                if ctype == "FII":
                    fii_long = int(row[1])
                    fii_short = int(row[2])
                elif ctype == "Client":
                    client_long = int(row[1])
                    client_short = int(row[2])
                    
    except Exception as e:
        log.error("Failed to fetch F&O Participant OI CSV: %s", e)
        # If both fail or CSV fails (maybe holiday/not uploaded yet), return False
        if fii_long == 0 and fii_short == 0:
            return False

    # Store in DB
    now_utc_str = datetime.now(timezone.utc).isoformat()
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fii_positioning 
                (report_date, fii_index_long, fii_index_short, client_index_long, client_index_short, dii_cash_net, fii_cash_net, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (today_str, fii_long, fii_short, client_long, client_short, dii_cash_net, fii_cash_net, now_utc_str)
            )
        log.info("Successfully fetched and stored FII/DII positioning for %s", today_str)
        return True
    except Exception as e:
        log.error("Failed to store FII/DII positioning: %s", e)
        return False
