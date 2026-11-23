"""Stock Futures Universe & Hygiene Engine.
Implements Section 3.1 of design:
- Excludes banned stocks, MWPL > 80%, events within 7 sessions.
- Contract choice: near month if >= 12 sessions left, else next month.
- Sizing check: 2 lots at 2.0x ATR must not exceed hard risk cap.
- Liquidity checks: OI value >= 50 Cr, spread <= 0.05%.
Fail-closed on fetch errors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from config.stockfut_loader import load_stockfut_config
from src.fetchers.kite_fetcher import KiteDataClient, get_kite_data_client
from src.fetchers.nse_eod_fetcher import fetch_ban_list, fetch_mwpl, fetch_events, FetchError

log = logging.getLogger("stockfut.universe")


@dataclass
class Contract:
    symbol: str
    tradingsymbol: str
    expiry: str
    lot_size: int
    sessions_to_expiry: int
    token: int = 0


def count_trading_sessions(start_date: date, end_date: date) -> int:
    """Count Monday-Friday weekdays between start_date and end_date (inclusive of end_date)."""
    if start_date >= end_date:
        return 0
    cur = start_date + timedelta(days=1)
    sessions = 0
    while cur <= end_date:
        if cur.weekday() < 5:  # Monday to Friday
            sessions += 1
        cur += timedelta(days=1)
    return sessions


def parse_date(d: str | date | datetime) -> date:
    """Parse string or date-like into a pure date object."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    s = str(d).strip().split("T")[0]
    return datetime.strptime(s, "%Y-%m-%d").date()


