import requests
import csv
import io
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_dhan_master_mcx")

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

try:
    log.info("Downloading Dhan scrip master via stream...")
    # Use streaming to avoid loading the entire large CSV into memory at once
    r = requests.get(MASTER_URL, stream=True, timeout=60)
    r.raise_for_status()
    
    # We will read line by line
    lines = (line.decode('utf-8') for line in r.iter_lines())
    reader = csv.DictReader(lines)
    
    mcx_records = []
    
    for row in reader:
        exch = row.get("SEM_EXM_EXCH_ID", "").upper()
        if exch != "MCX":
            continue
        
        symbol = (row.get("SEM_CUSTOM_SYMBOL") or "").upper()
        trading_symbol = (row.get("SEM_TRADING_SYMBOL") or "").upper()
        
        if "NATURALGAS" in symbol or "NATURALGAS" in trading_symbol or "CRUDEOIL" in symbol or "CRUDEOIL" in trading_symbol:
            mcx_records.append({
                "sec_id": row.get("SEM_SMST_SECURITY_ID"),
                "symbol": symbol,
                "trading_symbol": trading_symbol,
                "instrument": row.get("SEM_INSTRUMENT_NAME"),
                "expiry": row.get("SEM_EXPIRY_DATE"),
                "option_type": row.get("SEM_OPTION_TYPE"),
                "strike": row.get("SEM_STRIKE_PRICE"),
            })
            
            # Print a few examples
            if len(mcx_records) <= 20:
                log.info(f"Match: {mcx_records[-1]}")
                
    log.info(f"Total matching MCX records: {len(mcx_records)}")
    
    # Let's save the records to a file for analysis
    import json
    with open("scratch/mcx_scrip_matches.json", "w") as f:
        json.dump(mcx_records, f, indent=2)
    log.info("Saved matches to scratch/mcx_scrip_matches.json")
    
except Exception as e:
    log.exception("Error scanning Dhan master")
