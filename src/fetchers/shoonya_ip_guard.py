"""
Shoonya IP-change guard.

Shoonya validates the request's source IP at login (GenAcsTok); a rotating
ISP public IP (e.g. a 3–4 day DHCP lease) causes `Invalid Input : INVALID_IP`
and a wasted ~60s Playwright OAuth attempt on every login.

On the FIRST Shoonya attempt of each IST day this guard compares the current
public IP against the baseline that last worked. If it changed:
  * sends a Telegram alert once (old → new IP), and
  * marks Shoonya as skipped for the rest of the day, so the router falls
    through to fallback fetchers instead of a doomed login.

Fail-open: if the current IP can't be determined we do NOT block Shoonya —
the normal login + router fallback path handles it. State persists across
restarts so the skip window survives a process bounce within the same day.

Reuses src.utils.ip_monitor for public-IP detection (hardened provider chain,
public-IPv4 validation) and keeps its own small state file
(data/shoonya_ip_state.json) for the once-per-day baseline + skip window so
the two monitors stay independent.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.utils import ip_monitor

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Overridable for tests via SHOONYA_IP_STATE_PATH; runtime default sits next
# to the existing ip_monitor state (data/ip_state.json).
STATE_PATH = Path(
    os.environ.get(
        "SHOONYA_IP_STATE_PATH",
        str(ip_monitor.DATA_DIR / "shoonya_ip_state.json"),
    )
)

# Bounded detection for the synchronous once-per-day check — we don't want to
# block the router for the full ip_monitor chain (5 providers × retries × 10s).
_FAST_IP_TIMEOUT_S = 3.0
_FAST_IP_MAX_PROVIDERS = 2
_FAST_IP_RETRIES = 1

_lock = threading.Lock()


def _today() -> str:
    """IST date string (YYYY-MM-DD) — the day-boundary for the daily check."""
    return datetime.now(IST).strftime("%Y-%m-%d")


def _load_state() -> dict:
    defaults = {"baseline_ip": None, "checked_date": None, "skip_date": None}
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in ("baseline_ip", "checked_date", "skip_date"):
                    if k in data:
                        defaults[k] = data[k]
    except Exception as exc:
        log.warning("[shoonya-ip] failed to read state %s: %s", STATE_PATH, exc)
    return defaults


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as exc:
        log.warning("[shoonya-ip] failed to write state %s: %s", STATE_PATH, exc)


def _send_alert(old_ip: str, new_ip: str) -> None:
    try:
        from src.alerts.telegram_dispatcher import send_text

        send_text(
            "🚨 **Shoonya IP Changed** | ISP public IP moved `{}` → `{}`.\n"
            "Shoonya will reject login (`INVALID_IP`). **Skipping Shoonya "
            "fetches today — using fallback sources.**\n"
            "Action: bind `{}` in the Shoonya portal (api.shoonya.com) or "
            "contact support, then restart the bot.".format(old_ip, new_ip, new_ip)
        )
    except Exception as exc:
        log.warning("[shoonya-ip] failed to send Telegram alert: %s", exc)


def run_daily_ip_check() -> dict:
    """Once-per-IST-day public-IP check for the Shoonya guard.

    Returns a status dict:
      {"skip": bool, "old_ip": str|None, "new_ip": str|None, "reason": str}

    - skip is True only on the day a rotation was detected (alerted once).
    - Fail-open: returns skip False when detection fails.
    - Thread-safe; re-entrant calls on the same day return the cached decision
      without re-fetching or re-alerting.
    """
    with _lock:
        state = _load_state()
        today = _today()

        if state.get("checked_date") == today:
            # Already decided today — return the cached decision.
            return {
                "skip": state.get("skip_date") == today,
                "old_ip": state.get("baseline_ip"),
                "new_ip": None,
                "reason": "cached",
            }

        current_ip = ip_monitor._fetch_public_ip(
            timeout=_FAST_IP_TIMEOUT_S,
            max_providers=_FAST_IP_MAX_PROVIDERS,
            retries=_FAST_IP_RETRIES,
        )
        if not current_ip:
            log.warning(
                "[shoonya-ip] could not determine public IP — fail-open (Shoonya not blocked)"
            )
            return {"skip": False, "old_ip": None, "new_ip": None, "reason": "ip_detect_failed"}

        baseline = state.get("baseline_ip")
        if not baseline or not ip_monitor._is_valid_public_ipv4(str(baseline)):
            # First run — adopt the current IP as baseline, no alert/skip.
            state.update(baseline_ip=current_ip, checked_date=today, skip_date=None)
            _save_state(state)
            log.info("[shoonya-ip] initial IP baseline adopted: %s", current_ip)
            return {"skip": False, "old_ip": None, "new_ip": current_ip, "reason": "initial_adopt"}

        if current_ip == baseline:
            state.update(baseline_ip=current_ip, checked_date=today, skip_date=None)
            _save_state(state)
            log.debug("[shoonya-ip] public IP unchanged: %s", current_ip)
            return {"skip": False, "old_ip": baseline, "new_ip": current_ip, "reason": "unchanged"}

        # IP rotated — alert once and skip Shoonya for the rest of today.
        old_ip = baseline
        state.update(baseline_ip=current_ip, checked_date=today, skip_date=today)
        _save_state(state)
        log.warning(
            "[shoonya-ip] public IP changed %s → %s — skipping Shoonya today",
            old_ip,
            current_ip,
        )
        _send_alert(old_ip, current_ip)
        return {"skip": True, "old_ip": old_ip, "new_ip": current_ip, "reason": "ip_changed"}


def shoonya_should_skip() -> bool:
    """Network-free fast check for the router / login.

    True when today's once-per-day check detected an IP rotation (skip Shoonya
    → fallback). Lazy: if today's check hasn't run yet (e.g. `--once` or a
    process that started mid-day), runs the bounded daily check synchronously
    first so the first scan of the day verifies the IP before Shoonya.
    """
    state = _load_state()
    today = _today()
    if state.get("checked_date") != today:
        return bool(run_daily_ip_check().get("skip"))
    return state.get("skip_date") == today