class UniverseBuilder:
    """Builds the daily tradable stock futures universe with strict hygiene gates."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None, kite_client: Optional[KiteDataClient] = None):
        self.cfg = cfg or load_stockfut_config()
        self.kite_client = kite_client or get_kite_data_client()

    def build_universe(
        self,
        asof: date,
        capital: Optional[float] = None,
        ban_list: Optional[Set[str]] = None,
        mwpl_dict: Optional[Dict[str, float]] = None,
        events_dict: Optional[Dict[str, List[str]]] = None,
        atr_map: Optional[Dict[str, float]] = None,
        futures_quotes_map: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Contract]:
        """
        Build the list of eligible stock futures contracts for the trade date.
        Fails closed on fetch errors.
        """
        cap = float(capital if capital is not None else self.cfg.get("capital_reference", 10000000.0))
        budget_pct = float(self.cfg.get("risk_budget_pct", 0.75)) / 100.0
        max_risk_mult = float(self.cfg.get("max_risk_multiplier", 1.25))
        hard_risk_cap = cap * budget_pct * max_risk_mult
        min_sessions_near = int(self.cfg.get("min_sessions_to_expiry_near", 12))
        mwpl_max = float(self.cfg.get("mwpl_max_pct", 80.0))
        events_blackout = int(self.cfg.get("events_blackout_sessions", 7))
        min_oi_value_cr = float(self.cfg.get("min_oi_value_cr", 50.0))
        max_spread_pct = float(self.cfg.get("max_spread_pct", 0.05))

        # 1. Fetch hygiene lists if not injected
        try:
            if ban_list is None:
                ban_list = fetch_ban_list()
            if mwpl_dict is None:
                mwpl_dict = fetch_mwpl()
            if events_dict is None:
                events_dict = fetch_events(days=events_blackout)
        except Exception as exc:
            log.error("UniverseBuilder fetch error during hygiene refresh: %s; failing closed to empty universe", exc)
            return []

        # 2. Fetch all stock futures from instrument master
        all_contracts = self.kite_client.list_stock_futures()
        if not all_contracts:
            log.warning("No stock futures found in Kite instrument master")
            return []

        # Group by underlying symbol
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for c in all_contracts:
            grouped.setdefault(c["symbol"], []).append(c)

        universe: List[Contract] = []

        for symbol, contracts in grouped.items():
            sym_upper = symbol.upper()

            # Gate A: Ban List
            if sym_upper in ban_list:
                log.debug("[%s] Excluded: IN_BAN_LIST", sym_upper)
                continue

            # Gate B: MWPL > 80%
            mwpl = mwpl_dict.get(sym_upper, 0.0)
            if mwpl > mwpl_max:
                log.debug("[%s] Excluded: MWPL_BREACH (%.1f%% > %.1f%%)", sym_upper, mwpl, mwpl_max)
                continue

            # Gate C: Corporate action / Results / Split within blackout sessions
            if sym_upper in events_dict and events_dict[sym_upper]:
                log.debug("[%s] Excluded: EVENT_BLACKOUT (%s)", sym_upper, events_dict[sym_upper])
                continue

            # Sort contracts by expiry
            try:
                sorted_contracts = sorted(contracts, key=lambda x: parse_date(x["expiry"]))
            except Exception:
                sorted_contracts = contracts

            if not sorted_contracts:
                continue

            # Contract selection: Near (current) month if >= min_sessions_near (default: 1 session left, i.e. before expiry day)
            # On Expiry Day (asof == near_expiry_date or near_sessions < min_sessions_near), trade Next Month contract.
            chosen_contract: Optional[Dict[str, Any]] = None
            chosen_sessions = 0

            near_contract = sorted_contracts[0]
            try:
                near_expiry_date = parse_date(near_contract["expiry"])
                near_sessions = count_trading_sessions(asof, near_expiry_date)
            except Exception:
                near_sessions = 0

            if near_sessions >= min_sessions_near and near_expiry_date > asof:
                chosen_contract = near_contract
                chosen_sessions = near_sessions
            elif len(sorted_contracts) > 1:
                next_contract = sorted_contracts[1]
                try:
                    next_expiry_date = parse_date(next_contract["expiry"])
                    next_sessions = count_trading_sessions(asof, next_expiry_date)
                except Exception:
                    next_sessions = 0
                chosen_contract = next_contract
                chosen_sessions = next_sessions
            else:
                chosen_contract = near_contract
                chosen_sessions = near_sessions

            if not chosen_contract:
                log.debug("[%s] Excluded: NO_ELIGIBLE_EXPIRY", sym_upper)
                continue

            # Gate D: Sizing Check (2 lots at 2.0x ATR must not exceed hard cap)
            lot_size = chosen_contract["lot_size"]
            if atr_map and sym_upper in atr_map:
                daily_atr = atr_map[sym_upper]
                if daily_atr > 0:
                    max_stop_dist = 2.0 * daily_atr
                    risk_2_lots = 2 * (max_stop_dist * lot_size)
                    if risk_2_lots > hard_risk_cap:
                        log.debug(
                            "[%s] Excluded: UNSIZABLE_2_LOTS (Risk: ₹%.0f > Hard Cap: ₹%.0f)",
                            sym_upper, risk_2_lots, hard_risk_cap,
                        )
                        continue

            # Gate E: Liquidity Checks (OI value >= 50 Cr, spread <= 0.05%)
            if futures_quotes_map and chosen_contract["tradingsymbol"] in futures_quotes_map:
                q = futures_quotes_map[chosen_contract["tradingsymbol"]]
                ltp = float(q.get("price") or 0.0)
                oi = int(q.get("oi") or 0)
                bid = float(q.get("bid") or ltp)
                ask = float(q.get("ask") or ltp)

                if ltp > 0:
                    oi_value_cr = (oi * ltp) / 10000000.0  # 1 Cr = 10^7
                    if oi_value_cr < min_oi_value_cr:
                        log.debug("[%s] Excluded: LOW_OI_VALUE (₹%.1f Cr < ₹%.1f Cr)", sym_upper, oi_value_cr, min_oi_value_cr)
                        continue

                    if bid > 0 and ask >= bid:
                        spread_pct = ((ask - bid) / ltp) * 100.0
                        if spread_pct > max_spread_pct:
                            log.debug("[%s] Excluded: WIDE_SPREAD (%.3f%% > %.3f%%)", sym_upper, spread_pct, max_spread_pct)
                            continue

            universe.append(
                Contract(
                    symbol=sym_upper,
                    tradingsymbol=chosen_contract["tradingsymbol"],
                    expiry=str(chosen_contract["expiry"]),
                    lot_size=lot_size,
                    sessions_to_expiry=chosen_sessions,
                    token=chosen_contract.get("token", chosen_contract.get("instrument_token", 0)),
                )
            )

        return universe


# Module helper
def build_universe(asof: date, capital: float, **kwargs) -> List[Contract]:
    builder = UniverseBuilder()
    return builder.build_universe(asof=asof, capital=capital, **kwargs)
