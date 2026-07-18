"""
Scan Sentinel — Agentic AI Diagnostics System

Deterministic rule guards + Asynchronous LLM Diagnostic Agent.
Embedded directly in the pipeline flow to identify scan anomalies,
execute self-healing actions, and alert the user via Discord.
"""

import json
import logging
import os
import queue
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pydantic import BaseModel, Field
from logging.handlers import QueueHandler

# Local imports
from config.settings import LOG_DIR
from src.models.schema import stamp_health

log = logging.getLogger("nsebot.scan_sentinel")

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
SENTINEL_DIR = DATA_DIR / "sentinel"
RUNS_FILE = SENTINEL_DIR / "latest.jsonl"
KB_FILE = SENTINEL_DIR / "KNOWLEDGE_BASE.md"

# Ensure directories exist
SENTINEL_DIR.mkdir(parents=True, exist_ok=True)

# Self-healing config
SENTINEL_HEAL_ENABLED = os.environ.get("SENTINEL_HEAL_ENABLED", "false").lower() == "true"

# Report mode config
SENTINEL_REPORT_MODES = ("anomalies", "full")


def get_sentinel_report_mode() -> str:
    """Returns 'full' (report every scan) or 'anomalies' (only when rules fire)."""
    try:
        from config.runtime_config import load_runtime_config

        mode = str(load_runtime_config().get("sentinel_report_mode", "anomalies")).lower()
        if mode not in SENTINEL_REPORT_MODES:
            return "anomalies"
        return mode
    except Exception:
        return "anomalies"


@dataclass
class ScanRunReport:
    symbol: str
    timestamp_ist: str
    scan_duration_ms: int
    underlying_price: float
    expiry: str
    source: str
    total_strikes: int
    zero_ltp_strikes: int
    zero_oi_strikes: int
    llm_action: str | None
    llm_instrument: str | None
    llm_entry_premium: float | None
    llm_target_1: float | None
    llm_target_2: float | None
    llm_stop_loss: float | None
    trade_decision_status: str | None
    trade_decision_reason: str | None
    warnings: list[str]
    errors: list[str]
    fetcher_errors: list[str]
    option_premium_used: float | None
    log_lines: list[str]
    is_test: bool
    status: str


