# Scan Sentinel — Agentic AI Diagnostics System

An intelligent, self-healing monitor embedded in the pipeline that catches scan anomalies in real-time, diagnoses root causes using LLM reasoning grounded in codebase knowledge, takes bounded corrective actions, and alerts the operator with a clear diagnostic thesis.

## Architecture Overview

```mermaid
graph TD
    subgraph Pipeline Process
        A[run_pipeline] --> B["_process_symbol()"]
        B --> C["ScanRunRecorder (in-memory)"]
        C -->|"Structured metadata + log lines"| D["emit_scan_run_report()"]
        D --> E["data/scan_runs/latest.jsonl"]
    end

    subgraph Scan Sentinel (new module)
        F["scan_sentinel.py"] -->|"Reads latest.jsonl"| G["Rule Engine (Deterministic Guards)"]
        G -->|"PASS (99% of scans)"| H["No action"]
        G -->|"SUSPECT flag raised"| I["AI Diagnostic Agent"]
        I -->|"Reads KNOWLEDGE_BASE.md"| J["LLM Call (Gemini Flash)"]
        J --> K["ScanDiagnostic (Pydantic)"]
        K --> L{"Self-Heal?"}
        L -->|"Yes — bounded action"| M["Execute Playbook"]
        L -->|"No — alert only"| N["Send Discord Alert"]
        M --> N
    end

    D -.->|"Called from pipeline end"| F
```

## User Review Required

> [!IMPORTANT]
> **Token Budget:** The AI diagnostic call is gated behind deterministic rules. It will fire only when a rule flags an anomaly — estimated at 1–5 times per trading day. Each call costs ~3,000 tokens on Gemini 1.5 Flash (~$0.0002/call). Annual cost: under $1.
>
> **Self-Healing Scope:** The sentinel can only take a bounded set of safe actions (clear cache, force re-scan, pause a symbol). It **cannot** open/close trades or modify positions.

> [!WARNING]
> **Codebase Knowledge:** The sentinel's LLM prompt includes a static `KNOWLEDGE_BASE.md` file (~2,000 tokens) describing known failure modes and architecture. This file must be updated manually when major code changes are made, or it risks becoming stale. We can automate a refresh reminder in the daily digest.

## Open Questions

1. **Alert Channel:** Should sentinel alerts go to the same Discord webhook as existing Ops alerts, or a separate channel/thread for diagnostic alerts? *(Current plan: same Discord webhook, prefixed with `🔬 SENTINEL`)*
2. **Self-Healing Rollout:** Should self-healing actions be gated behind `ROLLOUT_LEVEL` like existing playbooks, or always active? *(Current plan: independent `SENTINEL_HEAL_ENABLED` env var, defaulting to `False` initially for observe-only rollout)*

---

## Proposed Changes

### Component 1: Scan Run Recorder (Pipeline Integration)

Captures structured metadata from each `_process_symbol()` call. This is **not** raw log parsing — it's a first-class data emission from the pipeline itself.

#### [MODIFY] [pipeline.py](file:///C:/Users/manve/Downloads/NSEBOT/src/engine/pipeline.py)

Add a `ScanRunRecorder` context manager that wraps each `_process_symbol()` call:

- Captures: symbol, underlying_price, expiry, source, scan_duration_ms, fetcher_errors, llm_verdict (action/instrument/targets), option_premium_used, warnings, errors
- Captures Python `logging` output via a temporary `QueueHandler` that intercepts log lines emitted during the symbol processing
- At the end of `_process_symbol()`, calls `emit_scan_run_report()` which writes a single JSON line to `data/scan_runs/latest.jsonl`
- The JSONL file is a rolling buffer: each pipeline run overwrites the file with one line per symbol (5-6 lines per run)

**Key data points captured:**

