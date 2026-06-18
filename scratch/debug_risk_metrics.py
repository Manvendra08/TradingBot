import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard_server import get_live_trades, get_risk_metrics

print("=== LIVE OPEN POSITIONS ===")
try:
    open_positions = get_live_trades(symbol="", status="OPEN", limit=300)
    for p in open_positions:
        print(f"Symbol: {p.get('symbol')}, OptType: {p.get('option_type')}, Strike: {p.get('strike')}, Side: {p.get('side')}, Entry: {p.get('entry_premium')}, SL: {p.get('sl_premium')}, Tgt: {p.get('target_premium')}")
except Exception as e:
    print(f"Error fetching open positions: {e}")

print("\n=== RISK METRICS LIVE ===")
try:
    metrics = get_risk_metrics(mode="live")
    print(json.dumps(metrics, indent=2))
except Exception as e:
    print(f"Error fetching risk metrics: {e}")
