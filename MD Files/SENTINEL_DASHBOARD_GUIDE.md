# Scan Sentinel Dashboard Integration

## Overview

The **Scan Sentinel AI Diagnostics System** is now fully integrated into the Ops Monitor dashboard. You can now see real-time scan anomaly detection, AI-powered diagnostics, and severity-based alerts all in one place.

## Where to View Sentinel Reports

### 1. **Ops Monitor Tab** (Main Integration)
```
http://localhost:8080/ops
```

#### New Section: "🛡️ Scan Sentinel AI Diagnostics"
Shows the latest scan anomalies detected by Sentinel:
- **Symbol** - Which symbol triggered the alert
- **Severity** - CRITICAL (red), WARNING (yellow), or INFO (green)
- **Summary** - One-line description of the anomaly
- **Root Cause** - AI-diagnosed root cause
- **Recommended Action** - SKIP_TRADE, PAUSE_SYMBOL, FORCE_RESCAN, CLEAR_CACHE, or ALERT_ONLY
- **Timestamp** - When the incident was detected

#### Color Coding:
- 🔴 **CRITICAL (Red)** - Will cause financial loss; recommend immediate action
- 🟡 **WARNING (Yellow)** - Degraded but not catastrophic
- 🟢 **INFO (Green)** - Informational; graceful fallback exists

---

## Data Sources

### 1. **Live API Endpoints**

#### `/api/sentinel-incidents` (Last 50)
```bash
curl http://localhost:8080/api/sentinel-incidents | jq .
```
Returns:
```json
[
  {
    "id": 1,
    "ts": "2026-07-15T10:30:00+00:00",
    "symbol": "NATURALGAS",
    "severity": "CRITICAL",
    "summary": "Premium within 5% of underlying spot",
    "root_cause": "Untraded BFO option; Shoonya returning fake LTP",
    "recommended_action": "SKIP_TRADE",
    "action_executed": 0,
    "diagnostics_json": "{...full diagnostic details...}"
  }
]
```

#### `/api/sentinel-runs` (Latest scan reports)
```bash
curl http://localhost:8080/api/sentinel-runs | jq .
```
Returns recent scan metadata (option chain health, warnings, errors, etc.)

### 2. **Database**

#### `sentinel_incidents` table
```sql
SELECT * FROM sentinel_incidents ORDER BY ts DESC LIMIT 20;
```

#### `sentinel_incidents` columns:
- `id` - Incident ID
- `ts` - Timestamp (ISO 8601)
- `symbol` - Symbol that triggered the alert
- `severity` - CRITICAL / WARNING / INFO
- `summary` - One-liner description
- `root_cause` - AI-diagnosed root cause
- `recommended_action` - Self-heal recommendation
- `action_executed` - 0=pending, 1=executed
- `diagnostics_json` - Full JSON with rule triggers & LLM analysis

### 3. **Files**

#### `data/sentinel/latest.jsonl` (Per-symbol scan reports)
Rolling JSON lines file with scan metadata:
```bash
tail -f data/sentinel/latest.jsonl | jq '.symbol, .status'
```

#### `data/sentinel/KNOWLEDGE_BASE.md` (47 documented failure modes)
Knowledge base used by Sentinel AI for grounding:
- F1-F47 failure modes with root causes
- P0-P3 severity rankings
- Self-healing actions

---

## Understanding the Dashboard Display

### Status Indicators

**Component Cards:**
- Left border color indicates status
  - 🟢 Green = OK
  - 🟡 Yellow = Warning/Degraded
  - 🔴 Red = Down/Critical

**Age Display:**
- Shows when each component was last updated
- Example: "Updated 2m ago"

**Detail Fields:**
- Symbol-specific or system-wide metrics
- Example: `source=dhan price=281.90`

### Empty States

- **"No incidents detected"** = All scans are healthy ✅
- **"Markets closed (sleeping)"** = Outside trading hours (normal)
- **"Heartbeat stale"** = Bot may be hung (check logs)

---

## Enabling Auto-Remediation (Optional)

