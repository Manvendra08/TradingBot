from __future__ import annotations
import os
import math
import logging
from datetime import datetime, time
import pytz
from scipy.stats import norm

log = logging.getLogger(__name__)

class GreeksCalculator:
    """
    Calculates Option Greeks (Delta, Theta, Gamma, Vega, IV) locally.
    Natively supports BSM (for NSE/BSE Spot) and Black-76 (for MCX Futures).
    Replaces vollib completely to prevent C-level compilation panics and 
    provides correct minute-precision expiry handling for Indian sessions.
    """
    def __init__(self, risk_free_rate: float | None = None) -> None:
        self.tz = pytz.timezone('Asia/Kolkata')
        if risk_free_rate is not None:
            self.r = risk_free_rate
        else:
            env_rate = os.environ.get("GREEKS_RISK_FREE_RATE")
            self.r = float(env_rate) if env_rate else 0.065  # RBI Repo Rate alignment

    def get_time_to_expiry(self, expiry_date_str: str, exchange: str = "NFO") -> float:
        """
        Calculates exact fractional years remaining. Maps expiry to official 
        closing bells (NSE/BFO: 15:30, MCX: 23:30).
        """
        now = datetime.now(self.tz)
        
        # Multi-format parse sequence
        parsed_date = None
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                parsed_date = datetime.strptime(expiry_date_str, fmt).date()
                break
            except ValueError:
                continue
                
        if not parsed_date:
            log.warning("[greeks] unparseable expiry string format: %s", expiry_date_str)
            return 0.0

        # Align target timestamp to the exchange closing hours
        if exchange.upper() == "MCX":
            expiry_datetime = self.tz.localize(datetime.combine(parsed_date, time(23, 30, 0)))
        else:
            expiry_datetime = self.tz.localize(datetime.combine(parsed_date, time(15, 30, 0)))

        total_seconds = (expiry_datetime - now).total_seconds()
        if total_seconds <= 0:
            return 1e-6  # Prevent division by zero close to expiry
            
        return total_seconds / (365 * 24 * 60 * 60)

    def calculate_greeks(
        self,
        underlying_price: float,
        strike_price: float,
        option_price: float,
        expiry_date: str,
        option_type: str,
        exchange: str = "NFO",
        iv: float | None = None,
    ) -> dict:
        """
        Executes local analytical calculations. Automatically switches context 
        between BSM and Black-76 based on exchange definition.
        """
        t = self.get_time_to_expiry(expiry_date, exchange)
        flag = "call" if option_type.lower() in ("ce", "call") else "put"
        
        # 1. Fallback IV Newton-Raphson Solver if IV is not supplied by stream
        if iv is None or iv <= 0:
            iv = self._solve_implied_vol(option_price, underlying_price, strike_price, t, flag, exchange)

        if iv <= 1e-4:
            return {"iv": 0, "delta": 0, "gamma": 0, "theta": 0, "vega": 0}

        # 2. Branch calculation engines based on asset category rules
        if exchange.upper() == "MCX":
            return self._calculate_black76(underlying_price, strike_price, t, iv, flag)
        else:
            return self._calculate_bsm(underlying_price, strike_price, t, iv, flag)

    def _calculate_bsm(self, S: float, K: float, T: float, sigma: float, flag: str) -> dict:
        """Black-Scholes-Merton Greek Engine for Index/Equity Spot."""
        sqrt_T = math.sqrt(T)
        d1 = (math.log(S / K) + (self.r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T

        n_d1 = norm.pdf(d1)
        exp_rt = math.exp(-self.r * T)

        if flag == "call":
            delta = norm.cdf(d1)
            theta = (- (S * n_d1 * sigma) / (2 * sqrt_T) - self.r * K * exp_rt * norm.cdf(d2))
        else:
            delta = norm.cdf(d1) - 1.0
            theta = (- (S * n_d1 * sigma) / (2 * sqrt_T) + self.r * K * exp_rt * norm.cdf(-d2))

        gamma = n_d1 / (S * sigma * sqrt_T)
        vega = S * sqrt_T * n_d1

        return {
            "iv": round(sigma * 100, 2),
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta / 365, 2),
            "vega": round(vega / 100, 2)
        }

    def _calculate_black76(self, F: float, K: float, T: float, sigma: float, flag: str) -> dict:
        """Black-76 Greek Engine for MCX Futures Contracts."""
        sqrt_T = math.sqrt(T)
        d1 = (math.log(F / K) + (0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T

        n_d1 = norm.pdf(d1)
        exp_rt = math.exp(-self.r * T)

        if flag == "call":
            price = exp_rt * (F * norm.cdf(d1) - K * norm.cdf(d2))
            delta = exp_rt * norm.cdf(d1)
            theta = - (F * exp_rt * n_d1 * sigma) / (2 * sqrt_T) - self.r * price
        else:
            price = exp_rt * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
            delta = -exp_rt * norm.cdf(-d1)
            theta = - (F * exp_rt * n_d1 * sigma) / (2 * sqrt_T) - self.r * price

        gamma = (exp_rt * n_d1) / (F * sigma * sqrt_T)
        vega = F * exp_rt * sqrt_T * n_d1

        return {
            "iv": round(sigma * 100, 2),
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta / 365, 2),
            "vega": round(vega / 100, 2)
        }

    def _solve_implied_vol(self, target_price: float, underlying: float, K: float, T: float, flag: str, exchange: str) -> float:
        """Robust internal numeric engine to derive IV without crashing external scripts."""
        sigma = 0.20
        for _ in range(25):
            if exchange.upper() == "MCX":
                res = self._calculate_black76(underlying, K, T, sigma, flag)
                # Reverse engine back to raw analytical option price
                exp_rt = math.exp(-self.r * T)
                sqrt_T = math.sqrt(T)
                d1 = (math.log(underlying / K) + (0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
                d2 = d1 - sigma * sqrt_T
                current_price = exp_rt * (underlying * norm.cdf(d1) - K * norm.cdf(d2)) if flag == "call" else exp_rt * (K * norm.cdf(-d2) - underlying * norm.cdf(-d1))
            else:
                res = self._calculate_bsm(underlying, K, T, sigma, flag)
                sqrt_T = math.sqrt(T)
                d1 = (math.log(underlying / K) + (self.r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
                d2 = d1 - sigma * sqrt_T
                current_price = underlying * norm.cdf(d1) - K * math.exp(-self.r * T) * norm.cdf(d2) if flag == "call" else K * math.exp(-self.r * T) * norm.cdf(-d2) - underlying * norm.cdf(-d1)

            diff = current_price - target_price
            if abs(diff) < 1e-4:
                return sigma
            vega = res["vega"] * 100.0
            if vega > 1e-3:
                sigma -= diff / vega
            else:
                break
        return max(0.01, min(sigma, 3.0)) # Hard limits to protect system threads

_calculator: GreeksCalculator | None = None

def get_greeks_calculator() -> GreeksCalculator:
    global _calculator
    if _calculator is None:
        _calculator = GreeksCalculator()
    return _calculator

def enrich_missing_greeks(strikes: list[dict], underlying: float, expiry: str, exchange: str = "NFO") -> int:
    """
    In-place dictionary pipeline updating engine configured to ingest 
    live data models straight from Shoonya packets safely.
    """
    calc = get_greeks_calculator()
    enriched = 0
    for s in strikes:
        d = s.get("delta")
        iv = s.get("iv", 0) or 0
        if d is not None and d != 0:
            continue
        ltp = float(s.get("ltp", 0) or 0)
        if ltp <= 0:
            continue
            
        g = calc.calculate_greeks(
            underlying_price=underlying,
            strike_price=float(s["strike"]),
            option_price=ltp,
            expiry_date=expiry,
            option_type=str(s.get("option_type", "")),
            exchange=exchange,
            iv=iv / 100.0 if iv else None,
        )
        if g["delta"] != 0:
            s["delta"] = g["delta"]
            s["theta"] = g["theta"]
            s["gamma"] = g["gamma"]
            s["vega"] = g["vega"]
            s["iv"] = g["iv"]
            enriched += 1
    return enriched
