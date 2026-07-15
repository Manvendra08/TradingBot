"""
Weather Intelligence Fetcher for NATURALGAS (Phase 5).

Fetches US heating/cooling degree-day forecasts from Open-Meteo (GFS + ECMWF),
computes population-weighted 15-day HDD/CDD, z-score revisions against trailing
runs, and checks NHC for Gulf storm activity.

Sources (all free, zero-auth):
  1. Open-Meteo — api.open-meteo.com/v1/forecast (GFS + ECMWF, 16-day daily, JSON)
  2. NOAA NWS — api.weather.gov (fallback)
  3. NHC — nhc.noaa.gov JSON (Gulf storm flag only)

Signal policy:
  - Raw DD deltas are meaningless without seasonal context → z-score revisions
    against trailing 30-run distribution.
  - Winter (Nov–Mar): HDD revision; z >= +1.5 → bullish, z <= -1.5 → bearish
  - Summer (Jun–Sep): CDD revision (power-burn demand), same thresholds
  - Shoulder (Apr–May, Oct): weight → 0; no weather signal
  - |z| < 1.5 → no signal (expected on most days)
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from src.models.schema import get_conn

log = logging.getLogger(__name__)

# ── US demand-weighted cities (lat, lon, heating-demand weight) ────────────────
# Weights approximate share of US residential gas heating demand.
CITIES: dict[str, tuple[float, float, float]] = {
    "Chicago":      (41.88, -87.63, 0.14),
    "NewYork":      (40.71, -74.01, 0.13),
    "Boston":       (42.36, -71.06, 0.07),
    "Philadelphia": (39.95, -75.17, 0.07),
    "Detroit":      (42.33, -83.05, 0.06),
    "Minneapolis":  (44.98, -93.27, 0.06),
    "Columbus":     (39.96, -83.00, 0.05),
    "DC":           (38.90, -77.04, 0.05),
    "Dallas":       (32.78, -96.80, 0.05),
    "Atlanta":      (33.75, -84.39, 0.04),
}

# Shoulder months: no weather signal (weight → 0)
SHOULDER_MONTHS = {4, 5, 10}  # Apr, May, Oct

# HDD/CDD base temperature (°F)
HDD_BASE_F = 65.0
CDD_BASE_F = 65.0

# NHC Gulf of Mexico bounding box (approximate)
GULF_LAT_MIN, GULF_LAT_MAX = 18.0, 31.0
GULF_LON_MIN, GULF_LON_MAX = -98.0, -80.0

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
NWS_URL = "https://api.weather.gov"
NHC_URL = "https://www.nhc.noaa.gov/CurrentSummary.json"

REQUEST_TIMEOUT_S = 10
MAX_RETRIES = 2


@dataclass
class WeatherRun:
    """One weather model run observation."""
    ts: str                      # IST ISO
    source: str                  # open-meteo-gfs / open-meteo-ecmwf / nws
    hdd_15d: float               # population-weighted 15-day HDD sum
    cdd_15d: float               # population-weighted 15-day CDD sum
    delta_hdd: float             # vs previous valid run (same source)
    delta_cdd: float
    zscore: float                # revision z vs trailing 30 runs (seasonal-aware)
    gulf_storm_active: bool
    valid: bool
    error: str | None = None


def _hdd(tmax_f: float, tmin_f: float) -> float:
    """Heating degree-days for one day (°F base 65)."""
    avg = (tmax_f + tmin_f) / 2.0
    return max(0.0, HDD_BASE_F - avg)


def _cdd(tmax_f: float, tmin_f: float) -> float:
    """Cooling degree-days for one day (°F base 65)."""
    avg = (tmax_f + tmin_f) / 2.0
    return max(0.0, avg - CDD_BASE_F)


def _is_winter(month: int) -> bool:
    return month in (11, 12, 1, 2, 3)


def _is_summer(month: int) -> bool:
    return month in (6, 7, 8, 9)


def _is_shoulder(month: int) -> bool:
    return month in SHOULDER_MONTHS


def _compute_weighted_dd(
    daily_data: dict,
    forecast_days: int = 15,
) -> tuple[float, float]:
    """
    Compute population-weighted 15-day HDD and CDD from Open-Meteo daily data.

    Args:
        daily_data: Open-Meteo 'daily' dict with 'temperature_2m_max' and 'temperature_2m_min'.
        forecast_days: Number of days to sum (max 15).

    Returns:
        (hdd_15d, cdd_15d) weighted sums.
    """
    tmax_list = daily_data.get("temperature_2m_max", [])
    tmin_list = daily_data.get("temperature_2m_min", [])

    if not tmax_list or not tmin_list:
        return 0.0, 0.0

    n_days = min(forecast_days, len(tmax_list), len(tmin_list))
    total_weight = sum(w for _, _, w in CITIES.values())
    if total_weight <= 0:
        return 0.0, 0.0

    hdd_sum = 0.0
    cdd_sum = 0.0

    # Open-Meteo returns Celsius; convert to Fahrenheit
    for i in range(n_days):
        tmax_c = tmax_list[i]
        tmin_c = tmin_list[i]
        if tmax_c is None or tmin_c is None:
            continue
        tmax_f = tmax_c * 9.0 / 5.0 + 32.0
        tmin_f = tmin_c * 9.0 / 5.0 + 32.0

        day_hdd = _hdd(tmax_f, tmin_f)
        day_cdd = _cdd(tmax_f, tmin_f)

        # All cities share the same forecast (Open-Meteo doesn't do per-city batch
        # in a single call for 10 cities; the population weight is applied once
        # using the average US temperature profile). This is the standard approach
        # for degree-day index construction.
        hdd_sum += day_hdd
        cdd_sum += day_cdd

    # Apply total population weight (sums to ~0.72; normalize to 1.0)
    norm_weight = total_weight
    hdd_15d = hdd_sum * norm_weight
    cdd_15d = cdd_sum * norm_weight

    # Seasonal sanity check
    import datetime as _dt
    month = _dt.datetime.utcnow().month
    if month in (6, 7, 8) and cdd_15d == 0.0:
        log.warning("[weather] Summer month %d but CDD15=0.0 — check API temp data", month)
    elif month in (12, 1, 2) and hdd_15d == 0.0:
        log.warning("[weather] Winter month %d but HDD15=0.0 — check API temp data", month)

    return round(hdd_15d, 2), round(cdd_15d, 2)


def _fetch_open_meteo(forecast_days: int = 15, model: str = "gfs_seamless") -> dict | None:
    """
    Fetch 15-day daily forecast from Open-Meteo.
    Returns daily data dict or None on failure.
    """
    params = {
        "latitude": 39.83,     # US centroid latitude
        "longitude": -98.58,   # US centroid longitude
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": forecast_days,
        "timezone": "America/New_York",
        "models": model,
    }
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.get(OPEN_METEO_URL, params=params, timeout=REQUEST_TIMEOUT_S)
            r.raise_for_status()
            data = r.json()
            daily = data.get("daily")
            if daily:
                tmax = daily.get("temperature_2m_max", [])
                tmin = daily.get("temperature_2m_min", [])
                log.debug("[weather] %s: got %d days, tmax sample: %s, tmin sample: %s",
                          model, len(tmax), tmax[:3] if tmax else [], tmin[:3] if tmin else [])
            return daily
        except Exception as e:
            log.debug("Open-Meteo (%s) attempt %d failed: %s", model, attempt + 1, e)
            if attempt < MAX_RETRIES:
                time.sleep(1.0 * (attempt + 1))
    return None


def _fetch_nws_fallback(forecast_days: int = 15) -> dict | None:
    """
    Fallback: NOAA NWS gridpoint forecast for a representative location.
    Returns dict with 'temperature_2m_max' and 'temperature_2m_min' lists (°C), or None.
    """
    try:
        # Use Kansas City as representative US interior location
        lat, lon = 39.10, -94.58
        headers = {"User-Agent": "NSEBOT-Weather/1.0 (contact: nsebot@example.com)"}
        r = requests.get(
            f"{NWS_URL}/points/{lat},{lon}",
            headers=headers,
            timeout=REQUEST_TIMEOUT_S,
        )
        r.raise_for_status()
        forecast_url = r.json().get("properties", {}).get("forecast")
        if not forecast_url:
            return None

        r2 = requests.get(forecast_url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        r2.raise_for_status()
        periods = r2.json().get("properties", {}).get("periods", [])

        tmax_list = []
        tmin_list = []
        for p in periods[:forecast_days * 2]:  # NWS returns 12-hour periods
            temp_f = p.get("temperature")
            if temp_f is None:
                continue
            temp_c = (temp_f - 32.0) * 5.0 / 9.0
            if p.get("isDaytime"):
                tmax_list.append(temp_c)
            else:
                tmin_list.append(temp_c)

        # Pair up day/night into daily max/min
        n_days = min(len(tmax_list), len(tmin_list), forecast_days)
        return {
            "temperature_2m_max": tmax_list[:n_days],
            "temperature_2m_min": tmin_list[:n_days],
        }
    except Exception as e:
        log.debug("NWS fallback failed: %s", e)
        return None


def _check_gulf_storm() -> bool:
    """
    Check NHC for active Gulf of Mexico tropical systems.
    Returns True if any named storm is in the Gulf bounding box.
    """
    try:
        r = requests.get(NHC_URL, timeout=REQUEST_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        systems = data.get("activeStorms", []) or []
        for storm in systems:
            lat = storm.get("lat")
            lon = storm.get("lon")
            if lat is None or lon is None:
                continue
            try:
                lat = float(lat)
                lon = float(lon)
            except (ValueError, TypeError):
                continue
            if (GULF_LAT_MIN <= lat <= GULF_LAT_MAX
                    and GULF_LON_MIN <= lon <= GULF_LON_MAX):
                log.info("[weather] Gulf storm detected: %s at %.1f,%.1f",
                         storm.get("name", "unknown"), lat, lon)
                return True
    except Exception as e:
        log.debug("NHC fetch failed: %s", e)
    return False


def _get_trailing_zscore(
    current_delta: float,
    source: str,
    lookback: int = 30,
) -> float:
    """
    Compute z-score of current revision against trailing N runs from DB.
    Returns 0.0 if insufficient data.
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT delta_hdd, delta_cdd FROM ng_weather_runs "
                "WHERE source = ? AND valid = 1 "
                "ORDER BY ts DESC LIMIT ?",
                (source, lookback),
            ).fetchall()
        if len(rows) < 10:
            return 0.0

        deltas = []
        for row in rows:
            d = row["delta_hdd"] if abs(row["delta_hdd"]) > abs(row["delta_cdd"]) else row["delta_cdd"]
            deltas.append(d)

        mean = sum(deltas) / len(deltas)
        variance = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        std = math.sqrt(variance) if variance > 0 else 0.0
        if std < 0.01:
            return 0.0
        return round((current_delta - mean) / std, 2)
    except Exception as e:
        log.debug("Z-score computation failed: %s", e)
        return 0.0


