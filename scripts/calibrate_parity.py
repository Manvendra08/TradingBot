"""
Calibration utility for Natural Gas Parity Strategy.
Analyzes historical deviation logs from ng_parity_log to optimize entry thresholds.
Writes a report to docs/parity_calibration_YYYYMMDD.md.
"""

import os
import sys
import sqlite3
import numpy as np
from datetime import datetime, timezone, timedelta
import pytz

# Add project root to sys.path so we can import from src/config
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.settings import DB_PATH

IST = pytz.timezone("Asia/Kolkata")

def calibrate():
    print(f"Opening database: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print("Database file does not exist. Run the pipeline first to generate data.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # Fetch all logged deviations
    rows = conn.execute(
        "SELECT ts, dev_pct, mcx_last AS underlying_price, fair_value FROM ng_parity_log ORDER BY ts ASC"
    ).fetchall()
    
    if not rows:
        print("No parity logs found in ng_parity_log. Cannot run calibration.")
        return

    timestamps = []
    devs = []
    prices = []
    
    for r in rows:
        # ISO string parsing
        try:
            ts_str = r["ts"].replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            timestamps.append(dt)
            devs.append(float(r["dev_pct"]))
            prices.append(float(r["underlying_price"]))
        except Exception:
            pass

    if not devs:
        print("Could not parse deviation values.")
        return

    abs_devs = np.abs(devs)
    p50 = np.percentile(abs_devs, 50)
    p80 = np.percentile(abs_devs, 80)
    p90 = np.percentile(abs_devs, 90)
    p95 = np.percentile(abs_devs, 95)
    
    print(f"Total samples: {len(devs)}")
    print(f"p50 deviation: {p50:.3f}%")
    print(f"p80 deviation: {p80:.3f}%")
    print(f"p90 deviation: {p90:.3f}%")
    print(f"p95 deviation: {p95:.3f}%")

    # Success rate analysis:
    # If deviation crosses a candidate threshold T:
    # How often does it return to within +/- 0.10% (parity touch) within 90 minutes?
    candidate_thresholds = [0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]
    success_rates = {}
    total_triggers = {}
    
    for T in candidate_thresholds:
        triggers = 0
        successes = 0
        cooldown_until = None
        
        for i in range(len(devs)):
            dt = timestamps[i]
            dev = devs[i]
            
            if cooldown_until and dt < cooldown_until:
                continue
                
            if abs(dev) >= T:
                triggers += 1
                cooldown_until = dt + timedelta(minutes=90)
                
                # Check next 90 minutes for reversion (dev <= 0.10%)
                reverted = False
                for j in range(i + 1, len(devs)):
                    future_dt = timestamps[j]
                    if (future_dt - dt).total_seconds() > 90 * 60:
                        break
                    if abs(devs[j]) <= 0.10:
                        reverted = True
                        break
                if reverted:
                    successes += 1
                    
        total_triggers[T] = triggers
        success_rates[T] = (successes / triggers * 100.0) if triggers > 0 else 0.0

    # Write report
    os.makedirs(os.path.join(PROJECT_ROOT, "docs"), exist_ok=True)
    today_str = datetime.now(IST).strftime("%Y%m%d")
    report_path = os.path.join(PROJECT_ROOT, "docs", f"parity_calibration_{today_str}.md")
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# Natural Gas Parity Calibration Report ({datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')})\n\n")
        f.write(f"Analyzed **{len(devs)}** historical parity logs.\n\n")
        f.write("## Deviation Distribution Percentiles\n\n")
        f.write("| Percentile | Deviation Absolute Value |\n")
        f.write("|------------|--------------------------|\n")
        f.write(f"| Median (p50) | {p50:.3f}% |\n")
        f.write(f"| p80 | {p80:.3f}% |\n")
        f.write(f"| p90 | {p90:.3f}% |\n")
        f.write(f"| p95 | {p95:.3f}% |\n\n")
        
        f.write("## Reversion Success Rate (90-Minute Window)\n\n")
        f.write("Success is defined as deviation reverting back to within +/- 0.10% after hitting the entry threshold.\n\n")
        f.write("| Entry Threshold | Total Triggers | Reversion Success Rate |\n")
        f.write("|-----------------|----------------|------------------------|\n")
        for T in candidate_thresholds:
            f.write(f"| {T:.2f}% | {total_triggers[T]} | {success_rates[T]:.1f}% |\n")
            
        f.write("\n## Recommendation\n\n")
        # Recommendation logic
        best_t = 0.5
        for T in candidate_thresholds:
            if success_rates[T] >= 80.0 and total_triggers[T] >= 5:
                best_t = T
                break
        f.write(f"Suggested entry threshold: **{best_t:.2f}%** (based on >=80% success rate with sample coverage).\n")

    print(f"Calibration report written to: {report_path}")

if __name__ == "__main__":
    calibrate()
