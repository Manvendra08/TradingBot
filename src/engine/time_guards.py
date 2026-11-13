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
from datetime import datetime, date, timedelta

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


def get_scheduled_eia_weekday(d: date) -> int:
    """Return target weekday (3=Thu, 4=Fri, 2=Wed) for EIA release in week of date d.
    
    Standard EIA release is Thursday (weekday 3).
    When a US Federal Holiday occurs on Mon-Wed (e.g. Veterans Day, MLK Day, Presidents' Day,
    Memorial Day, Juneteenth, July 4th, Labor Day, Columbus Day), the release shifts 24h to Friday (weekday 4).
    During Thanksgiving week (4th Thursday in November), the release shifts to Wednesday (weekday 2).
    """
    monday = d - timedelta(days=d.weekday())
    for offset in range(3):
        day = monday + timedelta(days=offset)
        y, m, dt_num = day.year, day.month, day.day
        # Fixed holidays
        if (m == 1 and dt_num == 1) or (m == 1 and dt_num == 2 and day.weekday() == 0):
            return 4
        if (m == 6 and dt_num == 19) or (m == 6 and dt_num in (18, 20) and day.weekday() in (0, 4)):
            return 4
        if (m == 7 and dt_num == 4) or (m == 7 and dt_num in (3, 5) and day.weekday() in (0, 4)):
            return 4
        if (m == 11 and dt_num == 11) or (m == 11 and dt_num in (10, 12) and day.weekday() in (0, 4)):
            return 4
        if (m == 12 and dt_num == 25) or (m == 12 and dt_num in (24, 26) and day.weekday() in (0, 4)):
            return 4
        # Floating Monday holidays
        if day.weekday() == 0:
            if m == 1 and 15 <= dt_num <= 21:   # MLK Jr. Day
                return 4
            if m == 2 and 15 <= dt_num <= 21:   # Presidents' Day
                return 4
            if m == 5 and 25 <= dt_num <= 31:   # Memorial Day
                return 4
            if m == 9 and 1 <= dt_num <= 7:     # Labor Day
                return 4
            if m == 10 and 8 <= dt_num <= 14:   # Columbus Day
                return 4
    # Thanksgiving week (4th Thursday of November) -> Wednesday release
    thursday = monday + timedelta(days=3)
    if thursday.month == 11 and 22 <= thursday.day <= 28:
        return 2
    return 3


def get_eia_schedule(dt: datetime | None = None) -> dict:
    """Return complete dynamic, DST-aware, and holiday-aware EIA Natural Gas Storage schedule.
    
    Fields returned:
      scheduled_release_dt: datetime in IST
      report_date: str 'YYYY-MM-DD'
      is_today: bool
      release_day_name: str ('Friday', 'Thursday', etc.)
      release_hour: int
      release_minute: int
      hours_until: float
      is_blackout: bool (±15 min of announcement)
      is_event_regime: bool (-15 min to +90 min of announcement)
      is_post_release: bool (today and dt >= scheduled_release_dt)
    """
    if dt is None:
        dt = datetime.now(IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)

    # 1. Check if Forex Factory cache has authoritative event date for this week
    ff_release_dt = None
    try:
        from src.fetchers.eia_consensus_fetcher import _EIA_CACHE
        if _EIA_CACHE and _EIA_CACHE.get("report_date"):
            rep_date_str = _EIA_CACHE["report_date"]
            c_date = datetime.strptime(rep_date_str, "%Y-%m-%d").date()
            if abs((c_date - dt.date()).days) <= 6:
                h, m = get_eia_announcement_time(dt)
                ff_release_dt = IST.localize(datetime(c_date.year, c_date.month, c_date.day, h, m))
    except Exception:
        pass

    if ff_release_dt is not None:
        release_dt = ff_release_dt
    else:
        curr_date = dt.date()
        target_weekday = get_scheduled_eia_weekday(curr_date)
        monday = curr_date - timedelta(days=curr_date.weekday())
        rel_date = monday + timedelta(days=target_weekday)
        rel_hour, rel_minute = get_eia_announcement_time(dt)
        release_dt = IST.localize(datetime(rel_date.year, rel_date.month, rel_date.day, rel_hour, rel_minute))
        if dt > release_dt + timedelta(hours=4):
            next_monday = monday + timedelta(days=7)
            next_target_weekday = get_scheduled_eia_weekday(next_monday)
            rel_date = next_monday + timedelta(days=next_target_weekday)
            rel_hour, rel_minute = get_eia_announcement_time(IST.localize(datetime(rel_date.year, rel_date.month, rel_date.day, 12, 0)))
            release_dt = IST.localize(datetime(rel_date.year, rel_date.month, rel_date.day, rel_hour, rel_minute))

    rel_h = release_dt.hour
    rel_m = release_dt.minute
    is_today = (dt.date() == release_dt.date())
    hours_until = (release_dt - dt).total_seconds() / 3600.0
    diff_minutes = (dt - release_dt).total_seconds() / 60.0

    is_blackout = is_today and (-15.0 <= diff_minutes <= 15.0)
    is_event_regime = is_today and (-15.0 <= diff_minutes <= 90.0)
    is_post_release = is_today and (dt >= release_dt)

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    return {
        "scheduled_release_dt": release_dt,
        "report_date": release_dt.strftime("%Y-%m-%d"),
        "is_today": is_today,
        "release_day_name": day_names[release_dt.weekday()],
        "release_hour": rel_h,
        "release_minute": rel_m,
        "hours_until": round(hours_until, 2),
        "is_blackout": is_blackout,
        "is_event_regime": is_event_regime,
        "is_post_release": is_post_release,
    }


def is_eia_release_day(dt: datetime | None = None) -> bool:
    """Return True if dt (or current IST time) falls on the scheduled EIA release day."""
    return get_eia_schedule(dt)["is_today"]


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

        # ── Window 0a: National Exchange Holidays (NSE/BSE & MCX) ─────────────
        from config.holidays import is_market_holiday, get_holiday_reason
        if is_market_holiday(sym, now):
            h_desc = get_holiday_reason(sym, now) or 'Exchange Trading Holiday'
            return False, f'Exchange holiday for {sym} ({now.strftime("%d-%b-%Y")}): {h_desc}'

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
                # On expiry day (0DTE), new entries are allowed strictly up to 14:00 IST (2:00 PM) only
                if operation != "EXIT" and (h, m) >= (14, 0):
                    return False, f"0DTE expiry entry cutoff reached (>= 14:00 IST) for expiry {expiry_str}"
        # ── Window 3: EIA report ±15 min (NATURALGAS Dynamic EIA day, CRUDEOIL Wednesday) ────────
        # EXIT operations bypass event/noise windows: that is exactly when
        # protective square-offs are most needed.
        if operation != "EXIT":
            if sym in ("NATURALGAS", "NATGAS"):
                eia_sched = get_eia_schedule(now)
                if eia_sched["is_blackout"]:
                    rel_day = eia_sched["release_day_name"][:3]
                    rel_h = eia_sched["release_hour"]
                    rel_m = eia_sched["release_minute"]
                    return (
                        False,
                        f"EIA Natural Gas Storage Report blackout window "
                        f"({rel_day} {rel_h:02d}:{rel_m:02d} IST "
                        f"±{_EIA_WINDOW} min)",
                    )
            elif sym == "CRUDEOIL":
                eia_hour, eia_minute = get_eia_announcement_time(now)
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
        # Fail-closed for all operations on unexpected system error to prevent
        # rogue execution or undefined states during engine failures.
        return False, f"Time guard system error (fail-closed): {exc}"