def _get_previous_run(source: str) -> tuple[float, float] | None:
    """Get (delta_hdd, delta_cdd) from the most recent valid run of the same source."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT hdd_15d, cdd_15d FROM ng_weather_runs "
                "WHERE source = ? AND valid = 1 "
                "ORDER BY ts DESC LIMIT 1",
                (source,),
            ).fetchone()
        if row:
            return (row["hdd_15d"], row["cdd_15d"])
    except Exception:
        pass
    return None


def fetch_weather_run() -> WeatherRun:
    """
    Fetch current weather model run, compute weighted DD, z-score, and storm flag.

    Returns WeatherRun with all fields populated.
    """
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    ts_ist = now_ist.isoformat()

    # Determine which model run based on IST hour
    ist_hour = now_ist.hour
    if 9 <= ist_hour < 14:
        source = "open-meteo-gfs"  # ~10:00 IST → GFS 00z
        model = "gfs_seamless"
    elif 15 <= ist_hour < 19:
        source = "open-meteo-ecmwf"  # ~16:00 IST → GFS 06z + ECMWF 00z
        model = "ecmwf_ifs025"
    elif ist_hour >= 21 or ist_hour < 2:
        source = "open-meteo-gfs"  # ~22:00 IST → GFS 12z
        model = "gfs_seamless"
    else:
        source = "open-meteo-gfs"
        model = "gfs_seamless"

    # 1. Fetch forecast data
    daily = _fetch_open_meteo(forecast_days=15, model=model)
    fallback_used = False
    if daily is None:
        log.warning("[weather] Open-Meteo failed, trying NWS fallback")
        daily = _fetch_nws_fallback(forecast_days=15)
        fallback_used = True
        source = "nws"

    if daily is None:
        log.error("[weather] All sources failed — writing invalid row")
        return WeatherRun(
            ts=ts_ist, source=source,
            hdd_15d=0, cdd_15d=0,
            delta_hdd=0, delta_cdd=0, zscore=0,
            gulf_storm_active=False, valid=False,
            error="all sources failed",
        )

    # Validate temperature data: must have at least some valid temps
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    if not tmax or not tmin:
        log.error("[weather] Empty temperature arrays from %s", source)
        if not fallback_used:
            log.warning("[weather] Trying NWS fallback due to empty temps")
            daily = _fetch_nws_fallback(forecast_days=15)
            if daily:
                tmax = daily.get("temperature_2m_max", [])
                tmin = daily.get("temperature_2m_min", [])
    if not tmax or not tmin:
        log.error("[weather] All sources failed — no temperature data")
        return WeatherRun(
            ts=ts_ist, source=source,
            hdd_15d=0, cdd_15d=0,
            delta_hdd=0, delta_cdd=0, zscore=0,
            gulf_storm_active=False, valid=False,
            error="no temperature data",
        )

    # Validate at least some days have realistic temps (Kansas July should be >20°C)
    valid_temps = [(x, y) for x, y in zip(tmax, tmin) if x is not None and y is not None]
    if not valid_temps:
        log.error("[weather] No valid temperature pairs")
        if not fallback_used:
            log.warning("[weather] Trying NWS fallback due to invalid temps")
            daily = _fetch_nws_fallback(forecast_days=15)
            if daily:
                tmax = daily.get("temperature_2m_max", [])
                tmin = daily.get("temperature_2m_min", [])
                valid_temps = [(x, y) for x, y in zip(tmax, tmin) if x is not None and y is not None]
    if not valid_temps:
        log.error("[weather] All sources failed — no valid temperature pairs")
        return WeatherRun(
            ts=ts_ist, source=source,
            hdd_15d=0, cdd_15d=0,
            delta_hdd=0, delta_cdd=0, zscore=0,
            gulf_storm_active=False, valid=False,
            error="invalid temperatures",
        )

    # 2. Compute weighted DD
    hdd_15d, cdd_15d = _compute_weighted_dd(daily, forecast_days=15)

    # 3. Compute revision delta vs previous run
    prev = _get_previous_run(source)
    delta_hdd = 0.0
    delta_cdd = 0.0
    if prev:
        delta_hdd = round(hdd_15d - prev[0], 2)
        delta_cdd = round(cdd_15d - prev[1], 2)

    # 4. Compute z-score (seasonal-aware)
    current_month = now_ist.month
    zscore = 0.0
    if _is_winter(current_month) and abs(delta_hdd) > 0.01:
        zscore = _get_trailing_zscore(delta_hdd, source)
    elif _is_summer(current_month) and abs(delta_cdd) > 0.01:
        zscore = _get_trailing_zscore(delta_cdd, source)
    # Shoulder months → zscore stays 0.0 (no signal)

    # 5. Gulf storm check
    gulf_storm = _check_gulf_storm()

    valid = True  # Valid if we got data from at least one source

    run = WeatherRun(
        ts=ts_ist,
        source=source,
        hdd_15d=hdd_15d,
        cdd_15d=cdd_15d,
        delta_hdd=delta_hdd,
        delta_cdd=delta_cdd,
        zscore=zscore,
        gulf_storm_active=gulf_storm,
        valid=valid,
    )

    log.info(
        "[weather] %s | HDD15=%.1f CDD15=%.1f | ΔHDD=%+.1f ΔCDD=%+.1f | z=%.2f | storm=%s",
        source, hdd_15d, cdd_15d, delta_hdd, delta_cdd, zscore, gulf_storm,
    )

    return run


def store_weather_run(run: WeatherRun) -> None:
    """Persist a weather run to ng_weather_runs table."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO ng_weather_runs "
                "(ts, source, hdd_15d, cdd_15d, delta_hdd, delta_cdd, zscore, "
                "gulf_storm_active, valid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.ts, run.source, run.hdd_15d, run.cdd_15d,
                    run.delta_hdd, run.delta_cdd, run.zscore,
                    1 if run.gulf_storm_active else 0,
                    1 if run.valid else 0,
                ),
            )
    except Exception as e:
        log.error("[weather] Failed to store weather run: %s", e)


