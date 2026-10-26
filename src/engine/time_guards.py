"""
Hard time guards for trade entry decisions.

Blocks entries during known high-noise / high-gamma windows where spreads
widen, gamma risk explodes, or scheduled macro events distort OI signals.

Guarded windows
---------------
F&O Pre-Open Auction (09:00–09:15 IST):
  - 09:00–09:08 IST: Order entry, modification, and cancellation window (random close ~09:07–09:08).
  - 09:08–09:12 IST: Order matching & opening price discovery.
  - 09:12–09:15 IST: Buffer period before normal continuous trading.

All symbols:
  - 09:15–09:30 IST  Opening auction noise — bid/ask spreads 3-5× normal.


Index F&O (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX) & Stock F&O:
  - 15:15 IST: Continuous regular trading for F&O-eligible stocks in the cash market ends.
  - 15:15–15:20 IST: CAS Transition period — orders frozen, reference prices published, out-of-bound orders canceled.
  - 15:20–15:25 IST: CAS Order Entry Session 1 — both market and limit orders permitted.
  - 15:25–15:30 IST: CAS Order Entry Session 2 — limit orders only; closes randomly between 15:28 and 15:30.
  - 15:30–15:35 IST: CAS Order matching at single equilibrium price (declared as official cash closing price).
  - 15:40 IST: Derivative F&O contracts continue trading until 15:40 IST (+10 min past old 15:30 close for hedging/adjustments).

Non-F&O Stocks:
  - Unaffected by CAS; continuous normal trading continues until 15:30 IST.


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

# Symbols that trade until 15:40 IST (index F&O, effective Aug 3 2026)
INDEX_FNO_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"})

US_EASTERN = pytz.timezone("America/New_York")

# EIA Storage Reports & MCX Trading Session Hours (DST-aware)
_EIA_WEEKDAY = 3    # Thursday (0=Monday)
_EIA_WINDOW  = 15   # minutes either side of the announcement

def get_mcx_close_time(dt: datetime | None = None) -> tuple[int, int]:
    """Return MCX close time (hour, minute) in IST.
    
    MCX extends trading hours during US Standard Time (winter):
    - US DST active (summer): 23:30 IST
    - US Standard Time (winter): 23:55 IST
    """
    if dt is None:
        dt = datetime.now(IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    et_dt = dt.astimezone(US_EASTERN)
    is_dst = bool(et_dt.dst())
    return (23, 30) if is_dst else (23, 55)

def get_eia_announcement_time(dt: datetime | None = None) -> tuple[int, int]:
    """Return EIA storage report release time (hour, minute) in IST.
    
    EIA reports are released at 10:30 AM US Eastern Time:
    - US DST (summer): 20:00 IST (10:30 EDT)
    - US Standard Time (winter): 21:00 IST (10:30 EST)
    """
    if dt is None:
        dt = datetime.now(IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    et_dt = dt.astimezone(US_EASTERN)
    is_dst = bool(et_dt.dst())
    return (20, 0) if is_dst else (21, 0)


def _parse_expiry_date(expiry_str: str | None) -> "date | None":
    """Tolerantly parse an expiry string into a date.

    Accepts: "YYYY-MM-DD" (optionally followed by a time component), and
    common Kite-style formats like "22Oct2026" / "22-Oct-2026".
    Returns ``None`` (with a warning log) on unparsable input so callers'
    expiry-day windows degrade predictably instead of raising.
    """
    if not expiry_str:
        return None
    s = str(expiry_str).strip()
    if not s:
        return None
    if len(s) > 10:  # e.g. "2026-10-22T09:15:00+05:30"
        s = s[:10]
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    log.warning(
        "time_guards: unparseable expiry %r — expiry-day windows will be skipped",
        expiry_str,
    )
    return None


def is_trading_allowed_now(symbol: str, expiry_str: str | None = None, operation: str = "ENTRY") -> tuple[bool, str]:
    """Return ``(allowed, reason)``.

    ``allowed=True``  — no guard is active; action may proceed.
    ``allowed=False`` — ``reason`` describes which guard tripped.

    operation : "ENTRY" | "EXIT". Entry cutoffs are strictly enforced.
    Exit operations (like Friday risk square-offs or broker-level closes)
    are permitted until market session close (23:30 IST on MCX, 15:40 on NSE F&O).
    """
    try:
        from config.settings import MCX_SYMBOLS
        now = datetime.now(IST)
        h, m = now.hour, now.minute
        sym  = str(symbol).upper().split()[0]  # "NIFTY 50" → "NIFTY"
        is_mcx = sym in MCX_SYMBOLS or sym in ("NATURALGAS", "NATGAS", "CRUDEOIL", "GOLD", "SILVER")
        is_index_fno = sym in INDEX_FNO_SYMBOLS

        # ── Window -1: Macro Event Shock Circuit Breaker (Jev System-1) ───────
        if operation != "EXIT":
            try:
                from src.engine.event_shock import is_event_shock_active
                shock_active, shock_reason = is_event_shock_active(sym)
                if shock_active:
                    return False, f"Event Shock Circuit Breaker: {shock_reason}"
            except Exception:
                pass

        # ── Window 0: CME (NYMEX) holidays (NATURALGAS, CRUDEOIL) ────────────
        if operation != "EXIT" and sym in ("NATURALGAS", "NATGAS", "CRUDEOIL"):
            from config.cme_holidays import is_cme_closed, is_cme_early_close
            if is_cme_closed(now.date()):
                return False, "CME holiday — no NYMEX price discovery, MCX zombie session"
            if is_cme_early_close(now.date()) and (h, m) >= (17, 30):
                return False, "CME early close — no NYMEX price discovery after 17:30 IST"

        # ── Window 1: Opening auction noise (NSE/BSE 09:15–09:30, MCX 09:00–09:15 IST) ───
        if operation != "EXIT":
            if not is_mcx and (h, m) >= (9, 15) and (h, m) < (9, 30):
                return False, "Opening auction noise window (09:15–09:30 IST)"
            if is_mcx and (h, m) >= (9, 0) and (h, m) < (9, 15):
                return False, "MCX opening auction noise window (09:00–09:15 IST)"

        # ── Window 2: Expiry end-of-session guard ────────────────────────────
        # Entries blocked ahead of the CAS wind-down on expiry day. EXIT
        # operations are NEVER blocked here: protective square-offs
        # (Friday weekend-risk sweep 15:35–15:40 IST, SL/target exits) must
        # stay executable right up to the session close; Window 5 applies the
        # hard post-close cutoff for exits.
        if not is_mcx and expiry_str:
            expiry_date = _parse_expiry_date(expiry_str)
            if expiry_date is not None and expiry_date == now.date():
                if is_index_fno:
                    # Index F&O: entries blocked 15:25–15:40; exits permitted
                    # until the 15:40 session close.
                    if operation != "EXIT" and (h, m) >= (15, 25) and (h, m) < (15, 40):
                        return False, f"Index F&O expiry end-of-session window (15:25–15:40 IST) for expiry {expiry_str}"
                else:
                    # F&O stocks: continuous stops 15:15, CAS 15:15–15:40
                    # Other NSE stocks: close 15:30, blocked 15:15–15:30
                    # (No clean symbol-level distinction here; treat F&O stocks as
                    #  the dominant path and keep the wider 15:15–15:40 window.)
                    if operation != "EXIT" and (h, m) >= (15, 15) and (h, m) <= (15, 40):
                        return False, f"F&O expiry end-of-session window (15:15–15:40 IST) for expiry {expiry_str}"

        # ── Window 3: EIA report ±15 min (NATURALGAS Thursday, CRUDEOIL Wednesday) ────────
        # EXIT operations bypass event/noise windows: that is exactly when
        # protective square-offs are most needed.
        if operation != "EXIT":
            eia_hour, eia_minute = get_eia_announcement_time(now)
            if sym in ("NATURALGAS", "NATGAS"):
                if now.weekday() == _EIA_WEEKDAY:
                    eia_min = eia_hour * 60 + eia_minute
                    now_min = h * 60 + m
                    if abs(now_min - eia_min) <= _EIA_WINDOW:
                        return (
                            False,
                            f"EIA Natural Gas Storage Report window "
                            f"(Thu {eia_hour:02d}:{eia_minute:02d} IST "
                            f"±{_EIA_WINDOW} min)",
                        )
            elif sym == "CRUDEOIL":
                if now.weekday() == 2:  # Wednesday
                    eia_min = eia_hour * 60 + eia_minute
                    now_min = h * 60 + m
                    if abs(now_min - eia_min) <= _EIA_WINDOW:
                        return (
                            False,
                            f"EIA Weekly Petroleum Status Report window "
                            f"(Wed {eia_hour:02d}:{eia_minute:02d} IST "
                            f"±{_EIA_WINDOW} min)",
                        )

        # ── Window 4: RBI announcement ±5 min (BANKNIFTY) ────────────────────
        if sym == "BANKNIFTY" and operation != "EXIT":
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
            expiry_date = _parse_expiry_date(expiry_str)
            if expiry_date is not None and expiry_date == now.date():
                if is_mcx:
                    # MCX cutoff: entries blocked after 20:00 IST on expiry day.
                    # Exits remain permitted through MCX session close (23:30 summer, 23:55 winter) to allow Friday/EOD auto-exits.
                    mcx_close_h, mcx_close_m = get_mcx_close_time(now)
                    if operation == "EXIT":
                        if (h, m) >= (mcx_close_h, mcx_close_m):
                            return False, f"MCX market session close (after {mcx_close_h:02d}:{mcx_close_m:02d} IST)"
                    else:
                        if (h, m) >= (20, 0):
                            return False, "Expiry day entry cutoff (after 20:00 IST for MCX on expiry day)"
                elif is_index_fno:
                    # Index F&O: trade until 15:40 (CAS closes); applies to
                    # both ENTRY (or Window 2 cuts it earlier at 15:25) and EXIT.
                    if (h, m) >= (15, 40):
                        return False, f"Expiry day trading cutoff (after 15:40 IST for Index F&O on expiry day)"
                else:
                    # F&O stocks & other NSE: entries cut at 15:25 (CAS),
                    # protective exits permitted until the 15:40 session close.
                    cutoff = (15, 40) if operation == "EXIT" else (15, 25)
                    if (h, m) >= cutoff:
                        return False, (
                            "Expiry day exit cutoff (after 15:40 IST for NSE on expiry day)"
                            if operation == "EXIT"
                            else "Expiry day trading cutoff (after 15:25 IST for NSE on expiry day)"
                        )

        return True, ""

    except Exception as exc:
        log.exception("time_guards.is_trading_allowed_now: unexpected error (%s)", exc)
        # Fail-closed for entries to prevent dangerous order placements during engine errors;
        # allow exits to ensure open risk can always be flattened.
        if operation == "EXIT":
            return True, ""
        return False, f"Time guard system error — fail-closed: {exc}"
