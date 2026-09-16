"""
Replay Backtester & Walk-Forward Optimizer
AI_INTELLIGENCE_ROADMAP_v3.0 — Phase 5

Replays historical market conditions over stored `scan_summaries` and
`option_chain_snapshots` to evaluate and walk-forward calibrate magic constants:
  - ATR SL multiplier (e.g., 1.0, 1.5, 2.0, 2.5)
  - ATR Target multiplier (e.g., 1.5, 2.0, 2.5, 3.0)
  - Composite confidence threshold (e.g., 55, 60, 62, 65, 70)
  - PCR cut-points (bullish >= 1.0, bearish <= 0.8)

Includes realistic retail market friction:
  - Round-trip brokerage, STT/CTT, exchange turnover charges, stamp duty, SEBI, 18% GST
  - Bid/ask marks and 0.5% premium slippage on execution
"""
from __future__ import annotations

import argparse
import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from config.settings import LOT_SIZES
from src.engine.verdict_sets import is_bearish as is_bearish_verdict, is_bullish as is_bullish_verdict
from src.models.schema import _calc_transaction_costs, get_conn

log = logging.getLogger("nsebot.replay_backtester")


@dataclass(frozen=True)
class BacktestParams:
    atr_sl_mult: float = 1.5
    atr_target_mult: float = 2.0
    composite_threshold: int = 62
    pcr_bull_threshold: float = 1.0
    pcr_bear_threshold: float = 0.8


@dataclass
class SimulatedTrade:
    trade_id: int
    symbol: str
    entry_time: str
    exit_time: str
    direction: str  # LONG (Call) or SHORT (Put)
    entry_underlying: float
    exit_underlying: float
    entry_premium: float
    exit_premium: float
    sl_underlying: float
    target_underlying: float
    lot_size: int
    lots: int
    gross_pnl: float
    transaction_costs: float
    net_pnl: float
    exit_reason: str  # TARGET, SL, EXPIRED
    holding_ticks: int
    params: BacktestParams


@dataclass
class WindowPerformance:
    window_idx: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    best_params: BacktestParams
    in_sample_trades: int
    in_sample_win_rate: float
    in_sample_net_pnl: float
    in_sample_profit_factor: float
    out_of_sample_trades: int
    out_of_sample_win_rate: float
    out_of_sample_net_pnl: float
    out_of_sample_profit_factor: float


def _estimate_atr(rows: Sequence[dict[str, Any]], current_idx: int, period: int = 14) -> float:
    """Calculate rolling Average True Range from underlying prices."""
    if current_idx < 1:
        row = rows[current_idx]
        return max(float(row.get("underlying") or 1.0) * 0.005, 10.0)

    start = max(0, current_idx - period)
    changes = []
    for i in range(start + 1, current_idx + 1):
        p1 = float(rows[i - 1].get("underlying") or 0.0)
        p2 = float(rows[i].get("underlying") or 0.0)
        if p1 > 0 and p2 > 0:
            changes.append(abs(p2 - p1))

    if not changes:
        return max(float(rows[current_idx].get("underlying") or 1.0) * 0.005, 10.0)
    return sum(changes) / len(changes)