By default, Sentinel **alerts only** (doesn't block trades). To enable **auto-remediation**:

```bash
export SENTINEL_HEAL_ENABLED=true
python main.py
```

When enabled, Sentinel will:
- **SKIP_TRADE**: Stamp symbol health as DEGRADED → strategy skips entry
- **PAUSE_SYMBOL**: Stamp symbol health as DOWN → no scans until resolved
- **CLEAR_CACHE**: Purge LLM verdict cache to force fresh analysis
- **FORCE_RESCAN**: Trigger immediate rescan (within 30 seconds)
- **ALERT_ONLY**: Log warning; no action

⚠️ **Warning**: Auto-remediation requires careful monitoring. Start with `ALERT_ONLY` mode to validate before enabling full auto-fix.

---

## Troubleshooting

### I don't see Scan Sentinel section
1. **Dashboard server not running:**
   ```bash
   python dashboard_server.py
   ```
   Access http://localhost:8080/ops

2. **No scans have run yet:**
   ```bash
   python main.py --now
   ```
   This triggers a scan. Wait 1-2 minutes, then refresh dashboard.

3. **SQLite database not initialized:**
   ```bash
   sqlite3 data/nsebot.db ".tables" | grep sentinel
   ```
   Should show `sentinel_incidents` table.

### Incidents not appearing
1. Check if scan Sentinel is active:
   ```bash
   grep -i "scan sentinel" logs/main.log | tail -5
   ```

2. Check for Sentinel errors:
   ```bash
   grep -i "sentinel" logs/main.log | grep -i error
   ```

3. Verify API endpoint:
   ```bash
   curl http://localhost:8080/api/sentinel-incidents
   ```
   Should return JSON array (even if empty `[]`).

---

## Key Metrics & KPIs on Dashboard

| Metric | Meaning |
|--------|---------|
| **Heartbeat** | Bot scheduler heartbeat (OK = within 120s) |
| **Components OK** | Healthy components out of total |
| **Down / Warn** | Failed or degraded components |
| **Open Positions** | Active paper/live trades |
| **HB Age** | Seconds since last scheduler tick |

---

## Real-World Example Workflow

### Scenario: Premium == Underlying bug detected

1. **Dashboard Alert:**
   ```
   🔴 CRITICAL | SENSEX
   Summary: Target premium within 5% of underlying spot
   Root Cause: Untraded BFO option; Shoonya returning fake LTP
   Recommended Action: SKIP_TRADE
   ```

2. **What happens next:**
   - ✅ Alert logged to `logs/main.log`
   - ✅ Trade is NOT executed (blocked by sanitizer)
   - ✅ User sees warning on dashboard
   - ✅ Incident saved to `sentinel_incidents` table
   - ❌ No impact on other symbols (isolated)

3. **Resolution:**
   - Root cause: BFO weekly option untraded
   - Action: Switch to monthly options or different strike
   - Monitor: Refresh dashboard after fix applied

---

## Integration Points

### Backend (`dashboard_server.py`)
- `/api/sentinel-incidents` - Query `sentinel_incidents` table
- `/api/sentinel-runs` - Parse `data/sentinel/latest.jsonl`

### Frontend (`ops.html`)
- `loadSentinelIncidents()` - Fetch & render severity cards
- Auto-refresh every 15/30/60 seconds (configurable)
- Color-coded severity badges

### Core Engine (`src/engine/scan_sentinel.py`)
- `run_sentinel()` - Rule checks + AI diagnostics
- Runs async after each scan (non-blocking)
- Writes to DB + JSONL

### Knowledge Base (`data/sentinel/KNOWLEDGE_BASE.md`)
- 47 documented failure modes (F1-F47)
- P0-P3 severity rankings
- Self-healing action recommendations

---

## Next Steps

1. **Run a scan and check dashboard:**
   ```bash
   python main.py --now
   # Wait 2-3 minutes for Sentinel to complete
   # Refresh http://localhost:8080/ops
   ```

2. **Check raw data:**
   ```bash
   sqlite3 data/nsebot.db "SELECT symbol, severity, summary FROM sentinel_incidents ORDER BY ts DESC LIMIT 5;"
   ```

3. **Enable auto-healing (optional):**
   ```bash
   SENTINEL_HEAL_ENABLED=true python main.py
   ```

4. **Monitor logs for Sentinel diagnostics:**
   ```bash
   tail -f logs/main.log | grep -i sentinel
   ```

---

## Support

- **Issue:** Sentinel not running → Check `SENTINEL_HEAL_ENABLED` and logs
- **Issue:** Old data showing → Clear `data/sentinel/latest.jsonl` and rescan
- **Issue:** API 404 → Ensure `dashboard_server.py` is running on port 8080
- **Issue:** Incorrect diagnosis → Update `KNOWLEDGE_BASE.md` with new failure mode

