# Scan Sentinel Integration

## Overview
The Scan Sentinel AI diagnostics system is now **integrated and active** in the pipeline. It validates every scan for data anomalies and corruption in parallel without blocking trades.

## How It Works

### 1. **Deterministic Rule Checks** (Instant)
After each symbol scan completes, Sentinel runs 6 hard-coded safety rules:

- **R1**: Premium == Underlying (catches BFO/SENSEX target bugs)
- **R2**: High error rate (≥3 errors during scan)
- **R3**: Dead option chain (>80% strikes with 0 LTP)
- **R4**: Slow scan (>120 seconds)
- **R5**: Option type mismatch (GO_LONG with PE instrument)
- **R6**: Premium out of bounds (>₹5000)

### 2. **AI Diagnostic** (Async, Non-Blocking)
If any rule triggers:
- Reads `data/sentinel/KNOWLEDGE_BASE.md` (all known failure modes F1-F43)
- Analyzes scan metadata + option chain health
- Calls LLM to diagnose root cause
- Recommends self-healing action:
  - `SKIP_TRADE` - blocks this symbol's trade
  - `PAUSE_SYMBOL` - stops future scans
  - `CLEAR_CACHE` - clears LLM verdict cache
  - `FORCE_RESCAN` - triggers immediate rescan
  - `ALERT_ONLY` - just logs (no action)

### 3. **Persistence & Alerts**
- Writes to `sentinel_incidents` table in SQLite
- Saves to `data/sentinel/latest.jsonl`
- Logs diagnosis: `WARNING: Sentinel Diagnosis: <summary> | Severity: <level> | Action: <recommendation>`

## Current Status

✅ **ACTIVE** - Rule checks run on every scan  
✅ **NON-BLOCKING** - Runs in background thread pool  
❌ **SELF-HEALING DISABLED** - Set `SENTINEL_HEAL_ENABLED=true` to enable auto-remediation

## Example Output

```log
2026-07-15 11:15:23 | INFO     | nsebot.scan_sentinel | SENSEX: Scan Sentinel flagged 1 suspect conditions. Launching AI Diagnostic...
2026-07-15 11:15:28 | WARNING  | nsebot.scan_sentinel | SENSEX: Sentinel Diagnosis: Target premium within 5% of underlying spot - likely untraded BFO option | Severity: CRITICAL | Recommended Action: SKIP_TRADE
```

## Enabling Self-Healing

To allow Sentinel to automatically execute remediation:

```bash
export SENTINEL_HEAL_ENABLED=true
python main.py
```

**⚠️ Warning**: Self-healing will automatically pause symbols, skip trades, or trigger rescans based on AI recommendations.

## Files

- `src/engine/scan_sentinel.py` - Core sentinel engine
- `src/engine/pipeline.py` - Integration point (line ~693)
- `data/sentinel/KNOWLEDGE_BASE.md` - Grounded failure mode catalog (F1-F43)
- `data/sentinel/latest.jsonl` - Rolling scan history per symbol
- DB table: `sentinel_incidents`

## Testing

```bash
# Run sentinel tests
pytest tests/test_scan_sentinel.py -v

# Check sentinel database
sqlite3 data/nsebot.db "SELECT * FROM sentinel_incidents ORDER BY ts DESC LIMIT 5;"
```

## Integration Summary

**What was changed:**
1. Added lightweight sentinel report builder at end of `_process_prefetched_symbol()` in `pipeline.py`
2. Submitted `run_sentinel()` to `pipeline_io_executor` for async execution
3. Updated `KNOWLEDGE_BASE.md` with F41 (MCX timeout), F42 (autopsy fix), F43 (sentinel integration)

**What trades see:**
- No impact on execution flow
- Trades proceed normally
- Alerts appear in logs if anomalies detected
- No blocking unless `SENTINEL_HEAL_ENABLED=true`
