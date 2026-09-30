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
from datetime import datetime

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
        "HDFCBANK": 0.1120, "ICICIBANK": 0.0880, "RELIANCE": 0.0830, "INFY": 0.0520, "BHARTIARTL": 0.0480,
        "LT": 0.0420, "TCS": 0.0380, "ITC": 0.0370, "AXISBANK": 0.0330, "SBIN": 0.0310,
        "KOTAKBANK": 0.0280, "M&M": 0.0250, "HINDUNILVR": 0.0220, "BAJFINANCE": 0.0200, "MARUTI": 0.0160,
        "SUNPHARMA": 0.0160, "NTPC": 0.0150, "TRENT": 0.0140, "TATASTEEL": 0.0130, "POWERGRID": 0.0130,
        "TITAN": 0.0130, "HCLTECH": 0.0130, "ULTRACEMCO": 0.0110, "BAJAJ-AUTO": 0.0110, "ASIANPAINT": 0.0105,
        "BEL": 0.0105, "COALINDIA": 0.0095, "JSWSTEEL": 0.0095, "ADANIPORTS": 0.0095, "ADANIENT": 0.0085,
        "ONGC": 0.0085, "GRASIM": 0.0080, "TECHM": 0.0080, "TMPV": 0.0080, "ETERNAL": 0.0080,
        "SHRIRAMFIN": 0.0080, "TATACONSUM": 0.0075, "SBILIFE": 0.0075, "DRREDDY": 0.0070, "CIPLA": 0.0070,
        "HDFCLIFE": 0.0070, "EICHERMOT": 0.0070, "JIOFIN": 0.0070, "NESTLEIND": 0.0070, "WIPRO": 0.0065,
        "APOLLOHOSP": 0.0065, "HINDALCO": 0.0065, "INDIGO": 0.0065, "BAJAJFINSV": 0.0060, "MAXHEALTH": 0.0050,
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

# In-memory caches to prevent rate limiting
_LIVE_CHANGES_CACHE = {}  # ticker -> (change_pct, timestamp)
_LIVE_CHANGES_CACHE_TTL_SEC = 180

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

def get_live_constituent_changes(symbol: str) -> dict:
    """
    Fetches the daily change percentage for constituents of the index (NIFTY, SENSEX, BANKNIFTY).
    Uses a 3-minute in-memory cache to prevent Yahoo Finance API rate limits.
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

    try:
        log.info("Fetching live constituent changePct for %d missing tickers from yfinance...", len(missing))

        # We download 2d daily candles to get the accurate current regularMarketChangePercent
        # Using yf.download is much faster than yf.Tickers.info in a loop
        # yfinance may return "possibly delisted" errors for Indian .NS tickers due to
        # intermittent data gaps or rate limiting. We use a persistent session with retry
        # headers and fall back to longer periods if the first attempt yields empty data.
        session = _get_yf_session()
        df = yf.download(
            missing,
            period="2d",
            group_by="ticker",
            progress=False,
            timeout=12,
            session=session,
        )

        # Check if data is empty (all tickers failed). If so, retry once with longer period.
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
                    # Fallback: check change from Open if only today's price exists
                    open_p = float(ticker_df["Open"].dropna().iloc[-1])
                    last_p = float(close_prices.iloc[-1])
                    if open_p > 0:
                        change_pct = ((last_p - open_p) / open_p) * 100.0

                # Cache it
                _LIVE_CHANGES_CACHE[ticker] = (change_pct, now)
                resolved[ticker] = change_pct
            except Exception as e:
                log.warning("Failed to parse changes for %s: %s", ticker, e)
                resolved[ticker] = 0.0
    except Exception as e:
        log.warning("yfinance live constituent changes download failed: %s", e)
        # If download failed completely, default missing to 0.0
        for ticker in missing:
            resolved[ticker] = 0.0

    return {t.split(".")[0]: v for t, v in resolved.items()}

def calculate_index_momentum(symbol: str) -> dict:
    """
    Computes the weighted index momentum score.
    Returns {"weighted_momentum": float, "direction": str, "constituents": list[dict], "last_refresh": str}
    """
    idx_name = symbol.upper()
    if idx_name not in INDEX_CONSTITUENTS:
        return {}

    state = get_index_weights_state()
    weights = state.get("weights", {}).get(idx_name, {})
    if not weights:
        # Fallback to default
        weights = DEFAULT_WEIGHTS.get(idx_name, {})
        if idx_name == "SENSEX":
            nifty_w = DEFAULT_WEIGHTS.get("NIFTY", {})
            total = sum(nifty_w.get(c, 0.0) for c in INDEX_CONSTITUENTS["SENSEX"])
            weights = {c: nifty_w.get(c, 0.0) / total for c in INDEX_CONSTITUENTS["SENSEX"]}

    live_changes = get_live_constituent_changes(idx_name)

    weighted_sum = 0.0
    total_weight = 0.0
    constituents_data = []

    for c in INDEX_CONSTITUENTS[idx_name]:
        weight = float(weights.get(c, 0.0))
        change = float(live_changes.get(c, 0.0))
        weighted_sum += weight * change
        total_weight += weight
        constituents_data.append({
            "symbol": c,
            "weight_pct": round(weight * 100.0, 2),
            "change_pct": round(change, 2)
        })

    weighted_momentum = (weighted_sum / total_weight) if total_weight > 0 else 0.0
    
    # Determine direction
    if weighted_momentum >= 0.50:
        direction = "BULLISH"
    elif weighted_momentum <= -0.50:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    # Sort constituents by absolute change descending for UI
    constituents_data = sorted(constituents_data, key=lambda x: abs(x["change_pct"]), reverse=True)

    return {
        "weighted_momentum": round(weighted_momentum, 3),
        "direction": direction,
        "constituents": constituents_data,
        "last_refresh": state.get("last_refresh", "N/A")
    }
