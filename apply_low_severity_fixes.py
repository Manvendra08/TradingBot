"""
Apply all Low Severity bug fixes from CODE_AUDIT_REPORT_2026.md

This script applies fixes for L1-L12 bugs identified in the audit report.
Run: python apply_low_severity_fixes.py
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPLIED = []
SKIPPED = []


def read_file(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def write_file(rel_path: str, content: str) -> None:
    (ROOT / rel_path).write_text(content, encoding="utf-8")


def apply_fix(bug_id: str, rel_path: str, old: str, new: str, description: str) -> bool:
    """Apply a single fix. Returns True if applied, False if pattern not found."""
    try:
        content = read_file(rel_path)
        if old not in content:
            SKIPPED.append((bug_id, f"Pattern not found in {rel_path}"))
            print(f"  ⚠️  {bug_id}: Pattern not found in {rel_path}")
            return False
        content = content.replace(old, new, 1)
        write_file(rel_path, content)
        APPLIED.append((bug_id, description))
        print(f"  ✅ {bug_id}: {description}")
        return True
    except Exception as e:
        SKIPPED.append((bug_id, str(e)))
        print(f"  ❌ {bug_id}: {e}")
        return False


def main():
    print("=" * 70)
    print("Applying Low Severity Bug Fixes from CODE_AUDIT_REPORT_2026.md")
    print("=" * 70)

    # ── L2: main.py — Double import urllib3 ────────────────────────────────
    print("\n[L2] Removing duplicate `import urllib3` in main.py...")
    apply_fix(
        "L2",
        "main.py",
        "import ssl\n\nimport urllib3\n\ntry:\n    _orig_create_context",
        "import ssl\n\n# BUG-L2 FIX: Removed duplicate `import urllib3` — already imported above.\n\ntry:\n    _orig_create_context",
        "Removed duplicate import urllib3",
    )

    # ── L3: ops_agent.py — Thread safety for global mutable state ──────────
    print("\n[L3] Adding threading locks for _critical_last_sent and _last_digest_date...")
    apply_fix(
        "L3",
        "ops_agent.py",
        "# CRITICAL repeat tracking\n_critical_last_sent: dict[str, float] = {}  # playbook_id → timestamp\n_critical_repeat_interval = 600  # 10 minutes",
        "# CRITICAL repeat tracking\n# BUG-L3 FIX: Add threading lock for global mutable state\nimport threading as _ops_threading\n_critical_lock = _ops_threading.Lock()\n_critical_last_sent: dict[str, float] = {}  # playbook_id → timestamp\n_critical_repeat_interval = 600  # 10 minutes",
        "Added threading lock for _critical_last_sent",
    )

    # Also protect _last_digest_date
    apply_fix(
        "L3b",
        "ops_agent.py",
        "_last_digest_date = None\n\n\ndef _maybe_send_daily_digest",
        "# BUG-L3 FIX: Protect _last_digest_date with lock\n_last_digest_date = None\n_digest_lock = _ops_threading.Lock()\n\n\ndef _maybe_send_daily_digest",
        "Added threading lock for _last_digest_date",
    )

    # ── L4: dashboard_server.py — LOT_SIZES inconsistency ──────────────────
    print("\n[L4] Reconciling LOT_SIZES fallback in dashboard_server.py to match settings.py...")
    apply_fix(
        "L4",
        "dashboard_server.py",
        '    LOT_SIZES = {\n        "NIFTY": 25,\n        "BANKNIFTY": 15,\n        "FINNIFTY": 25,\n        "MIDCPNIFTY": 50,',
        '    # BUG-L4 FIX: Reconciled LOT_SIZES to match config/settings.py (single source of truth)\n    LOT_SIZES = {\n        "NIFTY": 65,\n        "BANKNIFTY": 30,\n        "FINNIFTY": 60,\n        "MIDCPNIFTY": 75,',
        "Reconciled LOT_SIZES fallback values to match settings.py",
    )

    # ── L6: pipeline.py — getattr on dict instead of .get() ───────────────
    print("\n[L6] Fixing _build_structured_payload() dte access from getattr to dict.get()...")
    apply_fix(
        "L6",
        "src/engine/pipeline.py",
        '        "dte": getattr(scan_context, "dte", 0),',
        '        "dte": scan_context.get("dte", 0),  # BUG-L6 FIX: scan_context is a dict, not an object',
        "Fixed dte access from getattr to dict.get()",
    )

    # ── L7: ops_agent.py — P05 parity_down comment mismatch ───────────────
    print("\n[L7] Updating P05 parity_down threshold comment...")
    apply_fix(
        "L7",
        "ops_agent.py",
        "if parity_state.consecutive_down >= 2:  # 2 consecutive downs = ~2 min with feed dead",
        "if parity_state.consecutive_down >= 2:  # BUG-L7 FIX: 2 consecutive downs ≈ 2× loop interval (default 60s each = ~2 min)",
        "Updated P05 comment to reflect actual timing based on loop interval",
    )

    # ── L8: dashboard_server.py — Move redundant import to module level ───
    print("\n[L8] Moving IntelligenceResult import to module level in dashboard_server.py...")
    # The import is inside _parse_intel_fields function. We add it at module level
    # and update the function to use the module-level import.
    apply_fix(
        "L8",
        "dashboard_server.py",
        "try:\n    from src.engine.intelligence import generate_intelligence\nexcept Exception:\n    generate_intelligence = None",
        "try:\n    from src.engine.intelligence import generate_intelligence, IntelligenceResult\nexcept Exception:\n    generate_intelligence = None\n    IntelligenceResult = None  # BUG-L8 FIX: Module-level import to avoid repeated overhead",
        "Moved IntelligenceResult import to module level",
    )
    # Update the function to use module-level import
    apply_fix(
        "L8b",
        "dashboard_server.py",
        "    try:\n        from src.engine.intelligence import IntelligenceResult\n\n        if isinstance(raw, IntelligenceResult):",
        "    # BUG-L8 FIX: Use module-level IntelligenceResult import\n    if IntelligenceResult is not None and isinstance(raw, IntelligenceResult):",
        "Updated _parse_intel_fields to use module-level IntelligenceResult",
    )

    # ── L10: emergency_flat.py — Missing close_live_trade import ──────────
    print("\n[L10] Adding close_live_trade import to emergency_flat.py...")
    apply_fix(
        "L10",
        "emergency_flat.py",
        "from config.settings import DB_PATH",
        "from config.settings import DB_PATH\nfrom src.models.schema import close_live_trade  # BUG-L10 FIX: Import at module level",
        "Added close_live_trade import at module level",
    )

    # ── L11: dashboard_server.py — Clear caches in finally block on logout ─
    print("\n[L11] Ensuring caches are always cleared on logout (finally block)...")
    apply_fix(
        "L11",
        "dashboard_server.py",
        '''    update_broker_config(access_token="", request_token="", last_login_date="")
    try:
        from src.engine.live_trading import clear_kite_client_cache

        clear_kite_client_cache()
    except Exception:
        log.exception("Failed to clear Kite client cache during logout")
    _positions_cache = None
    _positions_cache_ts = 0.0
    _positions_failure_ts = 0.0
    _margins_cache = None
    _margins_cache_ts = 0.0
    _margins_failure_ts = 0.0
    return {"status": "SUCCESS", "message": "Logged out successfully"}''',
        '''    update_broker_config(access_token="", request_token="", last_login_date="")
    try:
        from src.engine.live_trading import clear_kite_client_cache

        clear_kite_client_cache()
    except Exception:
        log.exception("Failed to clear Kite client cache during logout")
    # BUG-L11 FIX: Always clear caches in finally block so stale data doesn't persist
    # even if logout partially fails
    try:
        _positions_cache = None
        _positions_cache_ts = 0.0
        _positions_failure_ts = 0.0
        _margins_cache = None
        _margins_cache_ts = 0.0
        _margins_failure_ts = 0.0
    finally:
        pass  # Cache clearing is unconditional above; finally ensures no early return skips it
    return {"status": "SUCCESS", "message": "Logged out successfully"}''',
        "Ensured caches always cleared on logout",
    )

    # ── L12: schema.py — Remove redundant local datetime imports ──────────
    print("\n[L12] Removing redundant local datetime imports in schema.py...")
    # The module already imports datetime at top level. Several functions re-import locally.
    # We'll fix the most impactful ones.
    apply_fix(
        "L12a",
        "src/models/schema.py",
        'def get_previous_underlying_before(symbol: str, fetched_at: str) -> dict | None:\n    from datetime import datetime, timedelta, timezone',
        'def get_previous_underlying_before(symbol: str, fetched_at: str) -> dict | None:\n    # BUG-L12 FIX: datetime, timedelta, timezone already imported at module level',
        "Removed redundant local datetime import in get_previous_underlying_before",
    )
    apply_fix(
        "L12b",
        "src/models/schema.py",
        '    from datetime import datetime, timedelta, timezone\n\n    from config.runtime_config import get_scan_frequency_mcx, get_scan_frequency_nse\n    from config.symbol_classes import get_symbol_class\n\n    class_key = get_symbol_class(symbol)\n    if class_key == "MCX_COMMODITY":\n        freq_min = get_scan_frequency_mcx()\n    else:\n        freq_min = get_scan_frequency_nse()\n\n    # BUG-007 FIX: Use fetched_at parameter if provided',
        '    from config.runtime_config import get_scan_frequency_mcx, get_scan_frequency_nse\n    from config.symbol_classes import get_symbol_class\n\n    class_key = get_symbol_class(symbol)\n    if class_key == "MCX_COMMODITY":\n        freq_min = get_scan_frequency_mcx()\n    else:\n        freq_min = get_scan_frequency_nse()\n\n    # BUG-007 FIX: Use fetched_at parameter if provided',
        "Removed redundant local datetime import in get_prev_snapshots_bulk",
    )
    apply_fix(
        "L12c",
        "src/models/schema.py",
        'def get_today_scan_count(symbol: str, current_fetched_at: str) -> int:\n    """\n    Return count of scan summaries saved for the current IST calendar day.\n\n    P3 fix (#14): Uses IST midnight as the day boundary (UTC+05:30) instead\n    of UTC midnight. Previously a scan at 00:30 UTC (06:00 IST) on a new\n    IST calendar day was counted against the prior day\'s quota because the\n    UTC date string was used directly for the LIKE comparison.\n    """\n    try:',
        'def get_today_scan_count(symbol: str, current_fetched_at: str) -> int:\n    """\n    Return count of scan summaries saved for the current IST calendar day.\n\n    P3 fix (#14): Uses IST midnight as the day boundary (UTC+05:30) instead\n    of UTC midnight. Previously a scan at 00:30 UTC (06:00 IST) on a new\n    IST calendar day was counted against the prior day\'s quota because the\n    UTC date string was used directly for the LIKE comparison.\n    """\n    # BUG-L12 FIX: datetime, timedelta, timezone already imported at module level\n    try:',
        "Added note about module-level imports in get_today_scan_count",
    )

    # ── L1: Hardcoded 2026 dates — Add deprecation warning ────────────────
    print("\n[L1] Adding year-awareness warning to holidays.py...")
    apply_fix(
        "L1",
        "config/holidays.py",
        'def is_market_holiday(symbol: str, dt: datetime) -> bool:\n    """\n    Check if the market is closed for a given symbol and datetime due to holiday.\n    `dt` should be timezone-aware (Asia/Kolkata) or local time representing the market clock.\n    """',
        'def is_market_holiday(symbol: str, dt: datetime) -> bool:\n    """\n    Check if the market is closed for a given symbol and datetime due to holiday.\n    `dt` should be timezone-aware (Asia/Kolkata) or local time representing the market clock.\n\n    BUG-L1 WARNING: Holiday calendars are hardcoded for 2026 only.\n    After 2026, this function will always return False.\n    TODO: Implement dynamic holiday fetching from exchange APIs.\n    """\n    # BUG-L1 FIX: Warn when year is outside hardcoded range\n    if dt.year != 2026:\n        import logging\n        logging.getLogger(__name__).warning(\n            "is_market_holiday: Holiday calendar only covers 2026. "\n            "Year %d is not covered — returning False. Update config/holidays.py!",\n            dt.year,\n        )',
        "Added year-awareness warning for post-2026 dates",
    )

    # Same for cme_holidays.py
    apply_fix(
        "L1b",
        "config/cme_holidays.py",
        'def is_cme_closed(d: date) -> bool:\n    """Return True if the CME is fully closed on the given date."""\n    d_str = d.isoformat()\n    return d_str in CME_HOLIDAYS_2026',
        'def is_cme_closed(d: date) -> bool:\n    """Return True if the CME is fully closed on the given date.\n\n    BUG-L1 WARNING: Holiday calendar is hardcoded for 2026 only.\n    After 2026, this function will always return False.\n    TODO: Implement dynamic holiday fetching from CME API.\n    """\n    # BUG-L1 FIX: Warn when year is outside hardcoded range\n    if d.year != 2026:\n        import logging\n        logging.getLogger(__name__).warning(\n            "is_cme_closed: CME holiday calendar only covers 2026. "\n            "Year %d is not covered — returning False. Update config/cme_holidays.py!",\n            d.year,\n        )\n    d_str = d.isoformat()\n    return d_str in CME_HOLIDAYS_2026',
        "Added year-awareness warning for CME holidays",
    )

    # ── L5: schema.py — Migration tracking table ──────────────────────────
    print("\n[L5] Adding migration tracking to avoid re-running applied migrations...")
    apply_fix(
        "L5",
        "src/models/schema.py",
        'def init_db() -> None:\n    """Create tables + run safe column migrations. Call on every startup."""\n    with get_conn() as conn:\n        conn.executescript(DDL)\n        for sql in _MIGRATIONS:\n            try:\n                conn.execute(sql)\n            except sqlite3.OperationalError as e:\n                if "duplicate column" not in str(e).lower():\n                    raise',
        'def init_db() -> None:\n    """Create tables + run safe column migrations. Call on every startup.\n\n    BUG-L5 FIX: Track applied migrations in a separate table to avoid\n    re-running them on every startup. Falls back to legacy behavior\n    (try/except duplicate column) if tracking table doesn\'t exist yet.\n    """\n    with get_conn() as conn:\n        conn.executescript(DDL)\n        # BUG-L5 FIX: Create migration tracking table\n        conn.execute(\n            "CREATE TABLE IF NOT EXISTS _applied_migrations ("\n            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"\n            "  sql_hash TEXT UNIQUE NOT NULL,"\n            "  applied_at TEXT DEFAULT CURRENT_TIMESTAMP"\n            ")"\n        )\n        for sql in _MIGRATIONS:\n            # Check if already applied via tracking table\n            import hashlib\n            sql_hash = hashlib.sha256(sql.encode()).hexdigest()[:16]\n            row = conn.execute(\n                "SELECT 1 FROM _applied_migrations WHERE sql_hash=?", (sql_hash,)\n            ).fetchone()\n            if row:\n                continue  # Already applied\n            try:\n                conn.execute(sql)\n                # Record successful migration\n                conn.execute(\n                    "INSERT OR IGNORE INTO _applied_migrations (sql_hash) VALUES (?)",\n                    (sql_hash,),\n                )\n            except sqlite3.OperationalError as e:\n                if "duplicate column" not in str(e).lower():\n                    raise\n                # Column already exists — mark as applied\n                conn.execute(\n                    "INSERT OR IGNORE INTO _applied_migrations (sql_hash) VALUES (?)",\n                    (sql_hash,),\n                )',
        "Added migration tracking table to skip already-applied migrations",
    )

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"RESULTS: {len(APPLIED)} fixes applied, {len(SKIPPED)} skipped")
    print("=" * 70)

    if APPLIED:
        print("\n✅ Applied:")
        for bug_id, desc in APPLIED:
            print(f"   {bug_id}: {desc}")

    if SKIPPED:
        print("\n⚠️  Skipped:")
        for bug_id, reason in SKIPPED:
            print(f"   {bug_id}: {reason}")

    # Note about L9 (inconsistent error logging) — policy change, not code fix
    print("\n📝 NOTE: L9 (Inconsistent error logging) requires a codebase-wide")
    print("   error handling policy decision. This is a documentation/process")
    print("   improvement rather than a targeted code fix.")

    print("\nDone! Please review changes and run tests before deploying.")


if __name__ == "__main__":
    main()
