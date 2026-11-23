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
    for c in u[:5]:
        print(f"Contract: {c.symbol} | {c.tradingsymbol} | token={c.token}")
        f_candles = client.get_historical_candles(instrument_token=c.token, interval="day", from_date=from_date, to_date=now_dt)
        print(f"  Futures candles: {len(f_candles)} bars")
        if f_candles:
            print(f"  Last candle keys: {list(f_candles[-1].keys())}")
            print(f"  Last OI: {f_candles[-1].get('oi')}")

if __name__ == "__main__":
    run()
