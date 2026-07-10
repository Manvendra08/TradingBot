"""
OPS Agent v2.0 — NSEBOT Operations Monitor

Standalone process (separate failure domain from the bot).
Reads health signals, matches playbooks, takes bounded actions.
No LLM in the act path. No position-opening code path.

Usage:
    python ops_agent.py                 # Normal mode
    python ops_agent.py --observe-only  # T0 only (rollout step 2)
"""

import json
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
AGENT_DB = DATA_DIR / "ops_agent.db"
HEARTBEAT_PATH = Path("/tmp/nsebot.heartbeat")
HEALTHCHECKS_URL = os.environ.get("HEALTHCHECKS_URL", "")
BOT_DB_PATH = str(DATA_DIR / "nsebot.db")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8080")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
OBSERVE_ONLY = "--observe-only" in sys.argv

# Fallback notification channel (ntfy.sh)
NTFY_URL = os.environ.get("NTFY_URL", "")  # e.g., "https://ntfy.sh/mytopic"
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")

# CRITICAL repeat tracking
_critical_last_sent: dict[str, float] = {}  # playbook_id → timestamp
_critical_repeat_interval = 600  # 10 minutes

IST = timezone(timedelta(hours=5, minutes=30))


def _is_market_hours() -> bool:
    """Check if current time is within NSE or MCX market hours (IST)."""
    now = datetime.now(IST)
    weekday = now.weekday()
    if weekday >= 5:  # Saturday or Sunday
        return False
    hour = now.hour
    minute = now.minute
    time_val = hour * 100 + minute
    # NSE: 09:15-15:30, MCX: 09:00-23:30
    nse_open = 915 <= time_val <= 1530
    mcx_open = 900 <= time_val <= 2330
    return nse_open or mcx_open


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | ops_agent | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ops_agent")

# ── Incident DB ──────────────────────────────────────────────────────────────

_INCIDENTS_DDL = """
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    playbook_id TEXT NOT NULL,
    trigger_state TEXT,
    action      TEXT,
    result      TEXT,
    acked       INTEGER DEFAULT 0
);
"""


def _get_incidents_conn():
    conn = sqlite3.connect(str(AGENT_DB), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_INCIDENTS_DDL)
    return conn


def _log_incident(
    playbook_id: str, trigger: str, action: str, result: str
) -> int:
    now = datetime.now(IST).isoformat()
    with _get_incidents_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO incidents (ts, playbook_id, trigger_state, action, result) VALUES (?,?,?,?,?)",
            (now, playbook_id, trigger, action, result),
        )
        return cursor.lastrowid


# ── Health Reading ───────────────────────────────────────────────────────────

@dataclass
class HealthSnapshot:
    heartbeat_age_s: float | None = None
    heartbeat_ok: bool = False
    health_rows: list[dict] = field(default_factory=list)
    open_positions: int = 0
    oldest_position_age_min: float | None = None
    dashboard_up: bool = False
    read_source: str = "none"  # heartbeat | dashboard | sqlite | none


def _read_heartbeat_age() -> float | None:
    try:
        if HEARTBEAT_PATH.exists():
            return time.time() - HEARTBEAT_PATH.stat().st_mtime
    except Exception:
        pass
    return None