def emit_scan_run_report(report: ScanRunReport):
    """Persists the ScanRunReport to the rolling latest runs file."""
    lines = []
    try:
        if RUNS_FILE.exists():
            with open(RUNS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            data = json.loads(line)
                            if data.get("symbol") != report.symbol:
                                lines.append(data)
                        except Exception:
                            pass
    except Exception as e:
        log.warning("Failed to read latest runs: %s", e)
        
    lines.append(asdict(report))
    
    try:
        with open(RUNS_FILE, "w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item) + "\n")
    except Exception as e:
        log.error("Failed to write latest run: %s", e)


def report_from_dict(r: dict) -> ScanRunReport:
    """Builds a ScanRunReport from the simplified pipeline dict."""
    return ScanRunReport(
        symbol=r.get("symbol"),
        timestamp_ist=r.get("timestamp_ist") or datetime.now(timezone.utc).isoformat(),
        scan_duration_ms=int(r.get("scan_duration_ms") or 0),
        underlying_price=float(r.get("underlying_price") or 0.0),
        expiry=r.get("expiry") or "",
        source=r.get("source") or "unknown",
        total_strikes=int(r.get("total_strikes") or 0),
        zero_ltp_strikes=int(r.get("zero_ltp_strikes") or 0),
        zero_oi_strikes=int(r.get("zero_oi_strikes") or 0),
        llm_action=r.get("llm_action"),
        llm_instrument=r.get("llm_instrument"),
        llm_entry_premium=r.get("llm_entry_premium"),
        llm_target_1=r.get("llm_target_1"),
        llm_target_2=r.get("llm_target_2"),
        llm_stop_loss=r.get("llm_stop_loss"),
        trade_decision_status=r.get("trade_decision_status"),
        trade_decision_reason=r.get("trade_decision_reason"),
        warnings=list(r.get("warnings") or []),
        errors=list(r.get("errors") or []),
        fetcher_errors=list(r.get("fetcher_errors") or []),
        option_premium_used=r.get("option_premium_used"),
        log_lines=list(r.get("log_lines") or []),
        is_test=bool(r.get("is_test", False)),
        status=r.get("status") or "COMPLETED",
    )


def persist_scan_run(report_dict: dict, flags: "list[SentinelFlag]") -> None:
    """Persists a per-scan summary row to sentinel_scan_runs (used by full-report mode)."""
    try:
        from src.models.schema import get_conn

        with get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sentinel_scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    source TEXT,
                    underlying_price REAL,
                    expiry TEXT,
                    total_strikes INTEGER,
                    zero_ltp_strikes INTEGER,
                    zero_oi_strikes INTEGER,
                    llm_action TEXT,
                    llm_instrument TEXT,
                    flags TEXT,
                    flag_count INTEGER DEFAULT 0,
                    report_mode TEXT
                )
                """
            )
            IST_offset = timedelta(hours=5, minutes=30)
            now_ist = datetime.now(timezone.utc) + IST_offset
            conn.execute(
                "INSERT INTO sentinel_scan_runs "
                "(ts, symbol, source, underlying_price, expiry, total_strikes, "
                " zero_ltp_strikes, zero_oi_strikes, llm_action, llm_instrument, "
                " flags, flag_count, report_mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    now_ist.isoformat(),
                    report_dict.get("symbol"),
                    report_dict.get("source"),
                    report_dict.get("underlying_price"),
                    report_dict.get("expiry"),
                    report_dict.get("total_strikes"),
                    report_dict.get("zero_ltp_strikes"),
                    report_dict.get("zero_oi_strikes"),
                    report_dict.get("llm_action"),
                    report_dict.get("llm_instrument"),
                    json.dumps([f.rule for f in flags]),
                    len(flags),
                    report_dict.get("_report_mode", "anomalies"),
                ),
            )
    except Exception as e:
        log.error("%s: Failed to persist sentinel scan run: %s", report_dict.get("symbol"), e)


class ScanRunRecorder:
    """Context manager to intercept logs and profile scan duration for a symbol."""
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.start_time = None
        self.log_handler = None
        self.log_queue = queue.Queue()
        self.captured_logs = []
        self.report = None

    def __enter__(self):
        self.start_time = time.time()
        self.log_handler = QueueHandler(self.log_queue)
        self.log_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.log_handler)
        return self

    def _drain_logs(self):
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        while not self.log_queue.empty():
            record = self.log_queue.get()
            try:
                msg = formatter.format(record)
                self.captured_logs.append(msg)
            except Exception:
                pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Remove interceptor immediately
        logging.getLogger().removeHandler(self.log_handler)
        self._drain_logs()
        duration_ms = int((time.time() - self.start_time) * 1000)
        
        # If pipeline crashed, we still emit a report with the exception details
        if exc_type:
            log_line = f"CRITICAL PIPELINE CRASH: {exc_val}"
            self.captured_logs.append(log_line)
            
            # Emit crash report
            try:
                self.emit_crash_report(duration_ms, str(exc_val))
            except Exception as e:
                log.error("Failed to emit crash report: %s", e)

    def finalize(self, oc_data: dict, scan_context: dict, intel: dict | None, 
                 llm_verdict=None, exit_advice=None, is_test: bool = False):
        """Builds and records the final ScanRunReport at the end of the symbol run."""
        try:
            duration_ms = int((time.time() - self.start_time) * 1000)
            underlying = float(oc_data.get("underlying_price") or 0.0)
            expiry = oc_data.get("expiry") or ""
            source = oc_data.get("source") or "unknown"
            
            # Calculate option chain health indicators
            strikes = oc_data.get("strikes") or []
            total_strikes = len(strikes)
            zero_ltp_strikes = sum(1 for s in strikes if float(s.get("ltp") or 0.0) == 0.0)
            zero_oi_strikes = sum(1 for s in strikes if int(s.get("oi") or 0) == 0)
            
            # Extract warnings and errors from captured logs
            warnings = [line for line in self.captured_logs if " | WARNING  |" in line]
            errors = [line for line in self.captured_logs if " | ERROR    |" in line or " | CRITICAL |" in line]
            
            # Extract fetcher_errors from scan_context or logs
            fetcher_errors = scan_context.get("fetcher_errors", []) if scan_context else []
            if not fetcher_errors:
                fetcher_errors = [line for line in errors if "fetch" in line.lower() or "router" in line.lower()]
            
            # Extract LLM verdict fields
            llm_action = None
            llm_instrument = None
            llm_entry_premium = None
            llm_target_1 = None
            llm_target_2 = None
            llm_stop_loss = None
            
            if llm_verdict:
                llm_action = getattr(llm_verdict, "action", None)
                llm_instrument = getattr(llm_verdict, "instrument", None)
                
                # Helper to extract digits from string
                def _get_num(attr):
                    val = getattr(llm_verdict, attr, None)
                    if val is None:
                        return None
                    m = re.search(r"(\d+(?:\.\d+)?)", str(val))
                    return float(m.group(1)) if m else None
                
                llm_entry_premium = _get_num("entry_premium_range")
                llm_target_1 = _get_num("target_1")
                llm_target_2 = _get_num("target_2")
                llm_stop_loss = _get_num("stop_loss")

            # Extract trade decision details
            td_status = None
            td_reason = None
            if intel and intel.get("trade_decision"):
                td = intel["trade_decision"]
                td_status = td.get("status")
                td_reason = td.get("reason")
            
            IST_offset = timedelta(hours=5, minutes=30)
            now_ist = datetime.now(timezone.utc) + IST_offset
            
            self.report = ScanRunReport(
                symbol=self.symbol,
                timestamp_ist=now_ist.isoformat(),
                scan_duration_ms=duration_ms,
                underlying_price=underlying,
                expiry=expiry,
                source=source,
                total_strikes=total_strikes,
                zero_ltp_strikes=zero_ltp_strikes,
                zero_oi_strikes=zero_oi_strikes,
                llm_action=llm_action,
                llm_instrument=llm_instrument,
                llm_entry_premium=llm_entry_premium,
                llm_target_1=llm_target_1,
                llm_target_2=llm_target_2,
                llm_stop_loss=llm_stop_loss,
                trade_decision_status=td_status,
                trade_decision_reason=td_reason,
                warnings=warnings,
                errors=errors,
                fetcher_errors=fetcher_errors,
                option_premium_used=llm_entry_premium,
                log_lines=self.captured_logs,
                is_test=is_test,
                status="COMPLETED"
            )
            
            emit_scan_run_report(self.report)
            
        except Exception as e:
            log.error("%s: Failed to finalize ScanRunRecorder: %s", self.symbol, e)

    def emit_crash_report(self, duration_ms: int, err_msg: str):
        """Emits a crash report if the pipeline execution threw an exception."""
        IST_offset = timedelta(hours=5, minutes=30)
        now_ist = datetime.now(timezone.utc) + IST_offset
        self.report = ScanRunReport(
            symbol=self.symbol,
            timestamp_ist=now_ist.isoformat(),
            scan_duration_ms=duration_ms,
            underlying_price=0.0,
            expiry="",
            source="failed",
            total_strikes=0,
            zero_ltp_strikes=0,
            zero_oi_strikes=0,
            llm_action=None,
            llm_instrument=None,
            llm_entry_premium=None,
            llm_target_1=None,
            llm_target_2=None,
            llm_stop_loss=None,
            trade_decision_status="CRASHED",
            trade_decision_reason=err_msg,
            warnings=[],
            errors=[f"Pipeline crash: {err_msg}"],
            fetcher_errors=[],
            option_premium_used=None,
            log_lines=self.captured_logs,
            is_test=False,
            status="CRASHED"
        )
        emit_scan_run_report(self.report)


class SentinelFlag(BaseModel):
    rule: str
    severity: str  # WARNING | CRITICAL
    detail: str


class ScanDiagnostic(BaseModel):
    """AI-generated diagnostic for a flagged scan."""
    anomaly_summary: str = Field(description="One-line summary of the anomaly")
    root_cause: str = Field(description="Probable root cause based on knowledge base")
    impact: str = Field(description="What would happen if this went undetected")
    severity: str = Field(description="CRITICAL / WARNING / INFO")
    recommended_action: str = Field(description="SKIP_TRADE / FORCE_RESCAN / PAUSE_SYMBOL / ALERT_ONLY / CLEAR_CACHE")
    reasoning: str = Field(description="Chain of reasoning connecting the log evidence to the diagnosis")


def run_sentinel(report_data: dict | ScanRunReport) -> ScanDiagnostic | None:
    """Runs the rule engine and invokes LLM diagnostic if a suspect flag is raised."""
    
    if isinstance(report_data, ScanRunReport):
        report_dict = asdict(report_data)
    else:
        report_dict = report_data
        
    symbol = report_dict.get("symbol")
    report_mode = get_sentinel_report_mode()
    report_dict["_report_mode"] = report_mode
    
    # 1. Run deterministic rule checks
    flags = _check_rules(report_dict)
    
    if not flags:
        if report_mode == "full":
            log.info("%s: Scan Sentinel | scan OK (no anomalies) — full-report logged", symbol)
            persist_scan_run(report_dict, flags)
            try:
                emit_scan_run_report(report_from_dict(report_dict))
            except Exception as e:
                log.warning("%s: Failed to emit full-run report: %s", symbol, e)
        return None
        
    log.info("%s: Scan Sentinel flagged %d suspect conditions. Launching AI Diagnostic...", symbol, len(flags))
    
    # 2. Invoke LLM diagnostic
    try:
        diagnostic = _run_ai_diagnostic(report_dict, flags)
        if diagnostic:
            log.warning("%s: Sentinel Diagnosis: %s | Severity: %s | Recommended Action: %s",
                        symbol, diagnostic.anomaly_summary, diagnostic.severity, diagnostic.recommended_action)
            
            # Log diagnostic findings to sentinel database or health state
            _persist_sentinel_incident(symbol, flags, diagnostic)
            
            # 3. Self-healing execution
            if SENTINEL_HEAL_ENABLED:
                _execute_self_healing(symbol, diagnostic, report_dict)
            else:
                log.info("%s: Self-healing disabled. Skipping action: %s", symbol, diagnostic.recommended_action)
                
            return diagnostic
    except Exception as e:
        log.exception("%s: Scan Sentinel diagnostic failed", symbol)
        
    return None


def _check_rules(r: dict) -> list[SentinelFlag]:
    """Runs deterministic, zero-latency safety guards."""
    flags = []
    symbol = r.get("symbol")
    underlying = float(r.get("underlying_price") or 0.0)
    
    # R1: Premium == Underlying (SENSEX target premium bug)
    # Check if target 1 or target 2 is close to underlying spot
    for tgt_key in ("llm_target_1", "llm_target_2"):
        tgt_val = r.get(tgt_key)
        if tgt_val and underlying > 0:
            instr = str(r.get("llm_instrument") or "").upper()
            if "FUT" not in instr:  # Only for options
                ratio = tgt_val / underlying
                if 0.95 < ratio < 1.05:
                    flags.append(SentinelFlag(
                        rule="R1_PREMIUM_IS_UNDERLYING",
                        severity="CRITICAL",
                        detail=f"Target {tgt_key[-1]} premium ({tgt_val}) is within 5% of underlying index spot ({underlying})"
                    ))

    # R2: High error rate (pipeline errors)
    errors = r.get("errors") or []
    if len(errors) >= 3:
        flags.append(SentinelFlag(
            rule="R2_HIGH_ERROR_RATE",
            severity="WARNING",
            detail=f"Detected {len(errors)} ERROR/CRITICAL messages in logs during symbol scan"
        ))

    # R3: Dead option chain
    total_strikes = int(r.get("total_strikes") or 0)
    zero_ltp = int(r.get("zero_ltp_strikes") or 0)
    if total_strikes > 10:
        dead_pct = zero_ltp / total_strikes
        if dead_pct > 0.8:
            flags.append(SentinelFlag(
                rule="R3_DEAD_OPTION_CHAIN",
                severity="WARNING",
                detail=f"{dead_pct:.0%} of option chain strikes ({zero_ltp}/{total_strikes}) have 0 LTP"
            ))

    # R4: Scan duration anomaly
    duration_ms = int(r.get("scan_duration_ms") or 0)
    if duration_ms > 120_000:
        flags.append(SentinelFlag(
            rule="R4_SLOW_SCAN",
            severity="WARNING",
            detail=f"Symbol scan execution took {duration_ms/1000:.1f} seconds (limit 120s)"
        ))

    # R5: Option type vs action mismatch
    llm_action = r.get("llm_action")
    llm_instrument = r.get("llm_instrument")
    if llm_action and llm_instrument:
        action = str(llm_action).upper()
        instr = str(llm_instrument).upper()
        if ("SHORT" in action and "CE" in instr) or ("LONG" in action and "PE" in instr):
            flags.append(SentinelFlag(
                rule="R5_OPTION_TYPE_MISMATCH",
                severity="CRITICAL",
                detail=f"Post-sanitized trade is action={action} but instrument={instr} (unresolved hedge mapping)"
            ))

    # R6: Entry premium out of bounds
    llm_prem = r.get("llm_entry_premium")
    if llm_prem and underlying > 0:
        instr = str(r.get("llm_instrument") or "").upper()
        if "FUT" not in instr:  # Only options
            # If the option premium itself is > 5,000, that is extremely high for standard trades
            # (which usually average 50 - 500 premium)
            if llm_prem > 5000.0:
                flags.append(SentinelFlag(
                    rule="R6_PREMIUM_OUT_OF_BOUNDS",
                    severity="CRITICAL",
                    detail=f"Target option entry premium is ₹{llm_prem} (above safety limit of ₹5000)"
                ))

    # R7: Expired contract check (DTE < 0)
    expiry_str = r.get("expiry")
    if expiry_str:
        try:
            exp_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            if exp_date < today:
                flags.append(SentinelFlag(
                    rule="R7_EXPIRED_CONTRACT",
                    severity="CRITICAL",
                    detail=f"Scan resolved to an expired contract: {expiry_str} (DTE: {(exp_date - today).days})"
                ))
        except Exception:
            pass

    return flags


def _run_ai_diagnostic(r: dict, flags: list[SentinelFlag]) -> ScanDiagnostic | None:
    """Builds prompt, reads knowledge base, and calls LLM to diagnose suspect logs."""
    symbol = r.get("symbol")
    
    # Load codebase KNOWLEDGE_BASE
    kb_content = ""
    try:
        if KB_FILE.exists():
            kb_content = KB_FILE.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Could not read KNOWLEDGE_BASE.md: %s", e)

    flags_summary = "\n".join([f"- [{f.rule}] {f.severity}: {f.detail}" for f in flags])
    recent_logs = "\n".join((r.get("log_lines") or [])[-50:])  # Last 50 log lines
    
    prompt = f"""You are the Scan Sentinel — an automated Agentic AI Operations Diagnostic Agent.
Review the following flagged scan metadata and logs to produce a diagnostic thesis.

---
CODEBASE KNOWLEDGE BASE:
{kb_content}
---

SUSPECT SCAN REPORT:
Symbol: {symbol}
Timestamp: {r.get('timestamp_ist')}
Scan Duration: {r.get('scan_duration_ms')} ms
Underlying Spot Price: {r.get('underlying_price')}
Expiry: {r.get('expiry')}
Fetcher Source: {r.get('source')}
Option Chain: Strikes={r.get('total_strikes')}, ZeroLTP={r.get('zero_ltp_strikes')}, ZeroOI={r.get('zero_oi_strikes')}
LLM Action: {r.get('llm_action')}
LLM Instrument: {r.get('llm_instrument')}
Sanitized Levels: EntryPremium={r.get('llm_entry_premium')}, T1={r.get('llm_target_1')}, T2={r.get('llm_target_2')}, SL={r.get('llm_stop_loss')}
Trade Decision Status: {r.get('trade_decision_status')} ({r.get('trade_decision_reason')})

---
TRIGGERED RULES:
{flags_summary}

---
RECENT RELEVANT LOG LINES:
{recent_logs}

---
DIAGNOSTIC CRITERIA:
1. Identify the probable failure mode (F1 to F6) from the Knowledge Base.
2. Determine if the rule engine flagged a genuine issue or a harmless warning.
3. Recommend a corrective self-healing action:
   - SKIP_TRADE: If target premiums are inflated, incorrect option mapping exists, or option chain is corrupt.
   - FORCE_RESCAN: If an intermittent fetcher failure/timeout occurred.
   - PAUSE_SYMBOL: If critical dependencies are permanently failing.
   - CLEAR_CACHE: If LLM caching got poisoned with bad levels.
   - ALERT_ONLY: If the issue is informational (e.g. yfinance warnings).
4. Outline the exact impact of leaving this issue unaddressed.
"""

    from src.engine.llm_enrichment import _call_llm_api
    
    # Call the API using Gemini/OpenRouter/Groq cascading infrastructure
    # Run with a 30s timeout to avoid holding up the process
    deadline = time.time() + 30.0
    try:
        diagnostic = _call_llm_api(symbol, prompt, ScanDiagnostic, deadline=deadline)
        return diagnostic
    except Exception as e:
        log.error("%s: LLM call for Scan Sentinel failed: %s", symbol, e)
        return None


def _persist_sentinel_incident(symbol: str, flags: list[SentinelFlag], diag: ScanDiagnostic):
    """Saves the diagnostic findings to sqlite database."""
    # We save this inside the ops_agent database or nsebot database
    # Let's save it directly to SQLite nsebot.db under sentinel_incidents for visibility
    try:
        from src.models.schema import get_conn
        with get_conn() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS sentinel_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                severity TEXT NOT NULL,
                summary TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                recommended_action TEXT NOT NULL,
                action_executed INTEGER DEFAULT 0,
                diagnostics_json TEXT NOT NULL
            )
            """)
            
            IST_offset = timedelta(hours=5, minutes=30)
            now_ist = datetime.now(timezone.utc) + IST_offset
            
            diag_dict = diag.model_dump() if hasattr(diag, "model_dump") else diag.dict()
            diag_dict["triggered_rules"] = [f.rule for f in flags]
            
            conn.execute(
                "INSERT INTO sentinel_incidents (ts, symbol, severity, summary, root_cause, recommended_action, diagnostics_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    now_ist.isoformat(),
                    symbol,
                    diag.severity,
                    diag.anomaly_summary,
                    diag.root_cause,
                    diag.recommended_action,
                    json.dumps(diag_dict)
                )
            )
    except Exception as e:
        log.error("%s: Failed to persist sentinel incident: %s", symbol, e)


