"""
Hard time guards for trade entry decisions.

Blocks entries during known high-noise / high-gamma windows where spreads
widen, gamma risk explodes, or scheduled macro events distort OI signals.

Guarded windows
---------------
All symbols:
  - 09:15–09:30 IST  Opening auction noise — bid/ask spreads 3-5× normal.
  - 15:00–15:30 IST  Expiry end-of-session — MMs widen quotes aggressively.

NATURALGAS / NATGAS:
  - Thursday 19:45–20:15 IST  EIA Weekly Natural Gas Storage Report (±15 min).

BANKNIFTY:
  - ±5 min of any RBI announcement time injected via runtime_config key
    "rbi_announcement_time" (format "HH:MM" IST).  When the key is absent
    no block is applied; set it on RBI meeting days as needed.

Usage
-----
    from src.engine.time_guards import is_trading_allowed_now

    allowed, reason = is_trading_allowed_now(symbol)
    if not allowed:
        return _blocked(f"Time guard: {reason}")
"""
from __future__ import annotations

import logging
from datetime import datetime

import pytz

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# EIA Natural Gas Storage Report — every Thursday, 20:00 IST
_EIA_WEEKDAY = 3    # Thursday (0=Monday)
_EIA_HOUR    = 20
_EIA_MINUTE  = 0
_EIA_WINDOW  = 15   # minutes either side of the announcement


def is_trading_allowed_now(symbol: str, expiry_str: str | None = None) -> tuple[bool, str]:
    """Return ``(allowed, reason)``.

    ``allowed=True``  — no guard is active; entry may proceed.
    ``allowed=False`` — ``reason`` describes which guard tripped.

    All checks are lightweight (clock reads + optional runtime-config
    lookup) and never raise; any unexpected error returns ``True``
    (allow) to avoid silently blocking the engine.
    """
    try:
        from config.settings import MCX_SYMBOLS
        now = datetime.now(IST)
        h, m = now.hour, now.minute
        sym  = str(symbol).upper().split()[0]  # "NIFTY 50" → "NIFTY"
        is_mcx = sym in MCX_SYMBOLS or sym in ("NATURALGAS", "NATGAS", "CRUDEOIL", "GOLD", "SILVER")

        # ── Window 0: CME (NYMEX) holidays (NATURALGAS, CRUDEOIL) ────────────
        if sym in ("NATURALGAS", "NATGAS", "CRUDEOIL"):
            from config.cme_holidays import is_cme_closed, is_cme_early_close
            if is_cme_closed(now.date()):
                return False, "CME holiday — no NYMEX price discovery, MCX zombie session"
            if is_cme_early_close(now.date()) and (h, m) >= (17, 30):
                return False, "CME early close — no NYMEX price discovery after 17:30 IST"

        # ── Window 1: Opening auction noise 09:15–09:30 IST ─────────────────
        if (h, m) >= (9, 15) and (h, m) < (9, 30):
            return False, "Opening auction noise window (09:15–09:30 IST)"

        # ── Window 2: Expiry end-of-session 15:00–15:30 IST ─────────────────
        if not is_mcx:
            if (h, m) >= (15, 0) and (h, m) <= (15, 30):
                return False, "Expiry end-of-session window (15:00–15:30 IST)"

        # ── Window 3: EIA report ±15 min (NATURALGAS Thursday, CRUDEOIL Wednesday) ────────
        if sym in ("NATURALGAS", "NATGAS"):
            if now.weekday() == _EIA_WEEKDAY:
                eia_min = _EIA_HOUR * 60 + _EIA_MINUTE
                now_min = h * 60 + m
                if abs(now_min - eia_min) <= _EIA_WINDOW:
                    return (
                        False,
                        f"EIA Natural Gas Storage Report window "
                        f"(Thu {_EIA_HOUR:02d}:{_EIA_MINUTE:02d} IST "
                        f"±{_EIA_WINDOW} min)",
                    )
        elif sym == "CRUDEOIL":
            if now.weekday() == 2:  # Wednesday
                eia_min = _EIA_HOUR * 60 + _EIA_MINUTE
                now_min = h * 60 + m
                if abs(now_min - eia_min) <= _EIA_WINDOW:
                    return (
                        False,
                        f"EIA Weekly Petroleum Status Report window "
                        f"(Wed {_EIA_HOUR:02d}:{_EIA_MINUTE:02d} IST "
                        f"±{_EIA_WINDOW} min)",
                    )

        # ── Window 4: RBI announcement ±5 min (BANKNIFTY) ────────────────────
        if sym == "BANKNIFTY":
            try:
                from config.runtime_config import load_runtime_config
                rconf = load_runtime_config()
                rbi_time_str: str | None = rconf.get("rbi_announcement_time")
                if rbi_time_str:
                    rh, rm = map(int, rbi_time_str.strip().split(":"))
                    rbi_min = rh * 60 + rm
                    now_min = h * 60 + m
                    if abs(now_min - rbi_min) <= 5:
                        return (
                            False,
                            f"RBI announcement window "
                            f"({rbi_time_str} IST ±5 min)",
                        )
            except Exception:
                pass  # runtime-config failure → do not block

        # ── Window 5: Expiry day trading cutoff ──────────────────────────────
        if expiry_str:
            try:
                expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                if expiry_date == now.date():
                    if is_mcx:
                        # MCX cutoff: 8:00 pm IST (20:00)
                        if (h, m) >= (20, 0):
                            return False, f"Expiry day trading cutoff (after 20:00 IST for MCX on expiry day)"
                    else:
                        # NSE/BSE cutoff: 2:30 pm IST (14:30)
                        if (h, m) >= (14, 30):
                            return False, f"Expiry day trading cutoff (after 14:30 IST for NSE/BSE on expiry day)"
            except Exception as parse_exc:
                log.warning("time_guards: failed to parse expiry date %s (%s)", expiry_str, parse_exc)

        return True, ""

    except Exception as exc:
        log.warning("time_guards.is_trading_allowed_now: unexpected error (%s) — allowing", exc)
        return True, ""
