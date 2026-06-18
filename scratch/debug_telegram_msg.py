import sys
from pathlib import Path
# Add root folder to sys.path to resolve src.* imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import json
from src.alerts.digest import build_digest_wrapper

db_path = Path("data/nsebot.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Get latest scan summary for NATURALGAS
row = conn.execute(
    "SELECT * FROM scan_summaries WHERE symbol='NATURALGAS' ORDER BY fetched_at DESC LIMIT 1"
).fetchone()

if row:
    row_dict = dict(row)
    print("Fetched latest NATURALGAS scan summary:")
    print(f"Fetched at: {row_dict['fetched_at']}, Digest ID: {row_dict['digest_id']}")
    
    # Let's find alerts for this digest_id (table is anomaly_alerts)
    alerts = conn.execute(
        "SELECT * FROM anomaly_alerts WHERE digest_id=?", (row_dict['digest_id'],)
    ).fetchall()
    alerts_list = [dict(a) for a in alerts]
    print(f"Found {len(alerts_list)} alerts for this scan.")
    
    # We need to construct scan_context
    paper_trade_status = {
        "action": "EXECUTED",
        "trade": {
            "symbol": "NATURALGAS",
            "option_type": "FUT",
            "strike": 300.0,
            "entry_underlying": 299.1,
            "sl_underlying": 296.2,
            "target_underlying": None,
            "lots": 10,
            "side": "BUY",
            "opened_at": "2026-06-16T07:30:13.812376+00:00"
        },
        "setup_type": "TIMEFRAME",
        "lots": 10,
        "reason": "timeframe entry | 3H close 300.30 > p3H_high 295.24 | level 1"
    }
    
    live_trade_status = {
        "action": "ERROR",
        "reason": "poll square-off failed: Kite instrument cache miss for NATURALGAS; refusing live broker order on fallback tradingsymbol"
    }
    
    # Reconstruction of scan_context
    scan_context = {
        "underlying": row_dict.get("underlying_price") or row_dict.get("underlying"),
        "atm_strike": row_dict.get("atm_strike"),
        "pcr": row_dict.get("pcr"),
        "support": row_dict.get("support"),
        "resistance": row_dict.get("resistance"),
        "max_pain": row_dict.get("max_pain"),
        "expiry": row_dict.get("expiry"),
        "price_change_pct": row_dict.get("price_change_pct"),
        "price_change_points": row_dict.get("price_change_points"),
        "ce_oi_change": row_dict.get("ce_oi_change") or 0,
        "pe_oi_change": row_dict.get("pe_oi_change") or 0,
        "total_ce_oi": row_dict.get("total_ce_oi") or 0,
        "total_pe_oi": row_dict.get("total_pe_oi") or 0,
        "chart_indicators": {
            "1h": {"sentiment": "BULLISH", "ohlc": {"open": 295.0, "high": 300.0, "low": 294.0, "close": 299.1}},
            "3h": {"sentiment": "BULLISH", "ohlc": {"open": 290.0, "high": 301.0, "low": 289.0, "close": 300.3}}
        }
    }
    
    # Call build_digest_wrapper
    digest_id, msg = build_digest_wrapper(
        symbol="NATURALGAS",
        alerts=alerts_list,
        fetched_at=row_dict["fetched_at"],
        scan_context=scan_context,
        intelligence_text=None,
        paper_trade_status=paper_trade_status,
        live_trade_status=live_trade_status
    )
    
    # Write message to UTF-8 file so it doesn't cause console encoding issues
    msg_path = Path("scratch/msg.txt")
    msg_path.write_text(msg, encoding="utf-8")
    print(f"Generated message written to {msg_path}")
    
    # Count formatting characters to see if there is any mismatch
    for char in ('*', '_', '`'):
        print(f"Count of '{char}': {msg.count(char)}")
        
    # Let's inspect where unclosed characters are
    # For example, split the message by lines and check which lines have odd count of * or _
    print("\n--- Markdown Verification Per Line ---")
    for i, line in enumerate(msg.splitlines()):
        star_count = line.count('*')
        und_count = line.count('_')
        # If there are any markdown characters in the line, check them
        if star_count % 2 != 0 or und_count % 2 != 0:
            safe_line = line.encode('ascii', errors='replace').decode('ascii')
            print(f"Line {i+1} has odd formatting: Stars={star_count}, Underscores={und_count}")
            print(f"  Content: {safe_line}")

else:
    print("No scan summaries found for NATURALGAS")

conn.close()
