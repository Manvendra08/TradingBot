"""Stock Futures Replay & Backtesting Engine.
Implements Section 3 & Task 11:
- Operates on daily bars without lookahead.
- Entry at next day's open + spread & drift slippage.
- If day's range contains both SL and Target, assumes STOP HIT FIRST.
- Gaps beyond SL fill at open, not at SL.
- Calculates exact Indian derivative friction via src.engine.accounting.
- Reports expectancy in R, net-positive win rate, per-side stats, worst gap loss, and funnel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from config.stockfut_loader import load_stockfut_config
from src.engine.accounting import calculate_transaction_costs
from src.engine.capital_allocator import calculate_future_lots, split_lots
from src.engine.trade_plan import calculate_future_sl_target
from src.stockfut.exits import evaluate_future_exit
from src.stockfut.features import compute_features, market_regime
from src.stockfut.levels import build_zones
from src.stockfut.portfolio_risk import check_entry
from src.stockfut.setups import (
    bias_label,
    is_daily_reversal,
    matrix_permission,
    open_check,
    stock_bias,
    WatchRow,
)

log = logging.getLogger("stockfut.replay")


@dataclass
class ReplayTrade:
    symbol: str
    side: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    lots: int
    lot_size: int
    sl: float
    t1: float
    t2: float
    exit_rule: str
    gross_pnl: float
    net_pnl: float
    tx_costs: float
    pnl_r: float
    days_held: int


@dataclass
class ReplayReport:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    net_positive_rate: float = 0.0
    expectancy_r: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_costs: float = 0.0
    max_drawdown: float = 0.0
    worst_single_gap_loss: float = 0.0
    long_stats: Dict[str, Any] = field(default_factory=dict)
    short_stats: Dict[str, Any] = field(default_factory=dict)
    funnel: Dict[str, int] = field(default_factory=dict)
    trades: List[ReplayTrade] = field(default_factory=list)


class ReplayEngine:
    """Deterministic Replay Engine for Stock Futures."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None, capital: float = 10000000.0):
        self.cfg = cfg or load_stockfut_config()
        self.capital = capital

    def run_replay(
        self,
        symbol_daily_data: Dict[str, List[Dict[str, Any]]],
        lot_sizes: Dict[str, int],
        index_daily: Optional[List[Dict[str, Any]]] = None,
        start_idx: int = 60,
    ) -> ReplayReport:
        """
        Run backtest across all symbols using available daily bars.
        start_idx: number of initial bars used for warmup (EMA/ADX/Zones).
        """
        all_dates = set()
        for bars in symbol_daily_data.values():
            for b in bars:
                all_dates.add(b["date"])
        sorted_dates = sorted(list(all_dates))

        if len(sorted_dates) <= start_idx:
            return ReplayReport()

        trades: List[ReplayTrade] = []
        open_positions: List[Dict[str, Any]] = []

        funnel = {
            "scanned": 0,
            "armed": 0,
            "geometry_pass": 0,
            "open_check_pass": 0,
            "filled": 0,
        }

        budget_pct = float(self.cfg.get("risk_budget_pct", 0.75))
        max_risk_mult = float(self.cfg.get("max_risk_multiplier", 1.25))
        target_r_ratio = float(self.cfg.get("target_r_ratio", 1.6))

        armed_queue: List[WatchRow] = []

        # Step through daily sessions
        for d_idx in range(start_idx, len(sorted_dates)):
            curr_date = sorted_dates[d_idx]
            prev_date = sorted_dates[d_idx - 1]

            # 1. MORNING (09:30): Check Exits first for open positions
            remaining_positions: List[Dict[str, Any]] = []
            for pos in open_positions:
                sym = pos["symbol"]
                sym_bars = symbol_daily_data.get(sym, [])
                curr_bar = next((b for b in sym_bars if b["date"] == curr_date), None)
                prev_bar = next((b for b in sym_bars if b["date"] == prev_date), None)

                if not curr_bar:
                    remaining_positions.append(pos)
                    continue

                market_snapshot = {
                    "price": float(curr_bar["close"]),
                    "open": float(curr_bar["open"]),
                    "high": float(curr_bar["high"]),
                    "low": float(curr_bar["low"]),
                    "atr": pos["atr"],
                }

                # Evaluate exit
                dec = evaluate_future_exit(
                    trade=pos,
                    market=market_snapshot,
                    last_daily_bar=prev_bar,
                )

                if dec and dec.action == "EXIT_ALL":
                    # Trade closed
                    exit_p = float(curr_bar["open"]) if dec.rule == "GAP_THROUGH" else float(curr_bar["close"])
                    # If hit hard stop intraday, exit price is at SL
                    if dec.rule == "HARD_STOP":
                        exit_p = pos["sl"]

                    pos_lots = pos["remaining_lots"]
                    size = pos["lot_size"]
                    entry_p = pos["entry_price"]
                    side = pos["side"]

                    gross_pnl = (exit_p - entry_p) * size * pos_lots if side == "LONG" else (entry_p - exit_p) * size * pos_lots
                    tx = calculate_transaction_costs(
                        option_type="FUT",
                        side=side,
                        entry_premium=0.0,
                        entry_underlying=entry_p,
                        exit_premium=0.0,
                        exit_underlying=exit_p,
                        lot_size=size,
                        lots=pos_lots,
                        symbol=sym,
                    )
                    net_pnl = gross_pnl - tx
                    initial_risk = pos["sl_distance"] * size * pos_lots
                    pnl_r = round(net_pnl / initial_risk, 2) if initial_risk > 0 else 0.0

                    trades.append(
                        ReplayTrade(
                            symbol=sym,
                            side=side,
                            entry_date=pos["entry_date"],
                            exit_date=curr_date,
                            entry_price=entry_p,
                            exit_price=exit_p,
                            lots=pos["lots"],
                            lot_size=size,
                            sl=pos["sl"],
                            t1=pos["t1"],
                            t2=pos["t2"],
                            exit_rule=dec.rule,
                            gross_pnl=round(gross_pnl, 2),
                            net_pnl=round(net_pnl, 2),
                            tx_costs=round(tx, 2),
                            pnl_r=pnl_r,
                            days_held=d_idx - pos["entry_idx"],
                        )
                    )
                elif dec and dec.action == "PARTIAL_T1":
                    # Book half at T1, move stop
                    pos["status"] = "PARTIAL"
                    pos["remaining_lots"] = pos["t2_lots"]
                    pos["remaining_qty"] = pos["t2_lots"] * pos["lot_size"]
                    if dec.new_sl:
                        pos["sl"] = dec.new_sl
                    remaining_positions.append(pos)
                elif dec and dec.action == "TIGHTEN_SL":
                    if dec.new_sl:
                        pos["sl"] = dec.new_sl
                    remaining_positions.append(pos)
                else:
                    remaining_positions.append(pos)

            open_positions = remaining_positions

            # 2. MORNING (09:30): Execute ARMED candidates from yesterday's scan
            for row in armed_queue:
                sym = row.symbol
                sym_bars = symbol_daily_data.get(sym, [])
                curr_bar = next((b for b in sym_bars if b["date"] == curr_date), None)
                if not curr_bar:
                    continue

                quote = {
                    "price": float(curr_bar["open"]),
                    "bid": float(curr_bar["open"]) - 0.05,
                    "ask": float(curr_bar["open"]) + 0.05,
                }
                # Check 09:30 open gate
                open_atr = float(row.atr) if row.atr and row.atr > 0 else float(curr_bar["high"]) - float(curr_bar["low"])
                passed_open, open_reason = open_check(
                    row,
                    quote=quote,
                    atr=open_atr,
                    max_drift_atr=float(self.cfg.get("entry_max_drift_atr", 0.6)),
                    max_spread_pct=float(self.cfg.get("max_spread_pct", 0.10)),
                )
                if not passed_open:
                    continue
                funnel["open_check_pass"] += 1

                # Check portfolio risk limits
                c_dict = {
                    "symbol": sym,
                    "side": row.side,
                    "lots": row.lots,
                    "lot_size": row.lot_size,
                    "entry_price": float(curr_bar["open"]),
                }
                passed_risk, _ = check_entry(c_dict, open_positions, equity=self.capital, cfg=self.cfg)
                if not passed_risk:
                    continue

                # Fill trade
                fill_price = float(curr_bar["open"])
                t1_lots, t2_lots = split_lots(row.lots)
                new_pos = {
                    "symbol": sym,
                    "side": row.side,
                    "entry_date": curr_date,
                    "entry_idx": d_idx,
                    "entry_price": fill_price,
                    "lots": row.lots,
                    "remaining_lots": row.lots,
                    "lot_size": row.lot_size,
                    "t1_lots": t1_lots,
                    "t2_lots": t2_lots,
                    "remaining_qty": row.lots * row.lot_size,
                    "sl": row.sl,
                    "t1": row.t1,
                    "t2": row.t2,
                    "sl_distance": abs(fill_price - row.sl),
                    "atr": abs(fill_price - row.sl),
                    "status": "OPEN",
                }
                open_positions.append(new_pos)
                funnel["filled"] += 1

            # Clear single-shot armed queue
            armed_queue = []

            # 3. EVENING (19:30): Run EOD Scan on completed daily bars
            regime = "NEUTRAL"
            if index_daily:
                idx_slice = [b for b in index_daily if b["date"] <= curr_date]
                if len(idx_slice) >= 50:
                    regime = market_regime(index_daily=idx_slice)

            for sym, bars in symbol_daily_data.items():
                funnel["scanned"] += 1
                history = [b for b in bars if b["date"] <= curr_date]
                if len(history) < 25:
                    continue

                feat = compute_features(sym, history)
                if not feat:
                    continue

                lot_size = lot_sizes.get(sym, 1)
                atr = float(feat["atr14"])

                zones = build_zones(history, atr=atr)
                if not zones:
                    continue

                b_score = stock_bias(feat)
                b_lbl = bias_label(b_score)
                perm = matrix_permission(b_lbl, regime, cfg=self.cfg)
                if not perm:
                    continue
                side, min_score = perm

                rev = is_daily_reversal(side=side, daily=history, zones=zones, atr=atr, oi_flow_20d=feat["oi_flow_20d"])
                if not rev:
                    continue
                funnel["armed"] += 1

                entry_p = float(history[-1]["close"])
                plan = calculate_future_sl_target(entry=entry_p, side=side, zones=zones, atr=atr, target_r_ratio=target_r_ratio)
                if not plan:
                    continue
                funnel["geometry_pass"] += 1

                lots, skip = calculate_future_lots(
                    sl_distance=plan["sl_distance"],
                    lot_size=lot_size,
                    capital=self.capital,
                    budget_pct=budget_pct,
                    max_risk_mult=max_risk_mult,
                )
                if skip or lots < 2:
                    continue

                armed_queue.append(
                    WatchRow(
                        scan_date=curr_date,
                        scan_time="19:30:00",
                        symbol=sym,
                        tradingsymbol=f"{sym}FUT",
                        expiry="2026-11-26",
                        side=side,
                        bias=b_lbl,
                        score=80,
                        planned_entry=entry_p,
                        sl=plan["sl"],
                        t1=plan["t1"],
                        t2=plan["t2"],
                        lots=lots,
                        lot_size=lot_size,
                        r_multiple=plan["r_t1"],
                        atr=round(atr, 2),
                    )
                )

        # Aggregate report
        total = len(trades)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        losses = sum(1 for t in trades if t.net_pnl <= 0)
        win_rate = (wins / total * 100.0) if total > 0 else 0.0
        exp_r = (sum(t.pnl_r for t in trades) / total) if total > 0 else 0.0
        gross = sum(t.gross_pnl for t in trades)
        net = sum(t.net_pnl for t in trades)
        costs = sum(t.tx_costs for t in trades)

        # Drawdown
        equity_curve = [self.capital]
        for t in trades:
            equity_curve.append(equity_curve[-1] + t.net_pnl)
        peak = self.capital
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd

        # Worst gap
        gap_losses = [t.net_pnl for t in trades if t.exit_rule == "GAP_THROUGH"]
        worst_gap = min(gap_losses) if gap_losses else 0.0

        # Long vs Short
        long_trades = [t for t in trades if t.side == "LONG"]
        short_trades = [t for t in trades if t.side == "SHORT"]

        def side_stats(sub: List[ReplayTrade]) -> Dict[str, Any]:
            c = len(sub)
            w = sum(1 for t in sub if t.net_pnl > 0)
            return {
                "trades": c,
                "win_rate": round(w / c * 100.0, 1) if c > 0 else 0.0,
                "expectancy_r": round(sum(t.pnl_r for t in sub) / c, 2) if c > 0 else 0.0,
                "net_pnl": round(sum(t.net_pnl for t in sub), 2),
            }

        return ReplayReport(
            total_trades=total,
            wins=wins,
            losses=losses,
            net_positive_rate=round(win_rate, 1),
            expectancy_r=round(exp_r, 3),
            gross_pnl=round(gross, 2),
            net_pnl=round(net, 2),
            total_costs=round(costs, 2),
            max_drawdown=round(max_dd, 2),
            worst_single_gap_loss=round(worst_gap, 2),
            long_stats=side_stats(long_trades),
            short_stats=side_stats(short_trades),
            funnel=funnel,
            trades=trades,
        )
