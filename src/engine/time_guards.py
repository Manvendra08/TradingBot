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


def is_trading_allowed_now(symbol: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)``.

    ``allowed=True``  — no guard is active; entry may proceed.
    ``allowed=False`` — ``reason`` describes which guard tripped.

    All checks are lightweight (clock reads + optional runtime-config
    lookup) and never raise; any unexpected error returns ``True``
    (allow) to avoid silently blocking the engine.
    """
    try:
        now = datetime.now(IST)
        h, m = now.hour, now.minute
        sym  = str(symbol).upper().split()[0]  # "NIFTY 50" → "NIFTY"

        # ── Window 1: Opening auction noise 09:15–09:30 IST ─────────────────
        if (h, m) >= (9, 15) and (h, m) <= (9, 30):
            return False, "Opening auction noise window (09:15–09:30 IST)"

        # ── Window 2: Expiry end-of-session 15:00–15:30 IST ─────────────────
        if (h, m) >= (15, 0) and (h, m) <= (15, 30):
            return False, "Expiry end-of-session window (15:00–15:30 IST)"

        # ── Window 3: EIA report ±15 min (NATURALGAS, every Thursday) ────────
        if sym in ("NATURALGAS", "NATGAS", "CRUDEOIL"):
            # EIA also moves CRUDEOIL via energy-complex correlation
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

        return True, ""

    except Exception as exc:
        log.debug("time_guards.is_trading_allowed_now: unexpected error (%s) — allowing", exc)
        return True, ""
