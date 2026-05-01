---
status: testing
phase: 01-testing-and-fix
source: 01-SUMMARY.md
started: 2026-04-22T09:18:00Z
updated: 2026-04-22T09:18:00Z
---

## Current Test
<!-- OVERWRITE each test - shows where we are -->

number: 1
name: Cold Start Smoke Test
expected: |
  Kill any running server/service. Start the application from scratch using `python main.py --bridge`.
  Server boots without errors and returns `{"status":"ok","service":"nsebot_bridge"}` at `http://localhost:8765/health`.
awaiting: user response

## Tests

### 1. Cold Start Smoke Test
expected: Kill any running server/service. Start the application from scratch using `python main.py --bridge`. Server boots without errors and returns live health data.
result: [pending]

### 2. Price Spike Detection
expected: Underlying price move (e.g. 22000 to 23000) correctly triggers a price spike anomaly alert in the logs and database.
result: [pending]

### 3. HTTP Bridge Load Performance
expected: Server handles 10 concurrent requests to `/health` with average latency below 200ms and 100% success rate.
result: [pending]

### 4. Database Alert Persistence
expected: Ingested alerts are persistently stored in the `anomaly_alerts` table and can be retrieved after a server restart.
result: [pending]

### 5. Chrome Extension Dashboard Metrics
expected: Extension popup displays accurate real-time metrics (PCR, Max Pain, OI Spikes) fetched from the backend bridge.
result: [pending]

### 6. Manual Alert Ingestion (API)
expected: `POST /ingest` with valid OI_SPIKE JSON returns `{"ok":true}` and records the alert in the system.
result: [pending]

### 7. Bridge Control via Extension
expected: Clicking 'Start'/'Stop' buttons in the Chrome extension popup correctly toggles the backend bridge state.
result: [pending]

## Summary

total: 7
passed: 0
issues: 0
pending: 7
skipped: 0
blocked: 0

## Gaps

[none yet]