def _heal_skip_trade(symbol: str, diag: ScanDiagnostic):
    try:
        stamp_health(f"last_scan_{symbol}", "DEGRADED", f"sentinel_blocked: {diag.anomaly_summary}")
        log.warning("%s: Bounded healing: Stamped symbol health DEGRADED to skip downstream strategy execution.", symbol)
    except Exception as e:
        log.error("%s: Failed to execute SKIP_TRADE healing: %s", symbol, e)


def _heal_pause_symbol(symbol: str, diag: ScanDiagnostic):
    try:
        stamp_health(f"last_scan_{symbol}", "DOWN", f"sentinel_paused: {diag.anomaly_summary}")
        log.warning("%s: Bounded healing: Stamped symbol health DOWN to pause scans for this symbol.", symbol)
    except Exception as e:
        log.error("%s: Failed to execute PAUSE_SYMBOL healing: %s", symbol, e)


def _heal_clear_cache(symbol: str, diag: ScanDiagnostic):
    try:
        from src.engine.llm_enrichment import _VERDICT_CACHE
        if symbol in _VERDICT_CACHE:
            del _VERDICT_CACHE[symbol]
            log.info("%s: Bounded healing: Cleared LLM verdict cache.", symbol)
    except Exception as e:
        log.error("%s: Failed to execute CLEAR_CACHE healing: %s", symbol, e)


