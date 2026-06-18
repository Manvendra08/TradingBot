import sys
from pathlib import Path

# Resolve project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn
from dashboard_server import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8081, log_level="info")
