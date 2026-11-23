from __future__ import annotations
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone, date

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("diagnose")
IST = timezone(timedelta(hours=5, minutes=30))

def run():
    from src.fetchers.kite_fetcher import get_kite_data_client
    from src.stockfut.universe import UniverseBuilder
    from src.stockfut.features import compute_features
    from src.stockfut.levels import build_zones
    from src.stockfut.setups import stock_bias, bias_label, matrix_permission, is_daily_reversal

    client = get_kite_data_client()
    now_dt = datetime.now(IST)
    from_date = now_dt - timedelta(days=120)

    builder = UniverseBuilder()
    u = builder.build_universe(date.today())
    print(f"Total universe: {len(u)}")

    # Fetch Nifty daily candles
    nifty_tok = client.resolve_instrument_token("NIFTY") or 256265
    nifty_daily = client.get_historical_candles(nifty_tok, interval="day", from_date=from_date, to_date=now_dt)

    drop_counts = {
        "NO_CANDLES": 0,
        "BIAS_NEUTRAL": 0,
        "NO_PERMISSION": 0,
        "LOW_SCORE": 0,
        "NO_REVERSAL": 0,
        "GEOMETRY_FAIL": 0,
        "SIZING_FAIL": 0,
        "ARMED": 0,
    }

    # Pass 1: returns
    stock_map = {}
    returns_20d = []
    for c in u:
        c_tok = client.resolve_instrument_token(c.symbol)
        if not c_tok:
            drop_counts["NO_CANDLES"] += 1
            continue
        candles = client.get_historical_candles(c_tok, interval="day", from_date=from_date, to_date=now_dt)
        if not candles or len(candles) < 25:
            drop_counts["NO_CANDLES"] += 1
            continue
        closes = [float(b["close"]) for b in candles]
        ret_20d = (closes[-1] - closes[-20]) / closes[-20] * 100.0 if len(closes) >= 20 and closes[-20] > 0 else 0.0
        returns_20d.append(ret_20d)
        stock_map[c.symbol] = (c, candles)

    print(f"Candles loaded for {len(stock_map)} stocks. Evaluating pipeline dropoffs...")
    from src.stockfut.setups import stock_bias, bias_label, matrix_permission, is_daily_reversal, calculate_setup_score
    from src.stockfut.levels import build_zones, shift_zones
    from src.engine.trade_plan import calculate_future_sl_target
    from src.engine.capital_allocator import calculate_future_lots

    armed_list = []
    reversal_candidates = []

    for sym, (c, candles) in stock_map.items():
        feat = compute_features(sym, candles, index_daily=nifty_daily, universe_20d_returns=returns_20d)
        if not feat:
            continue
        b_score = stock_bias(feat)
        b_lbl = bias_label(b_score)
        if b_lbl == "NEUTRAL":
            drop_counts["BIAS_NEUTRAL"] += 1
            continue
        perm = matrix_permission(b_lbl, "NEUTRAL")
        if not perm:
            drop_counts["NO_PERMISSION"] += 1
            continue
        side, min_score = perm
        score = calculate_setup_score(feat, b_lbl, "NEUTRAL")
        if score < min_score:
            drop_counts["LOW_SCORE"] += 1
            continue
        atr = float(feat.get("atr14", 10.0))
        zones = build_zones(candles, atr=atr)
        rev = is_daily_reversal(side=side, daily=candles, zones=zones, atr=atr, oi_flow_20d=feat.get("oi_flow_20d", "NONE"))
        if not rev:
            drop_counts["NO_REVERSAL"] += 1
            reversal_candidates.append((sym, side, score, b_lbl))
            continue
        drop_counts["ARMED"] += 1
        armed_list.append((sym, side, score, rev))

    print("Dropoff Summary:", drop_counts)
    print("Candidates that passed Bias+Permission+Score but missed reversal touch/close:", reversal_candidates[:10])
    print("Armed Setups:", armed_list)

if __name__ == "__main__":
    run()