def _heal_force_rescan(symbol: str, diag: ScanDiagnostic):
    log.info("%s: Bounded healing: Rescan suggested. Adding rescan flag to database.", symbol)
    try:
        from src.models.schema import get_conn
        with get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO health_state (key, status, detail, updated_at) VALUES (?,?,?,?)",
                         (f"rescan_trigger_{symbol}", "PENDING", diag.anomaly_summary, datetime.now(timezone.utc).isoformat()))
    except Exception as e:
        log.error("%s: Failed to execute FORCE_RESCAN healing: %s", symbol, e)


def _heal_alert_only(symbol: str, diag: ScanDiagnostic):
    log.info("%s: Bounded healing: ALERT_ONLY action executed (No-op).", symbol)


HEAL_ACTIONS = {
    "SKIP_TRADE": _heal_skip_trade,
    "PAUSE_SYMBOL": _heal_pause_symbol,
    "CLEAR_CACHE": _heal_clear_cache,
    "FORCE_RESCAN": _heal_force_rescan,
    "ALERT_ONLY": _heal_alert_only,
}


def _execute_self_healing(symbol: str, diag: ScanDiagnostic, report_dict: dict):
    """Executes bounded self-healing adjustments based on the AI diagnosis."""
    action = diag.recommended_action.upper()
    log.info("%s: Executing self-healing action: %s", symbol, action)
    
    handler = HEAL_ACTIONS.get(action)
    if handler:
        handler(symbol, diag)
    else:
        log.warning("%s: Unknown self-healing action: %s", symbol, action)
