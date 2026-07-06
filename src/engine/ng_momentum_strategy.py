"""
Natural Gas MOMENTUM Strategy.
Trend-following strategy running between 18:00 and 23:00 IST.
"""

import logging
from datetime import datetime, timezone
import pytz
import yfinance as yf
from src.models.schema import get_conn, get_open_paper_trade, insert_paper_trade, close_paper_trade
from src.engine.ng_risk_manager import check_ng_position_limit, check_ng_daily_loss_cap, calculate_ng_lot_size

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

def check_nymex_1h_trend() -> str:
    """
    Checks the 20 EMA trend of NYMEX NG=F on 1H charts.
    Returns "BULLISH" or "BEARISH" or "NEUTRAL".
    """
    try:
        df = yf.download("NG=F", period="5d", interval="1h", progress=False)
        if len(df) < 21:
            return "NEUTRAL"
            
        closes = df["Close"]
        # Calculate 20-period EMA
        ema20 = closes.ewm(span=20, adjust=False).mean()
        
        last_close = float(closes.iloc[-1])
        last_ema = float(ema20.iloc[-1])
        
        if last_close > last_ema:
            return "BULLISH"
        elif last_close < last_ema:
            return "BEARISH"
    except Exception as e:
        log.warning("Failed to check NYMEX 1H trend from yfinance: %s", e)
    return "NEUTRAL"

def check_ng_momentum_entry(side: str) -> tuple[bool, str]:
    """
    Evals momentum strategy gates:
    - NYMEX 1H trend alignment.
    - Position limit & Daily loss cap.
    """
    if not check_ng_position_limit():
        return False, "NG_POSITION_LIMIT_EXCEEDED"
        
    if check_ng_daily_loss_cap():
        return False, "NG_DAILY_LOSS_CAP_HIT"
        
    nymex_trend = check_nymex_1h_trend()
    log.info("NG Momentum Entry Check: Side=%s, NYMEX trend=%s", side, nymex_trend)
    
    if side == "BUY" and nymex_trend != "BULLISH":
        return False, "NYMEX_DIVERGENCE"
    elif side == "SELL" and nymex_trend != "BEARISH":
        return False, "NYMEX_DIVERGENCE"
        
    return True, "PASSED"

def check_ng_weekend_flat() -> None:
    """Force closes open Natural Gas positions past Friday 23:00 IST."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() == 4 and now_ist.time() >= datetime.strptime("23:00", "%H:%M").time():
        open_trade = get_open_paper_trade("NATURALGAS")
        if open_trade:
            log.info("Friday Weekend Flat rule: Force closing open NATURALGAS trade #%s", open_trade["id"])
            from src.fetchers.router import fetch_option_chain
            oc = fetch_option_chain("NATURALGAS")
            underlying = oc.get("underlying_price") if oc else open_trade["entry_underlying"]
            
            close_paper_trade(
                open_trade["id"],
                datetime.now(timezone.utc).isoformat(),
                underlying,
                underlying,
                "CLOSED_MANUAL",
                "Friday Weekend Flat protection"
            )
            
            try:
                from src.alerts.telegram_dispatcher import send_text
                send_text(f"🛑 **NG Friday Weekend Flat Close**\n"
                          f"• Closed trade #{open_trade['id']} at ₹{underlying:.2f} before weekend market close.")
            except Exception:
                pass
