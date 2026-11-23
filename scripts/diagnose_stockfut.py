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

    # 1. First pass: collect candles and 20d returns
    data_map = {}
    returns_20d = []
    for c in u:
        c_tok = client.resolve_instrument_token(c.symbol)
        if not c_tok:
            continue
        candles = client.get_historical_candles(c_tok, interval="day", from_date=from_date, to_date=now_dt)
        if not candles or len(candles) < 25:
            continue
        # Also get futures candles for OI
        f_candles = client.get_historical_candles(c.token, interval="day", from_date=from_date, to_date=now_dt)
        oi_series = [b.get("oi", 0) for b in f_candles] if f_candles else []
        closes = [b["close"] for b in candles]
        ret_20d = (closes[-1] - closes[-20]) / closes[-20] * 100.0 if len(closes) >= 20 and closes[-20] > 0 else 0.0
        returns_20d.append(ret_20d)
        data_map[c.symbol] = {
            "contract": c,
            "candles": candles,
            "oi_series": oi_series,
            "ret_20d": ret_20d,
        }
        if len(data_map) >= 20: # Test on first 20 for diagnostic
            break

    print(f"Processed {len(data_map)} sample stocks. Computing features with universe returns...")
    from src.stockfut.setups import stock_bias, bias_label, matrix_permission, is_daily_reversal, calculate_setup_score
    from src.stockfut.levels import build_zones

    for sym, d in list(data_map.items())[:3]:
        print(f"OI series for {sym} (len={len(d['oi_series'])}): {d['oi_series'][-5:]}")
        feat = compute_features(
            sym,
            d["candles"],
            index_daily=nifty_daily,
            universe_20d_returns=returns_20d,
            combined_oi_daily=d["oi_series"],
        )
        if not feat:
            continue
        b_score = stock_bias(feat)
        b_lbl = bias_label(b_score)
        score = calculate_setup_score(feat, b_lbl, "NEUTRAL")
        atr = float(feat.get("atr14", 10.0))
        zones = build_zones(d["candles"], atr=atr)
        perm = matrix_permission(b_lbl, "NEUTRAL")
        print(f"{sym}: bias={b_lbl}({b_score}), score={score}, perm={perm}, OI_flow={feat.get('oi_flow_20d')}, RS={feat.get('rs_rank_index')}")

if __name__ == "__main__":
    run()
