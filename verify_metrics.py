import sys
import os

# Add the project root to sys.path so we can import dashboard_server
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from dashboard_server import app, get_portfolio_metrics
except ImportError as e:
    print(f"ImportError: {e}")
    sys.exit(1)

# we can call the function directly
try:
    metrics = get_portfolio_metrics()
    import json
    print("Portfolio Metrics:", json.dumps(metrics, ensure_ascii=True))
except Exception as e:
    import traceback
    traceback.print_exc()
