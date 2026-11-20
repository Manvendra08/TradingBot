"""
Zerodha Kite Data Fetcher & Client.
Provides first-class integration for:
1. NSE Cash Stocks data (LTP, OHLC, volume, change %)
2. NFO F&O data (Instrument catalogs, option chain with ATM +/- 15 strikes, Greeks calculation)
3. Historical candle data
4. Integration with BaseFetcher and router.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from config.settings import STRIKES_AROUND_ATM
from src.fetchers.base_fetcher import BaseFetcher, filter_atm_strikes
from src.utils.greeks_calculator import GreeksCalculator

log = logging.getLogger("src.fetchers.kite_fetcher")
IST = timezone(timedelta(hours=5, minutes=30))

CACHE_DIR = Path("data/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class KiteDataClient:
    """Zerodha Kite market data client with local disk instrument caching and quote resolution."""

    _STATIC_INDEX_TOKENS: Dict[str, int] = {
        "NIFTY": 256265,
        "BANKNIFTY": 260105,
        "FINNIFTY": 257801,
        "MIDCPNIFTY": 288009,
        "SENSEX": 265,
        "INDIAVIX": 264969,
    }

    def __init__(self, kite_client: Any = None):
        self._kite = kite_client
        self._instruments_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._greeks_calc = GreeksCalculator()

    @property
    def kite(self) -> Any:
        if self._kite is None:
            from src.engine.live_trading import get_kite_client
            self._kite = get_kite_client()
        return self._kite

    def is_authenticated(self) -> bool:
        """Check if Kite client is active with a valid session."""
        try:
            k = self.kite
            if not k:
                return False
            # Verify lightweight profile call if not cached
            profile = k.profile()
            return bool(profile and profile.get("user_id"))
        except Exception as e:
            log.debug("Kite authentication verification check failed: %s", e)
            return False

    def get_profile(self) -> Optional[Dict[str, Any]]:
        """Get authenticated user profile."""
        try:
            k = self.kite
            if not k:
                return None
            return k.profile()
        except Exception as e:
            log.warning("Failed to get Kite user profile: %s", e)
            return None

    def get_instruments(self, segment: str = "NSE", force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Fetch instrument master catalog with local disk caching (24h TTL / refreshed daily).
        segment can be 'NSE', 'NFO', 'BSE', 'MCX'.
        """
        segment = segment.upper()
        if not force_refresh and segment in self._instruments_cache:
            return self._instruments_cache[segment]

        cache_file = CACHE_DIR / f"kite_instruments_{segment.lower()}.json"
        
        # Check disk cache (valid if modified today or less than 20 hours old)
        if not force_refresh and cache_file.exists():
            try:
                mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, tz=IST)
                now = datetime.now(IST)
                if (now - mtime).total_seconds() < 72000 and mtime.date() == now.date():
                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list) and len(data) > 0:
                        self._instruments_cache[segment] = data
                        log.info("[%s] Loaded %d Kite instruments from local cache (%s)", segment, len(data), cache_file)
                        return data
            except Exception as e:
                log.warning("[%s] Failed reading local instruments cache: %s", segment, e)

        # Download from Kite API
        k = self.kite
        if not k:
            # Fallback: if offline or unauthenticated, try to load disk cache even if older
            if cache_file.exists():
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._instruments_cache[segment] = data
                    log.warning("[%s] Using stale disk cache without active Kite client (%d instruments)", segment, len(data))
                    return data
                except Exception:
                    pass
            log.error("[%s] Cannot fetch instruments: Kite client not available", segment)
            return []

        try:
            log.info("[%s] Downloading live instrument catalog from Kite Connect API...", segment)
            raw = k.instruments(segment)
            # Normalize records into JSON-serializable dictionaries
            normalized: List[Dict[str, Any]] = []
            for item in raw:
                if isinstance(item, dict):
                    rec = dict(item)
                else:
                    rec = getattr(item, "__dict__", dict(item))
                
                # Format date fields
                if isinstance(rec.get("expiry"), (date, datetime)):
                    rec["expiry"] = rec["expiry"].strftime("%Y-%m-%d")
                normalized.append(rec)

            self._instruments_cache[segment] = normalized
            # Save to disk
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(normalized, f)
            log.info("[%s] Successfully saved %d instruments to %s", segment, len(normalized), cache_file)
            return normalized
        except Exception as e:
            log.error("[%s] Error fetching instruments from Kite Connect: %s", segment, e)
            if cache_file.exists():
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._instruments_cache[segment] = data
                    return data
                except Exception:
                    pass
            return []

    def get_stock_quotes(self, symbols: List[str], exchange: str = "NSE") -> Dict[str, Dict[str, Any]]:
        """
        Fetch real-time stock quotes for a list of symbols.
        Returns a dictionary keyed by base symbol:
        {
            "INFY": {
                "symbol": "INFY",
                "instrument_token": 408065,
                "ltp": 1850.5,
                "prev_close": 1840.0,
                "open": 1845.0,
                "high": 1860.0,
                "low": 1842.0,
                "volume": 2500000,
                "change": 10.5,
                "change_pct": 0.57
            }
        }
        """
        k = self.kite
        if not k:
            log.warning("Kite client not initialized for stock quotes")
            return {}

        query_keys = []
        sym_map = {}
        for sym in symbols:
            s_clean = sym.strip().upper()
            if ":" in s_clean:
                key = s_clean
                base_sym = s_clean.split(":")[-1]
            else:
                key = f"{exchange}:{s_clean}"
                base_sym = s_clean
            query_keys.append(key)
            sym_map[key] = base_sym

        try:
            raw_quotes = k.quote(query_keys)
            results: Dict[str, Dict[str, Any]] = {}
            for key, q in raw_quotes.items():
                base_sym = sym_map.get(key, key)
                ltp = float(q.get("last_price", 0.0))
                ohlc = q.get("ohlc", {})
                prev_close = float(ohlc.get("close", ltp))
                open_px = float(ohlc.get("open", ltp))
                high_px = float(ohlc.get("high", ltp))
                low_px = float(ohlc.get("low", ltp))
                vol = int(q.get("volume", 0))
                net_change = float(q.get("net_change", ltp - prev_close if prev_close else 0.0))
                chg_pct = (net_change / prev_close * 100.0) if prev_close > 0 else 0.0

                results[base_sym] = {
                    "symbol": base_sym,
                    "instrument_token": q.get("instrument_token"),
                    "ltp": ltp,
                    "prev_close": prev_close,
                    "open": open_px,
                    "high": high_px,
                    "low": low_px,
                    "volume": vol,
                    "change": round(net_change, 2),
                    "change_pct": round(chg_pct, 2),
                    "timestamp": q.get("timestamp"),
                }
            return results
        except Exception as e:
            err_str = str(e)
            if "Insufficient permission" in err_str:
                log.warning("Kite quotes add-on not active on this API key: %s", err_str)
            else:
                log.error("Failed fetching Kite stock quotes: %s", err_str)
            return {}

    def get_stock_quote(self, symbol: str, exchange: str = "NSE") -> Optional[Dict[str, Any]]:
        """Fetch real-time stock quote for a single symbol."""
        res = self.get_stock_quotes([symbol], exchange=exchange)
        clean = symbol.strip().upper().split(":")[-1]
        return res.get(clean)

    def resolve_tradingsymbol(self, symbol: str) -> Optional[Tuple[str, str]]:
        """
        Returns (exchange, tradingsymbol) for a given symbol.
        e.g. 'NIFTY' -> ('NSE', 'NIFTY 50')
             'BANKNIFTY' -> ('NSE', 'NIFTY BANK')
             'FINNIFTY' -> ('NSE', 'NIFTY FIN SERVICE')
             'MIDCPNIFTY' -> ('NSE', 'NIFTY MID SELECT')
             'SENSEX' -> ('BSE', 'SENSEX')
             'INDIAVIX' -> ('NSE', 'INDIA VIX')
             'NATURALGAS' -> ('MCX', 'NATURALGAS26NOVFUT')
        """
        clean = symbol.strip().upper().split(":")[-1]
        static_map = {
            "NIFTY": ("NSE", "NIFTY 50"),
            "BANKNIFTY": ("NSE", "NIFTY BANK"),
            "FINNIFTY": ("NSE", "NIFTY FIN SERVICE"),
            "MIDCPNIFTY": ("NSE", "NIFTY MID SELECT"),
            "SENSEX": ("BSE", "SENSEX"),
            "INDIAVIX": ("NSE", "INDIA VIX"),
        }
        if clean in static_map:
            return static_map[clean]

        if clean in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"):
            mcx = self.get_instruments("MCX")
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            futs = [
                i for i in mcx
                if i.get("name") == clean and i.get("instrument_type") == "FUT" and (i.get("expiry") or "") >= today_str
            ]
            if futs:
                futs.sort(key=lambda x: x.get("expiry") or "")
                return ("MCX", futs[0]["tradingsymbol"])

        return ("NSE", clean)

    def get_market_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetch real-time quotes for indices, commodities, or equities.
        Returns dict keyed by base symbol:
        {
            "NIFTY": {"price": 25730.5, "prev_close": 25514.3, "change": 216.2, "pct_change": 0.85},
            "NATURALGAS": {"price": 293.2, "prev_close": 308.5, ...}
        }
        """
        k = self.kite
        if not k:
            return {}

        query_keys = []
        key_to_base = {}
        for sym in symbols:
            resolved = self.resolve_tradingsymbol(sym)
            if resolved:
                exch, tsym = resolved
                kite_key = f"{exch}:{tsym}"
                query_keys.append(kite_key)
                key_to_base[kite_key] = sym.strip().upper().split(":")[-1]

        if not query_keys:
            return {}

        try:
            raw = k.quote(query_keys)
            results = {}
            for kite_key, q in raw.items():
                base_sym = key_to_base.get(kite_key, kite_key)
                ltp = float(q.get("last_price") or 0.0)
                ohlc = q.get("ohlc") or {}
                prev_close = float(ohlc.get("close") or ltp)
                net_change = float(q.get("net_change") or (ltp - prev_close if prev_close else 0.0))
                chg_pct = (net_change / prev_close * 100.0) if prev_close > 0 else 0.0

                results[base_sym] = {
                    "price": round(ltp, 2),
                    "prev_close": round(prev_close, 2),
                    "change": round(net_change, 2),
                    "pct_change": round(chg_pct, 2),
                    "open": float(ohlc.get("open") or ltp),
                    "high": float(ohlc.get("high") or ltp),
                    "low": float(ohlc.get("low") or ltp),
                    "volume": int(q.get("volume") or 0),
                }
            return results
        except Exception as e:
            log.warning("Failed fetching Kite market quotes: %s", e)
            return {}

    def get_historical_candles(
        self,
        instrument_token: int,
        interval: str = "5minute",
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        continuous: bool = False,
        oi: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical candle data via Kite Connect API.
        interval options: minute, 3minute, 5minute, 10minute, 15minute, 30minute, 60minute, day
        """
        k = self.kite
        if not k:
            log.warning("Kite client not initialized for historical data")
            return []

        if to_date is None:
            to_date = datetime.now(IST)
        if from_date is None:
            from_date = to_date - timedelta(days=5)

        try:
            return k.historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                continuous=continuous,
                oi=oi
            )
        except Exception as e:
            log.error("Failed to fetch historical candles for token %d: %s", instrument_token, e)
            return []

    def resolve_instrument_token(self, symbol: str) -> Optional[int]:
        """Resolve instrument token for an index, commodity or equity stock."""
        clean = symbol.strip().upper().split(":")[-1]
        if clean in self._STATIC_INDEX_TOKENS:
            return self._STATIC_INDEX_TOKENS[clean]

        # Check MCX commodities
        if clean in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"):
            mcx_instruments = self.get_instruments("MCX")
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            futs = [
                i for i in mcx_instruments
                if i.get("name") == clean and i.get("instrument_type") == "FUT" and (i.get("expiry") or "") >= today_str
            ]
            if futs:
                futs.sort(key=lambda x: x.get("expiry") or "")
                return int(futs[0]["instrument_token"])

        # Check NSE equities / indices
        nse_instruments = self.get_instruments("NSE")
        for i in nse_instruments:
            if i.get("tradingsymbol") == clean or i.get("name") == clean:
                return int(i["instrument_token"])
        return None

    def get_option_chain(
        self,
        symbol: str,
        expiry: Optional[str] = None,
        strikes_around_atm: int = STRIKES_AROUND_ATM,
    ) -> Optional[Dict[str, Any]]:
        """
        Build a normalised Option Chain matching BaseFetcher specification:
        {
          "symbol": str,
          "underlying_price": float,
          "expiry": "YYYY-MM-DD",
          "strikes": [
            {
              "strike": float,
              "option_type": "CE"|"PE",
              "ltp": float,
              "oi": int,
              "oi_change": int,
              "volume": int,
              "iv": float,
              "delta": float,
              "gamma": float,
              "theta": float,
              "vega": float,
              "bid": float,
              "ask": float
            }, ...
          ]
        }
        """
        base_sym = symbol.strip().upper()
        # Map symbol name for appropriate exchange segment
        segment = "NFO"
        if base_sym in ("SENSEX", "BANKEX"):
            segment = "BFO"
        elif base_sym in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"):
            segment = "MCX"

        instruments = self.get_instruments(segment=segment)
        if not instruments:
            log.warning("[%s] No instruments found for segment %s", base_sym, segment)
            return None

        # Filter option instruments for this underlying
        opt_contracts = [
            inst for inst in instruments
            if inst.get("name") == base_sym and inst.get("instrument_type") in ("CE", "PE")
        ]
        if not opt_contracts:
            log.warning("[%s] No option contracts found for %s in %s", base_sym, base_sym, segment)
            return None

        # Determine available expiries
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        all_expiries = sorted(list(set(
            c.get("expiry") for c in opt_contracts
            if c.get("expiry") and c.get("expiry") >= today_str
        )))

        if not all_expiries:
            log.warning("[%s] No future or active expiries available", base_sym)
            return None

        target_expiry = expiry if expiry and expiry in all_expiries else all_expiries[0]

        # Contracts for target expiry
        expiry_contracts = [
            c for c in opt_contracts if c.get("expiry") == target_expiry
        ]
        if not expiry_contracts:
            log.warning("[%s] No contracts found for expiry %s", base_sym, target_expiry)
            return None

        # Determine underlying price
        underlying_price = 0.0
        # Check spot or futures quote
        spot_keys = {
            "NIFTY": "NSE:NIFTY 50",
            "BANKNIFTY": "NSE:NIFTY BANK",
            "FINNIFTY": "NSE:NIFTY FIN SERVICE",
            "SENSEX": "BSE:SENSEX"
        }
        k = self.kite
        if k:
            spot_key = spot_keys.get(base_sym, f"NSE:{base_sym}")
            try:
                spot_q = k.quote([spot_key])
                if spot_key in spot_q:
                    underlying_price = float(spot_q[spot_key].get("last_price", 0.0))
            except Exception as e:
                log.debug("[%s] Could not fetch spot price for %s: %s", base_sym, spot_key, e)

        # If spot price is unavailable, estimate from futures contract or median strike
        if underlying_price <= 0:
            fut_contracts = [
                c for c in instruments
                if c.get("name") == base_sym and c.get("instrument_type") == "FUT" and c.get("expiry") >= today_str
            ]
            if fut_contracts and k:
                nearest_fut = sorted(fut_contracts, key=lambda x: x.get("expiry"))[0]
                try:
                    f_sym = f"{segment}:{nearest_fut['tradingsymbol']}"
                    fut_q = k.quote([f_sym])
                    if f_sym in fut_q:
                        underlying_price = float(fut_q[f_sym].get("last_price", 0.0))
                except Exception as e:
                    log.debug("[%s] Could not fetch futures price: %s", base_sym, e)

        # Fallback underlying price from median strike if still 0
        all_strikes = sorted(list(set(float(c["strike"]) for c in expiry_contracts if c.get("strike"))))
        if underlying_price <= 0 and all_strikes:
            underlying_price = all_strikes[len(all_strikes) // 2]

        # Find ATM strike
        atm_strike = min(all_strikes, key=lambda x: abs(x - underlying_price)) if all_strikes else 0.0
        atm_idx = all_strikes.index(atm_strike) if atm_strike in all_strikes else 0
        min_idx = max(0, atm_idx - strikes_around_atm)
        max_idx = min(len(all_strikes), atm_idx + strikes_around_atm + 1)
        selected_strikes_set = set(all_strikes[min_idx:max_idx])

        # Filter candidate contracts
        target_contracts = [
            c for c in expiry_contracts if float(c.get("strike", 0)) in selected_strikes_set
        ]

        # Fetch live quotes for candidate contracts
        contract_quote_map = {}
        if k:
            query_symbols = [f"{segment}:{c['tradingsymbol']}" for c in target_contracts]
            # Kite quote allows up to 500 items per request
            chunk_size = 250
            for i in range(0, len(query_symbols), chunk_size):
                chunk = query_symbols[i:i + chunk_size]
                try:
                    q_res = k.quote(chunk)
                    contract_quote_map.update(q_res)
                except Exception as e:
                    log.warning("[%s] Failed fetching batch quote chunk (%d items): %s", base_sym, len(chunk), e)

        # Compute Greeks and assemble normalised output
        strikes_output: List[Dict[str, Any]] = []

        for c in target_contracts:
            strike = float(c.get("strike", 0))
            opt_type = c.get("instrument_type")
            tsym = f"{segment}:{c['tradingsymbol']}"
            q_data = contract_quote_map.get(tsym, {})

            ltp = float(q_data.get("last_price", 0.0))
            oi = int(q_data.get("oi", 0))
            vol = int(q_data.get("volume", 0))
            depth = q_data.get("depth", {})
            bid = float(depth.get("buy", [{}])[0].get("price", 0.0)) if depth.get("buy") else 0.0
            ask = float(depth.get("sell", [{}])[0].get("price", 0.0)) if depth.get("sell") else 0.0

            # Compute Greeks using local Black-Scholes engine
            greeks = self._greeks_calc.calculate_greeks(
                underlying_price=underlying_price,
                strike_price=strike,
                option_price=ltp,
                expiry_date=target_expiry,
                option_type=opt_type,
                exchange=segment,
            )

            strikes_output.append({
                "strike": strike,
                "option_type": opt_type,
                "ltp": ltp,
                "oi": oi,
                "oi_change": 0,  # Kite live quote returns total OI; intraday change requires tick caching
                "volume": vol,
                "iv": round(greeks.get("iv", 0.0), 2),  # Already in percentage from GreeksCalculator
                "delta": round(greeks.get("delta", 0.0), 4),
                "gamma": round(greeks.get("gamma", 0.0), 6),
                "theta": round(greeks.get("theta", 0.0), 2),
                "vega": round(greeks.get("vega", 0.0), 2),
                "bid": bid,
                "ask": ask,
                "tradingsymbol": c.get("tradingsymbol"),
                "instrument_token": c.get("instrument_token"),
            })

        # Apply standard ATM strike filter to guarantee symmetric window
        filtered = filter_atm_strikes(
            strikes=strikes_output,
            underlying_price=underlying_price,
            strikes_around_atm=strikes_around_atm
        )

        return {
            "symbol": base_sym,
            "underlying_price": underlying_price,
            "expiry": target_expiry,
            "strikes": filtered,
            "source": "kite"
        }


class KiteFetcher(BaseFetcher):
    """BaseFetcher implementation wrapping Zerodha Kite Connect."""

    name: str = "kite"

    def __init__(self):
        super().__init__()
        self.client = KiteDataClient()

    def fetch_option_chain(self, symbol: str, expiry: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch normalised option chain via KiteDataClient."""
        try:
            return self.client.get_option_chain(symbol=symbol, expiry=expiry)
        except Exception as e:
            log.error("[KiteFetcher] Exception fetching option chain for %s: %s", symbol, e)
            return None


_kite_data_client_instance: Optional[KiteDataClient] = None
_kite_fetcher_instance: Optional[KiteFetcher] = None


def get_kite_data_client() -> KiteDataClient:
    global _kite_data_client_instance
    if _kite_data_client_instance is None:
        _kite_data_client_instance = KiteDataClient()
    return _kite_data_client_instance


def get_kite_fetcher() -> KiteFetcher:
    global _kite_fetcher_instance
    if _kite_fetcher_instance is None:
        _kite_fetcher_instance = KiteFetcher()
    return _kite_fetcher_instance


def main():
    parser = argparse.ArgumentParser(description="Zerodha Kite Data Fetcher & Verification Tool")
    parser.add_argument("--status", action="store_true", help="Check Kite Connect authentication and profile status")
    parser.add_argument("--stocks", type=str, help="Comma-separated stock symbols (e.g. RELIANCE,HDFCBANK,INFY)")
    parser.add_argument("--symbol", type=str, help="F&O underlying symbol (e.g. BANKNIFTY, NIFTY)")
    parser.add_argument("--expiry", type=str, help="Target expiry (YYYY-MM-DD)")
    parser.add_argument("--refresh-catalog", action="store_true", help="Force refresh instrument catalog")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    client = KiteDataClient()

    if args.status or (not args.stocks and not args.symbol):
        print("=" * 60)
        print("ZERODHA KITE CONNECT STATUS")
        print("=" * 60)
        authenticated = client.is_authenticated()
        print(f"Authenticated: {'âœ… YES' if authenticated else 'â Œ NO'}")
        if authenticated:
            prof = client.get_profile()
            if prof:
                print(f"User Name:    {prof.get('user_name')}")
                print(f"User ID:      {prof.get('user_id')}")
                print(f"Broker:       {prof.get('broker')}")
                print(f"Email:        {prof.get('email')}")
        print("=" * 60)

    if args.refresh_catalog:
        print("\nRefreshing instrument catalogs...")
        nse_inst = client.get_instruments("NSE", force_refresh=True)
        nfo_inst = client.get_instruments("NFO", force_refresh=True)
        print(f"Refreshed: NSE = {len(nse_inst)} instruments, NFO = {len(nfo_inst)} instruments.")

    if args.stocks:
        symbols = [s.strip() for s in args.stocks.split(",") if s.strip()]
        print(f"\nFetching live quotes for stocks: {symbols}")
        quotes = client.get_stock_quotes(symbols)
        if quotes:
            for s, q in quotes.items():
                print(f"â€¢ {s:12}: LTP â‚¹{q['ltp']:,.2f} | Chg {q['change_pct']:+.2f}% | Vol {q['volume']:,}")
        else:
            print("âš  No quotes returned (check if Market Quotes permission is active).")

    if args.symbol:
        sym = args.symbol.strip().upper()
        print(f"\nFetching Option Chain for {sym} (Expiry: {args.expiry or 'Nearest'})...")
        oc = client.get_option_chain(symbol=sym, expiry=args.expiry)
        if oc:
            print(f"âœ… Option Chain Loaded for {oc['symbol']}")
            print(f"  Underlying Price: â‚¹{oc['underlying_price']:,.2f}")
            print(f"  Expiry:           {oc['expiry']}")
            print(f"  Strikes Count:    {len(oc['strikes'])}")
            print("\nATM Strike Samples:")
            # Display ATM +/- 3 strikes
            strikes = oc['strikes']
            for s in strikes[:8]:
                print(f"  {s['option_type']} {s['strike']:8.1f} | LTP â‚¹{s['ltp']:6.2f} | OI {s['oi']:8,d} | IV {s['iv']:4.1f}% | Î” {s['delta']:+0.2f}")
        else:
            print("âš  Failed to fetch option chain.")


if __name__ == "__main__":
    main()