```python
@dataclass
class ScanRunReport:
    symbol: str
    timestamp_ist: str
    scan_duration_ms: int
    underlying_price: float
    expiry: str
    source: str                    # fetcher source (shoonya, dhan, etc.)
    
    # Option chain health
    total_strikes: int
    zero_ltp_strikes: int          # strikes where ltp == 0
    zero_oi_strikes: int           # strikes where oi == 0
    
    # LLM verdict summary (if any)
    llm_action: str | None         # GO_LONG / GO_SHORT / NO_TRADE
    llm_instrument: str | None     # e.g., "SENSEX 76900 PE"
    llm_entry_premium: float | None
    llm_target_1: float | None
    llm_target_2: float | None
    llm_stop_loss: float | None
    
    # Trade decision
    trade_decision_status: str | None   # TRIGGERED / BLOCKED / NO_SIGNAL
    trade_decision_reason: str | None
    
    # Anomaly flags (deterministic)
    warnings: list[str]            # WARNING-level log lines
    errors: list[str]              # ERROR-level log lines
    log_lines: list[str]           # All captured log lines for this symbol
```

---

### Component 2: Knowledge Base

A static markdown file that gives the LLM deep context about the codebase architecture, known failure modes, and what constitutes an anomaly. This is the "brain" that makes the AI's analysis intelligent rather than generic.

#### [NEW] [KNOWLEDGE_BASE.md](file:///C:/Users/manve/Downloads/NSEBOT/data/sentinel/KNOWLEDGE_BASE.md)

Contents (~2,000 tokens, structured for LLM consumption):

```markdown
# NSEBOT Architecture — Scan Sentinel Knowledge Base

## Pipeline Flow
1. `fetch_option_chain(symbol)` → fetches from Shoonya/Dhan/Paytm
2. `detect_anomalies()` → OI analysis, PCR, max pain, alerts
3. `get_llm_verdict()` → AI trade recommendation
4. `_sanitize_llm_verdict()` → validates/corrects LLM output
5. Paper/Live trading execution
6. `build_digest()` → Telegram/Discord alert

## Known Failure Modes
### F1: Premium == Underlying (CRITICAL)
- **Symptom:** LLM target_1 or target_2 within 5% of underlying_price
- **Root Cause:** BFO weekly options have zero volume. Shoonya returns 
  spot price as LTP for untraded options. Sanitizer uses this fake LTP.
- **Self-Heal:** Flag the verdict as INVALID, skip trade execution

### F2: yfinance Constituent Fetch Failures
- **Symptom:** "possibly delisted" errors for .NS tickers
- **Root Cause:** Yahoo Finance rate limiting or data gaps
- **Impact:** Index weight calculation falls back to static defaults
- **Self-Heal:** None needed (graceful fallback exists)

### F3: Option Type Mismatch (CE vs PE)
- **Symptom:** GO_SHORT action with CE instrument, or vice versa
- **Root Cause:** LLM outputs wrong option type
- **Self-Heal:** _sanitize_llm_verdict should auto-correct; if not, flag

### F4: Fetcher Source Degradation
- **Symptom:** Multiple symbols falling back to secondary fetcher
- **Root Cause:** Primary fetcher (Shoonya) auth failure or rate limit
- **Self-Heal:** Trigger Shoonya re-auth via ops_agent

### F5: Scan Duration Anomaly
- **Symptom:** Single symbol scan takes >120 seconds
- **Root Cause:** LLM provider timeout, chart fetcher hanging
- **Self-Heal:** None (informational alert)

### F6: Zero OI Option Chain
- **Symptom:** >80% of strikes have oi=0 AND volume=0
- **Root Cause:** After-hours scan, illiquid contract, or fetcher bug
- **Impact:** OI-based signals (PCR, max pain) are unreliable
- **Self-Heal:** Flag scan as LOW_CONFIDENCE

## Architecture Notes
- Pipeline logs go to `logs/main.log` (RotatingFileHandler, 10MB)
- Health state stored in SQLite `health_state` table via `stamp_health()`
- Ops Agent runs as separate process, reads health_state every 60s
- LLM providers: OpenRouter, Groq, Gemini, GitHub Models (cascading fallback)
- Symbols: NIFTY (NFO), BANKNIFTY (NFO), SENSEX (BFO), NATURALGAS (MCX), CRUDEOIL (MCX)
```

---

### Component 3: Scan Sentinel Module

The core diagnostic engine. Runs after each pipeline cycle completes.

#### [NEW] [scan_sentinel.py](file:///C:/Users/manve/Downloads/NSEBOT/src/engine/scan_sentinel.py)