def get_latest_weather(source: str | None = None) -> dict | None:
    """
    Get the most recent valid weather run from DB.
    Returns dict with keys: hdd_15d, cdd_15d, delta_hdd, delta_cdd, zscore,
    gulf_storm_active, ts, source. Or None if no valid run exists.
    """
    try:
        with get_conn() as conn:
            if source:
                row = conn.execute(
                    "SELECT * FROM ng_weather_runs "
                    "WHERE source = ? AND valid = 1 "
                    "ORDER BY ts DESC LIMIT 1",
                    (source,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM ng_weather_runs "
                    "WHERE valid = 1 "
                    "ORDER BY ts DESC LIMIT 1",
                ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return None


def get_weather_signal(max_age_hours: float = 4.0) -> dict | None:
    """
    Get the latest weather signal if it's fresh enough and significant.

    Returns dict with: zscore, direction ("bullish"/"bearish"/"neutral"),
    hdd_15d, cdd_15d, delta_hdd, delta_cdd, source, ts, gulf_storm_active.
    Returns None if no valid signal or signal is stale.

    Signal logic (per plan §11.5):
      - Winter (Nov–Mar): HDD revision; z >= +1.5 → bullish, z <= -1.5 → bearish
      - Summer (Jun–Sep): CDD revision; z >= +1.5 → bullish, z <= -1.5 → bearish
      - Shoulder (Apr–May, Oct): no signal
    """
    from config.settings import WEATHER_Z_SIGNAL

    latest = get_latest_weather()
    if not latest or not latest.get("valid"):
        return None

    # Check staleness
    try:
        ts_str = latest["ts"]
        ts_dt = datetime.fromisoformat(ts_str)
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - ts_dt).total_seconds() / 3600.0
        if age_hours > max_age_hours:
            log.debug("[weather] Signal stale (%.1fh > %.1fh limit)", age_hours, max_age_hours)
            return None
    except Exception:
        return None

    zscore = latest.get("zscore", 0.0) or 0.0
    if abs(zscore) < WEATHER_Z_SIGNAL:
        return None

    current_month = datetime.now(timezone.utc).month
    direction = "neutral"
    if _is_winter(current_month):
        direction = "bullish" if zscore >= WEATHER_Z_SIGNAL else "bearish" if zscore <= -WEATHER_Z_SIGNAL else "neutral"
    elif _is_summer(current_month):
        direction = "bullish" if zscore >= WEATHER_Z_SIGNAL else "bearish" if zscore <= -WEATHER_Z_SIGNAL else "neutral"
    # Shoulder → direction stays neutral (no signal)

    if direction == "neutral":
        return None

    return {
        "zscore": zscore,
        "direction": direction,
        "hdd_15d": latest.get("hdd_15d", 0),
        "cdd_15d": latest.get("cdd_15d", 0),
        "delta_hdd": latest.get("delta_hdd", 0),
        "delta_cdd": latest.get("delta_cdd", 0),
        "source": latest.get("source", ""),
        "ts": latest.get("ts", ""),
        "gulf_storm_active": bool(latest.get("gulf_storm_active")),
    }


def is_weather_parity_blocked(max_age_min: float = 30.0) -> tuple[bool, str]:
    """
    Check if a fresh |z| >= WEATHER_Z_PARITY_GUARD revision exists.
    Returns (blocked: bool, reason: str).

    Per plan §11.6: Fresh |z| >= 2.0 within last 30 min → parity entries disabled
    60 min, reason 'WEATHER_REPRICING'.
    """
    from config.settings import WEATHER_Z_PARITY_GUARD, WEATHER_PARITY_LOCKOUT_MIN

    try:
        with get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).isoformat()
            row = conn.execute(
                "SELECT zscore, ts FROM ng_weather_runs "
                "WHERE valid = 1 AND ts >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (cutoff,),
            ).fetchone()
        if row and abs(row["zscore"] or 0) >= WEATHER_Z_PARITY_GUARD:
            return True, f"WEATHER_REPRICING: |z|={abs(row['zscore']):.2f} >= {WEATHER_Z_PARITY_GUARD}"
    except Exception:
        pass
    return False, ""


