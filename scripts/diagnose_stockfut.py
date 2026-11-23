from __future__ import annotations
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("diagnose")
IST = timezone(timedelta(hours=5, minutes=30))

def run():
    from src.fetchers.kite_fetcher import get_kite_data_client
    from src.stockfut.features import compute_features
    from src.stockfut.levels import build_zones
    from src.stockfut.setups import stock_bias, bias_label, matrix_permission, is_daily_reversal

    client = get_kite_data_client()
    now_dt = datetime.now(IST)
    from_date = now_dt - timedelta(days=120)

    test_symbols = ["RELIANCE", "INFY", "TCS", "HDFCBANK", "SBIN", "TATASTEEL"]
    for sym in test_symbols:
        tok = client.resolve_instrument_token(sym)
        if not tok:
            print(f"{sym}: NO_TOKEN")
            continue
        candles = client.get_historical_candles(instrument_token=tok, interval="day", from_date=from_date, to_date=now_dt)
        if not candles:
            print(f"{sym}: NO_CANDLES for token {tok}")
            continue
        feat = compute_features(sym, candles)
        if not feat:
            print(f"{sym}: FEAT_FAILED")
            continue
        b_score = stock_bias(feat)
        b_lbl = bias_label(b_score)
        atr = float(feat.get("atr14", 10.0))
        zones = build_zones(candles, atr=atr)
        perm = matrix_permission(b_lbl, "NEUTRAL")
        rev = is_daily_reversal(perm[0] if perm else "LONG", candles, zones, atr, feat.get("oi_flow_20d", "NONE")) if perm else None
        print(f"{sym}: tok={tok}, bars={len(candles)}, bias={b_lbl}({b_score}), perm={perm}, rev={rev}, zones={len(zones)}")

if __name__ == "__main__":
    run()
