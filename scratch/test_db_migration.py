import sys
import os

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.schema import init_db, get_conn

print("Running database initialization...")
try:
    init_db()
    print("Database initialization completed successfully!")
    
    # Verify columns exist
    with get_conn() as conn:
        cursor = conn.execute("PRAGMA table_info(live_trades)")
        columns = [row["name"] for row in cursor.fetchall()]
        print("Columns in live_trades:", columns)
        
        required = [
            "trade_status", "setup_type", "decision_reason", 
            "confidence_score", "entry_quality_score", 
            "trend_alignment_score", "regime_score"
        ]
        
        missing = [col for col in required if col not in columns]
        if missing:
            print("ERROR: Missing columns:", missing)
            sys.exit(1)
        else:
            print("SUCCESS: All required columns verified!")
            
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)
