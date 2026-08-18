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


def _send_alert(
    old_ip: str, new_ip: str, success: bool = False, err_msg: str | None = None
) -> None:
    try:
        from src.alerts.telegram_dispatcher import send_text

        if success:
            send_text(
                "✅ **Shoonya IP Auto-Updated** | ISP public IP moved `{}` → `{}`.\n"
                "Successfully updated Primary & Backup IP binding headlessly on Shoonya portal.\n"
                "**Shoonya fetches active and running normally.**".format(old_ip, new_ip)
            )
        else:
            err_detail = f"\nError: `{err_msg}`" if err_msg else ""
            send_text(
                "🚨 **Shoonya IP Changed** | ISP public IP moved `{}` → `{}`.{}\n"
                "Shoonya will reject login (`INVALID_IP`). **Skipping Shoonya "
                "fetches today — using fallback sources.**\n"
                "Action: bind `{}` in the Shoonya portal (trade.shoonya.com / api.shoonya.com) or "
                "contact support, then restart the bot.".format(old_ip, new_ip, err_detail, new_ip)
            )
    except Exception as exc:
        log.warning("[shoonya-ip] failed to send Telegram alert: %s", exc)


def run_daily_ip_check() -> dict:
    """Once-per-IST-day public-IP check for the Shoonya guard.

    Returns a status dict:
      {"skip": bool, "old_ip": str|None, "new_ip": str|None, "reason": str}

    - When IP rotates, attempts headless Shoonya portal IP update automatically.
    - If update succeeds: adopts new IP, clears skip flag, and alerts success.
    - If update fails: sets skip True for today and alerts failure once.
    - Fail-open: returns skip False when IP detection fails.
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

        # IP rotated — attempt headless Shoonya portal update before giving up
        old_ip = baseline
        log.warning(
            "[shoonya-ip] public IP changed %s → %s — attempting automated headless portal update",
            old_ip,
            current_ip,
        )

        update_ok = False
        update_msg = ""
        try:
            from src.fetchers.shoonya_ip_updater import update_shoonya_portal_ip

            update_ok, update_msg = update_shoonya_portal_ip(new_ip=current_ip, headless=True)
        except Exception as upd_exc:
            update_msg = str(upd_exc)
            log.exception("[shoonya-ip] error executing shoonya_ip_updater: %s", upd_exc)

        if update_ok:
            # Successfully updated Shoonya portal binding
            state.update(baseline_ip=current_ip, checked_date=today, skip_date=None)
            _save_state(state)
            log.info(
                "[shoonya-ip] Shoonya portal IP updated successfully to %s. Resuming fetches.",
                current_ip,
            )
            _send_alert(old_ip, current_ip, success=True)
            return {"skip": False, "old_ip": old_ip, "new_ip": current_ip, "reason": "ip_auto_updated"}

        # Auto-update failed — alert once and skip Shoonya for today
        state.update(baseline_ip=current_ip, checked_date=today, skip_date=today)
        _save_state(state)
        log.warning(
            "[shoonya-ip] Shoonya portal IP auto-update failed (%s) — skipping Shoonya today",
            update_msg,
        )
        _send_alert(old_ip, current_ip, success=False, err_msg=update_msg)
        return {"skip": True, "old_ip": old_ip, "new_ip": current_ip, "reason": f"ip_auto_update_failed: {update_msg}"}


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


def reset_shoonya_ip_skip(new_ip: str | None = None) -> None:
    """Clear today's skip flag when user updates IP binding or login succeeds."""
    with _lock:
        state = _load_state()
        state["skip_date"] = None
        if new_ip:
            state["baseline_ip"] = new_ip
            state["checked_date"] = _today()
        _save_state(state)
        log.info("[shoonya-ip] IP skip flag reset/cleared (baseline=%s).", state.get("baseline_ip"))
