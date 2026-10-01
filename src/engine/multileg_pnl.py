"""
Shared multi-leg PnL helpers.

Extracted from multileg_paper_trading._calc_multileg_pnl and
multileg_live_trading._update_live_book_pnl so both runners resolve premiums
through an identical code path and never silently diverge again.
"""
from __future__ import annotations

import logging
from typing import Any

from config.settings import LOT_SIZES

log = logging.getLogger(__name__)


def _resolve_leg_current_premium(
    symbol: str,
    leg: dict[str, Any],
    scan_context: dict[str, Any] | None,
    book: dict[str, Any] | None,
) -> float:
    """Resolve the current market premium for a single option leg.

    Preference order:
      1. Live option_rows from scan_context (in-memory chain)
      2. Latest option_chain_snapshots row from DB (expiry-aware)
      3. Delta-approximation from underlying move (short-options friendly)
      4. Entry premium as last-resort fallback

    Returns a non-negative float. Never raises; logs warnings on fallbacks.
    """
    from src.engine.trade_plan import is_valid_option_premium
    from src.models.schema import get_read_conn

    sc = scan_context or {}
    option_rows = list(sc.get("option_rows") or [])
    base_sym = (symbol or "").upper().split()[0]
    lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 1))

    strike = float(leg.get("strike") or 0.0)
    option_type = (leg.get("option_type") or "").upper()
    entry_premium = float(leg.get("entry_premium") or leg.get("premium") or 0.0)
    leg_expiry = str(leg.get("expiry") or book.get("expiry") or "").strip()
    underlying = float(
        sc.get("underlying") or book.get("entry_underlying") or 0.0
    )

    current_premium: float | None = None

    # 1) In-memory option_rows
    for row in option_rows:
        row_strike = float(row.get("strike") or 0.0)
        row_type = (row.get("option_type") or "").upper()
        row_exp = str(row.get("expiry") or "").strip()
        if leg_expiry and row_exp and row_exp != leg_expiry:
            continue
        if abs(row_strike - strike) < 0.01 and row_type == option_type:
            ltp = float(row.get("ltp") or row.get("premium") or 0.0)
            if ltp > 0:
                if underlying > 0 and not is_valid_option_premium(
                    strike, option_type, ltp, underlying
                ):
                    log.warning(
                        "[multileg-pnl] %s: rejected corrupted row LTP %.2f for %s %.0f (spot=%.2f)",
                        symbol, ltp, option_type, strike, underlying,
                    )
                else:
                    current_premium = ltp
                    break

    # 2) DB snapshot fallback
    if current_premium is None:
        try:
            with get_read_conn() as conn:
                if leg_expiry:
                    opt_row = conn.execute(
                        "SELECT ltp FROM option_chain_snapshots "
                        "WHERE (symbol=? OR symbol=?) AND expiry=? "
                        "AND ABS(strike - ?) < 0.01 AND option_type=? "
                        "AND ltp IS NOT NULL AND ltp > 0 "
                        "ORDER BY fetched_at DESC LIMIT 1",
                        (symbol, base_sym, leg_expiry, strike, option_type),
                    ).fetchone()
                else:
                    opt_row = conn.execute(
                        "SELECT ltp FROM option_chain_snapshots "
                        "WHERE (symbol=? OR symbol=?) "
                        "AND ABS(strike - ?) < 0.01 AND option_type=? "
                        "AND ltp IS NOT NULL AND ltp > 0 "
                        "ORDER BY fetched_at DESC LIMIT 1",
                        (symbol, base_sym, strike, option_type),
                    ).fetchone()
                if opt_row:
                    snap_ltp = float(opt_row["ltp"])
                    if underlying > 0 and not is_valid_option_premium(
                        strike, option_type, snap_ltp, underlying
                    ):
                        log.warning(
                            "[multileg-pnl] %s: rejected corrupted DB snapshot LTP %.2f for %s %.0f (spot=%.2f)",
                            symbol, snap_ltp, option_type, strike, underlying,
                        )
                    else:
                        current_premium = snap_ltp
        except Exception:
            pass

    # 3) Delta-approximation fallback
    if current_premium is None and underlying > 0 and book.get("entry_underlying"):
        entry_und = float(book.get("entry_underlying") or underlying)
        und_move = underlying - entry_und
        delta = float(leg.get("delta") or 0.25)
        delta_sign = delta if option_type == "CE" else -abs(delta)
        current_premium = max(0.05, entry_premium + delta_sign * und_move)
        log.warning(
            "[multileg-pnl] %s: leg %s %.0f missing live LTP — "
            "delta-approximated current premium to %.2f (spot move=%.2f)",
            symbol, option_type, strike, current_premium, und_move,
        )

    # 4) Last-resort fallback
    if current_premium is None:
        current_premium = entry_premium
        if entry_premium > 0:
            log.warning(
                "[multileg-pnl] %s: leg %s %.0f missing live LTP — "
                "fell back to entry premium %.2f",
                symbol, option_type, strike, entry_premium,
            )

    return current_premium


def calc_leg_pnl(
    entry_premium: float,
    current_premium: float,
    side: str,
    lots: int,
    lot_size: int,
) -> float:
    """Calculate PnL for a single option leg.

    Short options (SELL):  PnL = (entry - current) * lots * lot_size
    Long options (BUY):    PnL = (current - entry) * lots * lot_size
    """
    side = (side or "SELL").upper()
    if side == "SELL":
        return (entry_premium - current_premium) * lots * lot_size
    return (current_premium - entry_premium) * lots * lot_size
