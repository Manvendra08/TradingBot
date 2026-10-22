"""
Index Weights Manager.
Tracks heavyweight constituents, free-float factors, caches relative weightings,
and calculates live weighted index momentum.
"""

import os
import json
import logging
import threading
import time
from datetime import date, datetime

import pytz
import requests as _requests
import yfinance as yf

log = logging.getLogger(__name__)

CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "cache"))
CACHE_FILE = os.path.join(CACHE_DIR, "index_weights_state.json")

IST = pytz.timezone("Asia/Kolkata")
REFRESH_LOCK = threading.Lock()

# ── Persistent yfinance session with retry ────────────────────────────────
# Yahoo Finance returns empty data / "possibly delisted" errors when requests
# lack proper User-Agent headers or when rate-limited. A persistent session
# with retry adapters mitigates both issues.
_YF_SESSION = None
_YF_SESSION_LOCK = threading.Lock()


def _get_yf_session():
    global _YF_SESSION
    if _YF_SESSION is not None:
        return _YF_SESSION
    with _YF_SESSION_LOCK:
        if _YF_SESSION is not None:
            return _YF_SESSION
        sess = _requests.Session()
        sess.headers[
            "User-Agent"
        ] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        sess.headers["Accept"] = "application/json"
        # Mount retry adapter
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retry = Retry(total=2, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        sess.mount("https://", adapter)
        _YF_SESSION = sess
        return sess

# Curated free-float factors for all constituents across NIFTY 50, BANK NIFTY, and SENSEX
FREE_FLOAT_FACTORS = {
    "RELIANCE": 0.50, "TCS": 0.28, "HDFCBANK": 1.00, "ICICIBANK": 1.00, "INFY": 0.85,
    "ITC": 1.00, "BHARTIARTL": 0.45, "LT": 1.00, "AXISBANK": 1.00, "SBIN": 0.43,
    "KOTAKBANK": 0.74, "M&M": 0.81, "HINDUNILVR": 0.38, "TMPV": 0.54, "TMCV": 0.54,
    "BAJFINANCE": 0.45, "MARUTI": 0.44, "SUNPHARMA": 0.46, "NTPC": 0.49, "HCLTECH": 0.39,
    "POWERGRID": 0.49, "TRENT": 0.63, "TITAN": 0.47, "TATASTEEL": 0.66, "ULTRACEMCO": 0.40,
    "ASIANPAINT": 0.47, "BAJAJ-AUTO": 0.45, "BEL": 0.49, "COALINDIA": 0.34, "JSWSTEEL": 0.55,
    "ADANIPORTS": 0.34, "ADANIENT": 0.25, "ONGC": 0.41, "GRASIM": 0.57, "TECHM": 0.65,
    "ETERNAL": 0.98, "SHRIRAMFIN": 0.75, "TATACONSUM": 0.65, "SBILIFE": 0.44, "DRREDDY": 0.73,
    "CIPLA": 0.66, "HDFCLIFE": 0.49, "EICHERMOT": 0.51, "JIOFIN": 0.54, "NESTLEIND": 0.37,
    "WIPRO": 0.27, "APOLLOHOSP": 0.71, "HINDALCO": 0.65, "INDIGO": 0.62, "BAJAJFINSV": 0.39,
    "MAXHEALTH": 0.76, "INDUSINDBK": 0.85, "PNB": 0.27, "BANKBARODA": 0.36, "AUBANK": 0.75,
    "FEDERALBNK": 1.00, "IDFCFIRSTB": 0.60, "CANBK": 0.37, "UNIONBANK": 0.25, "YESBANK": 1.00,
    "BANDHANBNK": 0.60,
}

# Complete constituent lists: 50 Nifty, 14 Bank Nifty, 30 Sensex
INDEX_CONSTITUENTS = {
    "NIFTY": [
        "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
        "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
        "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
        "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
        "HINDUNILVR", "ICICIBANK", "ITC", "INFY", "INDIGO",
        "JSWSTEEL", "JIOFIN", "KOTAKBANK", "LT", "M&M",
        "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC",
        "POWERGRID", "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN",
        "SUNPHARMA", "TCS", "TATACONSUM", "TMPV", "TATASTEEL",
        "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO"
    ],
    "BANKNIFTY": [
        "AUBANK", "AXISBANK", "BANKBARODA", "CANBK", "FEDERALBNK",
        "HDFCBANK", "ICICIBANK", "IDFCFIRSTB", "INDUSINDBK", "KOTAKBANK",
        "PNB", "SBIN", "UNIONBANK", "YESBANK"
    ],
    "SENSEX": [
        "ADANIPORTS", "ASIANPAINT", "AXISBANK", "BAJFINANCE", "BAJAJFINSV",
        "BEL", "BHARTIARTL", "ETERNAL", "HCLTECH", "HDFCBANK",
        "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC",
        "KOTAKBANK", "LT", "M&M", "MARUTI", "NTPC",
        "POWERGRID", "RELIANCE", "SBIN", "SUNPHARMA", "TCS",
        "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO"
    ]
}

# Comprehensive baseline free-float weights for all constituents (normalized to 1.0)
DEFAULT_WEIGHTS = {
    "NIFTY": {
        "HDFCBANK": 0.1024, "ICICIBANK": 0.0914, "RELIANCE": 0.0747, "BHARTIARTL": 0.0488,
        "LT": 0.0455, "TCS": 0.0401, "ITC": 0.0387, "AXISBANK": 0.0366, "INFY": 0.0364,
        "SBIN": 0.0348, "KOTAKBANK": 0.0293, "M&M": 0.0262, "HINDUNILVR": 0.0230,
        "BAJFINANCE": 0.0209, "MARUTI": 0.0168, "SUNPHARMA": 0.0168, "NTPC": 0.0157,
        "TRENT": 0.0147, "TATASTEEL": 0.0136, "POWERGRID": 0.0136, "TITAN": 0.0136,
        "HCLTECH": 0.0136, "ULTRACEMCO": 0.0115, "BAJAJ-AUTO": 0.0115, "ASIANPAINT": 0.0110,
        "BEL": 0.0110, "COALINDIA": 0.0099, "JSWSTEEL": 0.0099, "ADANIPORTS": 0.0099,
        "ADANIENT": 0.0089, "ONGC": 0.0089, "GRASIM": 0.0084, "TECHM": 0.0084,
        "TMPV": 0.0084, "ETERNAL": 0.0084, "SHRIRAMFIN": 0.0084, "TATACONSUM": 0.0079,
        "SBILIFE": 0.0079, "DRREDDY": 0.0073, "CIPLA": 0.0073, "HDFCLIFE": 0.0073,
        "EICHERMOT": 0.0073, "JIOFIN": 0.0073, "NESTLEIND": 0.0073, "WIPRO": 0.0068,
        "APOLLOHOSP": 0.0068, "HINDALCO": 0.0068, "INDIGO": 0.0068, "BAJAJFINSV": 0.0063,
        "MAXHEALTH": 0.0052,
    },
    "BANKNIFTY": {
        "HDFCBANK": 0.2750, "ICICIBANK": 0.2350, "SBIN": 0.1120, "AXISBANK": 0.1020, "KOTAKBANK": 0.0850,
        "INDUSINDBK": 0.0450, "BANKBARODA": 0.0280, "FEDERALBNK": 0.0250, "PNB": 0.0220, "AUBANK": 0.0200,
        "IDFCFIRSTB": 0.0180, "CANBK": 0.0150, "UNIONBANK": 0.0100, "YESBANK": 0.0080,
    },
    "SENSEX": {
        "HDFCBANK": 0.1380, "ICICIBANK": 0.1080, "RELIANCE": 0.1020, "INFY": 0.0640, "BHARTIARTL": 0.0590,
        "LT": 0.0520, "TCS": 0.0470, "ITC": 0.0450, "AXISBANK": 0.0400, "SBIN": 0.0380,
        "KOTAKBANK": 0.0340, "M&M": 0.0310, "HINDUNILVR": 0.0270, "BAJFINANCE": 0.0250, "MARUTI": 0.0200,
        "SUNPHARMA": 0.0200, "NTPC": 0.0180, "TRENT": 0.0170, "TATASTEEL": 0.0160, "POWERGRID": 0.0160,
        "TITAN": 0.0160, "HCLTECH": 0.0160, "ULTRACEMCO": 0.0140, "ASIANPAINT": 0.0130, "BEL": 0.0130,
        "ADANIPORTS": 0.0120, "TECHM": 0.0100, "ETERNAL": 0.0100, "INDIGO": 0.0080, "BAJAJFINSV": 0.0070,
    }
}

# Top Heavyweights Reference Config with official sectors and weights
INDEX_TOP_HEAVYWEIGHTS = {
    "NIFTY": [
        {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "sector": "Financial Services", "weight": 10.24},
        {"symbol": "ICICIBANK", "name": "ICICI Bank Ltd.", "sector": "Financial Services", "weight": 9.14},
        {"symbol": "RELIANCE", "name": "Reliance Industries Ltd.", "sector": "Oil & Gas / Conglomerate", "weight": 7.47},
        {"symbol": "BHARTIARTL", "name": "Bharti Airtel Ltd.", "sector": "Telecommunication", "weight": 4.88},
        {"symbol": "LT", "name": "Larsen & Toubro Ltd.", "sector": "Construction / Engineering", "weight": 4.55},
        {"symbol": "AXISBANK", "name": "Axis Bank Ltd.", "sector": "Financial Services", "weight": 3.66},
        {"symbol": "INFY", "name": "Infosys Ltd.", "sector": "Information Technology", "weight": 3.64},
        {"symbol": "SBIN", "name": "State Bank of India", "sector": "Financial Services", "weight": 3.48},
    ],
    "BANKNIFTY": [
        {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "sector": "Financial Services", "weight": 27.50},
        {"symbol": "ICICIBANK", "name": "ICICI Bank Ltd.", "sector": "Financial Services", "weight": 23.50},
        {"symbol": "SBIN", "name": "State Bank of India", "sector": "Financial Services", "weight": 11.20},
        {"symbol": "AXISBANK", "name": "Axis Bank Ltd.", "sector": "Financial Services", "weight": 10.20},
        {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank Ltd.", "sector": "Financial Services", "weight": 8.50},
    ],
}

# ScanX API endpoints for rapid live constituent snapshots
_SCANX_HEATMAP_API = "https://ow-scanx-analytics.dhan.co/customscan/fetchdt"
_SCANX_INDEX_IDS = {"NIFTY": "13", "BANKNIFTY": "25"}

# In-memory caches to prevent rate limiting
_LIVE_CHANGES_CACHE = {}  # ticker -> (change_pct, timestamp)
_LIVE_CHANGES_CACHE_TTL_SEC = 90  # 90 seconds TTL for fast responsive tracking

def get_index_weights_state() -> dict:
    """Loads weights from data/cache/index_weights_state.json. If missing, returns default fallback."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                if data and "weights" in data:
                    return data
        except Exception as e:
            log.warning("Failed to parse cached index weights: %s", e)

    # Reconstruct default state
    default_state = {
        "last_refresh": "Fallback (Static Defaults)",
        "weights": {}
    }
    for idx_name, constituents in INDEX_CONSTITUENTS.items():
        raw_weights = DEFAULT_WEIGHTS.get(idx_name, {})
        total = sum(raw_weights.get(c, 0.0) for c in constituents)
        if total > 0:
            default_state["weights"][idx_name] = {c: raw_weights.get(c, 0.0) / total for c in constituents}
        else:
            default_state["weights"][idx_name] = {c: 1.0 / len(constituents) for c in constituents}
    return default_state

def refresh_index_weights(force: bool = False) -> dict:
    """
    Sync implementation of weights refresh. Fetch marketCap from yfinance,
    computes relative float-based weightings, and updates data/cache/index_weights_state.json.
    """
    global REFRESH_LOCK
    if not REFRESH_LOCK.acquire(blocking=False):
        log.info("Weights refresh already in progress.")
        return get_index_weights_state()

    try:
        now_dt = datetime.now(IST)
        state = get_index_weights_state()
        
        # Check if weekly refresh is required (Every Monday, or if last refresh was > 6 days ago, or force)
        need_refresh = force
        if not force and state.get("last_refresh") != "Fallback (Static Defaults)":
            try:
                last_dt = datetime.fromisoformat(state.get("last_refresh"))
                # If different ISO week, trigger refresh
                if last_dt.isocalendar()[1] != now_dt.isocalendar()[1] or (now_dt - last_dt).days >= 7:
                    need_refresh = True
            except Exception:
                need_refresh = True
        else:
            need_refresh = True

        if not need_refresh:
            return state

        log.info("Starting weekly Index Weightage refresh from Yahoo Finance...")
        
        # Gather all unique constituents
        all_unique = set()
        for constituents in INDEX_CONSTITUENTS.values():
            all_unique.update(constituents)

        # Build list of tickers to download. We query NSE for all, plus BO suffix for SENSEX constituents
        nse_tickers = [f"{c}.NS" for c in all_unique]
        bo_tickers = [f"{c}.BO" for c in INDEX_CONSTITUENTS["SENSEX"]]
        query_tickers = list(set(nse_tickers + bo_tickers))

        log.info("Fetching marketCap for %d tickers in a single batch...", len(query_tickers))

        # Fetch tickers info with retry session
        session = _get_yf_session()
        tickers_data = yf.Tickers(" ".join(query_tickers), session=session)
        mcaps = {}
        for ticker in query_tickers:
            try:
                t_obj = tickers_data.tickers[ticker]
                mcap = getattr(getattr(t_obj, "fast_info", None), "market_cap", None)
                if not mcap:
                    mcap = t_obj.info.get("marketCap")
                if mcap:
                    mcaps[ticker] = float(mcap)
            except Exception as e:
                log.warning("Failed to fetch mcap for %s: %s", ticker, e)

        # Compute free float market caps
        ff_mcaps = {}
        for ticker, mcap in mcaps.items():
            base = ticker.split(".")[0]
            factor = FREE_FLOAT_FACTORS.get(base, 1.0)
            ff_mcaps[ticker] = mcap * factor

        # Compute relative weights for each index
        new_weights = {}
        for idx_name, constituents in INDEX_CONSTITUENTS.items():
            idx_ff_mcaps = {}
            for c in constituents:
                # Prefer .NS ticker since Yahoo Finance provides robust data on NSE
                val = ff_mcaps.get(f"{c}.NS")
                if val is None:
                    val = ff_mcaps.get(f"{c}.BO")
                if val is None:
                    log.warning("Constituent %s mcap missing during refresh. Using static fallback.", c)
                    default_idx = DEFAULT_WEIGHTS.get(idx_name, {})
                    fallback_w = default_idx.get(c, 0.01)
                    val = fallback_w * 1e12  # arbitrary dummy large float
                idx_ff_mcaps[c] = val

            total_ff = sum(idx_ff_mcaps.values())
            if total_ff > 0:
                new_weights[idx_name] = {c: val / total_ff for c, val in idx_ff_mcaps.items()}
            else:
                default_idx = DEFAULT_WEIGHTS.get(idx_name, {})
                tot_def = sum(default_idx.get(c, 0.01) for c in constituents)
                new_weights[idx_name] = {c: default_idx.get(c, 0.01) / tot_def for c in constituents}

        # Cache the results
        os.makedirs(CACHE_DIR, exist_ok=True)
        cached_data = {
            "last_refresh": now_dt.isoformat(),
            "weights": new_weights
        }
        with open(CACHE_FILE, "w") as f:
            json.dump(cached_data, f, indent=2)
            
        log.info("Index weights refreshed successfully! Caching completed.")
        return cached_data
    except Exception as e:
        log.exception("Failed to refresh index weights: %s", e)
        return get_index_weights_state()
    finally:
        REFRESH_LOCK.release()

def refresh_index_weights_async(force: bool = False) -> None:
    """Launches index weights refresh on a background thread to prevent blocking main scans."""
    t = threading.Thread(target=refresh_index_weights, args=(force,), name="IndexWeightsRefresh")
    t.daemon = True
    t.start()


def get_last_weights_refresh_date() -> date | None:
    """Return the date of the last successful weights refresh, or None."""
    state = get_index_weights_state()
    lr = state.get("last_refresh")
    if not lr or lr == "Fallback (Static Defaults)":
        return None
    try:
        return datetime.fromisoformat(lr).date()
    except Exception:
        return None


def is_index_weights_updated_today(target_date: date | None = None) -> bool:
    """Check if index weights have already been refreshed on target_date (defaults to today IST)."""
    if target_date is None:
        target_date = datetime.now(IST).date()
    last_date = get_last_weights_refresh_date()
    return last_date is not None and last_date == target_date


def _fetch_scanx_constituent_changes(idx_name: str) -> dict:
    """Fetch live percentage changes for index constituents via Dhan ScanX API."""
    if idx_name not in _SCANX_INDEX_IDS:
        return {}
    idx_id = _SCANX_INDEX_IDS[idx_name]
    payload = {
        "data": {
            "params": [
                {"field": "idxlist.Indexid", "op": "", "val": idx_id},
                {"field": "Exch", "op": "", "val": "NSE"},
            ],
            "logic_op": "AND",
            "fields": ["Sym", "PPerchange", "Mcap", "Sector"],
            "count": 500,
            "sort": "Mcap",
            "sorder": "desc",
        }
    }
    try:
        res = _requests.post(
            _SCANX_HEATMAP_API,
            json=payload,
            timeout=8,
            verify=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        res.raise_for_status()
        body = res.json() if res.content else {}
        data = body.get("data") if isinstance(body, dict) else []
        if isinstance(data, list) and data:
            now = time.time()
            out = {}
            for item in data:
                sym = str(item.get("Sym") or "").strip().upper()
                if sym:
                    try:
                        pch = float(item.get("PPerchange") or 0.0)
                        out[sym] = pch
                        _LIVE_CHANGES_CACHE[f"{sym}.NS"] = (pch, now)
                    except (ValueError, TypeError):
                        pass
            return out
    except Exception as exc:
        log.warning("ScanX constituent fetch failed for %s: %s", idx_name, exc)
    return {}


def get_live_constituent_changes(symbol: str) -> dict:
    """
    Fetches the daily change percentage for constituents of the index (NIFTY, SENSEX, BANKNIFTY).
    Uses Dhan ScanX fast API first with in-memory caching, then falls back to yfinance.
    """
    idx_name = symbol.upper()
    if idx_name not in INDEX_CONSTITUENTS:
        return {}

    constituents = INDEX_CONSTITUENTS[idx_name]
    suffix = ".BO" if idx_name == "SENSEX" else ".NS"
    tickers = [f"{c}{suffix}" for c in constituents]

    now = time.time()
    
    # Resolve from cache first
    missing = []
    resolved = {}
    for ticker in tickers:
        cached = _LIVE_CHANGES_CACHE.get(ticker)
        if cached and (now - cached[1]) < _LIVE_CHANGES_CACHE_TTL_SEC:
            resolved[ticker] = cached[0]
        else:
            missing.append(ticker)

    if not missing:
        return {t.split(".")[0]: v for t, v in resolved.items()}

    # Attempt 1: Dhan ScanX API for NIFTY / BANKNIFTY (instant sub-300ms live NSE snapshot)
    if idx_name in _SCANX_INDEX_IDS:
        scanx_data = _fetch_scanx_constituent_changes(idx_name)
        if scanx_data and len(scanx_data) >= len(constituents) * 0.70:
            for ticker in list(missing):
                base_sym = ticker.split(".")[0]
                if base_sym in scanx_data:
                    val = scanx_data[base_sym]
                    resolved[ticker] = val
                    _LIVE_CHANGES_CACHE[ticker] = (val, now)
                    missing.remove(ticker)

    if not missing:
        return {t.split(".")[0]: v for t, v in resolved.items()}

    # Attempt 2: yfinance batch download fallback
    try:
        log.info("Fetching live constituent changePct for %d missing tickers from yfinance...", len(missing))

        session = _get_yf_session()
        df = yf.download(
            missing,
            period="2d",
            group_by="ticker",
            progress=False,
            timeout=12,
            session=session,
        )

        if df is None or df.empty:
            log.info("yfinance returned empty data for %s — retrying with period=5d", missing)
            time.sleep(2)
            df = yf.download(
                missing,
                period="5d",
                group_by="ticker",
                progress=False,
                timeout=12,
                session=session,
            )

        for ticker in missing:
            try:
                if df is None or df.empty:
                    resolved[ticker] = 0.0
                    continue
                ticker_df = df[ticker] if len(missing) > 1 else df
                if ticker_df is None or ticker_df.empty:
                    resolved[ticker] = 0.0
                    continue
                close_prices = ticker_df["Close"].dropna()
                change_pct = 0.0
                if len(close_prices) >= 2:
                    prev_close = float(close_prices.iloc[-2])
                    last_price = float(close_prices.iloc[-1])
                    if prev_close > 0:
                        change_pct = ((last_price - prev_close) / prev_close) * 100.0
                elif len(close_prices) == 1:
                    open_p = float(ticker_df["Open"].dropna().iloc[-1])
                    last_p = float(close_prices.iloc[-1])
                    if open_p > 0:
                        change_pct = ((last_p - open_p) / open_p) * 100.0

                _LIVE_CHANGES_CACHE[ticker] = (change_pct, now)
                resolved[ticker] = change_pct
            except Exception as e:
                log.warning("Failed to parse changes for %s: %s", ticker, e)
                resolved[ticker] = 0.0
    except Exception as e:
        log.warning("yfinance live constituent changes download failed: %s", e)
        for ticker in missing:
            resolved[ticker] = 0.0

    return {t.split(".")[0]: v for t, v in resolved.items()}


def calculate_index_momentum(symbol: str) -> dict:
    """
    Computes the weighted index momentum score and constituent breakdown.
    Tracks top index heavyweights, banking sector drag/support, and directional alignment.
    Returns:
        dict: {
            "weighted_momentum": float,
            "direction": str,
            "banking_drag": float,
            "top_heavyweights": list[dict],
            "heavyweight_summary": str,
            "heavyweight_conflict": bool,
            "advance_decline": dict,
            "constituents": list[dict],
            "last_refresh": str,
        }
    """
    idx_name = symbol.upper()
    if idx_name not in INDEX_CONSTITUENTS:
        return {}

    state = get_index_weights_state()
    weights = state.get("weights", {}).get(idx_name, {})
    if not weights:
        weights = DEFAULT_WEIGHTS.get(idx_name, {})
        if idx_name == "SENSEX":
            nifty_w = DEFAULT_WEIGHTS.get("NIFTY", {})
            total = sum(nifty_w.get(c, 0.0) for c in INDEX_CONSTITUENTS["SENSEX"])
            weights = {c: nifty_w.get(c, 0.0) / total for c in INDEX_CONSTITUENTS["SENSEX"]}

    live_changes = get_live_constituent_changes(idx_name)

    weighted_sum = 0.0
    total_weight = 0.0
    constituents_data = []
    adv_count = 0
    dec_count = 0
    unch_count = 0

    for c in INDEX_CONSTITUENTS[idx_name]:
        weight = float(weights.get(c, 0.0))
        change = float(live_changes.get(c, 0.0))
        weighted_sum += weight * change
        total_weight += weight

        if change > 0.01:
            adv_count += 1
        elif change < -0.01:
            dec_count += 1
        else:
            unch_count += 1

        constituents_data.append({
            "symbol": c,
            "weight_pct": round(weight * 100.0, 2),
            "change_pct": round(change, 2),
            "weighted_contribution": round(weight * change, 4)
        })

    weighted_momentum = (weighted_sum / total_weight) if total_weight > 0 else 0.0
    
    # Determine direction
    if weighted_momentum >= 0.25:
        direction = "BULLISH"
    elif weighted_momentum <= -0.25:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    # Compute Banking Drag Factor
    # For NIFTY: HDFCBANK, ICICIBANK, AXISBANK, SBIN, KOTAKBANK
    # For BANKNIFTY: all constituents
    bank_symbols = {"HDFCBANK", "ICICIBANK", "AXISBANK", "SBIN", "KOTAKBANK", "INDUSINDBK", "BANKBARODA", "FEDERALBNK", "PNB"}
    bank_weight_sum = 0.0
    bank_contrib_sum = 0.0
    for c in constituents_data:
        sym = c["symbol"]
        if sym in bank_symbols or idx_name == "BANKNIFTY":
            w = c["weight_pct"]
            bank_weight_sum += w
            bank_contrib_sum += w * c["change_pct"]
    
    banking_drag = round(bank_contrib_sum / bank_weight_sum, 2) if bank_weight_sum > 0 else 0.0

    # Top Heavyweights Reference Details
    top_cfg = INDEX_TOP_HEAVYWEIGHTS.get(idx_name, [])
    top_heavyweights = []
    top_summary_parts = []
    top_pillars_up = 0
    top_pillars_down = 0

    for item in top_cfg:
        sym = item["symbol"]
        chg = live_changes.get(sym, 0.0)
        w = item["weight"]
        sec = item.get("sector", "")
        status = "🟢" if chg > 0.05 else ("🔴" if chg < -0.05 else "⚪")
        if sym in ("HDFCBANK", "ICICIBANK", "RELIANCE"):
            if chg > 0.05:
                top_pillars_up += 1
            elif chg < -0.05:
                top_pillars_down += 1

        top_heavyweights.append({
            "symbol": sym,
            "name": item.get("name", sym),
            "sector": sec,
            "weight_pct": w,
            "change_pct": round(chg, 2),
            "weighted_impact": round(w * chg / 100.0, 3),
            "status": status,
        })
        top_summary_parts.append(f"{sym} {chg:+.2f}%")

    # Conflict detection: If top 3 pillars (HDFCBANK, ICICIBANK, RELIANCE) oppose the weighted direction
    heavyweight_conflict = False
    if direction == "BULLISH" and top_pillars_down >= 2:
        heavyweight_conflict = True
    elif direction == "BEARISH" and top_pillars_up >= 2:
        heavyweight_conflict = True
    elif idx_name == "NIFTY" and direction == "BULLISH" and banking_drag <= -0.75:
        heavyweight_conflict = True
    elif idx_name == "NIFTY" and direction == "BEARISH" and banking_drag >= 0.75:
        heavyweight_conflict = True

    # Concise summary for notifications and logs
    top_summary_str = " | ".join(top_summary_parts[:5])
    if banking_drag != 0.0 and idx_name == "NIFTY":
        top_summary_str += f" | Banks: {banking_drag:+.2f}%"

    # Sort constituents by absolute change descending for UI
    constituents_data = sorted(constituents_data, key=lambda x: abs(x["change_pct"]), reverse=True)

    return {
        "weighted_momentum": round(weighted_momentum, 3),
        "direction": direction,
        "banking_drag": banking_drag,
        "top_heavyweights": top_heavyweights,
        "heavyweight_summary": top_summary_str,
        "heavyweight_conflict": heavyweight_conflict,
        "advance_decline": {"adv": adv_count, "dec": dec_count, "unch": unch_count},
        "constituents": constituents_data,
        "last_refresh": state.get("last_refresh", "N/A"),
    }