**Rule Engine (Deterministic Guards):**

```python
def _check_rules(report: ScanRunReport) -> list[SentinelFlag]:
    """Fast, deterministic checks. Returns flags for any anomalies found."""
    flags = []
    
    # R1: Premium == Underlying (the SENSEX bug)
    if report.llm_target_1 and report.underlying_price > 0:
        ratio = report.llm_target_1 / report.underlying_price
        if 0.8 < ratio < 1.2:  # target is within 20% of underlying
            flags.append(SentinelFlag(
                rule="R1_PREMIUM_IS_UNDERLYING",
                severity="CRITICAL",
                detail=f"T1={report.llm_target_1} is {ratio:.0%} of underlying={report.underlying_price}"
            ))
    
    # R2: High error rate
    if len(report.errors) >= 3:
        flags.append(SentinelFlag(
            rule="R2_HIGH_ERROR_RATE", 
            severity="WARNING",
            detail=f"{len(report.errors)} errors in single symbol scan"
        ))
    
    # R3: Dead option chain
    if report.total_strikes > 0:
        dead_pct = report.zero_ltp_strikes / report.total_strikes
        if dead_pct > 0.8:
            flags.append(SentinelFlag(
                rule="R3_DEAD_OPTION_CHAIN",
                severity="WARNING",
                detail=f"{dead_pct:.0%} of {report.total_strikes} strikes have zero LTP"
            ))
    
    # R4: Scan duration anomaly
    if report.scan_duration_ms > 120_000:
        flags.append(SentinelFlag(
            rule="R4_SLOW_SCAN",
            severity="WARNING", 
            detail=f"Scan took {report.scan_duration_ms/1000:.1f}s"
        ))
    
    # R5: Option type vs action mismatch (post-sanitization)
    if report.llm_action and report.llm_instrument:
        action = report.llm_action.upper()
        instr = report.llm_instrument.upper()
        if ("SHORT" in action and "CE" in instr) or ("LONG" in action and "PE" in instr):
            flags.append(SentinelFlag(
                rule="R5_OPTION_TYPE_MISMATCH",
                severity="CRITICAL",
                detail=f"Action={action} but instrument={instr}"
            ))
    
    # R6: Entry premium out of bounds
    if report.llm_entry_premium and report.llm_entry_premium > 5000:
        flags.append(SentinelFlag(
            rule="R6_PREMIUM_OUT_OF_BOUNDS",
            severity="CRITICAL",
            detail=f"Entry premium {report.llm_entry_premium} exceeds safety bound"
        ))
    
    return flags
```

**AI Diagnostic Agent:**

When rule engine flags a SUSPECT scan, the sentinel feeds:
1. The `ScanRunReport` JSON (structured data)
2. The captured log lines for that symbol
3. The `KNOWLEDGE_BASE.md` context

...to a lightweight LLM call (Gemini 1.5 Flash via the existing `_call_llm_api` infrastructure) and gets back:

```python
class ScanDiagnostic(BaseModel):
    """AI-generated diagnostic for a flagged scan."""
    anomaly_summary: str = Field(description="One-line summary of the anomaly")
    root_cause: str = Field(description="Probable root cause based on knowledge base")
    impact: str = Field(description="What would happen if this went undetected: e.g., 'Order placed at ₹76,967 instead of ₹482'")
    severity: str = Field(description="CRITICAL / WARNING / INFO")
    recommended_action: str = Field(description="SKIP_TRADE / FORCE_RESCAN / PAUSE_SYMBOL / ALERT_ONLY / CLEAR_CACHE")
    reasoning: str = Field(description="Chain of reasoning connecting the log evidence to the diagnosis")
```

**Self-Healing Actions (bounded, safe):**

```python
HEAL_ACTIONS = {
    "SKIP_TRADE": _heal_skip_trade,         # Set trade_decision to BLOCKED for this symbol
    "FORCE_RESCAN": _heal_force_rescan,     # Re-run _process_symbol() once
    "PAUSE_SYMBOL": _heal_pause_symbol,     # stamp_health(last_scan_SYM, DOWN, "sentinel_paused")
    "CLEAR_CACHE": _heal_clear_cache,       # Clear verdict cache for this symbol
    "ALERT_ONLY": lambda _: None,           # No action, just alert
}
```

