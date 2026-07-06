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
import yfinance as yf

log = logging.getLogger(__name__)

CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "cache"))
CACHE_FILE = os.path.join(CACHE_DIR, "index_weights_state.json")

IST = pytz.timezone("Asia/Kolkata")
REFRESH_LOCK = threading.Lock()

# Curated free-float factors
FREE_FLOAT_FACTORS = {
    # NIFTY / SENSEX / BANKNIFTY unique constituents
    "RELIANCE": 0.50,
    "TCS": 0.28,
    "HDFCBANK": 1.00,
    "ICICIBANK": 1.00,
    "INFY": 0.85,
    "ITC": 1.00,
    "BHARTIARTL": 0.45,
    "LT": 1.00,
    "AXISBANK": 1.00,
    "SBIN": 0.43,
    "KOTAKBANK": 0.74,
    "M&M": 0.81,
    "HINDUNILVR": 0.38,
    "TMPV": 0.54,      # successor demerged passenger vehicles entity
    "TMCV": 0.54,      # commercial vehicles entity
    "BAJFINANCE": 0.45,
    "MARUTI": 0.44,
    "SUNPHARMA": 0.46,
    "NTPC": 0.49,
    "HCLTECH": 0.39,
    "POWERGRID": 0.51,
    "INDUSINDBK": 0.85,
    "PNB": 0.27,
    "BANKBARODA": 0.36,
    "AUBANK": 0.75,
    "FEDERALBNK": 1.00,
    "IDFCFIRSTB": 0.60,
    "BANDHANBNK": 0.60,
    "CANBK": 0.37,
    "UNIONBANK": 0.25,
}

INDEX_CONSTITUENTS = {
    "NIFTY": [
        "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY",
        "ITC", "BHARTIARTL", "LT", "AXISBANK", "SBIN",
        "KOTAKBANK", "M&M", "HINDUNILVR", "TMPV", "BAJFINANCE",
        "MARUTI", "SUNPHARMA", "NTPC", "HCLTECH", "POWERGRID"
    ],
    "SENSEX": [
        "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY",
        "ITC", "BHARTIARTL", "LT", "AXISBANK", "SBIN",
        "KOTAKBANK", "M&M", "HINDUNILVR", "TMPV", "BAJFINANCE",
        "MARUTI", "SUNPHARMA", "NTPC", "HCLTECH", "POWERGRID"
    ],
    "BANKNIFTY": [
        "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK",
        "INDUSINDBK", "PNB", "BANKBARODA", "AUBANK", "FEDERALBNK",
        "IDFCFIRSTB", "BANDHANBNK", "CANBK", "UNIONBANK"
    ],
}

DEFAULT_WEIGHTS = {
    "NIFTY": {
        "RELIANCE": 0.130, "HDFCBANK": 0.120, "ICICIBANK": 0.090, "INFY": 0.070, "ITC": 0.060,
        "TCS": 0.055, "BHARTIARTL": 0.050, "LT": 0.045, "AXISBANK": 0.040, "SBIN": 0.035,
        "KOTAKBANK": 0.030, "M&M": 0.025, "HINDUNILVR": 0.025, "TMPV": 0.020, "BAJFINANCE": 0.020,
        "MARUTI": 0.018, "SUNPHARMA": 0.015, "NTPC": 0.015, "HCLTECH": 0.015, "POWERGRID": 0.012,
    },
    "BANKNIFTY": {
        "HDFCBANK": 0.290, "ICICIBANK": 0.230, "SBIN": 0.110, "AXISBANK": 0.100, "KOTAKBANK": 0.090,
        "INDUSINDBK": 0.060, "PNB": 0.025, "BANKBARODA": 0.025, "AUBANK": 0.015, "FEDERALBNK": 0.015,
        "IDFCFIRSTB": 0.012, "BANDHANBNK": 0.010, "CANBK": 0.010, "UNIONBANK": 0.008,
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
        if idx_name in DEFAULT_WEIGHTS:
            raw_weights = DEFAULT_WEIGHTS[idx_name]
            total = sum(raw_weights.values())
            if total > 0:
                default_state["weights"][idx_name] = {c: w / total for c, w in raw_weights.items()}
            else:
                default_state["weights"][idx_name] = raw_weights
        elif idx_name == "SENSEX":
            # Derive SENSEX fallback from NIFTY (since they share constituents)
            nifty_fallback = DEFAULT_WEIGHTS["NIFTY"]
            total = sum(nifty_fallback.get(c, 0.0) for c in constituents)
            if total > 0:
                default_state["weights"]["SENSEX"] = {c: nifty_fallback.get(c, 0.0) / total for c in constituents}
            else:
                default_state["weights"]["SENSEX"] = {c: 1.0 / len(constituents) for c in constituents}
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
        
        # Fetch tickers info
        tickers_data = yf.Tickers(" ".join(query_tickers))
        mcaps = {}
        for ticker in query_tickers:
            try:
                info = tickers_data.tickers[ticker].info
                mcap = info.get("marketCap")
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
            suffix = ".BO" if idx_name == "SENSEX" else ".NS"
            idx_ff_mcaps = {}
            for c in constituents:
                ticker = f"{c}{suffix}"
                # Fallback to defaults if fetching failed
                val = ff_mcaps.get(ticker)
                if val is None:
                    log.warning("Constituent %s mcap missing during refresh. Using static fallback.", ticker)
                    # Get static fallback relative weight
                    default_idx = DEFAULT_WEIGHTS.get(idx_name, {})
                    if idx_name == "SENSEX":
                        default_idx = DEFAULT_WEIGHTS.get("NIFTY", {})
                    fallback_w = default_idx.get(c, 0.05)
                    val = fallback_w * 1e12  # arbitrary dummy large float
                idx_ff_mcaps[c] = val

            total_ff = sum(idx_ff_mcaps.values())
            if total_ff > 0:
                new_weights[idx_name] = {c: val / total_ff for c, val in idx_ff_mcaps.items()}
            else:
                new_weights[idx_name] = {c: 1.0 / len(constituents) for c in constituents}

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
        
        # We download 2d daily candles to get the accurate current regularMarketChangePercent (previous close vs today's close)
        # Using yf.download is much faster than yf.Tickers.info in a loop
        df = yf.download(missing, period="2d", group_by="ticker", progress=False, timeout=8)
        
        for ticker in missing:
            try:
                ticker_df = df[ticker] if len(missing) > 1 else df
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
