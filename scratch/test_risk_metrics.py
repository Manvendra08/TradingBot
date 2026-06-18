import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_server import get_risk_metrics, get_live_trades
import json

try:
    print("OPEN TRADES:")
    trades = get_live_trades(status="OPEN")
    for t in trades:
        print(f"  {t.get('symbol')} {t.get('option_type')} qty={t.get('lots')} pnl={t.get('pnl_rupees')} mode={t.get('exit_mode')}")
        
    print("\nRISK METRICS:")
    metrics = get_risk_metrics("live")
    print(json.dumps(metrics, indent=2))
except Exception as e:
    import traceback
    traceback.print_exc()