> [!IMPORTANT]
> Self-healing actions are **never position-modifying**. They can only prevent bad trades, not create them. This is a fundamental safety constraint.

---

### Component 4: Integration Points

#### [MODIFY] [pipeline.py](file:///C:/Users/manve/Downloads/NSEBOT/src/engine/pipeline.py)

At the end of `_process_symbol()`, after all DB writes and alerts:

```python
# ── Scan Sentinel: emit run report for diagnostics ──
try:
    from src.engine.scan_sentinel import emit_scan_report, run_sentinel
    report = emit_scan_report(symbol, scan_context, ...)
    sentinel_result = run_sentinel(report)
    if sentinel_result and sentinel_result.severity == "CRITICAL":
        log.warning("SENTINEL: %s — %s", symbol, sentinel_result.anomaly_summary)
except Exception:
    log.debug("Scan Sentinel failed gracefully")
```

#### [MODIFY] [ops_agent.py](file:///C:/Users/manve/Downloads/NSEBOT/ops_agent.py)

Add a new playbook `P13: Scan Sentinel Alerts` that reads `data/scan_runs/latest.jsonl` and forwards sentinel findings to Discord:

```python
# ── P13: Scan Sentinel Findings ──
try:
    sentinel_alerts = _read_sentinel_findings()
    for alert in sentinel_alerts:
        if alert["severity"] == "CRITICAL":
            _escalate("P13", f"🔬 SENTINEL: {alert['summary']} | Action: {alert['action']}", critical=True)
        else:
            _escalate("P13", f"🔬 SENTINEL: {alert['summary']}")
except Exception:
    pass
```

---

### Component 5: Discord Alert Format

The sentinel alert in Discord will look like:

```
🔬 SENTINEL | SENSEX | CRITICAL

📊 Anomaly: Option target premium (₹76,967) equals underlying spot price (₹76,900)
🔍 Root Cause: BFO weekly 76900CE has zero volume/OI. Shoonya returned spot 
   price as LTP fallback. Sanitizer accepted fake premium with delta=1.0.
💥 Impact: GTT order would be placed at ₹76,967 instead of ₹482 (actual premium)
🛠️ Action Taken: SKIP_TRADE — blocked this verdict from execution

📋 Evidence: "T1=Premium 76967.82" in sanitized output, 
   option_chain ltp=76685.33 for 76900CE (volume=0, oi=0)
```

---

## File Summary

| File | Action | Purpose |
|------|--------|---------|
| [pipeline.py](file:///C:/Users/manve/Downloads/NSEBOT/src/engine/pipeline.py) | MODIFY | Add `ScanRunRecorder` + sentinel hook at end of `_process_symbol()` |
| [scan_sentinel.py](file:///C:/Users/manve/Downloads/NSEBOT/src/engine/scan_sentinel.py) | NEW | Core sentinel: rule engine + AI diagnostics + self-healing |
| [KNOWLEDGE_BASE.md](file:///C:/Users/manve/Downloads/NSEBOT/data/sentinel/KNOWLEDGE_BASE.md) | NEW | Static codebase knowledge for LLM grounding |
| [ops_agent.py](file:///C:/Users/manve/Downloads/NSEBOT/ops_agent.py) | MODIFY | Add P13 playbook to forward sentinel alerts to Discord |

---

## Verification Plan

### Automated Tests
```bash
pytest tests/test_scan_sentinel.py -v
```

New test file covering:
- Rule engine: each rule (R1–R6) triggers correctly on synthetic data
- Rule engine: clean scan data produces zero flags
- AI diagnostic: mock LLM response parses into `ScanDiagnostic` correctly
- Self-heal actions: each action modifies the expected state
- `ScanRunRecorder`: captures log lines and metadata correctly
- Integration: full pipeline mock → sentinel → alert emission

### Manual Verification
1. Run `python main.py --once --symbols SENSEX` and verify `data/scan_runs/latest.jsonl` is written
2. Inject a synthetic anomaly (set T1 = underlying) and verify sentinel fires CRITICAL alert
3. Verify Discord alert is received with the diagnostic thesis format
