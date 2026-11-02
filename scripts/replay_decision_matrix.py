"""
Historical Snapshot Replay Script for Decision Matrix Calibration.
Replays the deterministic decision matrix over historical option_chain_snapshots
to calibrate:
1. Credit-to-width ratios on fixed wings (>=30% vs 25-30% vs <25%).
2. Volatility bucket distribution (LOW, MID, HIGH).
3. MCX strike liquidity pass rates.
4. Structure recommendations across market regimes.
"""

import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Any

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.settings import DB_PATH
from config.decision_matrix_loader import load_decision_matrix
from src.engine.decision_matrix import (
    vol_bucket,
    select_structure,
    is_mcx_strike_liquid,
    delta_band,
    MCX_SYMBOLS,
)

_IST = timezone(timedelta(hours=5, minutes=30))


def run_replay(
    lookback_days: int = 30,
    symbols: list[str] | None = None,
    delta_profile: str = "aggressive",
):
    print("=" * 78)
    print(f"DECISION MATRIX CALIBRATION REPLAY (Profile: {delta_profile.upper()})")
    print(f"Database: {DB_PATH}")
    print(f"Lookback: {lookback_days} days")
    print("=" * 78)

    cfg = load_decision_matrix()
    cfg["delta_profile"] = delta_profile

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if not symbols:
        symbols = ["NIFTY", "BANKNIFTY", "NATURALGAS"]

    for symbol in symbols:
        print(f"\nAnalyzing symbol: {symbol}...")
        is_mcx = symbol in MCX_SYMBOLS

        # Get snapshots grouped by fetched_at and expiry
        query = """
            SELECT fetched_at, expiry, COUNT(*) as leg_count, AVG(underlying_price) as avg_und
            FROM option_chain_snapshots
            WHERE symbol = ?
              AND fetched_at >= datetime('now', ?)
            GROUP BY fetched_at, expiry
            HAVING leg_count >= 10
            ORDER BY fetched_at ASC
        """
        snapshots = conn.execute(query, (symbol, f"-{lookback_days} days")).fetchall()

        if not snapshots:
            print(f"  No snapshot clusters found for {symbol} in the last {lookback_days} days.")
            continue

        print(f"  Found {len(snapshots)} snapshot clusters to evaluate.")

        vol_counts = defaultdict(int)
        structure_counts = defaultdict(int)
        credit_ratios = []
        full_size_count = 0
        half_size_count = 0
        rejected_credit_count = 0

        mcx_liq_passes = 0
        mcx_liq_fails = 0
        mcx_liq_fail_reasons = defaultdict(int)

        # Preload historical IVs for IVP calculation
        iv_rows = conn.execute(
            """
            SELECT iv FROM option_chain_snapshots
            WHERE symbol = ? AND iv IS NOT NULL AND iv > 0
              AND fetched_at >= datetime('now', ?)
            ORDER BY fetched_at ASC
            """,
            (symbol, f"-{lookback_days + 15} days"),
        ).fetchall()
        all_iv_pool = [float(r["iv"]) for r in iv_rows]

        wing_width = float(cfg.get("wings", {}).get(symbol, 200.0 if not is_mcx else 10.0))

        for snap in snapshots:
            fetched_at = snap["fetched_at"]
            expiry = snap["expiry"]
            underlying = float(snap["avg_und"] or 0.0)

            # Fetch all strikes for this snapshot
            legs = conn.execute(
                """
                SELECT strike, option_type, ltp, bid, ask, delta, iv, volume, oi
                FROM option_chain_snapshots
                WHERE symbol = ? AND fetched_at = ? AND expiry = ?
                """,
                (symbol, fetched_at, expiry),
            ).fetchall()

            if not legs:
                continue

            # Compute approximate IVP from snapshot ATM IV
            valid_ivs = [float(l["iv"]) for l in legs if l["iv"] and float(l["iv"]) > 0]
            atm_iv = sum(valid_ivs) / len(valid_ivs) if valid_ivs else None

            if atm_iv and all_iv_pool:
                rank = sum(1 for x in all_iv_pool if x <= atm_iv)
                ivp = (rank / len(all_iv_pool)) * 100.0
            else:
                ivp = None

            vol_reg = vol_bucket(symbol, ivp=ivp, cfg=cfg)
            vol_counts[vol_reg] += 1

            # Determine direction from ATM call vs put volume/OI
            ce_oi = sum(int(l["oi"] or 0) for l in legs if l["option_type"] == "CE")
            pe_oi = sum(int(l["oi"] or 0) for l in legs if l["option_type"] == "PE")
            if pe_oi > ce_oi * 1.15:
                direction = "BULLISH"
            elif ce_oi > pe_oi * 1.15:
                direction = "BEARISH"
            else:
                direction = "NEUTRAL"

            # Parse DTE
            try:
                exp_dt = datetime.strptime(expiry, "%Y-%m-%d").date()
                snap_dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00")).astimezone(_IST)
                dte = (exp_dt - snap_dt.date()).days
            except Exception:
                dte = 5
                snap_dt = datetime.now(_IST)

            rec_struct, allowed, reason = select_structure(
                direction=direction,
                vol=vol_reg,
                dte=dte,
                symbol=symbol,
                now_ist=snap_dt,
                cfg=cfg,
            )
            structure_counts[rec_struct or "NO_TRADE"] += 1

            # MCX Liquidity gate evaluation across legs
            if is_mcx:
                for leg in legs:
                    leg_dict = dict(leg)
                    ok, fail_reason = is_mcx_strike_liquid(leg_dict, now_ist=snap_dt, cfg=cfg)
                    if ok:
                        mcx_liq_passes += 1
                    else:
                        mcx_liq_fails += 1
                        short_reason = fail_reason.split(":")[1].split("(")[0].strip() if ":" in fail_reason else fail_reason
                        mcx_liq_fail_reasons[short_reason] += 1

            # Credit-to-width evaluation for spreads & iron condors
            if rec_struct in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "IRON_CONDOR"):
                ce_legs = sorted([l for l in legs if l["option_type"] == "CE"], key=lambda x: float(x["strike"]))
                pe_legs = sorted([l for l in legs if l["option_type"] == "PE"], key=lambda x: float(x["strike"]))

                # Estimate credit from short leg at ~0.30 delta and long leg at wing_width
                if rec_struct == "BULL_PUT_SPREAD" and pe_legs:
                    short_candidates = [l for l in pe_legs if l["delta"] and abs(float(l["delta"])) <= 0.35]
                    if short_candidates:
                        short_leg = short_candidates[-1]
                        short_k = float(short_leg["strike"])
                        long_k = short_k - wing_width
                        long_candidates = [l for l in pe_legs if float(l["strike"]) <= long_k]
                        if long_candidates:
                            long_leg = long_candidates[-1]
                            s_prem = float(short_leg["ltp"] or 0)
                            l_prem = float(long_leg["ltp"] or 0)
                            net_credit = s_prem - l_prem
                            if net_credit > 0 and wing_width > 0:
                                ratio = net_credit / wing_width
                                credit_ratios.append(ratio)
                                if ratio >= 0.30:
                                    full_size_count += 1
                                elif ratio >= 0.25:
                                    half_size_count += 1
                                else:
                                    rejected_credit_count += 1

        # Summary for symbol
        print(f"\n--- Results for {symbol} ---")
        print(f"Total Snapshots: {len(snapshots)}")
        print("Volatility Buckets:")
        for vb, cnt in sorted(vol_counts.items()):
            pct = (cnt / len(snapshots)) * 100
            print(f"  {vb:5s}: {cnt:4d} ({pct:5.1f}%)")

        print("Recommended Structures:")
        for st, cnt in sorted(structure_counts.items(), key=lambda x: -x[1]):
            pct = (cnt / len(snapshots)) * 100
            print(f"  {st:20s}: {cnt:4d} ({pct:5.1f}%)")

        if credit_ratios:
            avg_ratio = sum(credit_ratios) / len(credit_ratios)
            print(f"\nCredit-to-Wing Ratio Distribution (Wing: {wing_width} pts):")
            print(f"  Average Net Credit / Wing: {avg_ratio * 100:.1f}%")
            tot_c = len(credit_ratios)
            print(f"  >= 30% (Full Size):  {full_size_count:4d} ({(full_size_count/tot_c)*100:5.1f}%)")
            print(f"  25-30% (Half Size):  {half_size_count:4d} ({(half_size_count/tot_c)*100:5.1f}%)")
            print(f"  < 25% (Rejected):    {rejected_credit_count:4d} ({(rejected_credit_count/tot_c)*100:5.1f}%)")

        if is_mcx and (mcx_liq_passes + mcx_liq_fails) > 0:
            tot_l = mcx_liq_passes + mcx_liq_fails
            print(f"\nMCX Strike Liquidity Gate (Total Leg Evaluations: {tot_l}):")
            print(f"  Pass: {mcx_liq_passes:5d} ({(mcx_liq_passes/tot_l)*100:5.1f}%)")
            print(f"  Fail: {mcx_liq_fails:5d} ({(mcx_liq_fails/tot_l)*100:5.1f}%)")
            print("  Top Failure Reasons:")
            for r, cnt in sorted(mcx_liq_fail_reasons.items(), key=lambda x: -x[1])[:5]:
                print(f"    - {r}: {cnt} ({(cnt/tot_l)*100:5.1f}%)")

    conn.close()
    print("\n" + "=" * 78)
    print("REPLAY COMPLETE")
    print("=" * 78)


if __name__ == "__main__":
    run_replay()
