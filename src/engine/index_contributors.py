"""
Index Contributors & Point Impact Engine.
Calculates stock-wise point contributions for NIFTY 50, BANK NIFTY, and SENSEX.
Supports Zerodha Kite live quotes when authenticated, with yfinance caching fallback.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

import yfinance as yf
from src.engine.index_weights import (
    get_index_weights_state,
    _get_yf_session,
    INDEX_CONSTITUENTS as BASE_INDEX_CONSTITUENTS,
)

log = logging.getLogger("src.engine.index_contributors")
IST = ZoneInfo("Asia/Kolkata")

# 50 Nifty Constituents, 14 Bank Nifty Constituents, 30 Sensex Constituents
INDEX_CONSTITUENTS: Dict[str, List[str]] = {
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

# Index display names & benchmark tickers
INDEX_INFO = {
    "NIFTY": {
        "display_name": "Nifty 50",
        "yf_ticker": "^NSEI",
        "kite_ticker": "NSE:NIFTY 50",
        "count": 50
    },
    "BANKNIFTY": {
        "display_name": "Bank Nifty",
        "yf_ticker": "^NSEBANK",
        "kite_ticker": "NSE:NIFTY BANK",
        "count": 14
    },
    "SENSEX": {
        "display_name": "Sensex 30",
        "yf_ticker": "^BSESN",
        "kite_ticker": "BSE:SENSEX",
        "count": 30
    }
}

# Stock metadata: Company Name & Sector
STOCK_METADATA: Dict[str, Dict[str, str]] = {
    "ADANIENT": {"name": "Adani Enterprises Ltd.", "sector": "Metals & Mining"},
    "ADANIPORTS": {"name": "Adani Ports and SEZ Ltd.", "sector": "Services / Infrastructure"},
    "APOLLOHOSP": {"name": "Apollo Hospitals Enterprise Ltd.", "sector": "Healthcare"},
    "ASIANPAINT": {"name": "Asian Paints Ltd.", "sector": "Consumer Durables"},
    "AUBANK": {"name": "AU Small Finance Bank Ltd.", "sector": "Financial Services"},
    "AXISBANK": {"name": "Axis Bank Ltd.", "sector": "Financial Services"},
    "BAJAJ-AUTO": {"name": "Bajaj Auto Ltd.", "sector": "Automobile"},
    "BAJFINANCE": {"name": "Bajaj Finance Ltd.", "sector": "Financial Services"},
    "BAJAJFINSV": {"name": "Bajaj Finserv Ltd.", "sector": "Financial Services"},
    "BANKBARODA": {"name": "Bank of Baroda", "sector": "Financial Services"},
    "BEL": {"name": "Bharat Electronics Ltd.", "sector": "Capital Goods / Defence"},
    "BHARTIARTL": {"name": "Bharti Airtel Ltd.", "sector": "Telecommunication"},
    "CANBK": {"name": "Canara Bank", "sector": "Financial Services"},
    "CIPLA": {"name": "Cipla Ltd.", "sector": "Healthcare"},
    "COALINDIA": {"name": "Coal India Ltd.", "sector": "Energy / Mining"},
    "DRREDDY": {"name": "Dr. Reddy's Laboratories Ltd.", "sector": "Healthcare"},
    "EICHERMOT": {"name": "Eicher Motors Ltd.", "sector": "Automobile"},
    "ETERNAL": {"name": "Eternal Ltd.", "sector": "Consumer Services / Tech"},
    "FEDERALBNK": {"name": "Federal Bank Ltd.", "sector": "Financial Services"},
    "GRASIM": {"name": "Grasim Industries Ltd.", "sector": "Construction Materials"},
    "HCLTECH": {"name": "HCL Technologies Ltd.", "sector": "Information Technology"},
    "HDFCBANK": {"name": "HDFC Bank Ltd.", "sector": "Financial Services"},
    "HDFCLIFE": {"name": "HDFC Life Insurance Co. Ltd.", "sector": "Financial Services"},
    "HINDALCO": {"name": "Hindalco Industries Ltd.", "sector": "Metals & Mining"},
    "HINDUNILVR": {"name": "Hindustan Unilever Ltd.", "sector": "FMCG"},
    "ICICIBANK": {"name": "ICICI Bank Ltd.", "sector": "Financial Services"},
    "IDFCFIRSTB": {"name": "IDFC First Bank Ltd.", "sector": "Financial Services"},
    "INDIGO": {"name": "InterGlobe Aviation (IndiGo)", "sector": "Services / Aviation"},
    "INDUSINDBK": {"name": "IndusInd Bank Ltd.", "sector": "Financial Services"},
    "INFY": {"name": "Infosys Ltd.", "sector": "Information Technology"},
    "ITC": {"name": "ITC Ltd.", "sector": "FMCG"},
    "JIOFIN": {"name": "Jio Financial Services Ltd.", "sector": "Financial Services"},
    "JSWSTEEL": {"name": "JSW Steel Ltd.", "sector": "Metals & Mining"},
    "KOTAKBANK": {"name": "Kotak Mahindra Bank Ltd.", "sector": "Financial Services"},
    "LT": {"name": "Larsen & Toubro Ltd.", "sector": "Construction / Engineering"},
    "M&M": {"name": "Mahindra & Mahindra Ltd.", "sector": "Automobile"},
    "MARUTI": {"name": "Maruti Suzuki India Ltd.", "sector": "Automobile"},
    "MAXHEALTH": {"name": "Max Healthcare Institute Ltd.", "sector": "Healthcare"},
    "NESTLEIND": {"name": "Nestle India Ltd.", "sector": "FMCG"},
    "NTPC": {"name": "NTPC Ltd.", "sector": "Power / Utilities"},
    "ONGC": {"name": "Oil & Natural Gas Corp. Ltd.", "sector": "Oil & Gas"},
    "PNB": {"name": "Punjab National Bank", "sector": "Financial Services"},
    "POWERGRID": {"name": "Power Grid Corp. of India Ltd.", "sector": "Power / Utilities"},
    "RELIANCE": {"name": "Reliance Industries Ltd.", "sector": "Oil & Gas / Conglomerate"},
    "SBILIFE": {"name": "SBI Life Insurance Co. Ltd.", "sector": "Financial Services"},
    "SBIN": {"name": "State Bank of India", "sector": "Financial Services"},
    "SHRIRAMFIN": {"name": "Shriram Finance Ltd.", "sector": "Financial Services"},
    "SUNPHARMA": {"name": "Sun Pharmaceutical Industries Ltd.", "sector": "Healthcare"},
    "TATACONSUM": {"name": "Tata Consumer Products Ltd.", "sector": "FMCG"},
    "TATASTEEL": {"name": "Tata Steel Ltd.", "sector": "Metals & Mining"},
    "TCS": {"name": "Tata Consultancy Services Ltd.", "sector": "Information Technology"},
    "TECHM": {"name": "Tech Mahindra Ltd.", "sector": "Information Technology"},
    "TITAN": {"name": "Titan Company Ltd.", "sector": "Consumer Durables"},
    "TMPV": {"name": "Tata Motors Passenger Vehicles Ltd.", "sector": "Automobile"},
    "TRENT": {"name": "Trent Ltd.", "sector": "Consumer Services / Retail"},
    "ULTRACEMCO": {"name": "UltraTech Cement Ltd.", "sector": "Construction Materials"},
    "UNIONBANK": {"name": "Union Bank of India", "sector": "Financial Services"},
    "WIPRO": {"name": "Wipro Ltd.", "sector": "Information Technology"},
    "YESBANK": {"name": "Yes Bank Ltd.", "sector": "Financial Services"}
}

# In-memory cache for contributors payload to avoid hammering Yahoo Finance
_CONTRIBUTORS_CACHE: Dict[str, tuple[dict, float]] = {}
_CACHE_TTL_SECONDS = 60.0 # 1 minute TTL


def _get_kite():
    """Lazily load Kite client if available and active."""
    try:
        from src.engine.live_trading import get_kite_client
        return get_kite_client()
    except Exception:
        return None


def get_index_contributors(index_name: str = "NIFTY", force_refresh: bool = False) -> Dict[str, Any]:
    """
    Computes stock-wise point contributions for the requested index.
    
    Returns:
        dict: Full summary, KPI cards, sorted gainers/losers, and detailed constituents table.
    """
    idx_key = index_name.upper().strip()
    if idx_key not in INDEX_CONSTITUENTS:
        idx_key = "NIFTY"

    now = time.time()
    if not force_refresh and idx_key in _CONTRIBUTORS_CACHE:
        cached_data, cached_at = _CONTRIBUTORS_CACHE[idx_key]
        if (now - cached_at) < _CACHE_TTL_SECONDS:
            return cached_data

    constituents = INDEX_CONSTITUENTS[idx_key]
    weights_state = get_index_weights_state()
    index_weights = weights_state.get("weights", {}).get(idx_key, {})
    
    # Fallback to equal weighting if index_weights missing or incomplete
    if not index_weights or len(index_weights) < len(constituents) * 0.7:
        eq = 1.0 / len(constituents)
        index_weights = {c: eq for c in constituents}
    else:
        # Normalize weights so they sum to exactly 1.0
        tot_w = sum(index_weights.get(c, 0.0) for c in constituents)
        if tot_w > 0:
            index_weights = {c: index_weights.get(c, 0.0) / tot_w for c in constituents}
        else:
            eq = 1.0 / len(constituents)
            index_weights = {c: eq for c in constituents}

    quotes: Dict[str, Dict[str, float]] = {}
    index_price: float = 0.0
    index_prev_close: float = 0.0

    kite = _get_kite()
    kite_success = False

    # Attempt 1: Kite real-time quote
    if kite:
        try:
            exchange = "BSE" if idx_key == "SENSEX" else "NSE"
            symbol_map = {}
            query_symbols = []
            
            # Map index ticker
            idx_kite_sym = INDEX_INFO[idx_key]["kite_ticker"]
            query_symbols.append(idx_kite_sym)

            for sym in constituents:
                # Handle special tickers if needed
                inst = f"{exchange}:{sym}"
                symbol_map[inst] = sym
                query_symbols.append(inst)

            batch_quotes = kite.quote(query_symbols)
            if idx_kite_sym in batch_quotes:
                idx_q = batch_quotes[idx_kite_sym]
                index_price = float(idx_q.get("last_price", 0.0))
                ohlc = idx_q.get("ohlc", {})
                index_prev_close = float(ohlc.get("close", index_price))

            for inst, sym in symbol_map.items():
                if inst in batch_quotes:
                    q = batch_quotes[inst]
                    ltp = float(q.get("last_price", 0.0))
                    prev = float(q.get("ohlc", {}).get("close", ltp))
                    if prev > 0:
                        chg_pct = ((ltp - prev) / prev) * 100.0
                    else:
                        chg_pct = 0.0
                    quotes[sym] = {
                        "ltp": ltp,
                        "prev_close": prev,
                        "change_pct": chg_pct
                    }
            if len(quotes) >= len(constituents) * 0.8:
                kite_success = True
                log.info("[%s] Successfully loaded live quotes from Kite for %d constituents", idx_key, len(quotes))
        except Exception as e:
            log.warning("[%s] Kite quote fetch failed, falling back to yfinance: %s", idx_key, e)

    # Attempt 2: Yahoo Finance batch download fallback
    if not kite_success:
        try:
            suffix = ".BO" if idx_key == "SENSEX" else ".NS"
            tickers = [f"{c}{suffix}" for c in constituents]
            idx_yf_ticker = INDEX_INFO[idx_key]["yf_ticker"]
            tickers.append(idx_yf_ticker)

            session = _get_yf_session()
            df = yf.download(
                tickers,
                period="2d",
                group_by="ticker",
                progress=False,
                timeout=12,
                session=session
            )

            # Extract index price & previous close
            if df is not None and not df.empty and idx_yf_ticker in df:
                idx_df = df[idx_yf_ticker]
                closes = idx_df["Close"].dropna()
                if len(closes) >= 2:
                    index_prev_close = float(closes.iloc[-2])
                    index_price = float(closes.iloc[-1])
                elif len(closes) == 1:
                    index_price = float(closes.iloc[-1])
                    index_prev_close = index_price

            for sym in constituents:
                t = f"{sym}{suffix}"
                if df is not None and not df.empty and t in df:
                    tdf = df[t]
                    closes = tdf["Close"].dropna()
                    if len(closes) >= 2:
                        prev = float(closes.iloc[-2])
                        curr = float(closes.iloc[-1])
                        chg_pct = ((curr - prev) / prev) * 100.0 if prev > 0 else 0.0
                        quotes[sym] = {"ltp": curr, "prev_close": prev, "change_pct": chg_pct}
                    elif len(closes) == 1:
                        curr = float(closes.iloc[-1])
                        quotes[sym] = {"ltp": curr, "prev_close": curr, "change_pct": 0.0}
                    else:
                        quotes[sym] = {"ltp": 0.0, "prev_close": 0.0, "change_pct": 0.0}
                else:
                    quotes[sym] = {"ltp": 0.0, "prev_close": 0.0, "change_pct": 0.0}
        except Exception as e:
            log.error("[%s] yfinance constituent fetch failed: %s", idx_key, e)
            for sym in constituents:
                quotes[sym] = {"ltp": 0.0, "prev_close": 0.0, "change_pct": 0.0}

    # If index previous close is still not found, establish default or approximate base
    if index_prev_close <= 0:
        index_prev_close = 24000.0 if idx_key == "NIFTY" else (52000.0 if idx_key == "BANKNIFTY" else 78000.0)
    if index_price <= 0:
        index_price = index_prev_close

    # Calculate point contributions for each constituent
    constituent_records: List[Dict[str, Any]] = []
    total_positive_pts = 0.0
    total_negative_pts = 0.0
    positive_weight_sum = 0.0
    negative_weight_sum = 0.0
    gainers_count = 0
    losers_count = 0
    neutral_count = 0

    max_abs_pts = 0.01

    for sym in constituents:
        q = quotes.get(sym, {"ltp": 0.0, "prev_close": 0.0, "change_pct": 0.0})
        w = index_weights.get(sym, 0.0)
        chg_pct = q["change_pct"]
        
        # Point impact formula: Index_Prev_Close * Weight * (Change_Pct / 100)
        point_impact = index_prev_close * w * (chg_pct / 100.0)
        abs_impact = abs(point_impact)
        if abs_impact > max_abs_pts:
            max_abs_pts = abs_impact

        if point_impact > 0.0001:
            total_positive_pts += point_impact
            positive_weight_sum += w
            gainers_count += 1
        elif point_impact < -0.0001:
            total_negative_pts += point_impact
            negative_weight_sum += w
            losers_count += 1
        else:
            neutral_count += 1

        meta = STOCK_METADATA.get(sym, {"name": sym, "sector": "Equities"})
        constituent_records.append({
            "symbol": sym,
            "name": meta["name"],
            "sector": meta["sector"],
            "weight": round(w * 100.0, 2),
            "weight_raw": w,
            "ltp": round(q["ltp"], 2),
            "prev_close": round(q["prev_close"], 2),
            "change_pct": round(chg_pct, 2),
            "point_impact": round(point_impact, 2),
            "abs_impact": abs_impact
        })

    # Add relative visual bar percentage for UI
    for rec in constituent_records:
        rec["bar_pct"] = min(100.0, round((rec["abs_impact"] / max_abs_pts) * 100.0, 1))

    # Sort gainers (highest positive first) and losers (most negative first)
    gainers = sorted(
        [r for r in constituent_records if r["point_impact"] > 0],
        key=lambda x: x["point_impact"],
        reverse=True
    )
    losers = sorted(
        [r for r in constituent_records if r["point_impact"] < 0],
        key=lambda x: x["point_impact"]
    )
    
    # Sort all by absolute point impact (highest impact first)
    all_sorted = sorted(
        constituent_records,
        key=lambda x: x["abs_impact"],
        reverse=True
    )
    for rank, item in enumerate(all_sorted, start=1):
        item["rank"] = rank

    net_points = total_positive_pts + total_negative_pts
    index_change_pct = ((index_price - index_prev_close) / index_prev_close) * 100.0 if index_prev_close > 0 else 0.0

    payload = {
        "index_name": idx_key,
        "display_name": INDEX_INFO[idx_key]["display_name"],
        "spot_price": round(index_price, 2),
        "prev_close": round(index_prev_close, 2),
        "net_points": round(net_points, 2),
        "index_change_pct": round(index_change_pct, 2),
        "positive_points": round(total_positive_pts, 2),
        "negative_points": round(total_negative_pts, 2),
        "positive_weight_pct": round(positive_weight_sum * 100.0, 1),
        "negative_weight_pct": round(negative_weight_sum * 100.0, 1),
        "gainers_count": gainers_count,
        "losers_count": losers_count,
        "neutral_count": neutral_count,
        "total_constituents": len(constituents),
        "breadth_ratio": round(gainers_count / max(1, losers_count), 2),
        "last_updated": datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST"),
        "gainers": gainers,
        "losers": losers,
        "all_constituents": all_sorted,
        "sectors": sorted(list({r["sector"] for r in constituent_records}))
    }

    _CONTRIBUTORS_CACHE[idx_key] = (payload, now)
    return payload
