from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, timedelta

from config.settings import STRIKES_AROUND_ATM

from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Sensibull oxide API base
_OXIDE_BASE = "https://oxide.sensibull.com/v1/compute/cache"

# Symbol → token mapping for NSE/BSE indices
_TOKEN_MAP: dict[str, str] = {
    "NIFTY": "256265",
    "BANKNIFTY": "260105",
    "FINNIFTY": "257801",
    "MIDCPNIFTY": "288009",
    "SENSEX": "265",
}

# Strike intervals per symbol
_INTERVAL_MAP: dict[str, int] = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 100,
    "SENSEX": 100,
}

_REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://web.sensibull.com",
    "Referer": "https://web.sensibull.com/",
}


class SensibullFetcher(BaseFetcher):
    name = "sensibull"

    def __init__(self):
        super().__init__()
        self.session.headers.update(_REQ_HEADERS)
        self._warmed_up = False
        self._warm_up_lock = threading.Lock()

    def _reset_session(self):
        super().__init__()
        self.session.headers.update(_REQ_HEADERS)
        self._warmed_up = False

    def _warm_up_session(self) -> bool:
        with self._warm_up_lock:
            if self._warmed_up:
                return True
            try:
                try:
                    from curl_cffi import requests as curl_requests
                    session = curl_requests.Session(impersonate="chrome120")
                    session.headers.update(_REQ_HEADERS)
                    r = session.get("https://oxide.sensibull.com/v1/pluto/auth/web/session/a/platform/identify", timeout=15)
                    r.raise_for_status()
                    self.session = session
                    self._warmed_up = True
                    log.info("[sensibull] curl_cffi session successfully warmed up")
                    return True
                except ImportError:
                    pass

                r = self.session.get("https://oxide.sensibull.com/v1/pluto/auth/web/session/a/platform/identify", timeout=15)
                r.raise_for_status()
                self._warmed_up = True
                log.info("[sensibull] requests session successfully warmed up")
                return True
            except Exception as e:
                log.warning("[sensibull] session warm-up failed: %s", e)
                return False

    def fetch_option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
        sym = symbol.upper().strip()
        token = _TOKEN_MAP.get(sym)
        if not token:
            log.warning("[sensibull] no token for '%s'", sym)
            return None

        if not self._warmed_up:
            self._warm_up_session()

        raw = self._get(f"{_OXIDE_BASE}/live_derivative_prices/{token}")
        if not raw:
            log.warning("[sensibull] request failed or returned empty for %s, resetting session and retrying...", sym)
            self._reset_session()
            self._warm_up_session()
            raw = self._get(f"{_OXIDE_BASE}/live_derivative_prices/{token}")

        if not raw or not isinstance(raw, dict):
            log.warning("[sensibull] empty or invalid response for %s after retry", sym)
            return None

        data = raw.get("data")
        if not data:
            log.warning("[sensibull] no data in response for %s", sym)
            return None

        underlying = data.get("underlying_price")
        if underlying is None:
            log.warning("[sensibull] no underlying price for %s", sym)
            return None

        per_expiry = data.get("per_expiry_data", {})
        if not per_expiry:
            log.warning("[sensibull] no expiry data for %s", sym)
            return None

        # Pick target expiry: user-provided or nearest (chronologically)
        all_expiries = sorted(per_expiry.keys())
        if expiry:
            target_expiry = expiry
        else:
            target_expiry = all_expiries[0]

        exp_data = per_expiry_data = per_expiry.get(target_expiry)
        if not exp_data:
            log.warning("[sensibull] expiry '%s' not found; available: %s", target_expiry, all_expiries)
            return None

        opts = exp_data.get("options", [])
        if not opts:
            log.warning("[sensibull] no options for %s/%s", sym, target_expiry)
            return None

        atm_strike_num = exp_data.get("atm_strike") or round(underlying)
        interval = _INTERVAL_MAP.get(sym, 50)

        # Build token map
        token_map = {o["token"]: o for o in opts}
        used = set()
        pairs = []

        # Phase 1: pair by token ±256, verify with delta
        for o in opts:
            t = o["token"]
            if t in used:
                continue
            partner = None
            for delta in (256, -256):
                pt = t + delta
                if pt in token_map and pt not in used:
                    partner = token_map[pt]
                    break
            if not partner:
                continue

            g1 = o.get("greeks_with_iv") or {}
            g2 = partner.get("greeks_with_iv") or {}
            d1, d2 = g1.get("delta", 0) or 0, g2.get("delta", 0) or 0

            if d1 >= d2:
                ce, pe = o, partner
            else:
                ce, pe = partner, o

            used.add(t)
            used.add(partner["token"])

            ceg = ce.get("greeks_with_iv") or {}
            peg = pe.get("greeks_with_iv") or {}
            pairs.append({
                "ce_ltp": ce.get("last_price", 0) or 0,
                "pe_ltp": pe.get("last_price", 0) or 0,
                "ce_delta": ceg.get("delta", 0) or 0,
                "pe_delta": peg.get("delta", 0) or 0,
                "ce_theta": ceg.get("theta", 0) or 0,
                "pe_theta": peg.get("theta", 0) or 0,
                "ce_gamma": ceg.get("gamma", 0) or 0,
                "pe_gamma": peg.get("gamma", 0) or 0,
                "ce_vega": ceg.get("vega", 0) or 0,
                "pe_vega": peg.get("vega", 0) or 0,
                "ce_iv": ceg.get("iv", 0) or 0,
                "pe_iv": peg.get("iv", 0) or 0,
                "ce_oi": ce.get("oi", 0) or 0,
                "pe_oi": pe.get("oi", 0) or 0,
                "ce_volume": ce.get("volume", 0) or 0,
                "pe_volume": pe.get("volume", 0) or 0,
            })

        # Phase 2: theta + opposite delta fallback for remaining
        remaining = [o for o in opts if o["token"] not in used]
        for o in remaining:
            t = o["token"]
            if t in used:
                continue
            g = o.get("greeks_with_iv") or {}
            theta = g.get("theta")
            delta = g.get("delta", 0) or 0
            if theta is None:
                used.add(t)
                continue
            best = None
            best_match = None
            for pt, po in token_map.items():
                if pt in used or pt == t:
                    continue
                pg = po.get("greeks_with_iv") or {}
                ptheta = pg.get("theta")
                pdelta = pg.get("delta", 0) or 0
                if ptheta == theta and (delta >= 0) != (pdelta >= 0):
                    best = (pt, po)
                    break
            if best:
                pt, po = best
                pg = po.get("greeks_with_iv") or {}
                pdelta = pg.get("delta", 0) or 0
                if delta >= 0:
                    ce, pe = o, po
                else:
                    ce, pe = po, o
                ceg2 = ce.get("greeks_with_iv") or {}
                peg2 = pe.get("greeks_with_iv") or {}
                pairs.append({
                    "ce_ltp": ce.get("last_price", 0) or 0,
                    "pe_ltp": pe.get("last_price", 0) or 0,
                    "ce_delta": ceg2.get("delta", 0) or 0,
                    "pe_delta": peg2.get("delta", 0) or 0,
                    "ce_theta": ceg2.get("theta", 0) or 0,
                    "pe_theta": peg2.get("theta", 0) or 0,
                    "ce_gamma": ceg2.get("gamma", 0) or 0,
                    "pe_gamma": peg2.get("gamma", 0) or 0,
                    "ce_vega": ceg2.get("vega", 0) or 0,
                    "pe_vega": peg2.get("vega", 0) or 0,
                    "ce_iv": ceg2.get("iv", 0) or 0,
                    "pe_iv": peg2.get("iv", 0) or 0,
                    "ce_oi": ce.get("oi", 0) or 0,
                    "pe_oi": pe.get("oi", 0) or 0,
                    "ce_volume": ce.get("volume", 0) or 0,
                    "pe_volume": pe.get("volume", 0) or 0,
                })
                used.add(t)
                used.add(pt)

        if not pairs:
            log.warning("[sensibull] no pairs constructed for %s/%s", sym, target_expiry)
            return None

        # Sort by CE LTP descending → increasing strike
        pairs.sort(key=lambda x: x["ce_ltp"], reverse=True)

        # Locate ATM: CE delta closest to 0.5
        valid_indices = [i for i, p in enumerate(pairs) if p["ce_delta"] is not None]
        if not valid_indices:
            log.warning("[sensibull] no valid deltas for %s", sym)
            return None
        atm_pair_idx = min(valid_indices, key=lambda i: abs(pairs[i]["ce_delta"] - 0.5))

        # Limit output to ATM +/- STRIKES_AROUND_ATM strikes (driven by config.settings)
        start_idx = max(0, atm_pair_idx - STRIKES_AROUND_ATM)
        end_idx = min(len(pairs), atm_pair_idx + STRIKES_AROUND_ATM + 1)
        pairs = pairs[start_idx:end_idx]
        atm_pair_idx = atm_pair_idx - start_idx

        first_strike = round(atm_strike_num - atm_pair_idx * interval)

        # Build normalized strikes list
        strikes_out = []
        for i, p in enumerate(pairs):
            strike_price = round(first_strike + i * interval)
            strikes_out.append({
                "strike": float(strike_price),
                "option_type": "CE",
                "ltp": p["ce_ltp"],
                "oi": p["ce_oi"],
                "volume": p["ce_volume"],
                "iv": p["ce_iv"],
                "delta": p["ce_delta"],
                "theta": p["ce_theta"],
                "gamma": p["ce_gamma"],
                "vega": p["ce_vega"],
            })
            strikes_out.append({
                "strike": float(strike_price),
                "option_type": "PE",
                "ltp": p["pe_ltp"],
                "oi": p["pe_oi"],
                "volume": p["pe_volume"],
                "iv": p["pe_iv"],
                "delta": p["pe_delta"],
                "theta": p["pe_theta"],
                "gamma": p["pe_gamma"],
                "vega": p["pe_vega"],
            })

        if not strikes_out:
            log.warning("[sensibull] no strikes in normalized output for %s", sym)
            return None

        total_oi = sum(s.get("oi", 0) for s in strikes_out)
        total_ltp = sum(s.get("ltp", 0) for s in strikes_out)
        if total_oi == 0 and total_ltp == 0:
            log.warning("[sensibull] all-zero strikes for %s — discarding", sym)
            return None

        result = {
            "symbol": sym,
            "underlying_price": float(underlying),
            "expiry": str(target_expiry),
            "strikes": strikes_out,
        }

        # Add all available expiries
        result["all_expiries"] = all_expiries

        # Calculate unpaired count within the bot's filtered strikes range
        kept_strikes = set(s["strike"] for s in strikes_out) if strikes_out else set()
        unpaired_count = sum(
            1 for o in opts 
            if o.get("strike") is not None and float(o["strike"]) in kept_strikes and o["token"] not in used
        )
        log.info(
            "[sensibull] %s | underlying=%.2f expiry=%s strikes=%d pairs=%d unpaired=%d",
            sym, underlying, target_expiry, len(strikes_out), len(pairs), unpaired_count,
        )
        return result


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    f = SensibullFetcher()
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        r = f.fetch_option_chain(sym)
        if r:
            # Show ATM zone
            strikes = r["strikes"]
            n = len(strikes) // 2
            print(f"\n{sym}: {n} pairs, {len(strikes)} strikes")
            underlying = r["underlying_price"]
            atm_strike = min(strikes, key=lambda s: abs(s["strike"] - underlying))
            atm_ce = next(s for s in strikes if s["strike"] == atm_strike["strike"] and s["option_type"] == "CE")
            atm_pe = next(s for s in strikes if s["strike"] == atm_strike["strike"] and s["option_type"] == "PE")
            print(f"  ATM strike={atm_strike['strike']:.0f}: CE ltp={atm_ce['ltp']:.2f} d={atm_ce.get('delta',0):.2f} iv={atm_ce.get('iv',0):.3f} | PE ltp={atm_pe['ltp']:.2f} d={atm_pe.get('delta',0):.2f} iv={atm_pe.get('iv',0):.3f}")
