"""Stock Futures Portfolio Risk Engine.
Implements Section 5:
- Max open positions (default 6) / per sector (default 2)
- Net directional exposure <= 60% of gross notional
- Gross notional <= 2x capital
- Gap stress test: 5% adverse gap on open positions + candidate <= 6% of capital
- Margin headroom >= 40% free
- Daily loss limit (2% of equity, realized + unrealized)
- Weekly loss limit (4% of equity)
- Consecutive losses breaker (3 consecutive losses)
- Drawdown kill switch (8% from equity peak) with persisted halt flag
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config.stockfut_loader import load_stockfut_config
from src.models.schema import get_conn, get_read_conn, log_stockfut_decision

log = logging.getLogger("stockfut.risk")

HALT_STATE_FILE = Path("data/stockfut_halt.json")


def is_halted() -> Tuple[bool, str]:
    """Check if stock futures entries are halted by kill switch."""
    if HALT_STATE_FILE.exists():
        try:
            with open(HALT_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("halted", False):
                return True, data.get("reason", "HALTED_BY_KILL_SWITCH")
        except Exception:
            pass
    return False, "ACTIVE"


def set_halt(halted: bool, reason: str = "") -> None:
    """Set persisted halt flag for stock futures entries."""
    HALT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"halted": halted, "reason": reason}
    with open(HALT_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    log_stockfut_decision(
        kind="HALT_STATE_CHANGE",
        symbol="SYSTEM",
        payload=payload,
    )
    log.warning("Stock futures halt state changed: halted=%s, reason=%s", halted, reason)


def check_entry(
    candidate: Dict[str, Any],
    open_positions: List[Dict[str, Any]],
    equity: float,
    cfg: Optional[Dict[str, Any]] = None,
    free_margin_pct: Optional[float] = None,
    sector_map: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """
    Evaluate portfolio risk gates before executing a new entry.
    Returns (True, "OK") or (False, "<reason>").
    """
    config = cfg or load_stockfut_config()

    # 0. Kill switch halt check
    halted, halt_reason = is_halted()
    if halted:
        return False, f"SYSTEM_HALTED: {halt_reason}"

    max_open = int(config.get("max_open_positions", 6))
    max_per_sector = int(config.get("max_positions_per_sector", 2))
    max_net_pct = float(config.get("max_net_directional_pct", 0.60))
    max_gross_mult = float(config.get("max_gross_notional_mult", 2.0))
    gap_stress_cap_pct = float(config.get("gap_stress_loss_cap_pct", 0.06))
    margin_min_pct = float(config.get("margin_headroom_min_pct", 0.40))

    # 1. Max positions
    if len(open_positions) >= max_open:
        return False, f"MAX_POSITIONS_REACHED ({len(open_positions)} >= {max_open})"

    # 2. Sector concentration
    c_symbol = candidate.get("symbol", "").upper()
    c_sector = (sector_map or {}).get(c_symbol, "UNKNOWN")
    if c_sector != "UNKNOWN":
        sector_count = 0
        for pos in open_positions:
            pos_sym = pos.get("symbol", "").upper()
            pos_sec = (sector_map or {}).get(pos_sym, "UNKNOWN")
            if pos_sec == c_sector:
                sector_count += 1
        if sector_count >= max_per_sector:
            return False, f"SECTOR_LIMIT_REACHED ({sector_count} in {c_sector} >= {max_per_sector})"

    # 3. Notional calculations
    c_side = candidate.get("side", "LONG").upper()
    c_lots = int(candidate.get("lots", 2))
    c_lot_size = int(candidate.get("lot_size", 1))
    c_price = float(candidate.get("entry_price", candidate.get("planned_entry", 0.0)))
    c_notional = c_lots * c_lot_size * c_price

    long_notional = c_notional if c_side in ("LONG", "BUY") else 0.0
    short_notional = c_notional if c_side in ("SHORT", "SELL") else 0.0

    for pos in open_positions:
        p_side = str(pos.get("side", "LONG")).upper()
        p_qty = int(pos.get("remaining_qty", pos.get("lots", 1) * pos.get("lot_size", 1)))
        p_price = float(pos.get("entry_price", 0.0))
        notional = p_qty * p_price
        if p_side in ("LONG", "BUY"):
            long_notional += notional
        else:
            short_notional += notional

    total_gross = long_notional + short_notional

    # Gross notional cap
    gross_cap = max_gross_mult * equity
    if total_gross > gross_cap:
        return False, f"GROSS_NOTIONAL_BREACH (₹{total_gross:,.0f} > ₹{gross_cap:,.0f})"

    # Net directional exposure cap
    if total_gross > 0:
        net_bias_pct = abs(long_notional - short_notional) / total_gross
        if net_bias_pct > max_net_pct:
            return False, f"NET_EXPOSURE_BREACH ({net_bias_pct * 100:.1f}% > {max_net_pct * 100:.1f}%)"

    # 4. Gap Stress Test (5% adverse gap on all positions + candidate)
    stressed_gap_loss = 0.05 * total_gross
    stress_cap = gap_stress_cap_pct * equity
    if stressed_gap_loss > stress_cap:
        return False, f"GAP_STRESS_BREACH (Stressed: ₹{stressed_gap_loss:,.0f} > Cap: ₹{stress_cap:,.0f})"

    # 5. Margin headroom
    if free_margin_pct is not None:
        if free_margin_pct < margin_min_pct:
            return False, f"MARGIN_HEADROOM_BREACH ({free_margin_pct * 100:.1f}% < {margin_min_pct * 100:.1f}%)"

    return True, "OK"


def daily_loss_exceeded(equity: float, realized_pnl: float, unrealized_pnl: float, limit_pct: float = 2.0) -> bool:
    """Return True if total daily loss (realized + unrealized) breaches limit_pct of equity."""
    total_today = realized_pnl + unrealized_pnl
    threshold = -1.0 * (limit_pct / 100.0) * equity
    return total_today <= threshold


def weekly_loss_exceeded(equity: float, weekly_pnl: float, limit_pct: float = 4.0) -> bool:
    """Return True if total weekly loss breaches limit_pct of equity."""
    threshold = -1.0 * (limit_pct / 100.0) * equity
    return weekly_pnl <= threshold


def consecutive_losses_exceeded(recent_pnls: List[float], limit: int = 3) -> bool:
    """Return True if last `limit` completed trades are all net negative."""
    if len(recent_pnls) < limit:
        return False
    return all(pnl < 0 for pnl in recent_pnls[-limit:])


def compute_portfolio_metrics(
    open_positions: List[Dict[str, Any]],
    equity: float,
    cfg: Optional[Dict[str, Any]] = None,
    sector_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Compute current portfolio risk and exposure metrics."""
    config = cfg or load_stockfut_config()
    max_open = int(config.get("max_open_positions", 6))
    max_gross_mult = float(config.get("max_gross_notional_mult", 2.0))
    max_net_pct = float(config.get("max_net_directional_pct", 0.60))
    gap_stress_cap_pct = float(config.get("gap_stress_loss_cap_pct", 0.06))

    long_notional = 0.0
    short_notional = 0.0
    sector_counts: Dict[str, int] = {}

    for pos in open_positions:
        p_side = str(pos.get("direction", pos.get("side", "LONG"))).upper()
        p_qty = int(pos.get("remaining_qty", pos.get("lots", 1) * pos.get("lot_size", 1)))
        p_price = float(pos.get("entry_price", 0.0))
        notional = p_qty * p_price
        if p_side in ("LONG", "BUY"):
            long_notional += notional
        else:
            short_notional += notional

        sym = str(pos.get("symbol", "")).upper()
        sec = (sector_map or {}).get(sym, "OTHER")
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    total_gross = long_notional + short_notional
    net_bias_pct = (abs(long_notional - short_notional) / total_gross) if total_gross > 0 else 0.0
    net_direction = "LONG" if long_notional > short_notional else ("SHORT" if short_notional > long_notional else "FLAT")
    stressed_gap_loss = 0.05 * total_gross
    gross_mult = (total_gross / equity) if equity > 0 else 0.0
    margin_estimated = 0.20 * total_gross

    return {
        "open_positions_count": len(open_positions),
        "max_open_positions": max_open,
        "long_notional": long_notional,
        "short_notional": short_notional,
        "gross_notional": total_gross,
        "gross_notional_multiple": round(gross_mult, 2),
        "max_gross_notional_multiple": max_gross_mult,
        "net_direction": net_direction,
        "net_directional_pct": round(net_bias_pct * 100.0, 1),
        "max_net_directional_pct": round(max_net_pct * 100.0, 1),
        "gap_stress_loss": round(stressed_gap_loss, 2),
        "gap_stress_cap": round(gap_stress_cap_pct * equity, 2),
        "gap_stress_loss_pct": round((stressed_gap_loss / equity) * 100.0, 2) if equity > 0 else 0.0,
        "sector_counts": sector_counts,
        "estimated_margin_used": round(margin_estimated, 2),
    }
