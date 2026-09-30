"""
NiftyTrader Option Chain Fetcher

Scrapes the embedded Next.js __NEXT_DATA__ from NiftyTrader option-chain pages
and normalises it to the bot's standard option-chain schema.

Supported symbols:
  NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX,
  NATURALGAS, CRUDEOIL, GOLD, SILVER
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

import requests

from config.settings import HTTP_TIMEOUT_SECONDS
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

_BASE_URL = "https://www.niftytrader.in"

_SYMBOL_PATHS: Dict[str, str] = {
    "NIFTY": "/nse-option-chain/nifty",
    "BANKNIFTY": "/nse-option-chain/banknifty",
    "FINNIFTY": "/nse-option-chain/finnifty",
    "MIDCPNIFTY": "/nse-option-chain/midcpnifty",
    "SENSEX": "/bse-option-chain",
    "NATURALGAS": "/commodities-option-chain-nse/naturalgas",
    "CRUDEOIL": "/commodities-option-chain-nse/crudeoil",
    "GOLD": "/commodities-option-chain-nse/gold",
    "SILVER": "/commodities-option-chain-nse/silver",
}


class NiftyTraderFetcher(BaseFetcher):
    name = "niftytrader"

    def __init__(self) -> None:
        super().__init__()
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def fetch_option_chain(self, symbol: str, expiry: Optional[str] = None) -> Optional[Dict]:
        base = symbol.upper().split()[0]
        path = _SYMBOL_PATHS.get(base)
        if not path:
            log.warning("[niftytrader] unsupported symbol '%s'", base)
            return None

        url = f"{_BASE_URL}{path}"
        log.info("[niftytrader] fetching %s from %s", base, url)

        try:
            resp = self.session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("[niftytrader] page fetch failed for %s: %s", base, exc)
            return None

        text = resp.text
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", text, re.DOTALL)
        if not scripts:
            log.warning("[niftytrader] no script tags found for %s", base)
            return None

        # The __NEXT_DATA__ blob is the largest script on the page.
        next_data_script = max(scripts, key=len)
        if "initialOptionChainData" not in next_data_script:
            log.warning("[niftytrader] initialOptionChainData not found for %s", base)
            return None

        try:
            payload = _parse_next_data(next_data_script)
        except Exception as exc:
            log.warning("[niftytrader] failed to parse __NEXT_DATA__ for %s: %s", base, exc)
            return None

        return _normalise(base, symbol, expiry, payload, self.name)


def _parse_next_data(script: str) -> Dict:
    """Extract and parse the JSON payload from a Next.js __NEXT_DATA__ script tag."""
    start = script.find("{")
    if start == -1:
        raise ValueError("no JSON object found in script")
    return _decode_json(script[start:])


def _decode_json(raw: str) -> Dict:
    """Decode JSON, tolerating escaped unicode and common Next.js serialisation quirks."""
    import json

    # Some pages serialise HTML entities inside JSON strings; undo that first.
    cleaned = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
    return json.loads(cleaned)


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalise(base: str, symbol: str, requested_expiry: Optional[str], payload: Dict, source_name: str) -> Optional[Dict]:
    page_props = payload.get("props", {}).get("pageProps", {})
    chain: List[Dict] = page_props.get("initialOptionChainData") or []
    spot: Dict = page_props.get("initialSpot") or {}

    if not chain:
        log.warning("[niftytrader] empty option chain for %s", base)
        return None

    page_expiry_raw = chain[0].get("expiry_date", "")
    page_expiry = _normalise_expiry(page_expiry_raw)
    if not page_expiry:
        log.warning("[niftytrader] missing expiry for %s", base)
        return None

    if requested_expiry:
        req = requested_expiry.strip()
        if req not in {page_expiry, page_expiry_raw}:
            log.info("[niftytrader] %s requested expiry %s but page shows %s — skipping", base, requested_expiry, page_expiry)
            return None

    underlying_price = _safe_float(spot.get("last_trade_price") or spot.get("close") or chain[0].get("index_close"))

    strikes = []
    for item in chain:
        strike_price = _safe_float(item.get("strike_price"))
        if not strike_price:
            continue

        ce_ltp = _safe_float(item.get("calls_ltp"))
        pe_ltp = _safe_float(item.get("puts_ltp"))

        if ce_ltp > 0 or _safe_float(item.get("calls_oi")) > 0:
            strikes.append({
                "strike": strike_price,
                "option_type": "CE",
                "ltp": ce_ltp,
                "oi": _safe_int(item.get("calls_oi")),
                "oi_change": _safe_int(item.get("calls_change_oi")),
                "volume": _safe_int(item.get("calls_volume")),
                "iv": _safe_float(item.get("calls_iv") or item.get("calls_iv_eod")),
                "bid": _safe_float(item.get("calls_bid_price")),
                "ask": _safe_float(item.get("calls_ask_price")),
                "delta": _safe_float(item.get("call_delta")),
                "gamma": _safe_float(item.get("call_gamma")),
                "vega": _safe_float(item.get("call_vega")),
                "theta": _safe_float(item.get("call_theta")),
                "rho": _safe_float(item.get("call_rho")),
            })

        if pe_ltp > 0 or _safe_float(item.get("puts_oi")) > 0:
            strikes.append({
                "strike": strike_price,
                "option_type": "PE",
                "ltp": pe_ltp,
                "oi": _safe_int(item.get("puts_oi")),
                "oi_change": _safe_int(item.get("puts_change_oi")),
                "volume": _safe_int(item.get("puts_volume")),
                "iv": _safe_float(item.get("puts_iv") or item.get("puts_iv_eod")),
                "bid": _safe_float(item.get("puts_bid_price")),
                "ask": _safe_float(item.get("puts_ask_price")),
                "delta": _safe_float(item.get("put_delta")),
                "gamma": _safe_float(item.get("put_gamma")),
                "vega": _safe_float(item.get("put_vega")),
                "theta": _safe_float(item.get("put_theta")),
                "rho": _safe_float(item.get("put_rho")),
            })

    if not strikes:
        log.warning("[niftytrader] no valid strikes parsed for %s", base)
        return None

    log.info(
        "[niftytrader] %s | expiry=%s underlying=%.2f strikes=%d",
        base, page_expiry, underlying_price, len(strikes),
    )

    return {
        "symbol": symbol,
        "underlying_price": underlying_price,
        "expiry": page_expiry,
        "strikes": strikes,
        "all_expiries": [page_expiry],
        "source": f"{source_name}:{base}",
    }


def _normalise_expiry(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    # Accept both "2026-09-29T00:00:00" and "2026-09-29"
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw
