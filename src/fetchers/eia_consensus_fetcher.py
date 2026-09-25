"""
EIA Weekly Natural Gas Storage Consensus, Actuals & Macro Benchmarks Fetcher.
Pulls from:
1. Official US EIA Real-time Endpoints (wngsr.json / wngsr.txt) for official actuals,
   total storage, 5-year historical average total, and surplus/deficit metrics.
2. Forex Factory calendar JSON feed for analyst consensus expectations.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, timedelta
import pytz
import requests

from src.models.schema import get_conn
from src.engine.ng_macro_context import (
    get_seasonal_regime,
    classify_storage_event,
    _TYPICAL_5YR_WEEKLY_CHANGE,
)

log = logging.getLogger(__name__)

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
EIA_JSON_URL = "https://ir.eia.gov/ngs/wngsr.json"
EIA_TXT_URL = "https://ir.eia.gov/ngs/wngsr.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

_EIA_CACHE: dict | None = None
_EIA_CACHE_TIME: float = 0.0
_EIA_CACHE_TTL_SEC: float = 60.0  # 60s TTL avoids back-to-back burst requests
_IST = pytz.timezone("Asia/Kolkata")


def parse_bcf_value(val_str: str | None) -> float | None:
    """Helper to parse value strings like '87B', '-12B', '53' to float numbers."""
    if val_str is None:
        return None
    val_str = str(val_str).strip()
    cleaned = re.sub(r"[^\d.\-]", "", val_str)
    if cleaned:
        try:
            return float(cleaned)
        except ValueError:
            pass
    return None


def fetch_official_eia_wngsr() -> dict | None:
    """
    Fetches real-time official Lower 48 Natural Gas storage data directly
    from the US Energy Information Administration (EIA).
    """
    try:
        log.info("Querying official EIA wngsr.json endpoint...")
        r = requests.get(EIA_JSON_URL, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            payload = r.json()
            series_list = payload.get("series", [])
            tot_series = next((s for s in series_list if s.get("name") == "total lower 48 states"), None)
            if tot_series:
                calc = tot_series.get("calculated", {})
                data_rows = tot_series.get("data", [])

                actual_bcf = float(calc.get("net_change")) if calc.get("net_change") is not None else None
                total_bcf = float(data_rows[0][1]) if len(data_rows) > 0 and len(data_rows[0]) > 1 else None
                prev_bcf = float(data_rows[1][1]) if len(data_rows) > 1 and len(data_rows[1]) > 1 else None
                year_ago_bcf = float(data_rows[2][1]) if len(data_rows) > 2 and len(data_rows[2]) > 1 else None

                five_yr_total = float(calc.get("5yr-avg")) if calc.get("5yr-avg") is not None else None
                surplus_pct = float(calc.get("pct-chg_5yr-avg")) if calc.get("pct-chg_5yr-avg") is not None else None
                pct_chg_yrago = float(calc.get("pct-change_yrago")) if calc.get("pct-change_yrago") is not None else None

                # Check revision flags
                rev_flag = 1 if str(calc.get("revision_flag_net_change", "")).lower() == "true" else 0
                report_date = payload.get("release_date", "").split()[0]
                if not report_date:
                    report_date = datetime.now(_IST).strftime("%Y-%m-%d")

                return {
                    "report_date": report_date,
                    "actual_bcf": actual_bcf,
                    "total_storage_bcf": total_bcf,
                    "prev_week_storage_bcf": prev_bcf,
                    "year_ago_bcf": year_ago_bcf,
                    "five_year_total_bcf": five_yr_total,
                    "surplus_vs_5yr_pct": surplus_pct,
                    "pct_change_yrago": pct_chg_yrago,
                    "prior_week_revision_flag": rev_flag,
                    "source": "official_eia_json",
                }
    except Exception as e:
        log.warning("Official EIA JSON query failed: %s, falling back to text endpoint", e)

    # Fallback to EIA summary text endpoint
    try:
        r = requests.get(EIA_TXT_URL, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            text = r.text
            net_match = re.search(r"Net change:\s*([\d.\-]+)", text)
            tot_match = re.search(r"Total\s*\([\d/]+\):\s*([\d,]+)", text)
            fyr_match = re.search(r"5-year avg stocks:\s*([\d,]+)", text)
            pct_fyr_match = re.search(r"% change from 5-year avg:\s*([\d.\-]+)", text)
            yrago_match = re.search(r"Year ago stocks:\s*([\d,]+)", text)

            actual_bcf = parse_bcf_value(net_match.group(1)) if net_match else None
            total_bcf = parse_bcf_value(tot_match.group(1)) if tot_match else None
            fyr_total = parse_bcf_value(fyr_match.group(1)) if fyr_match else None
            surplus_pct = parse_bcf_value(pct_fyr_match.group(1)) if pct_fyr_match else None
            yrago_bcf = parse_bcf_value(yrago_match.group(1)) if yrago_match else None

            report_date = datetime.now(_IST).strftime("%Y-%m-%d")
            return {
                "report_date": report_date,
                "actual_bcf": actual_bcf,
                "total_storage_bcf": total_bcf,
                "five_year_total_bcf": fyr_total,
                "surplus_vs_5yr_pct": surplus_pct,
                "year_ago_bcf": yrago_bcf,
                "prior_week_revision_flag": 0,
                "source": "official_eia_text",
            }
    except Exception as e:
        log.warning("Official EIA text fallback query failed: %s", e)

    return None


def fetch_eia_weekly_data(force_refresh: bool = False) -> dict | None:
    """
    Fetch the EIA Natural Gas Storage event from the Forex Factory JSON feed.
    Caches parsed result for 60s to prevent burst rate-limiting (429).
    Returns parsed dict or None on failure/missing event.
    """
    global _EIA_CACHE, _EIA_CACHE_TIME
    now = time.time()
    if not force_refresh and _EIA_CACHE is not None and (now - _EIA_CACHE_TIME) < _EIA_CACHE_TTL_SEC:
        log.debug("Returning cached EIA weekly data (age=%.1fs)", now - _EIA_CACHE_TIME)
        return _EIA_CACHE

    try:
        log.info("Fetching economic calendar from Forex Factory JSON feed...")
        r = requests.get(FF_CALENDAR_URL, headers=HEADERS, timeout=10)
        if r.status_code == 429:
            log.warning("Forex Factory calendar feed returned 429 — falling back to cache/DB")
            return _EIA_CACHE
        if r.status_code != 200:
            log.warning("Forex Factory calendar feed status: %d", r.status_code)
            return _EIA_CACHE

        events = r.json()
        for item in events:
            title = item.get("title", "")
            country = item.get("country", "")

            if "Natural Gas Storage" in title and country == "USD":
                date_str = item.get("date", "")
                if not date_str:
                    continue
                report_date = date_str.split("T")[0]
                forecast_val = parse_bcf_value(item.get("forecast"))
                actual_val = parse_bcf_value(item.get("actual"))

                surprise_val = None
                if forecast_val is not None and actual_val is not None:
                    surprise_val = actual_val - forecast_val

                result = {
                    "report_date": report_date,
                    "consensus_bcf": forecast_val,
                    "actual_bcf": actual_val,
                    "surprise_bcf": surprise_val,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "source": "forexfactory",
                }
                _EIA_CACHE = result
                _EIA_CACHE_TIME = time.time()
                return result

        log.warning("Natural Gas Storage event not found in Forex Factory calendar")
    except Exception as e:
        log.warning("Failed to fetch EIA weekly calendar data: %s", e)
    return None


def fetch_complete_eia_dataset() -> dict | None:
    """
    Synthesizes Forex Factory consensus with official EIA dataset, computes
    5-year historical benchmarks, surplus trajectory, and seasonal stance.
    """
    now_ist = datetime.now(_IST)
    season = get_seasonal_regime(now_ist)
    today_str = now_ist.strftime("%Y-%m-%d")

    # 1. Fetch Forex Factory consensus
    ff_data = fetch_eia_weekly_data() or {}
    consensus = ff_data.get("consensus_bcf")
    report_date = ff_data.get("report_date") or today_str
    actual_bcf = ff_data.get("actual_bcf")

    # 2. Fetch official EIA dataset
    eia_official = fetch_official_eia_wngsr() or {}
    if eia_official.get("actual_bcf") is not None:
        actual_bcf = eia_official["actual_bcf"]
    if eia_official.get("report_date"):
        # Prefer Thursday date format YYYY-MM-DD
        if re.match(r"^\d{4}-\d{2}-\d{2}$", eia_official["report_date"]):
            report_date = eia_official["report_date"]

    total_storage = eia_official.get("total_storage_bcf")
    five_yr_total = eia_official.get("five_year_total_bcf")
    surplus_pct = eia_official.get("surplus_vs_5yr_pct")
    surplus_bcf = None
    if total_storage is not None and five_yr_total is not None:
        surplus_bcf = total_storage - five_yr_total

    # 3. Compute 5-year average weekly change
    five_year_avg_bcf = None
    prior_five_yr_total = None
    try:
        with get_conn() as conn:
            prev_row = conn.execute(
                """
                SELECT five_year_total_bcf FROM eia_consensus 
                WHERE report_date < ? AND five_year_total_bcf IS NOT NULL 
                ORDER BY report_date DESC LIMIT 1
                """,
                (report_date,),
            ).fetchone()
            if prev_row and prev_row["five_year_total_bcf"]:
                prior_five_yr_total = float(prev_row["five_year_total_bcf"])
    except Exception:
        pass

    if five_yr_total is not None and prior_five_yr_total is not None:
        five_year_avg_bcf = five_yr_total - prior_five_yr_total
    else:
        # Fallback to seasonal benchmark for this calendar week
        cal_week = now_ist.isocalendar()[1]
        five_year_avg_bcf = _TYPICAL_5YR_WEEKLY_CHANGE.get(cal_week, season["expected_benchmark_bcf"])

    # 4. Classify macro stance if actual_bcf is known
    macro_stance = "NEUTRAL_BALANCED"
    stance_summary = season["rule_description"]
    if actual_bcf is not None:
        stance, summary, _ = classify_storage_event(
            actual_bcf=actual_bcf,
            consensus_bcf=consensus,
            five_year_avg_bcf=five_year_avg_bcf,
            surplus_vs_5yr_bcf=surplus_bcf,
            surplus_vs_5yr_pct=surplus_pct,
            seasonal_regime=season["regime"],
        )
        macro_stance = stance
        stance_summary = summary

    # Next Thursday release validity window (20:00 IST)
    days_to_next_thu = (3 - now_ist.weekday()) % 7
    if days_to_next_thu == 0:
        days_to_next_thu = 7
    next_thu = (now_ist + timedelta(days=days_to_next_thu)).replace(hour=20, minute=0, second=0, microsecond=0)
    valid_until = next_thu.astimezone(timezone.utc).isoformat()

    surprise_bcf = (actual_bcf - consensus) if (actual_bcf is not None and consensus is not None) else None

    result = {
        "report_date": report_date,
        "consensus_bcf": consensus,
        "actual_bcf": actual_bcf,
        "surprise_bcf": surprise_bcf,
        "five_year_avg_bcf": five_year_avg_bcf,
        "five_year_total_bcf": five_yr_total,
        "surplus_vs_5yr_bcf": surplus_bcf,
        "surplus_vs_5yr_pct": surplus_pct,
        "total_storage_bcf": total_storage,
        "year_ago_bcf": eia_official.get("year_ago_bcf"),
        "pct_change_yrago": eia_official.get("pct_change_yrago"),
        "prior_week_revised_bcf": None,
        "prior_week_revision_flag": eia_official.get("prior_week_revision_flag", 0),
        "seasonal_regime": season["regime"],
        "macro_stance": macro_stance,
        "stance_summary": stance_summary,
        "valid_until": valid_until,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "eia_official_plus_forexfactory",
    }
    return result


def store_eia_weekly_data(data: dict) -> None:
    """Insert or replace complete EIA macro data into the SQLite database."""
    if not data or not data.get("report_date"):
        return

    sql = """
        INSERT INTO eia_consensus (
            report_date, consensus_bcf, actual_bcf, surprise_bcf,
            five_year_avg_bcf, five_year_total_bcf, surplus_vs_5yr_bcf, surplus_vs_5yr_pct,
            total_storage_bcf, year_ago_bcf, pct_change_yrago,
            prior_week_revised_bcf, prior_week_revision_flag,
            seasonal_regime, macro_stance, stance_summary, key_levels_json, valid_until,
            fetched_at, source
        )
        VALUES (
            :report_date, :consensus_bcf, :actual_bcf, :surprise_bcf,
            :five_year_avg_bcf, :five_year_total_bcf, :surplus_vs_5yr_bcf, :surplus_vs_5yr_pct,
            :total_storage_bcf, :year_ago_bcf, :pct_change_yrago,
            :prior_week_revised_bcf, :prior_week_revision_flag,
            :seasonal_regime, :macro_stance, :stance_summary, :key_levels_json, :valid_until,
            :fetched_at, :source
        )
        ON CONFLICT(report_date) DO UPDATE SET
            consensus_bcf = COALESCE(excluded.consensus_bcf, eia_consensus.consensus_bcf),
            actual_bcf = COALESCE(excluded.actual_bcf, eia_consensus.actual_bcf),
            surprise_bcf = COALESCE(excluded.surprise_bcf, eia_consensus.surprise_bcf),
            five_year_avg_bcf = COALESCE(excluded.five_year_avg_bcf, eia_consensus.five_year_avg_bcf),
            five_year_total_bcf = COALESCE(excluded.five_year_total_bcf, eia_consensus.five_year_total_bcf),
            surplus_vs_5yr_bcf = COALESCE(excluded.surplus_vs_5yr_bcf, eia_consensus.surplus_vs_5yr_bcf),
            surplus_vs_5yr_pct = COALESCE(excluded.surplus_vs_5yr_pct, eia_consensus.surplus_vs_5yr_pct),
            total_storage_bcf = COALESCE(excluded.total_storage_bcf, eia_consensus.total_storage_bcf),
            year_ago_bcf = COALESCE(excluded.year_ago_bcf, eia_consensus.year_ago_bcf),
            pct_change_yrago = COALESCE(excluded.pct_change_yrago, eia_consensus.pct_change_yrago),
            prior_week_revised_bcf = COALESCE(excluded.prior_week_revised_bcf, eia_consensus.prior_week_revised_bcf),
            prior_week_revision_flag = COALESCE(excluded.prior_week_revision_flag, eia_consensus.prior_week_revision_flag),
            seasonal_regime = COALESCE(excluded.seasonal_regime, eia_consensus.seasonal_regime),
            macro_stance = COALESCE(excluded.macro_stance, eia_consensus.macro_stance),
            stance_summary = COALESCE(excluded.stance_summary, eia_consensus.stance_summary),
            key_levels_json = COALESCE(excluded.key_levels_json, eia_consensus.key_levels_json),
            valid_until = COALESCE(excluded.valid_until, eia_consensus.valid_until),
            fetched_at = excluded.fetched_at,
            source = excluded.source;
    """
    # Ensure all dictionary keys exist
    keys = [
        "report_date", "consensus_bcf", "actual_bcf", "surprise_bcf",
        "five_year_avg_bcf", "five_year_total_bcf", "surplus_vs_5yr_bcf", "surplus_vs_5yr_pct",
        "total_storage_bcf", "year_ago_bcf", "pct_change_yrago",
        "prior_week_revised_bcf", "prior_week_revision_flag",
        "seasonal_regime", "macro_stance", "stance_summary", "key_levels_json", "valid_until",
        "fetched_at", "source"
    ]
    payload = {k: data.get(k) for k in keys}
    try:
        with get_conn() as conn:
            conn.execute(sql, payload)
        log.info("Persisted EIA macro record for %s with stance=%s", payload["report_date"], payload.get("macro_stance"))
    except Exception as e:
        log.error("Failed to store EIA weekly data in DB: %s", e)


def fetch_and_store_eia_consensus() -> None:
    """Job entry point to fetch and persist complete EIA storage macro intelligence."""
    data = fetch_complete_eia_dataset()
    if data:
        store_eia_weekly_data(data)
