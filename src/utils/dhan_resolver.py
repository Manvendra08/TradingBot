"""
Dynamic Dhan monthly contract scrip/security ID resolver.
Fetches the current active contract ID from Dhan's public web pages for MCX symbols,
caching results to prevent repeated network overhead.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import datetime

from config.settings import DATA_DIR, DHAN_FALLBACK_EXPIRIES, DHAN_SECURITY_IDS

log = logging.getLogger(__name__)

_SYMBOL_SLUGS = {
    "NATURALGAS": "natural-gas",
    "CRUDEOIL": "crude-oil",
    "GOLD": "gold",
    "SILVER": "silver",
}

_CACHE: dict[tuple, tuple[int, float]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 14400  # 4 hours


def _get_from_cache(key: tuple) -> int | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry:
            sec_id, ts = entry
            if (time.time() - ts) < _CACHE_TTL_SECONDS:
                return sec_id
            _CACHE.pop(key, None)
    return None


def _set_in_cache(key: tuple, sec_id: int) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (sec_id, time.time())


def _parse_dhan_expiry(exp_str: str) -> datetime | None:
    """Safely parse various expiry date strings from Dhan master CSV."""
    if not exp_str:
        return None
    cleaned = exp_str.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%Y %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    # Try regex fallback for YYYY-MM-DD
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", cleaned)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _download_dhan_master_if_needed() -> str:
    """Download the Dhan master CSV to DATA_DIR, caching it for 7 days."""
    dest_path = os.path.join(DATA_DIR, "dhan_scrip_master.csv")
    if os.path.exists(dest_path):
        mtime = os.path.getmtime(dest_path)
        if (time.time() - mtime) < 604800:
            return dest_path

    log.info("Downloading/updating Dhan scrip master CSV...")
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            data = response.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        log.info("Successfully saved Dhan scrip master to %s", dest_path)
    except Exception as e:
        log.error("Failed to download Dhan scrip master CSV: %s", e)
        if os.path.exists(dest_path):
            log.warning("Falling back to existing (older) Dhan scrip master file")
        else:
            raise e

    return dest_path


def _resolve_from_master_csv(
    symbol: str, target_year: int | None = None, target_month: int | None = None
) -> int | None:
    """Query local Dhan master CSV to find the FUTCOM contract ID for MCX."""
    try:
        csv_path = _download_dhan_master_if_needed()
        if not os.path.exists(csv_path):
            return None

        now = datetime.now()
        matches = []

        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                exch = row.get("SEM_EXM_EXCH_ID")
                inst = row.get("SEM_INSTRUMENT_NAME")
                sym_name = row.get("SM_SYMBOL_NAME")

                if exch == "MCX" and inst == "FUTCOM" and sym_name == symbol:
                    exp_date_str = row.get("SEM_EXPIRY_DATE")
                    sec_id_str = row.get("SEM_SMST_SECURITY_ID")
                    if exp_date_str and sec_id_str:
                        parsed_dt = _parse_dhan_expiry(exp_date_str)
                        if parsed_dt:
                            matches.append(
                                {
                                    "sec_id": int(sec_id_str),
                                    "expiry_str": exp_date_str,
                                    "expiry_dt": parsed_dt,
                                    "trading_symbol": row.get("SEM_TRADING_SYMBOL"),
                                }
                            )

        if not matches:
            log.warning("No MCX FUTCOM instruments found in Dhan master for %s", symbol)
            return None

        # If target month is specified, filter for it
        if target_year is not None and target_month is not None:
            filtered_matches = [
                m
                for m in matches
                if m["expiry_dt"].year == target_year and m["expiry_dt"].month == target_month
            ]
            if filtered_matches:
                filtered_matches.sort(key=lambda x: x["expiry_dt"])
                best_match = filtered_matches[0]
                log.info(
                    "[resolver] Resolved target-month Dhan ID for MCX %s via master CSV: %d (%s, Expiry: %s)",
                    symbol,
                    best_match["sec_id"],
                    best_match["trading_symbol"],
                    best_match["expiry_str"],
                )
                return best_match["sec_id"]
            else:
                log.warning(
                    "No FUTCOM instruments found in Dhan master for %s matching %d-%02d",
                    symbol,
                    target_year,
                    target_month,
                )

        # Filter for future/today expiries
        valid_matches = [m for m in matches if m["expiry_dt"] >= now]
        if not valid_matches:
            valid_matches = matches

        # Sort to find the nearest future contract
        valid_matches.sort(key=lambda x: x["expiry_dt"])
        best_match = valid_matches[0]
        log.info(
            "[resolver] Resolved near-month Dhan ID for MCX %s via master CSV: %d (%s, Expiry: %s)",
            symbol,
            best_match["sec_id"],
            best_match["trading_symbol"],
            best_match["expiry_str"],
        )
        return best_match["sec_id"]

    except Exception as e:
        log.error("Error resolving Dhan security ID from master CSV: %s", e)
        return None


def get_dhan_security_id(symbol: str, target_expiry: str | None = None) -> int | None:
    """
    Dynamically resolve the current front-month Dhan security ID for commodities,
    falling back to config.settings.DHAN_SECURITY_IDS if resolution fails.
    """
    symbol = symbol.upper().split()[0]

    # Non-commodities (indices) are static and never change
    if symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
        return DHAN_SECURITY_IDS.get(symbol)

    target_year = None
    target_month = None
    if target_expiry:
        try:
            parts = target_expiry.split("-")
            target_year = int(parts[0])
            target_month = int(parts[1])
        except Exception:
            pass

    cache_key = (symbol, target_year, target_month)
    cached_val = _get_from_cache(cache_key)
    if cached_val is not None:
        return cached_val

    # If target expiry is requested, query master CSV first to ensure we get the correct month
    if target_year is not None and target_month is not None:
        sec_id = _resolve_from_master_csv(symbol, target_year, target_month)
        if sec_id:
            _set_in_cache(cache_key, sec_id)
            return sec_id

    slug = _SYMBOL_SLUGS.get(symbol)
    if slug:
        url = f"https://dhan.co/commodity/{slug}-option-chain/"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                },
            )
            with urllib.request.urlopen(req, timeout=15) as res:
                html = res.read().decode("utf-8")

            # Try NextData JSON extraction
            match_next = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html
            )
            if match_next:
                data = json.loads(match_next.group(1))
                props = data.get("props", {}).get("pageProps", {})
                scrip_info = props.get("scripData", {}) or props.get("optionChainData", {}).get(
                    "scripData", {}
                )
                if scrip_info:
                    sec_id_val = (
                        scrip_info.get("sid")
                        or scrip_info.get("scripId")
                        or scrip_info.get("nr_f_sid")
                    )
                    if sec_id_val:
                        sec_id = int(sec_id_val)
                        _set_in_cache(cache_key, sec_id)
                        log.info(
                            "[resolver] Dynamically resolved Dhan security ID for %s: %d",
                            symbol,
                            sec_id,
                        )
                        return sec_id

            # Regex fallback
            match_sid = re.search(r'"(?:scripId|sid)"\s*:\s*(\d+)', html)
            if match_sid:
                sec_id = int(match_sid.group(1))
                _set_in_cache(cache_key, sec_id)
                log.info(
                    "[resolver] Dynamically resolved Dhan security ID (regex fallback) for %s: %d",
                    symbol,
                    sec_id,
                )
                return sec_id

        except Exception as e:
            log.warning("[resolver] Dynamic Dhan web scraping failed for %s: %s", symbol, e)

    # 2. Try dynamic scrip master CSV resolution (robust secondary backup)
    sec_id = _resolve_from_master_csv(symbol, target_year, target_month)
    if sec_id:
        _set_in_cache(cache_key, sec_id)
        return sec_id

    # Check if the fallback is stale or mismatched to avoid silent data failures
    fallback_id = DHAN_SECURITY_IDS.get(symbol)
    fallback_expiry_str = DHAN_FALLBACK_EXPIRIES.get(symbol)
    if fallback_expiry_str:
        try:
            exp_year, exp_month = map(int, fallback_expiry_str.split("-"))
            now_dt = datetime.now()

            # Check target expiry compatibility if requested
            if target_year is not None and target_month is not None:
                if target_year != exp_year or target_month != exp_month:
                    log.warning(
                        "DHAN fallback ID for %s (%s) does not match requested expiry (%d-%02d)",
                        symbol,
                        fallback_expiry_str,
                        target_year,
                        target_month,
                    )
                    return None

            if (now_dt.year > exp_year) or (now_dt.year == exp_year and now_dt.month > exp_month):
                log.critical(
                    "DHAN FALLBACK ID STALE FOR %s! Hardcoded ID is for %s, but current date is %s. "
                    "Rollover update required in config/settings.py!",
                    symbol,
                    fallback_expiry_str,
                    now_dt.strftime("%Y-%m-%d"),
                )
                return None
        except Exception as ve:
            log.error("Error checking fallback ID staleness: %s", ve)

    return fallback_id