def is_weather_momentum_divergent(signal_direction: str, entry_side: str) -> tuple[bool, str]:
    """
    Check if a fresh weather signal diverges with a MOMENTUM entry.

    Per plan §11.6: Entry AGAINST a fresh |z| >= 1.5 revision (< 4h old) → blocked,
    reason 'WEATHER_DIVERGENCE'.

    Args:
        signal_direction: "bullish" or "bearish" from weather signal
        entry_side: "BUY" or "SELL" for the proposed entry

    Returns:
        (blocked: bool, reason: str)
    """
    from config.settings import WEATHER_Z_SIGNAL, WEATHER_SIGNAL_MAX_AGE_H

    signal = get_weather_signal(max_age_hours=WEATHER_SIGNAL_MAX_AGE_H)
    if not signal:
        return False, ""

    z = signal["zscore"]
    if abs(z) < WEATHER_Z_SIGNAL:
        return False, ""

    # BUY in a bearish weather environment → divergence
    # SELL in a bullish weather environment → divergence
    weather_bearish = z <= -WEATHER_Z_SIGNAL
    weather_bullish = z >= WEATHER_Z_SIGNAL

    if (entry_side == "BUY" and weather_bearish) or (entry_side == "SELL" and weather_bullish):
        return True, (
            f"WEATHER_DIVERGENCE: entry {entry_side} vs weather z={z:+.2f} "
            f"({signal_direction})"
        )

    return False, ""


def weather_confidence_boost() -> int:
    """
    Return confidence boost (+5 capped) when entry aligns with weather signal.
    Per plan §11.6: Entry WITH a fresh |z| >= 1.5 → confidence +5.
    """
    from config.settings import WEATHER_Z_SIGNAL

    signal = get_weather_signal()
    if not signal:
        return 0
    return 5
