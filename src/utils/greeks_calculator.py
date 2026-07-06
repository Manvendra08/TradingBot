from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger(__name__)


class GreeksCalculator:
    """
    Calculates Option Greeks (Delta, Theta, Gamma, Vega, IV) locally
    using vollib (Black-Scholes). Uses lazy import to avoid the ~19s
    native-code compilation cost on module load.

    Intended for MCX commodities where the data source (Shoonya) provides
    IV but no greeks. For NSE indices, Sensibull already supplies greeks.
    """

    def __init__(self, risk_free_rate: float = 0.10) -> None:
        self.r = risk_free_rate
        self._vollib = None

    @staticmethod
    def get_time_to_expiry(expiry_date_str: str) -> float:
        try:
            expiry = datetime.strptime(expiry_date_str, "%d-%b-%Y")
        except ValueError:
            try:
                expiry = datetime.strptime(expiry_date_str, "%Y-%m-%d")
            except ValueError:
                log.warning("[greeks] unparseable expiry: %s", expiry_date_str)
                return 0.0
        minutes_remaining = (expiry - datetime.now()).total_seconds() / 60
        if minutes_remaining <= 0:
            return 1e-5
        return minutes_remaining / (365 * 24 * 60)

    def _lazy_import(self):
        if self._vollib is not None:
            return
        try:
            from vollib.black_scholes.greeks.analytical import delta, gamma, theta, vega
            from vollib.black_scholes.implied_volatility import implied_volatility

            class V:
                pass

            v = V()
            v.delta = delta
            v.gamma = gamma
            v.theta = theta
            v.vega = vega
            v.implied_volatility = implied_volatility
            self._vollib = v
        except ImportError:
            log.warning(
                "[greeks] vollib not installed — Greeks calculation disabled. Install: pip install vollib>=0.2.3"
            )
            self._vollib = None
        except Exception as exc:
            log.warning("[greeks] Failed to import vollib: %s", exc)
            self._vollib = None

    def calculate_greeks(
        self,
        spot_price: float,
        strike_price: float,
        option_price: float,
        expiry_date: str,
        option_type: str,
        iv: float | None = None,
    ) -> dict:
        """
        Parameters
        ----------
        spot_price : float
            Underlying index/stock LTP.
        strike_price : float
            Option strike price.
        option_price : float
            Option LTP.
        expiry_date : str
            Expiry date, e.g. '26-JUN-2025' or '2025-06-26'.
        option_type : str
            'ce', 'pe', 'call', or 'put'.
        iv : float, optional
            Implied volatility (decimal, e.g. 0.15 for 15%).
            If None, IV is computed from option price first.

        Returns
        -------
        dict with keys: iv, delta, gamma, theta, vega
        """
        self._lazy_import()
        v = self._vollib
        
        if v is None:
            log.debug("[greeks] vollib not available — returning zero greeks")
            return {"iv": 0, "delta": 0, "gamma": 0, "theta": 0, "vega": 0}

        t = self.get_time_to_expiry(expiry_date)
        if t <= 0:
            return {"iv": 0, "delta": 0, "gamma": 0, "theta": 0, "vega": 0}

        flag = "c" if option_type.lower() in ("ce", "call") else "p"

        if iv is None or iv == 0:
            try:
                iv = v.implied_volatility(
                    option_price, spot_price, strike_price, t, self.r, flag
                )
            except Exception:
                iv = 0.0

        if iv == 0.0:
            return {"iv": 0, "delta": 0, "gamma": 0, "theta": 0, "vega": 0}

        _delta = v.delta(flag, spot_price, strike_price, t, self.r, iv)
        _gamma = v.gamma(flag, spot_price, strike_price, t, self.r, iv)
        _theta = v.theta(flag, spot_price, strike_price, t, self.r, iv)
        _vega = v.vega(flag, spot_price, strike_price, t, self.r, iv)

        return {
            "iv": round(iv * 100, 2),
            "delta": round(_delta, 4),
            "gamma": round(_gamma, 6),
            "theta": round(_theta / 365, 2),
            "vega": round(_vega / 100, 2),
        }


_calculator: GreeksCalculator | None = None


def get_greeks_calculator() -> GreeksCalculator:
    global _calculator
    if _calculator is None:
        _calculator = GreeksCalculator()
    return _calculator


def enrich_missing_greeks(strikes: list[dict], underlying: float, expiry: str) -> int:
    """
    In-place update for any strike dict that is missing delta (or delta is 0
    when IV is available).  If IV is also missing, it is computed from the
    option price via Black-Scholes IV solver.

    Returns the count of strikes enriched.
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
            spot_price=underlying,
            strike_price=float(s["strike"]),
            option_price=ltp,
            expiry_date=expiry,
            option_type=str(s.get("option_type", "")),
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