def _read_health_via_dashboard() -> dict | None:
    try:
        import requests
        r = requests.get(f"{DASHBOARD_URL}/health", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _read_health_via_sqlite() -> list[dict]:
    """Read-only SQLite fallback (file:...?mode=ro)."""
    try:
        db_uri = Path(BOT_DB_PATH).as_uri() + "?mode=ro"
        conn = sqlite3.connect(
            db_uri,
            uri=True,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM health_state").fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result
    except Exception:
        return []


def _read_open_positions_count() -> int:
    try:
        db_uri = Path(BOT_DB_PATH).as_uri() + "?mode=ro"
        conn = sqlite3.connect(
            db_uri,
            uri=True,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        paper = conn.execute(
            "SELECT COUNT(*) as c FROM paper_trades WHERE status='OPEN'"
        ).fetchone()["c"]
        live = conn.execute(
            "SELECT COUNT(*) as c FROM live_trades WHERE status='OPEN'"
        ).fetchone()["c"]
        conn.close()
        return paper + live
    except Exception:
        return 0


def _read_oldest_position_age() -> float | None:
    try:
        db_uri = Path(BOT_DB_PATH).as_uri() + "?mode=ro"
        conn = sqlite3.connect(
            db_uri,
            uri=True,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT MIN(opened_at) as oldest FROM ("
            "  SELECT opened_at FROM paper_trades WHERE status='OPEN'"
            "  UNION ALL"
            "  SELECT opened_at FROM live_trades WHERE status='OPEN'"
            ")"
        ).fetchone()
        conn.close()
        if not row or not row["oldest"]:
            return None
        opened = datetime.fromisoformat(row["oldest"].replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
    except Exception:
        return None


def read_health() -> HealthSnapshot:
    snap = HealthSnapshot()

    # 1. Heartbeat file
    snap.heartbeat_age_s = _read_heartbeat_age()
    snap.heartbeat_ok = snap.heartbeat_age_s is not None and snap.heartbeat_age_s < 120

    # 2. Try dashboard /health endpoint
    dash_data = _read_health_via_dashboard()
    if dash_data and dash_data.get("status") == "ok":
        snap.health_rows = dash_data.get("health", [])
        snap.open_positions = dash_data.get("open_positions", 0)
        hb_age = dash_data.get("heartbeat_age_s")
        if hb_age is not None:
            snap.heartbeat_age_s = hb_age
            snap.heartbeat_ok = hb_age < 120
        snap.dashboard_up = True
        snap.read_source = "dashboard"
        return snap

    # 3. Fallback: read-only SQLite
    snap.health_rows = _read_health_via_sqlite()
    snap.open_positions = _read_open_positions_count()
    snap.oldest_position_age_min = _read_oldest_position_age()
    snap.read_source = "sqlite"
    return snap


# ── Component State Machine ──────────────────────────────────────────────────

_COMPONENT_THRESHOLDS = {
    "scheduler_loop": {"stale_min": 3},
    "shoonya_session": {"stale_min": 10},
    "parity_feed": {"stale_min": 10},
    "telegram_send": {"stale_min": 999},  # on-failure only
    "db_write": {"stale_min": 5},
}


@dataclass
class ComponentState:
    status: str = "UNKNOWN"  # OK | DEGRADED | DOWN | UNKNOWN
    consecutive_down: int = 0
    last_ok_time: float = 0.0
    detail: str = ""


class StateMachine:
    def __init__(self):
        self.components: dict[str, ComponentState] = {}

    def _get(self, key: str) -> ComponentState:
        if key not in self.components:
            self.components[key] = ComponentState()
        return self.components[key]

    def evaluate(self, key: str, health_row: dict | None) -> str:
        """Evaluate a component health row. Returns new status."""
        state = self._get(key)
        now = time.time()

        if health_row is None:
            state.consecutive_down += 1
            state.status = "DOWN"
            return state.status

        status = (health_row.get("status") or "UNKNOWN").upper()
        state.detail = health_row.get("detail", "")

        if status == "OK":
            state.consecutive_down = 0
            state.status = "OK"
            state.last_ok_time = now
        elif status == "DEGRADED":
            state.consecutive_down += 1
            state.status = "DEGRADED" if state.consecutive_down < 2 else "DOWN"
        else:  # DOWN or UNKNOWN
            state.consecutive_down += 1
            state.status = "DOWN" if state.consecutive_down >= 2 else "DEGRADED"

        return state.status

    def evaluate_stale(self, key: str, health_row: dict | None, stale_min: float) -> str:
        """Evaluate staleness based on updated_at timestamp."""
        state = self._get(key)
        now = time.time()

        if health_row is None:
            state.consecutive_down += 1
            state.status = "DOWN"
            state.detail = "no health row"
            return state.status

        updated_at = health_row.get("updated_at", "")
        if not updated_at:
            state.consecutive_down += 1
            state.status = "DOWN"
            state.detail = "missing updated_at"
            return state.status

        try:
            ts = datetime.fromisoformat(updated_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            age_s = (datetime.now(IST) - ts).total_seconds()
        except Exception:
            state.consecutive_down += 1
            state.status = "DOWN"
            state.detail = "parse error"
            return state.status

        status = (health_row.get("status") or "UNKNOWN").upper()
        # Auto-heal telegram_send transient error if idle > 30m without new failures
        if key == "telegram_send" and status in ("DOWN", "DEGRADED") and age_s > 1800:
            status = "OK"
            try:
                from src.models.schema import stamp_health
                stamp_health("telegram_send", "OK", "Idle (no send errors in >30m)")
            except Exception:
                pass

        if status in ("DOWN", "DEGRADED"):
            state.consecutive_down += 1
            state.status = status if state.consecutive_down >= 2 or status == "DOWN" else "DEGRADED"
            state.detail = health_row.get("detail", "")
            return state.status

        if age_s < stale_min * 60:
            state.consecutive_down = 0
            state.status = "OK"
            state.last_ok_time = now
            state.detail = health_row.get("detail", "")
        else:
            state.consecutive_down += 1
            state.status = "DOWN" if state.consecutive_down >= 2 else "DEGRADED"
            state.detail = f"stale {age_s:.0f}s (threshold {stale_min*60:.0f}s)"

        return state.status


# ── Playbook Actions ─────────────────────────────────────────────────────────

def _send_discord(text: str) -> bool:
    """Send message to Discord via webhook. Returns True on success."""
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "your_discord_webhook_url":
        return False
    try:
        payload = json.dumps({"content": text[:2000]}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "NSEBOT-OPS/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        log.error("Discord send failed: %s", e)
        return False


def _send_fallback(text: str) -> bool:
    """Send notification via fallback channel (ntfy.sh)."""
    if not NTFY_URL:
        return False
    try:
        headers = {"Content-Type": "text/plain"}
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        req = urllib.request.Request(
            NTFY_URL,
            data=text.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log.error("Fallback notification failed: %s", e)
        return False


def _escalate(playbook_id: str, message: str, critical: bool = False) -> None:
    """Send escalation via Discord + fallback for CRITICAL."""
    global _critical_last_sent
    prefix = "🚨 CRITICAL" if critical else "⚙️ OPS"
    ts = datetime.now(IST).strftime("%H:%M IST")
    text = f"{prefix} | {playbook_id} | {message} | {ts}"
    
    # Send to Discord (primary channel for ops alerts)
    _send_discord(text)
    
    # For CRITICAL: send fallback (ntfy.sh) simultaneously + track for repeat
    if critical:
        _send_fallback(text)
        _critical_last_sent[playbook_id] = time.time()
    
    log.warning("ESCALATE [%s] %s: %s", playbook_id, "CRITICAL" if critical else "INFO", message)


def _repeat_critical_alerts() -> None:
    """Repeat CRITICAL alerts every 10 minutes until acked."""
    global _critical_last_sent
    now = time.time()
    for playbook_id, last_sent in list(_critical_last_sent.items()):
        if now - last_sent >= _critical_repeat_interval:
            # Check if acked in incidents DB
            try:
                conn = _get_incidents_conn()
                row = conn.execute(
                    "SELECT acked FROM incidents WHERE playbook_id=? AND acked=0 ORDER BY ts DESC LIMIT 1",
                    (playbook_id,)
                ).fetchone()
                conn.close()
                if row and not row["acked"]:
                    # Not acked yet, repeat
                    _send_fallback(f"🚨 CRITICAL REPEAT | {playbook_id} | Still active — not yet acked | {datetime.now(IST).strftime('%H:%M IST')}")
                    _critical_last_sent[playbook_id] = now
                else:
                    # Acked or no unacked incidents, stop repeating
                    del _critical_last_sent[playbook_id]
            except Exception:
                pass


def _restart_nsebot() -> bool:
    """Restart nsebot service via systemctl."""
    if os.name == "nt":
        log.warning("Auto-restart via systemctl not supported on Windows. Manual restart required.")
        return False
    try:
        result = subprocess.run(
            ["systemctl", "restart", "nsebot"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception as e:
        log.error("systemctl restart nsebot failed: %s", e)
        return False


def _reauth_shoonya() -> bool:
    """Trigger bot's re-auth hook."""
    try:
        import requests
        r = requests.post(f"{DASHBOARD_URL}/internal/reauth", timeout=10)
        return r.status_code == 200
    except Exception:
        # Fallback: restart with reauth
        return _restart_nsebot()


def _verify_shoonya_session() -> bool:
    """Verify Shoonya session by fetching one quote."""
    try:
        from src.fetchers.shoonya_fetcher import get_shoonya_fetcher
        f = get_shoonya_fetcher()
        if not f.login():
            return False
        # Try to fetch a quote for NIFTY (always available)
        search_res = f._search_scrip("NSE", "NIFTY")
        if not search_res or search_res.get("stat") != "Ok":
            return False
        values = search_res.get("values", [])
        if not values:
            return False
        token = values[0].get("token")
        if not token:
            return False
        quote = f._get_quotes("NSE", token)
        return quote is not None and quote.get("stat") == "Ok"
    except Exception as e:
        log.warning("Shoonya session verification failed: %s", e)
        return False


def _set_trading_paused() -> bool:
    """Set runtime_config.trading_paused = true (one-way safety switch)."""
    try:
        config_path = DATA_DIR / "runtime_config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            config = {}
        config["trading_paused"] = True
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        log.error("Failed to set trading_paused: %s", e)
        return False


def _run_emergency_flat() -> bool:
    """Run emergency_flat.py to close all open positions."""
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "emergency_flat.py")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            log.info("Emergency flat succeeded: %s", result.stdout[:200])
            return True
        else:
            log.error("Emergency flat failed: %s", result.stderr[:200])
            return False
    except Exception as e:
        log.error("Emergency flat exception: %s", e)
        return False


def _check_disk_usage() -> float:
    """Return disk usage percentage for the data partition."""
    try:
        import shutil
        usage = shutil.disk_usage(str(DATA_DIR))
        return (usage.used / usage.total) * 100.0
    except Exception:
        return 0.0


def _rotate_logs() -> bool:
    """Rotate log files to free disk space."""
    try:
        log_dir = ROOT / "logs"
        if not log_dir.exists():
            return True
        import glob
        log_files = sorted(glob.glob(str(log_dir / "*.log")), key=lambda f: os.path.getmtime(f))
        # Keep only the 5 most recent log files
        for old_log in log_files[:-5]:
            try:
                os.remove(old_log)
                log.info("Rotated old log: %s", old_log)
            except Exception:
                pass
        return True
    except Exception:
        return False


def _vacuum_database() -> bool:
    """Vacuum the bot database to reclaim space."""
    try:
        conn = sqlite3.connect(BOT_DB_PATH, timeout=10.0)
        conn.execute("VACUUM")
        conn.close()
        return True
    except Exception:
        return False


def _prune_temp() -> bool:
    """Prune /tmp files older than 1 day."""
    try:
        import glob
        temp_dir = Path("/tmp")
        cutoff = time.time() - 86400  # 1 day
        for f in temp_dir.glob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
        return True
    except Exception:
        return False


def _validate_time_via_healthchecks() -> bool:
    """
    Validate system time via healthchecks.io HTTP Date header.
    Returns True if time is within 60 seconds of external reference.
    Prevents clock-drift induced force-flats (P09/P10).
    """
    if not HEALTHCHECKS_URL:
        return True  # No external reference available, trust local clock
    try:
        req = urllib.request.Request(HEALTHCHECKS_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            date_header = resp.headers.get("Date")
            if not date_header:
                return True
            # Parse HTTP Date: "Mon, 08 Jul 2026 06:45:00 GMT"
            from email.utils import parsedate_to_datetime
            external_dt = parsedate_to_datetime(date_header)
            external_ts = external_dt.timestamp()
            local_ts = time.time()
            drift = abs(local_ts - external_ts)
            if drift > 60:
                log.warning("Time drift detected: %.1f seconds from external reference", drift)
                return False
            return True
    except Exception:
        return True  # On error, trust local clock


def _get_validated_now_ist() -> datetime:
    """Get current IST time, validated against external reference if available."""
    if not _validate_time_via_healthchecks():
        log.error("Time validation failed — clock may be drifting!")
    return datetime.now(IST)


# ── Playbook Engine ──────────────────────────────────────────────────────────

# Rollout level: 0=observe-only, 1=T1 enabled, 2=T2 enabled
ROLLOUT_LEVEL = 0 if OBSERVE_ONLY else 2


@dataclass
class PlaybookResult:
    action_taken: str = ""
    escalated: bool = False
    critical: bool = False


def run_playbooks(snap: HealthSnapshot, sm: StateMachine) -> list[PlaybookResult]:
    results = []
    now_ts = time.time()
    open_pos = snap.open_positions

    # ── Reset Counters if Healthy ──
    if snap.heartbeat_ok:
        sm._get("_restart_count").consecutive_down = 0
        sm._get("_restart_first_ts").consecutive_down = 0  # Reset window tracker

    shoonya_state = sm.components.get("shoonya_session")
    if shoonya_state and shoonya_state.status == "OK":
        sm._get("_reauth_count").consecutive_down = 0

    # ── P01: Bot dead (heartbeat stale) + no open positions → restart ──
    hb_stale = not snap.heartbeat_ok and (snap.heartbeat_age_s is None or snap.heartbeat_age_s > 180)
    if hb_stale and open_pos == 0 and ROLLOUT_LEVEL >= 1:
        restart_count = sm._get("_restart_count").consecutive_down
        # Track first restart timestamp for 30-min window
        first_ts_state = sm._get("_restart_first_ts")
        if restart_count == 0:
            first_ts_state.last_ok_time = now_ts  # Reuse field for first restart ts
        # Check 30-min window: if first restart was >30 min ago, reset counter
        if first_ts_state.last_ok_time > 0 and (now_ts - first_ts_state.last_ok_time) > 1800:
            restart_count = 0
            sm._get("_restart_count").consecutive_down = 0
        if restart_count < 2:
            sm._get("_restart_count").consecutive_down += 1
            ok = _restart_nsebot()
            result = PlaybookResult(action_taken="restart_nsebot")
            if ok:
                # Verify heartbeat within 90 seconds
                log.info("P01: Waiting 90s to verify heartbeat after restart...")
                time.sleep(90)
                hb_age = _read_heartbeat_age()
                if hb_age is not None and hb_age < 120:
                    _escalate("P01", f"Restarted nsebot (attempt {restart_count+1}). Heartbeat verified OK ({hb_age:.0f}s)")
                else:
                    _escalate("P01", f"Restarted nsebot (attempt {restart_count+1}) but heartbeat NOT verified ({hb_age}s). May need manual check.", critical=True)
                    result.critical = True
            else:
                _escalate("P01", f"Restart failed (attempt {restart_count+1})", critical=True)
                result.critical = True
            results.append(result)
        else:
            # P02: crash-loop → escalate CRITICAL
            results.append(PlaybookResult(action_taken="P02_crash_loop", escalated=True, critical=True))
            _escalate("P02", "Crash-loop detected (>2 restarts). Bot dead, no open positions.", critical=True)

    # ── P02: Bot dead + open positions → emergency flat ──
    if hb_stale and open_pos > 0 and ROLLOUT_LEVEL >= 2:
        ok = _run_emergency_flat()
        result = PlaybookResult(action_taken="emergency_flat", escalated=True, critical=True)
        if ok:
            _escalate("P02", f"Emergency flat executed — {open_pos} positions closed", critical=True)
        else:
            _escalate("P02", "Emergency flat FAILED — manual intervention required", critical=True)
        results.append(result)

    # ── P03: Shoonya auth-fail → re-auth ──
    shoonya_state = sm.components.get("shoonya_session")
    if shoonya_state and shoonya_state.status == "DOWN" and ROLLOUT_LEVEL >= 1:
        # Check if it's specifically an auth-fail (not timeout/502)
        detail = shoonya_state.detail.lower()
        is_auth_fail = "401" in detail or "403" in detail or "invalid token" in detail
        if is_auth_fail:
            reauth_count = sm._get("_reauth_count").consecutive_down
            if reauth_count < 3:
                sm._get("_reauth_count").consecutive_down += 1
                ok = _reauth_shoonya()
                result = PlaybookResult(action_taken="reauth_shoonya")
                if ok:
                    # Verify session by fetching one quote
                    quote_ok = _verify_shoonya_session()
                    if quote_ok:
                        _escalate("P03", f"Shoonya re-auth succeeded (attempt {reauth_count+1}). Quote verified OK.")
                    else:
                        _escalate("P03", f"Shoonya re-auth succeeded but quote verification FAILED (attempt {reauth_count+1})", critical=True)
                        result.critical = True
                else:
                    _escalate("P03", f"Shoonya re-auth failed (attempt {reauth_count+1})")
                results.append(result)
            else:
                # P04: re-auth failed ×3 → pause trading
                _set_trading_paused()
                results.append(PlaybookResult(action_taken="P04_trading_paused", escalated=True, critical=True))
                _escalate("P04", "Shoonya re-auth failed 3×. Trading PAUSED.", critical=True)
        else:
            # P04: Broker down (502/Timeout) → pause trading
            is_broker_down = "502" in detail or "timeout" in detail
            if is_broker_down:
                _set_trading_paused()
                results.append(PlaybookResult(action_taken="P04_broker_down", escalated=True, critical=True))
                _escalate("P04", "Broker down (502/Timeout). Trading PAUSED.", critical=True)

    # ── P05: Parity feed DOWN in NG hours → notify ──
    parity_state = sm.components.get("parity_feed")
    if parity_state and parity_state.status == "DOWN":
        now_ist = datetime.now(IST)
        # NG hours: 09:00-23:30 IST Mon-Sat
        is_ng_hours = now_ist.weekday() < 6 and 9 <= now_ist.hour < 24
        if is_ng_hours:
            results.append(PlaybookResult(action_taken="P05_parity_down"))
            _escalate("P05", f"Parity feed DOWN during NG hours. Detail: {parity_state.detail}")
            
            # Check if PARITY position is open with feed dead > 15 min → P10
            if parity_state.consecutive_down >= 2:  # 2 consecutive downs = ~2 min with feed dead
                # Check for open PARITY position
                try:
                    conn = sqlite3.connect(f"file:{BOT_DB_PATH}?mode=ro", uri=True, timeout=5.0)
                    conn.row_factory = sqlite3.Row
                    parity_open = conn.execute(
                        "SELECT COUNT(*) as c FROM paper_trades WHERE symbol='NATURALGAS' AND setup_type='NG_PARITY' AND status='OPEN'"
                    ).fetchone()["c"]
                    conn.close()
                    if parity_open > 0 and ROLLOUT_LEVEL >= 2:
                        # Feed dead with open PARITY position → emergency flat
                        ok = _run_emergency_flat()
                        result = PlaybookResult(action_taken="P05_parity_flat", escalated=True, critical=True)
                        if ok:
                            _escalate("P05", f"Emergency flat: PARITY position open with feed dead >15min. Closed.", critical=True)
                        else:
                            _escalate("P05", "Emergency flat FAILED for PARITY position", critical=True)
                        results.append(result)
                except Exception:
                    pass

    # ── P06: Disk/DB health → rotate logs, vacuum, prune ──
    db_write_state = sm.components.get("db_write")
    if db_write_state and db_write_state.status == "DOWN" and ROLLOUT_LEVEL >= 1:
        disk_pct = _check_disk_usage()
        # T1: Try remediation first
        log.info("P06: db_write DOWN, disk=%.1f%%. Attempting remediation...", disk_pct)
        _rotate_logs()
        _vacuum_database()
        _prune_timeout = 300  # 5 minutes
        time.sleep(2)
        # Re-check after remediation
        new_disk_pct = _check_disk_usage()
        if new_disk_pct > 90:
            # T2: Still failing → pause + escalate
            _set_trading_paused()
            results.append(PlaybookResult(action_taken="P06_db_disk_critical", escalated=True, critical=True))
            _escalate("P06", f"Disk critical ({new_disk_pct:.1f}%) after remediation. Trading PAUSED.", critical=True)
        else:
            results.append(PlaybookResult(action_taken="P06_db_disk_remediated"))
            _escalate("P06", f"Disk/DB issue remediated. Disk: {disk_pct:.1f}% → {new_disk_pct:.1f}%")

    # ── P07: Telegram failing > 10 min → switch to fallback ──
    tg_state = sm.components.get("telegram_send")
    if tg_state and tg_state.status == "DOWN" and ROLLOUT_LEVEL >= 1:
        results.append(PlaybookResult(action_taken="P07_telegram_down"))
        _escalate("P07", "Telegram sending failing. User may be blind to alerts.")

    # ── P08: VM reboot (heartbeat age < 10 min but stale health) ──
    if snap.heartbeat_ok and snap.heartbeat_age_s is not None and snap.heartbeat_age_s < 600:
        # Heartbeat is fresh, check if this is a recent reboot
        scheduler_state = sm.components.get("scheduler_loop")
        if scheduler_state and scheduler_state.status in ("DEGRADED", "DOWN"):
            results.append(PlaybookResult(action_taken="P08_vm_reboot"))
            _escalate("P08", "VM reboot detected — services restarting")

    # ── P09: Force-flat sentinel (Thursday 19:40 IST for NG) ──
    now_ist = _get_validated_now_ist()
    if now_ist.weekday() == 3 and now_ist.hour == 19 and now_ist.minute >= 40:
        # Check if NG position still open
        try:
            conn = sqlite3.connect(f"file:{BOT_DB_PATH}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            ng_open = conn.execute(
                "SELECT COUNT(*) as c FROM live_trades WHERE symbol='NATURALGAS' AND status='OPEN'"
            ).fetchone()["c"]
            conn.close()
            if ng_open > 0 and ROLLOUT_LEVEL >= 2:
                ok = _run_emergency_flat()
                result = PlaybookResult(action_taken="P09_force_flat", escalated=True, critical=True)
                if ok:
                    _escalate("P09", "Force-flat: NG position still open at EIA cutoff. Closed.", critical=True)
                else:
                    _escalate("P09", "Force-flat: NG position STILL OPEN — emergency flat failed", critical=True)
                results.append(result)
        except Exception:
            pass

    # ── P11: last_scan_<SYM> stale → notify (fetcher-source degradation pattern) ──
    for key, comp_state in sm.components.items():
        if key.startswith("last_scan_") and not key.startswith("_"):
            if comp_state.status == "DOWN":
                symbol = key[len("last_scan_"):]
                results.append(PlaybookResult(action_taken=f"P11_scan_stale_{symbol}"))
                _escalate("P11", f"last_scan_{symbol} stale >2× interval. Detail: {comp_state.detail}")

    # ── P12: Catch-all — unknown states → notify only ──
    has_unknown = any(
        c.status == "UNKNOWN" for k, c in sm.components.items() if not k.startswith("_")
    )
    if has_unknown:
        results.append(PlaybookResult(action_taken="P12_unknown"))
        _escalate("P12", "Unknown component state detected — manual review needed")

    # ── P13: Scan Sentinel Diagnostics ──
    try:
        db_uri = Path(BOT_DB_PATH).as_uri() + "?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        
        # Check if table sentinel_incidents exists
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sentinel_incidents'"
        ).fetchone()
        
        if table_exists:
            # Get latest unnotified sentinel incidents
            local_conn = _get_incidents_conn()
            notified_rows = local_conn.execute(
                "SELECT trigger_state FROM incidents WHERE playbook_id='P13'"
            ).fetchall()
            notified_ids = {int(r["trigger_state"]) for r in notified_rows if r["trigger_state"].isdigit()}
            local_conn.close()
            
            # Fetch incidents from nsebot.db
            db_incidents = conn.execute(
                "SELECT * FROM sentinel_incidents ORDER BY id ASC"
            ).fetchall()
            
            for incident in db_incidents:
                inc_id = incident["id"]
                if inc_id not in notified_ids:
                    # Log locally to prevent duplicate alerts
                    _log_incident(
                        playbook_id="P13",
                        trigger=str(inc_id),
                        action=incident["recommended_action"],
                        result="Alerted"
                    )
                    
                    # Send escalation
                    msg = (
                        f"🔬 **SENTINEL | {incident['symbol']} | {incident['severity']}**\n\n"
                        f"📊 **Anomaly:** {incident['summary']}\n"
                        f"🔍 **Root Cause:** {incident['root_cause']}\n"
                        f"🛠️ **Action:** {incident['recommended_action']}\n"
                    )
                    
                    is_crit = incident["severity"].upper() == "CRITICAL"
                    _escalate("P13", msg, critical=is_crit)
                    results.append(PlaybookResult(action_taken=f"P13_sentinel_{incident['symbol']}", escalated=True, critical=is_crit))
                    
        conn.close()
    except Exception as e:
        log.warning("P13: Failed to check sentinel incidents: %s", e)

    return results


# ── Healthchecks.io Ping ─────────────────────────────────────────────────────

def _ping_healthchecks() -> None:
    if not HEALTHCHECKS_URL:
        return
    try:
        urllib.request.urlopen(HEALTHCHECKS_URL, timeout=5)
    except Exception:
        pass


# ── Daily Digest ─────────────────────────────────────────────────────────────

_last_digest_date = None


def _maybe_send_daily_digest(snap: HealthSnapshot) -> None:
    global _last_digest_date
    now_ist = datetime.now(IST)
    today = now_ist.date()

    if _last_digest_date == today:
        return
    if now_ist.hour != 8 or now_ist.minute != 45:
        return

    _last_digest_date = today

    # Read overnight incidents
    try:
        conn = _get_incidents_conn()
        yesterday = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM incidents WHERE ts >= ? ORDER BY ts DESC",
            (yesterday,),
        ).fetchall()
        conn.close()
    except Exception:
        rows = []

    # Build digest
    incident_lines = []
    for r in rows[:10]:
        incident_lines.append(f"  {r['ts'][:16]} | {r['playbook_id']} | {r['action']}")
    incident_text = "\n".join(incident_lines) if incident_lines else "  None"

    health_lines = []
    for h in snap.health_rows:
        health_lines.append(f"  {h['key']}: {h['status']} ({h.get('detail', '')[:50]})")
    health_text = "\n".join(health_lines) if health_lines else "  No health data"

    hb_line = f"*Heartbeat Age:* {snap.heartbeat_age_s:.0f}s" if snap.heartbeat_age_s else "*Heartbeat Age:* N/A"
    msg = (
        f"📋 *OPS Daily Digest* — {today}\n\n"
        f"*Overnight Incidents:*\n{incident_text}\n\n"
        f"*Health State:*\n{health_text}\n\n"
        f"*Open Positions:* {snap.open_positions}\n"
        f"{hb_line}"
    )
    _send_discord(msg)


# ── Main Loop ────────────────────────────────────────────────────────────────

def main():
    log.info("OPS Agent starting (observe_only=%s, rollout=%d)", OBSERVE_ONLY, ROLLOUT_LEVEL)

    # Ensure data dir exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize incidents DB
    _get_incidents_conn().close()

    sm = StateMachine()
    loop_count = 0

    while True:
        loop_count += 1
        try:
            snap = read_health()

            # Evaluate each component
            health_by_key = {h["key"]: h for h in snap.health_rows}

            # Staleness-based checks
            in_market = _is_market_hours()
            for key, cfg in _COMPONENT_THRESHOLDS.items():
                row = health_by_key.get(key)
                # Only check shoonya_session staleness during market hours
                if key == "shoonya_session" and not in_market:
                    continue
                sm.evaluate_stale(key, row, cfg["stale_min"])

            # Check last_scan_<SYM> staleness (2× scan interval = 10 min for NSE, 30 min for MCX)
            for key, row in health_by_key.items():
                if key.startswith("last_scan_"):
                    symbol = key[len("last_scan_"):]
                    # Default stale threshold: 10 min (2× 5-min NSE interval)
                    stale_min = 10
                    # MCX symbols get 30 min (2× 15-min interval)
                    mcx_symbols = {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER", "COPPER"}
                    if symbol.upper() in mcx_symbols:
                        stale_min = 30
                    sm.evaluate_stale(key, row, stale_min)

            # Heartbeat-based scheduler check
            if not snap.heartbeat_ok:
                sm.evaluate("scheduler_loop", {"status": "DOWN", "detail": f"heartbeat stale {snap.heartbeat_age_s:.0f}s"})
            else:
                sm.evaluate("scheduler_loop", health_by_key.get("scheduler_loop"))

            # Open positions stamp
            sm.evaluate("open_positions", {"status": "OK", "detail": f"count={snap.open_positions}"})

            # Run playbooks
            results = run_playbooks(snap, sm)

            # Repeat CRITICAL alerts every 10 min until acked
            _repeat_critical_alerts()

            # Log if actions were taken
            if results:
                for r in results:
                    log.info("Playbook %s → action=%s critical=%s", r.action_taken, r.action_taken, r.critical)

            # Daily digest
            _maybe_send_daily_digest(snap)

            # Healthchecks.io ping
            _ping_healthchecks()

            if loop_count % 10 == 0:
                log.info(
                    "OPS Agent loop %d | source=%s hb=%s pos=%d components=%s",
                    loop_count,
                    snap.read_source,
                    "OK" if snap.heartbeat_ok else "STALE",
                    snap.open_positions,
                    {k: v.status for k, v in sm.components.items() if not k.startswith("_")},
                )

        except Exception as e:
            log.error("OPS Agent loop error: %s", e)

        time.sleep(60)


if __name__ == "__main__":
    main()
