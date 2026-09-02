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
def is_sentinel_heal_enabled() -> bool:
    """Returns True if self-healing actions should be executed upon anomaly diagnosis."""
    try:
        from config.runtime_config import load_runtime_config

        cfg_val = load_runtime_config().get("sentinel_heal_enabled")
        if cfg_val is not None:
            return bool(cfg_val)
    except Exception:
        pass
    return os.environ.get("SENTINEL_HEAL_ENABLED", "false").lower() == "true"


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


import threading

_RUNS_FILE_LOCK = threading.Lock()


def emit_scan_run_report(report: ScanRunReport):
    """Persists the ScanRunReport to the rolling latest runs file."""
    with _RUNS_FILE_LOCK:
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
    """Runs the rule engine, logs telemetry, and invokes LLM diagnostic if an anomaly is raised."""
    if isinstance(report_data, ScanRunReport):
        report_dict = asdict(report_data)
    else:
        report_dict = dict(report_data)
        
    symbol = report_dict.get("symbol")
    report_mode = get_sentinel_report_mode()
    report_dict["_report_mode"] = report_mode
    
    # 1. Run deterministic rule checks
    flags = _check_rules(report_dict)
    
    # ALWAYS record the scan run and persist health state
    try:
        emit_scan_run_report(report_from_dict(report_dict))
        persist_scan_run(report_dict, flags)
    except Exception as e:
        log.warning("%s: Failed to emit/persist scan run report: %s", symbol, e)

    # Stamp component health for Sentinel
    try:
        total_strikes = int(report_dict.get("total_strikes") or 0)
        zero_ltp = int(report_dict.get("zero_ltp_strikes") or 0)
        source = report_dict.get("source", "unknown")
        dur_s = (int(report_dict.get("scan_duration_ms") or 0)) / 1000.0
        if any(f.severity == "CRITICAL" for f in flags):
            stamp_health(f"sentinel_{symbol}", "DOWN", f"Critical anomaly: {flags[0].detail}")
        elif flags:
            stamp_health(f"sentinel_{symbol}", "DEGRADED", f"{len(flags)} warning(s): {flags[0].detail}")
        else:
            stamp_health(f"sentinel_{symbol}", "OK", f"{source} ({total_strikes} strk, {dur_s:.1f}s)")
    except Exception:
        pass

    if not flags:
        return None
        
    log.info("%s: Scan Sentinel flagged %d suspect condition(s). Launching AI Diagnostic...", symbol, len(flags))
    
    # 2. Invoke LLM diagnostic
    try:
        diagnostic = _run_ai_diagnostic(report_dict, flags)
        if diagnostic:
            # Enforce severity cap: LLM diagnostic severity cannot escalate beyond max rule flag severity
            max_flag_severity = "CRITICAL" if any(f.severity == "CRITICAL" for f in flags) else "WARNING"
            if max_flag_severity != "CRITICAL" and diagnostic.severity == "CRITICAL":
                log.warning("%s: Capping LLM diagnostic severity from CRITICAL to WARNING (all rule flags were WARNING)", symbol)
                diagnostic.severity = "WARNING"
                if diagnostic.recommended_action == "PAUSE_SYMBOL":
                    diagnostic.recommended_action = "ALERT_ONLY"

            log.warning("%s: Sentinel Diagnosis: %s | Severity: %s | Recommended Action: %s",
                        symbol, diagnostic.anomaly_summary, diagnostic.severity, diagnostic.recommended_action)
            
            # Log diagnostic findings to sentinel database
            _persist_sentinel_incident(symbol, flags, diagnostic)
            
            # 3. Self-healing execution
            if is_sentinel_heal_enabled():
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
    if duration_ms > 90_000:
        flags.append(SentinelFlag(
            rule="R4_SLOW_SCAN",
            severity="WARNING",
            detail=f"Symbol scan execution took {duration_ms/1000:.1f} seconds (limit 90s)"
        ))

    # R5: Option type vs action review (informational only)
    # NOTE: GO_SHORT + CE (sell call) and GO_LONG + PE (sell put) are VALID short-premium
    # constructions used by MULTILEG/TFSS strategies. `_sanitize_llm_verdict` documents all four
    # action/instrument combos as valid, so these are NOT an unresolved hedge mapping. Downgraded
    # from CRITICAL to WARNING to stop false-positive CRITICAL incidents; kept as an informational
    # signal for review on CORE buy-premium symbols only.
    llm_action = r.get("llm_action")
    llm_instrument = r.get("llm_instrument")
    if llm_action and llm_instrument:
        action = str(llm_action).upper()
        instr = str(llm_instrument).upper()
        if ("SHORT" in action and "CE" in instr) or ("LONG" in action and "PE" in instr):
            flags.append(SentinelFlag(
                rule="R5_OPTION_TYPE_MISMATCH",
                severity="WARNING",
                detail=(
                    f"action={action} with instrument={instr} — valid short-premium construction "
                    f"(sell call / sell put) for MULTILEG/TFSS; review only if unexpected for a "
                    f"CORE buy-premium symbol"
                )
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
            from config.settings import IST
            exp_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            today = datetime.now(IST).date()
            if exp_date < today:
                flags.append(SentinelFlag(
                    rule="R7_EXPIRED_CONTRACT",
                    severity="CRITICAL",
                    detail=f"Scan resolved to an expired contract: {expiry_str} (DTE: {(exp_date - today).days})"
                ))
        except Exception:
            pass

    # R8: Inverse Target / SL Premium Order
    llm_prem = r.get("llm_entry_premium")
    llm_t1 = r.get("llm_target_1")
    llm_sl = r.get("llm_stop_loss")
    llm_action = r.get("llm_action")
    llm_instr = str(r.get("llm_instrument") or "").upper()
    # Detect if SL is an underlying-level value (not a premium).
    # SL within ±30% of underlying price is almost certainly underlying-level, not premium.
    _sl_is_underlying_level = (
        llm_sl is not None and underlying > 0
        and abs(llm_sl - underlying) / underlying < 0.30
    )
    if llm_prem and llm_action:
        action_str = str(llm_action).upper()
        # Directional options (GO_LONG, GO_SHORT, BUY_CE, BUY_PE, BUY) buy options -> Target premium > Entry premium
        is_option_buy = any(k in action_str for k in ("GO_LONG", "GO_SHORT", "BUY")) or ("PE" in llm_instr and "SELL" not in action_str) or ("CE" in llm_instr and "SELL" not in action_str and "WRITING" not in action_str)
        is_option_sell = any(k in action_str for k in ("SELL_CE", "SELL_PE", "WRITING")) or (action_str.startswith("SELL") and "BUY" not in action_str)

        if is_option_buy and not is_option_sell:
            if llm_t1 and llm_t1 <= llm_prem:
                flags.append(SentinelFlag(
                    rule="R8_INVERSE_TARGET_SL",
                    severity="CRITICAL",
                    detail=f"BUY option target premium (₹{llm_t1}) <= entry premium (₹{llm_prem})"
                ))
            # Only validate SL if it's an option premium (not underlying-level spot level)
            if llm_sl and not _sl_is_underlying_level and llm_sl >= llm_prem:
                flags.append(SentinelFlag(
                    rule="R8_INVERSE_TARGET_SL",
                    severity="CRITICAL",
                    detail=f"BUY option stop loss premium (₹{llm_sl}) >= entry premium (₹{llm_prem})"
                ))
        elif is_option_sell:
            if llm_t1 and llm_t1 >= llm_prem:
                flags.append(SentinelFlag(
                    rule="R8_INVERSE_TARGET_SL",
                    severity="CRITICAL",
                    detail=f"SELL option target premium (₹{llm_t1}) >= entry premium (₹{llm_prem})"
                ))
            if llm_sl and not _sl_is_underlying_level and llm_sl <= llm_prem:
                flags.append(SentinelFlag(
                    rule="R8_INVERSE_TARGET_SL",
                    severity="CRITICAL",
                    detail=f"SELL option stop loss premium (₹{llm_sl}) <= entry premium (₹{llm_prem})"
                ))

    # R9: Zero or Missing Spot Price
    if underlying <= 0.0:
        flags.append(SentinelFlag(
            rule="R9_ZERO_SPOT_PRICE",
            severity="CRITICAL",
            detail=f"Underlying spot price is 0.0 or missing for {symbol}"
        ))

    # R10: Unhandled Pipeline Exception / Crash
    scan_status = str(r.get("status") or "").upper()
    td_status = str(r.get("trade_decision_status") or "").upper()
    if scan_status == "CRASHED" or td_status == "CRASHED":
        flags.append(SentinelFlag(
            rule="R10_PIPELINE_CRASH",
            severity="CRITICAL",
            detail=f"Pipeline scan encountered an unhandled exception crash for {symbol}: {r.get('trade_decision_reason')}"
        ))

    # R11: Extreme Strike Distance Out of Bounds
    llm_instr = str(r.get("llm_instrument") or "")
    m_strike = re.search(r"(\d+(?:\.\d+)?)", llm_instr)
    if m_strike and underlying > 0 and "FUT" not in llm_instr.upper():
        strike_val = float(m_strike.group(1))
        dist_pct = abs(strike_val - underlying) / underlying
        if dist_pct > 0.40:
            flags.append(SentinelFlag(
                rule="R11_EXTREME_STRIKE_DISTANCE",
                severity="INFO",
                detail=f"Instrument strike {strike_val} is {dist_pct:.1%} away from underlying spot {underlying} (>40% safety limit)"
            ))

    # R12: Zero OI Dominance (TODO: filter to ATM ±5 strikes only, not full chain)
    total_strikes = int(r.get("total_strikes") or 0)
    zero_oi = int(r.get("zero_oi_strikes") or 0)
    if total_strikes > 10:
        zero_oi_pct = zero_oi / total_strikes
        if zero_oi_pct > 0.85:
            flags.append(SentinelFlag(
                rule="R12_ZERO_OI_DOMINANCE",
                severity="WARNING",
                detail=f"{zero_oi_pct:.0%} of option chain strikes ({zero_oi}/{total_strikes}) have 0 Open Interest"
            ))

    # R13: Cross-expiry strike mismatch
    resolved_exp = r.get("resolved_expiry")
    scan_exp = r.get("scan_expiry") or r.get("expiry")
    if resolved_exp and scan_exp and resolved_exp != scan_exp:
        flags.append(SentinelFlag(
            rule="R13_CROSS_EXPIRY_STRIKE_MISMATCH",
            severity="CRITICAL",
            detail=f"Resolved instrument expiry {resolved_exp} != scan expiry {scan_exp}"
        ))

    # R14: Lot size zero or negative (Only applies when a trade is being entered or triggered)
    td_status = str(r.get("trade_decision_status") or "").upper()
    llm_act = str(r.get("llm_action") or "").upper()
    has_trade_intent = (
        td_status in ("ENTERED", "TRIGGERED", "LIVE_ENTERED", "OPEN")
        or (llm_act in ("GO_LONG", "GO_SHORT", "BUY", "SELL", "ENTER") and td_status != "BLOCKED")
    )
    if has_trade_intent:
        lots = r.get("llm_lots") or r.get("lots") or 0
        if lots <= 0:
            flags.append(SentinelFlag(
                rule="R14_LOT_SIZE_ZERO_OR_NEGATIVE",
                severity="CRITICAL",
                detail=f"Computed lot size is {lots} — trade would have zero quantity"
            ))

    # R15: Margin exceeds available capital
    margin = r.get("margin_req") or 0
    capital = r.get("available_capital") or 0
    if margin > 0 and capital > 0 and margin > capital * 2.0:
        flags.append(SentinelFlag(
            rule="R15_MARGIN_EXCEEDS_CAPITAL",
            severity="CRITICAL",
            detail=f"Required margin ₹{margin:,.0f} exceeds 2x available capital ₹{capital:,.0f}"
        ))

    # R16: Duplicate signal key (requires DB query — deferred to v2)
    # signal_key = r.get("signal_key")
    # if signal_key:
    #     # TODO: Query DB for existing signal_key
    #     pass

    # R17: Confidence below 50% threshold
    conf = r.get("llm_confidence") or 0
    if 0 < conf < 50:
        flags.append(SentinelFlag(
            rule="R17_CONFIDENCE_BELOW_THRESHOLD",
            severity="WARNING",
            detail=f"LLM confidence {conf}% is below 50% (random threshold)"
        ))

    # R18: IV percentile missing or stale (Only validate if IV percentile timestamp is tracked)
    iv_pct_ts = r.get("iv_percentile_timestamp")
    if iv_pct_ts:
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(iv_pct_ts.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - ts).days
            if age_days > 7:
                flags.append(SentinelFlag(
                    rule="R18_IV_PERCENTILE_MISSING_OR_STALE",
                    severity="WARNING",
                    detail=f"IV percentile data {age_days} days stale (>7 days)"
                ))
        except Exception:
            pass

    # R19: MCX entry after 23:15 IST on expiry day
    try:
        from config.settings import IST, MCX_SYMBOLS
        now_ist = datetime.now(IST)
        is_mcx = symbol in MCX_SYMBOLS
        dte = r.get("dte") or 999
        if is_mcx and dte == 0 and now_ist.hour == 23 and now_ist.minute >= 15:
            flags.append(SentinelFlag(
                rule="R19_MCX_AFTER_CUTOFF_TIME",
                severity="CRITICAL",
                detail=f"MCX entry suggested at {now_ist.strftime('%H:%M')} IST on expiry day (cutoff: 23:15)"
            ))
    except Exception:
        pass

    # R20: Zero volume on selected strike (requires OC data — deferred to v2)
    # selected_strike = r.get("llm_strike") or 0
    # selected_ot = r.get("llm_option_type") or ""
    # TODO: Extract volume from option chain data

    # R21: Greeks calculation failed (all near zero for an active option trade)
    llm_act = str(r.get("llm_action") or "").upper()
    llm_instr = str(r.get("llm_instrument") or "").upper()
    if llm_instr and llm_instr not in ("NONE", "N/A", "") and llm_act not in ("NO_TRADE", "NONE", ""):
        delta = abs(r.get("delta") or 0)
        theta = abs(r.get("theta") or 0)
        vega = abs(r.get("vega") or 0)
        if delta < 0.01 and theta < 0.01 and vega < 0.01:
            flags.append(SentinelFlag(
                rule="R21_GREEKS_CALCULATION_FAILED",
                severity="WARNING",
                detail="All greeks near zero — calculation may have failed"
            ))

    # R22: Broker session expired (requires health stamp query — deferred to v2)
    # broker_health = get_health_stamp("shoonya_session")
    # TODO: Query health_stamps table for broker session status

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
    recent_logs = "\n".join((r.get("log_lines") or [])[-20:])  # Last 20 log lines to keep prompt token-compact
    
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
1. Identify the probable failure mode from the Knowledge Base and cite it by its exact
   section id (e.g. "F2", "F131"). If no documented mode fits, say "UNDOCUMENTED" —
   do not force-fit an unrelated failure mode.
2. Determine if the rule engine flagged a genuine issue or a harmless warning.
3. Recommend a corrective self-healing action:
   - SKIP_TRADE: If target premiums are inflated, incorrect option mapping exists, or option chain is corrupt.
   - FORCE_RESCAN: If an intermittent fetcher failure/timeout occurred.
   - PAUSE_SYMBOL: If critical dependencies are permanently failing.
   - CLEAR_CACHE: If LLM caching got poisoned with bad levels.
   - ALERT_ONLY: If the issue is informational (e.g. yfinance warnings, R8 false-positive on underlying-level SL).
4. Outline the exact impact of leaving this issue unaddressed.

IMPORTANT — RULE DIAGNOSIS GUIDELINES:
- If R8_INVERSE_TARGET_SL is in TRIGGERED RULES, cite section "F133" from Knowledge Base.
  If the SL value is close to the Underlying Spot Price (within ±30%), the SL is an UNDERLYING-LEVEL
  stop loss (not an option premium) — classify as ALERT_ONLY, not SKIP_TRADE or PAUSE_SYMBOL.
- If R5_OPTION_TYPE_MISMATCH is in TRIGGERED RULES: GO_SHORT + CE (sell call) and GO_LONG + PE
  (sell put) are VALID short-premium constructions for MULTILEG/TFSS strategies. This is NOT an
  option-chain structure error and NOT an AttributeError. Do NOT cite the resolved option-chain
  normalization entry (F134) or claim "list instead of dict" / "AttributeError" / "option chain
  structure" issues for R5. Diagnose it as an action/instrument mapping question only, and classify
  as ALERT_ONLY unless the symbol is a CORE buy-premium symbol where the mapping is genuinely
  unexpected.
- EVIDENCE REQUIREMENT: Every diagnosis MUST be grounded in the actual RECENT RELEVANT LOG LINES
  provided. Do NOT claim an exception (AttributeError, TypeError, NameError, etc.) unless the exact
  traceback or error line appears verbatim in those log lines. If no error line is present, do not
  invent one. Knowledge Base entries marked RESOLVED/FIXED describe past issues — do not re-diagnose
  them as current failures without a fresh matching error in the log lines.
- If read_only keyword argument or TypeError is cited, verify that F131 is marked RESOLVED.
  get_previous_underlying accepts *args, **kwargs — do NOT cite F131 or read_only TypeError as an
  active bug unless the exact traceback appears verbatim in the log lines.
- Severity must be proportional to the evidence: no error line in the logs → at most WARNING, never
  CRITICAL. CRITICAL requires a concrete error/traceback or a genuinely corrupt trade plan.
- If Strikes > 0, option chain data IS present. Do NOT report "no option chain data" or "no populated option chain"
  when Strikes > 0. Base your diagnosis strictly on the actual metadata and TRIGGERED RULES provided.
"""

    from src.engine.llm_enrichment import _call_llm_api
    
    # Run with a 60s timeout to allow full multi-provider cascading if primary models stall
    deadline = time.time() + 60.0
    try:
        diagnostic = _call_llm_api(symbol, prompt, ScanDiagnostic, deadline=deadline, purpose="sentinel_diagnostic")
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
            
            IST = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(IST)
            
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