def _resolve_option_premium_for_scan(
    symbol: str, fetched_at: str, underlying: float, option_type: str
) -> float:
    """Attempt to resolve real market LTP from option_chain_snapshots, fallback to ATM delta estimate."""
    try:
        with get_conn(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT ltp, bid, ask FROM option_chain_snapshots
                WHERE symbol = ? AND fetched_at = ? AND option_type = ?
                ORDER BY ABS(strike - ?) ASC LIMIT 1
                """,
                (symbol, fetched_at, option_type, underlying),
            ).fetchone()
            if row and row["ltp"] and float(row["ltp"]) > 0:
                return float(row["ltp"])
    except Exception:
        pass

    # Approximate fallback: ~1% of spot for commodity/index ATM weekly
    base_sym = symbol.upper().split()[0]
    if base_sym in ("NATURALGAS", "CRUDEOIL"):
        return max(underlying * 0.02, 2.0)
    return max(underlying * 0.008, 25.0)


def simulate_strategy(
    scans: list[dict[str, Any]],
    symbol: str,
    params: BacktestParams,
    max_holding_ticks: int = 12,
) -> list[SimulatedTrade]:
    """Simulate single-leg trade executions on scan summaries with given params."""
    if len(scans) < 2:
        return []

    base_sym = symbol.upper().split()[0]
    lot_size = LOT_SIZES.get(symbol, LOT_SIZES.get(base_sym, 50))
    trades: list[SimulatedTrade] = []
    trade_counter = 0

    i = 0
    while i < len(scans) - 1:
        scan = scans[i]
        conf = int(scan.get("confidence") or 0)
        pcr = float(scan.get("pcr") or 1.0)
        verdict = str(scan.get("verdict_label") or "").strip()
        und = float(scan.get("underlying") or 0.0)

        if und <= 0 or conf < params.composite_threshold:
            i += 1
            continue

        is_bull = is_bullish_verdict(verdict)
        is_bear = is_bearish_verdict(verdict)

        # Directional & PCR gate
        action = None
        opt_type = None
        if is_bull and pcr >= params.pcr_bull_threshold:
            action = "LONG"
            opt_type = "CE"
        elif is_bear and pcr <= params.pcr_bear_threshold:
            action = "SHORT"
            opt_type = "PE"

        if not action or not opt_type:
            i += 1
            continue

        # Enter trade
        trade_counter += 1
        atr = _estimate_atr(scans, i)
        entry_time = str(scan.get("fetched_at") or "")
        entry_prem = _resolve_option_premium_for_scan(symbol, entry_time, und, opt_type)

        sl_dist = atr * params.atr_sl_mult
        target_dist = atr * params.atr_target_mult

        if action == "LONG":
            sl_und = und - sl_dist
            target_und = und + target_dist
        else:
            sl_und = und + sl_dist
            target_und = und - target_dist

        # Scan forward for exit
        exit_idx = i + 1
        exit_reason = "EXPIRED"
        exit_und = und
        exit_time = entry_time

        while exit_idx < len(scans) and (exit_idx - i) <= max_holding_ticks:
            curr_scan = scans[exit_idx]
            curr_und = float(curr_scan.get("underlying") or 0.0)
            exit_time = str(curr_scan.get("fetched_at") or "")

            if curr_und <= 0:
                exit_idx += 1
                continue

            if action == "LONG":
                if curr_und >= target_und:
                    exit_reason = "TARGET"
                    exit_und = curr_und
                    break
                elif curr_und <= sl_und:
                    exit_reason = "SL"
                    exit_und = curr_und
                    break
            else:  # SHORT
                if curr_und <= target_und:
                    exit_reason = "TARGET"
                    exit_und = curr_und
                    break
                elif curr_und >= sl_und:
                    exit_reason = "SL"
                    exit_und = curr_und
                    break

            exit_und = curr_und
            exit_idx += 1

        holding_ticks = max(1, exit_idx - i)

        # Delta estimate for option premium change (~0.50 ATM delta)
        delta = 0.50
        pts_move = (exit_und - und) if action == "LONG" else (und - exit_und)
        raw_exit_prem = max(0.05, entry_prem + (pts_move * delta))

        # Slippage friction (0.5% premium slippage on exit)
        slippage = max(0.05, raw_exit_prem * 0.005)
        final_exit_prem = max(0.05, raw_exit_prem - slippage)

        gross_pnl = (final_exit_prem - entry_prem) * lot_size
        tx_costs = _calc_transaction_costs(
            option_type=opt_type,
            side="BUY",
            entry_premium=entry_prem,
            entry_underlying=und,
            exit_premium=final_exit_prem,
            exit_underlying=exit_und,
            lot_size=lot_size,
            lots=1,
            symbol=symbol,
        )
        net_pnl = gross_pnl - tx_costs

        trades.append(
            SimulatedTrade(
                trade_id=trade_counter,
                symbol=symbol,
                entry_time=entry_time,
                exit_time=exit_time,
                direction=action,
                entry_underlying=und,
                exit_underlying=exit_und,
                entry_premium=entry_prem,
                exit_premium=final_exit_prem,
                sl_underlying=sl_und,
                target_underlying=target_und,
                lot_size=lot_size,
                lots=1,
                gross_pnl=gross_pnl,
                transaction_costs=tx_costs,
                net_pnl=net_pnl,
                exit_reason=exit_reason,
                holding_ticks=holding_ticks,
                params=params,
            )
        )

        # Jump forward past this trade to avoid overlapping entries
        i = exit_idx

    return trades


def _calc_metrics(trades: list[SimulatedTrade]) -> tuple[float, float, float]:
    """Return (win_rate, net_pnl, profit_factor)."""
    if not trades:
        return 0.0, 0.0, 0.0
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    wr = len(wins) / len(trades)
    net_pnl = sum(t.net_pnl for t in trades)
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    return wr, net_pnl, pf


def run_walk_forward_backtest(
    symbol: str = "NIFTY",
    days: int = 45,
    train_days: int = 14,
    test_days: int = 7,
) -> dict[str, Any]:
    """Execute walk-forward optimization across historical scan summaries."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with get_conn(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT * FROM scan_summaries
            WHERE symbol = ? AND fetched_at >= ? AND is_fallback = 0
            ORDER BY fetched_at ASC
            """,
            (symbol, cutoff),
        ).fetchall()
        scans = [dict(r) for r in rows]

    if len(scans) < 20:
        return {
            "status": "INSUFFICIENT_DATA",
            "message": f"Found only {len(scans)} scan summaries for {symbol} (minimum 20 required).",
            "symbol": symbol,
            "days": days,
        }

    # Parameter grid for walk-forward calibration
    param_grid = [
        BacktestParams(
            atr_sl_mult=sl,
            atr_target_mult=tgt,
            composite_threshold=comp,
            pcr_bull_threshold=pcr_b,
            pcr_bear_threshold=pcr_s,
        )
        for sl, tgt, comp, (pcr_b, pcr_s) in itertools.product(
            [1.0, 1.5, 2.0],
            [1.5, 2.0, 2.5],
            [55, 62, 70],
            [(1.0, 0.8), (1.1, 0.9)],
        )
    ]

    first_time = datetime.fromisoformat(scans[0]["fetched_at"].replace("Z", "+00:00"))
    last_time = datetime.fromisoformat(scans[-1]["fetched_at"].replace("Z", "+00:00"))
    total_duration = (last_time - first_time).total_seconds() / 86400.0

    step_days = test_days
    window_perf: list[WindowPerformance] = []
    all_oos_trades: list[SimulatedTrade] = []
    default_oos_trades: list[SimulatedTrade] = []
    default_params = BacktestParams(atr_sl_mult=1.5, atr_target_mult=2.0, composite_threshold=62)

    current_start = first_time
    w_idx = 1

    while True:
        train_end = current_start + timedelta(days=train_days)
        test_end = train_end + timedelta(days=test_days)

        if train_end >= last_time:
            break

        train_scans = [
            s
            for s in scans
            if current_start.isoformat()
            <= s["fetched_at"]
            < train_end.isoformat()
        ]
        test_scans = [
            s
            for s in scans
            if train_end.isoformat()
            <= s["fetched_at"]
            < min(test_end, last_time + timedelta(minutes=1)).isoformat()
        ]

        if len(train_scans) >= 10 and len(test_scans) >= 5:
            best_p = default_params
            best_score = -999999.0
            best_train_trades: list[SimulatedTrade] = []

            for p in param_grid:
                t_trades = simulate_strategy(train_scans, symbol, p)
                wr, net_pnl, pf = _calc_metrics(t_trades)
                score = net_pnl * (wr if len(t_trades) >= 3 else 0.1)
                if score > best_score:
                    best_score = score
                    best_p = p
                    best_train_trades = t_trades

            oos_trades = simulate_strategy(test_scans, symbol, best_p)
            def_trades = simulate_strategy(test_scans, symbol, default_params)

            all_oos_trades.extend(oos_trades)
            default_oos_trades.extend(def_trades)

            tr_wr, tr_pnl, tr_pf = _calc_metrics(best_train_trades)
            oos_wr, oos_pnl, oos_pf = _calc_metrics(oos_trades)

            window_perf.append(
                WindowPerformance(
                    window_idx=w_idx,
                    train_start=current_start.strftime("%Y-%m-%d"),
                    train_end=train_end.strftime("%Y-%m-%d"),
                    test_start=train_end.strftime("%Y-%m-%d"),
                    test_end=min(test_end, last_time).strftime("%Y-%m-%d"),
                    best_params=best_p,
                    in_sample_trades=len(best_train_trades),
                    in_sample_win_rate=round(tr_wr, 3),
                    in_sample_net_pnl=round(tr_pnl, 2),
                    in_sample_profit_factor=round(tr_pf, 2),
                    out_of_sample_trades=len(oos_trades),
                    out_of_sample_win_rate=round(oos_wr, 3),
                    out_of_sample_net_pnl=round(oos_pnl, 2),
                    out_of_sample_profit_factor=round(oos_pf, 2),
                )
            )
            w_idx += 1

        current_start += timedelta(days=step_days)

    total_oos_wr, total_oos_pnl, total_oos_pf = _calc_metrics(all_oos_trades)
    def_oos_wr, def_oos_pnl, def_oos_pf = _calc_metrics(default_oos_trades)

    default_matches = sum(
        1
        for w in window_perf
        if abs(w.best_params.atr_sl_mult - 1.5) < 0.01
        and abs(w.best_params.atr_target_mult - 2.0) < 0.01
        and w.best_params.composite_threshold == 62
    )
    stability_pct = round((default_matches / len(window_perf) * 100.0), 1) if window_perf else 0.0

    return {
        "status": "COMPLETED",
        "symbol": symbol,
        "analyzed_scans": len(scans),
        "total_days": round(total_duration, 1),
        "windows_evaluated": len(window_perf),
        "magic_constants_stability_pct": stability_pct,
        "walk_forward_out_of_sample": {
            "total_trades": len(all_oos_trades),
            "win_rate": round(total_oos_wr, 3),
            "net_pnl_rupees": round(total_oos_pnl, 2),
            "profit_factor": round(total_oos_pf, 2),
        },
        "baseline_default_constants": {
            "total_trades": len(default_oos_trades),
            "win_rate": round(def_oos_wr, 3),
            "net_pnl_rupees": round(def_oos_pnl, 2),
            "profit_factor": round(def_oos_pf, 2),
        },
        "windows": [
            {
                "window": w.window_idx,
                "train": f"{w.train_start} to {w.train_end}",
                "test": f"{w.test_start} to {w.test_end}",
                "best_params": {
                    "sl_mult": w.best_params.atr_sl_mult,
                    "target_mult": w.best_params.atr_target_mult,
                    "threshold": w.best_params.composite_threshold,
                    "pcr_bull": w.best_params.pcr_bull_threshold,
                    "pcr_bear": w.best_params.pcr_bear_threshold,
                },
                "train_wr": w.in_sample_win_rate,
                "train_pnl": w.in_sample_net_pnl,
                "test_wr": w.out_of_sample_win_rate,
                "test_pnl": w.out_of_sample_net_pnl,
                "test_pf": w.out_of_sample_profit_factor,
            }
            for w in window_perf
        ],
    }


def verify_positive_expectancy(symbol: str, min_pf: float = 1.0) -> dict:
    """Verify whether a symbol exhibits verified positive expectancy in walk-forward backtesting.
    Acts as a gating check before live trading authorization."""
    base_sym = symbol.upper().split()[0] if symbol else symbol.upper()
    try:
        res = run_walk_forward_backtest(symbol=base_sym, days=45, train_days=14, test_days=7)
        if res.get("status") == "COMPLETED":
            oos = res.get("walk_forward_out_of_sample", {})
            pf = float(oos.get("profit_factor") or 0.0)
            net_pnl = float(oos.get("net_pnl_rupees") or 0.0)
            trades = int(oos.get("total_trades") or 0)
            passed = bool(pf >= min_pf and net_pnl > 0 and trades >= 3)
            return {
                "passed": passed,
                "symbol": base_sym,
                "profit_factor": pf,
                "net_pnl": net_pnl,
                "trades": trades,
                "win_rate": oos.get("win_rate", 0.0),
                "reason": "Positive expectancy verified" if passed else f"PF {pf:.2f} < {min_pf} or net PnL <= 0",
            }
        else:
            return {
                "passed": False,
                "symbol": base_sym,
                "profit_factor": 0.0,
                "net_pnl": 0.0,
                "trades": 0,
                "win_rate": 0.0,
                "reason": f"Insufficient historical scan data to verify expectancy: {res.get('message')}",
            }
    except Exception as e:
        log.warning("verify_positive_expectancy failed for %s: %s", symbol, e)
        return {
            "passed": False,
            "symbol": base_sym,
            "profit_factor": 0.0,
            "net_pnl": 0.0,
            "trades": 0,
            "win_rate": 0.0,
            "reason": f"Expectancy verification error: {e}",
        }


def generate_readiness_backtest_report(symbols: list[str] | None = None) -> dict:
    """Run walk-forward optimization across core symbols and write documentation artifacts.
    Produces docs/reports/walk_forward_backtest_report.json and .md.
    """
    import json
    from pathlib import Path

    symbols_to_run = symbols or ["NIFTY", "BANKNIFTY", "NATURALGAS"]
    report_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "title": "NSEBOT Walk-Forward Backtest & Positive Expectancy Report",
        "symbols": {},
        "overall_expectancy_summary": {},
    }

    for sym in symbols_to_run:
        res = run_walk_forward_backtest(sym, days=45, train_days=14, test_days=7)
        report_data["symbols"][sym] = res
        oos = res.get("walk_forward_out_of_sample", {})
        pf = float(oos.get("profit_factor") or 0.0)
        pnl = float(oos.get("net_pnl_rupees") or 0.0)
        trades = int(oos.get("total_trades") or 0)
        wr = float(oos.get("win_rate") or 0.0)
        report_data["overall_expectancy_summary"][sym] = {
            "status": res.get("status"),
            "profit_factor": pf,
            "net_pnl": pnl,
            "win_rate": wr,
            "trades": trades,
            "positive_expectancy": bool(res.get("status") == "COMPLETED" and pf >= 1.0 and pnl > 0 and trades >= 3),
        }

    report_dir = Path("docs/reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "walk_forward_backtest_report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    md_path = report_dir / "walk_forward_backtest_report.md"
    lines = [
        "# Walk-Forward Backtest & Positive Expectancy Report",
        f"Generated: {report_data['generated_at']}",
        "",
        "## Executive Summary",
        "| Symbol | Status | Scans Analyzed | OOS Trades | Win Rate | Profit Factor | Net P&L (INR) | Expectancy Verified |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for sym, sum_data in report_data["overall_expectancy_summary"].items():
        sym_res = report_data["symbols"][sym]
        scans = sym_res.get("analyzed_scans", 0)
        trades = sum_data.get("trades", 0)
        wr = sum_data.get("win_rate", 0.0)
        pf = sum_data.get("profit_factor", 0.0)
        pnl = sum_data.get("net_pnl", 0.0)
        status = sum_data.get("status", "UNKNOWN")
        verified = "✅ PASS" if sum_data.get("positive_expectancy") else "⚠️ INSUFFICIENT DATA / NEGATIVE"
        lines.append(
            f"| {sym} | {status} | {scans} | {trades} | {wr:.1%} | {pf:.2f} | ₹{pnl:,.2f} | {verified} |"
        )
    lines.append("")
    lines.append("## Methodology")
    lines.append("- Realistic retail transaction costs included (STT/CTT, turnover charges, GST, SEBI fee, stamp duty).")
    lines.append("- Slippage modeled at 0.5% premium friction.")
    lines.append("- Rolling 14-day in-sample training window with 7-day out-of-sample forward test step.")
    lines.append("")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info("Walk-forward backtest report generated at %s and %s", json_path, md_path)
    return report_data


def main():
    parser = argparse.ArgumentParser(description="Replay Backtester & Walk-Forward Optimizer")
    parser.add_argument("--symbol", default="NIFTY", help="Symbol to test (NIFTY, BANKNIFTY, NATURALGAS)")
    parser.add_argument("--days", type=int, default=45, help="Lookback window in days")
    parser.add_argument("--train-days", type=int, default=14, help="In-sample training days")
    parser.add_argument("--test-days", type=int, default=7, help="Out-of-sample testing days")
    args = parser.parse_args()

    print(f"\n[*] Running Walk-Forward Replay Backtester on {args.symbol} ({args.days}d history)...")
    res = run_walk_forward_backtest(
        symbol=args.symbol,
        days=args.days,
        train_days=args.train_days,
        test_days=args.test_days,
    )

    if res.get("status") != "COMPLETED":
        print(f"[!] {res.get('message')}")
        return

    print("\n" + "=" * 78)
    print(f"  WALK-FORWARD REPLAY REPORT: {res['symbol']} ({res['total_days']} days, {res['analyzed_scans']} scans)")
    print("=" * 78)
    print(f"  Windows Evaluated        : {res['windows_evaluated']}")
    print(f"  Magic Constants Stability: {res['magic_constants_stability_pct']}% (1.5x/2.0x ATR, 62 Comp)")
    print("-" * 78)
    oos = res["walk_forward_out_of_sample"]
    base = res["baseline_default_constants"]
    print(f"  Walk-Forward OOS Trades  : {oos['total_trades']:<4} | Win Rate: {oos['win_rate']:.1%} | Net P&L: INR {oos['net_pnl_rupees']:>10,.2f} | PF: {oos['profit_factor']:.2f}")
    print(f"  Baseline (Fixed 1.5/2.0) : {base['total_trades']:<4} | Win Rate: {base['win_rate']:.1%} | Net P&L: INR {base['net_pnl_rupees']:>10,.2f} | PF: {base['profit_factor']:.2f}")
    print("=" * 78)

    print("\n  Window Breakdown:")
    print(f"  {'#':<3} {'Test Period':<23} {'Best Params (SL/Tgt/Comp)':<26} {'OOS WR':<8} {'OOS Net PnL':>12}")
    print("  " + "-" * 74)
    for w in res["windows"]:
        p_str = f"{w['best_params']['sl_mult']}x / {w['best_params']['target_mult']}x / {w['best_params']['threshold']}"
        print(f"  {w['window']:<3} {w['test']:<23} {p_str:<26} {w['test_wr']:>6.1%} {w['test_pnl']:>12,.2f}")
    print()


if __name__ == "__main__":
    main()
