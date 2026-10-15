"""Centralized Accounting Engine for NSEBOT.

Provides a single source of truth for:
- Leg-level and book-level gross P&L (short and long options, futures).
- Indian exchange transaction costs (Brokerage, STT/CTT, Exchange charges, SEBI turnover fees, Stamp Duty, GST).
- Net realized P&L and net mark-to-market (MTM).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

log = logging.getLogger(__name__)

INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"})

MCX_COMMODITY_SYMBOLS = frozenset({
    "NATURALGAS", "NATGAS", "CRUDEOIL", "GOLD", "SILVER",
    "COPPER", "ZINC", "ALUMINIUM", "LEAD", "NICKEL", "MENTHA", "COTTON", "CPO"
})


def calculate_leg_pnl(
    entry_premium: float,
    current_or_exit_premium: float,
    side: str,
    lots: int,
    lot_size: int,
) -> float:
    """Calculate gross P&L for a single option leg.

    Short options (SELL):  (entry - exit) * lots * lot_size
    Long options (BUY):    (exit - entry) * lots * lot_size
    """
    entry = float(entry_premium or 0.0)
    current = float(current_or_exit_premium or 0.0)
    num_lots = int(lots or 0)
    size = int(lot_size or 0)
    s = str(side or "SELL").upper().strip()

    if s == "SELL":
        return round((entry - current) * num_lots * size, 2)
    return round((current - entry) * num_lots * size, 2)


def calculate_transaction_costs(
    option_type: str,
    side: str,
    entry_premium: float,
    entry_underlying: float,
    exit_premium: float,
    exit_underlying: float,
    lot_size: int,
    lots: int,
    symbol: str = "",
) -> float:
    """Calculate realistic Indian derivative transaction costs.

    Includes:
    - Brokerage: ₹20 entry + ₹20 exit = ₹40 flat per leg.
    - STT / CTT:
        - Options (NSE Index): 0.0625% on round-trip premium turnover
        - Options (Stock): 0.0625% on sell-side premium turnover
        - Options (MCX): 0.05% CTT on sell-side premium turnover
        - Futures (MCX): 0.01% CTT on sell-side notional turnover
        - Futures (NSE Index): 0.0125% STT on sell-side notional turnover
        - Futures (Stock): 0.02% STT on sell-side notional turnover
    - Exchange Transaction Charges:
        - Options: 0.05% on round-trip premium turnover
        - Futures: 0.0019% on round-trip notional turnover
    - SEBI Turnover Charges: ₹10 per crore (0.000001) on total turnover
    - Stamp Duty:
        - Options: 0.003% on buy premium turnover
        - Futures: 0.002% on buy notional turnover
    - GST: 18% on (Brokerage + Exchange Charges + SEBI Charges)
    """
    brokerage = 40.0
    base_symbol = symbol.upper().split()[0] if symbol else ""
    is_index = base_symbol in INDEX_SYMBOLS
    is_mcx = base_symbol in MCX_COMMODITY_SYMBOLS

    opt_type_upper = str(option_type or "").upper().strip()
    is_option = opt_type_upper in ("CE", "PE")
    s = str(side or "SELL").upper().strip()
    num_lots = int(lots or 1)
    size = int(lot_size or 1)

    if is_option:
        entry_turnover = float(entry_premium or 0.0) * size * num_lots
        exit_turnover = float(exit_premium or 0.0) * size * num_lots
        total_turnover = entry_turnover + exit_turnover
        buy_turnover = entry_turnover if s == "BUY" else exit_turnover
        sell_turnover = exit_turnover if s == "BUY" else entry_turnover

        if is_mcx:
            stt = sell_turnover * 0.0005
        elif is_index:
            stt = total_turnover * 0.000625
        else:
            stt = sell_turnover * 0.000625

        exchange_charges = total_turnover * 0.0005
        stamp_duty = buy_turnover * 0.00003
        sebi_charges = total_turnover * 0.000001
    else:
        # Futures
        entry_turnover = float(entry_underlying or 0.0) * size * num_lots
        exit_turnover = float(exit_underlying or 0.0) * size * num_lots
        total_turnover = entry_turnover + exit_turnover
        buy_turnover = entry_turnover if s == "BUY" else exit_turnover
        sell_turnover = exit_turnover if s == "BUY" else entry_turnover

        if is_mcx:
            stt = sell_turnover * 0.0001
        elif is_index:
            stt = sell_turnover * 0.000125
        else:
            stt = sell_turnover * 0.0002

        exchange_charges = total_turnover * 0.000019
        stamp_duty = buy_turnover * 0.00002
        sebi_charges = total_turnover * 0.000001

    gst = (brokerage + exchange_charges + sebi_charges) * 0.18
    total_costs = brokerage + stt + exchange_charges + sebi_charges + stamp_duty + gst
    return round(total_costs, 2)


def calculate_book_pnl(
    legs: list[Mapping[str, Any] | dict[str, Any]],
    lot_size: int,
    symbol: str = "",
    exit_underlying: float = 0.0,
    deduct_transaction_costs: bool = False,
) -> tuple[float, list[dict[str, Any]]]:
    """Calculate total book P&L and return enriched legs.

    Correctly handles:
    - OPEN legs using their current market CMP.
    - CLOSED/rolled legs using their locked exit_premium.
    - Optional transaction cost deduction for net realized settlement.

    Returns:
        tuple of (total_pnl, list_of_enriched_legs)
    """
    total_pnl = 0.0
    total_tx_costs = 0.0
    enriched_legs: list[dict[str, Any]] = []

    for raw_leg in legs:
        leg = dict(raw_leg)
        leg_status = str(leg.get("status") or "OPEN").upper().strip()
        side = str(leg.get("side") or "SELL").upper().strip()
        lots = int(leg.get("lots") or leg.get("ratio") or 1)
        entry_p = float(leg.get("entry_premium") or leg.get("entry_price") or 0.0)
        opt_type = str(leg.get("option_type") or "CE").upper().strip()

        if leg_status == "CLOSED":
            if leg.get("exit_premium") is not None:
                cmp_val = float(leg.get("exit_premium"))
            elif leg.get("current_premium") is not None:
                cmp_val = float(leg.get("current_premium"))
            else:
                cmp_val = entry_p
        else:
            if leg.get("cmp") is not None:
                cmp_val = float(leg.get("cmp"))
            elif leg.get("current_premium") is not None:
                cmp_val = float(leg.get("current_premium"))
            else:
                cmp_val = entry_p

        leg_pnl = calculate_leg_pnl(
            entry_premium=entry_p,
            current_or_exit_premium=cmp_val,
            side=side,
            lots=lots,
            lot_size=lot_size,
        )

        leg["cmp"] = round(cmp_val, 2)
        leg["pnl"] = round(leg_pnl, 2)
        total_pnl += leg_pnl

        if deduct_transaction_costs:
            tx = calculate_transaction_costs(
                option_type=opt_type,
                side=side,
                entry_premium=entry_p,
                entry_underlying=exit_underlying,
                exit_premium=cmp_val,
                exit_underlying=exit_underlying,
                lot_size=lot_size,
                lots=lots,
                symbol=symbol,
            )
            total_tx_costs += tx

        enriched_legs.append(leg)

    final_pnl = round(total_pnl - total_tx_costs, 2) if deduct_transaction_costs else round(total_pnl, 2)
    return final_pnl, enriched_legs
