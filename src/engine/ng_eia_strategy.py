"""
Natural Gas EVENT Strategy (Thursday EIA surprise play).
"""

import logging
from datetime import datetime, timezone, timedelta
import pytz
import yfinance as yf
from src.models.schema import get_conn, get_open_paper_trade, insert_paper_trade, close_paper_trade
from src.engine.ng_risk_manager import check_ng_position_limit, check_ng_daily_loss_cap, calculate_ng_lot_size
from src.fetchers.eia_consensus_fetcher import fetch_eia_weekly_data, store_eia_weekly_data, parse_bcf_value
from config.runtime_config import load_runtime_config
from config.settings import LOT_SIZES

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

def force_close_eia_pre_print() -> None:
    """Closes all open Natural Gas trades on Thursday 19:40 IST before print."""
    open_trade = get_open_paper_trade("NATURALGAS")
    if not open_trade:
        return
        
    log.info("Thursday pre-print flat protection: Force closing NATURALGAS trade #%s", open_trade["id"])
    
    # Get current price
    from src.fetchers.router import fetch_option_chain
    oc = fetch_option_chain("NATURALGAS")
    underlying = oc.get("underlying_price") if oc else open_trade["entry_underlying"]
    
    close_paper_trade(
        open_trade["id"],
        datetime.now(timezone.utc).isoformat(),
        underlying,
        underlying,
        "CLOSED_MANUAL",
        "EIA Pre-print flat protection"
    )
    
    try:
        from src.alerts.telegram_dispatcher import send_text
        send_text(f"⚠️ **NG Pre-Print Force Close**\n"
                  f"• Closed trade #{open_trade['id']} at ₹{underlying:.2f} to protect against Thursday print volatility.")
    except Exception:
        pass

def fetch_eia_actual_fallback() -> float | None:
    """Fallback: Scrape actual net change from official EIA page."""
    try:
        import requests
        from bs4 import BeautifulSoup
        import re
        url = "https://ir.eia.gov/ngs/ngs.html"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for tr in soup.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if cells and cells[0] == "Total":
                    numbers = []
                    for cell in cells[1:]:
                        cleaned = re.sub(r"[^\d.\-]", "", cell)
                        if cleaned:
                            try:
                                numbers.append(float(cleaned))
                            except ValueError:
                                pass
                    if len(numbers) >= 3:
                        return numbers[2]
    except Exception as e:
        log.warning("EIA fallback scrape failed: %s", e)
    return None

def get_pre_release_price() -> float | None:
    """Finds NATURALGAS price logged in database between 19:40 and 19:59 IST today."""
    now_ist = datetime.now(IST)
    start_ist = now_ist.replace(hour=19, minute=40, second=0, microsecond=0)
    start_utc = start_ist.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT price FROM underlying_price 
            WHERE symbol = 'NATURALGAS' 
              AND fetched_at >= ? 
            ORDER BY fetched_at ASC 
            LIMIT 1
            """,
            (start_utc,)
        ).fetchone()
        if row:
            return float(row["price"])
    return None

def calculate_nymex_5m_atr(period_days: int = 1) -> float:
    """Calculate 14-period ATR for NYMEX NG=F using 5-min candles."""
    try:
        df = yf.download("NG=F", period="1d", interval="5m", progress=False)
        if len(df) < 15:
            return 0.05 # safe fallback
        # Compute True Range
        df['H-L'] = df['High'] - df['Low']
        df['H-PC'] = abs(df['High'] - df['Close'].shift(1))
        df['L-PC'] = abs(df['Low'] - df['Close'].shift(1))
        df['TR'] = df[['H-L', 'H-PC', 'L-PC']].max(axis=1)
        # Compute ATR (Rolling mean of TR)
        df['ATR'] = df['TR'].rolling(14).mean()
        atr = df['ATR'].iloc[-1]
        if not atr or atr <= 0:
            return 0.05
        return float(atr)
    except Exception as e:
        log.warning("Failed to calculate ATR from yfinance: %s", e)
        return 0.05

def run_ng_eia_strategy(
    symbol: str,
    scan_context: dict,
    digest_id: str,
    intel: dict,
    ai_verdict=None,
) -> dict | None:
    """ Thursday EIA storage surprise play. """
    from config.settings import NG_STRATEGY_ENABLED, EIA_MIN_SURPRISE_BCF, EIA_NO_TRADE_BAND_BCF
    
    if not NG_STRATEGY_ENABLED:
        return None
        
    now_ist = datetime.now(IST)
    from src.engine.ng_session_router import get_ng_regime
    regime, reason = get_ng_regime(now_ist)
    if regime != "EVENT":
        return None
        
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    if underlying <= 0:
        return None
        
    # Get consensus from DB
    thursday_str = now_ist.strftime("%Y-%m-%d")
    consensus = None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT consensus_bcf FROM eia_consensus WHERE report_date = ?",
            (thursday_str,)
        ).fetchone()
        if row and row["consensus_bcf"] is not None:
            consensus = float(row["consensus_bcf"])
            
    # Try fetching consensus fresh if not found
    if consensus is None:
        log.info("EIA consensus not found in DB. Trying fresh fetch...")
        ff_data = fetch_eia_weekly_data()
        if ff_data:
            store_eia_weekly_data(ff_data)
            consensus = ff_data.get("consensus_bcf")
            
    if consensus is None:
        log.warning("EVENT Strategy skipped: No consensus storage estimate available.")
        return {"action": "HOLD", "reason": "EIA_NO_CONSENSUS"}
        
    # Scrape actual
    actual = None
    # 1. Try from Forex Factory
    ff_data = fetch_eia_weekly_data()
    if ff_data and ff_data.get("actual_bcf") is not None:
        actual = ff_data.get("actual_bcf")
        
    # 2. Try fallback scrape from EIA site
    if actual is None:
        actual = fetch_eia_actual_fallback()
        
    if actual is None:
        log.info("EIA actual release not yet available. Retrying on next scan.")
        return {"action": "HOLD", "reason": "EIA actual release not yet available"}

    surprise = actual - consensus
    abs_surprise = abs(surprise)

    # Store surprise in DB
    with get_conn() as conn:
        conn.execute(
            "UPDATE eia_consensus SET actual_bcf=?, surprise_bcf=? WHERE report_date=?",
            (actual, surprise, thursday_str)
        )

    if abs_surprise < EIA_NO_TRADE_BAND_BCF:
        log.info("EIA surprise (%+d Bcf) is within no-trade band (%d Bcf).", surprise, EIA_NO_TRADE_BAND_BCF)
        return {"action": "HOLD", "reason": f"EIA_NO_TRADE_BAND: surprise={surprise}"}

    if abs_surprise < EIA_MIN_SURPRISE_BCF:
        log.info("EIA surprise (%+d Bcf) is below minimum threshold (%d Bcf).", surprise, EIA_MIN_SURPRISE_BCF)
        return {"action": "HOLD", "reason": f"EIA_BELOW_MIN_SURPRISE: surprise={surprise}"}

    # Position check
    if not check_ng_position_limit():
        return {"action": "BLOCKED_RISK", "reason": "NG position limit hit"}
    if check_ng_daily_loss_cap():
        return {"action": "BLOCKED_RISK", "reason": "NG daily loss cap hit"}

    # Determine side
    # build > consensus (positive surprise, bearish) -> SELL
    # draw > consensus (negative surprise, bullish) -> BUY
    side = "SELL" if surprise > 0 else "BUY"
    verdict = "SHORT" if side == "SELL" else "LONG"

    # Check confirmation: price must have moved in surprise direction vs pre-release
    pre_price = get_pre_release_price()
    if not pre_price:
        pre_price = underlying  # default

    price_confirmed = False
    if side == "BUY" and underlying > pre_price:
        price_confirmed = True
    elif side == "SELL" and underlying < pre_price:
        price_confirmed = True

    if not price_confirmed:
        log.info("EVENT entry blocked: No price confirmation. Price has not moved in surprise direction yet "
                 "(current=%g, pre-release=%g, side=%s)", underlying, pre_price, side)
        return {"action": "HOLD", "reason": f"No surprise direction confirmation (pre={pre_price:.2f}, current={underlying:.2f})"}
        
    # Sizing / Risk Stop
    # Stop distance = 0.6 * ATR(5m, 14) * USDINR
    from src.engine.parity_engine import _get_shoonya_usdinr, _get_yf_quote
    usdinr, _, _ = _get_shoonya_usdinr()
    if usdinr == 0:
        usdinr, _ = _get_yf_quote("INR=X")
    if usdinr == 0:
        usdinr = 83.5
        
    atr_usd = calculate_nymex_5m_atr()
    stop_distance_points = 0.6 * atr_usd * usdinr
    if stop_distance_points <= 1.0:
        stop_distance_points = 2.0  # minimum stop
        
    sl_underlying = underlying + stop_distance_points if side == "SELL" else underlying - stop_distance_points
    # Target = 2R reward target
    target_underlying = underlying - 2.0 * stop_distance_points if side == "SELL" else underlying + 2.0 * stop_distance_points
    
    config = load_runtime_config()
    capital = float(config.get("live_capital_per_trade_inr") or 50000.0)
    lots = calculate_ng_lot_size(capital, stop_distance_points)
    
    opened_at = datetime.now(timezone.utc).isoformat()
    signal_key = f"NG_EVENT_{opened_at}_{side}"
    
    trade_data = {
        "opened_at": opened_at,
        "symbol": "NATURALGAS",
        "expiry": scan_context.get("futures_expiry"),
        "verdict_label": verdict,
        "side": side,
        "option_type": "FUT",
        "strike": None,
        "entry_underlying": underlying,
        "entry_premium": underlying,
        "sl_underlying": sl_underlying,
        "target_underlying": target_underlying,
        "lots": lots,
        "status": "OPEN",
        "reason": f"NG EVENT surprise play | actual={actual} (consensus={consensus}, surprise={surprise:+.1f} Bcf)",
        "digest_id": digest_id,
        "trade_status": "TRIGGERED_CORE",
        "setup_type": "NG_EVENT",
        "decision_reason": f"EIA surprise %+d Bcf triggers {side}",
        "confidence_score": 100,
        "entry_quality_score": 100,
        "trend_alignment_score": 100,
        "regime_score": 100,
        "signal_key": signal_key,
        "regime": "EVENT",
        "underlying": underlying
    }
    
    trade_id = insert_paper_trade(trade_data)
    if trade_id:
        log.info("Opened NG EVENT paper trade #%d | %s FUT %d lots at %g | SL %g, Tgt %g",
                 trade_id, side, lots, underlying, sl_underlying, target_underlying)
        try:
            from src.alerts.telegram_dispatcher import send_text
            send_text(f"🔥 **NG EVENT EIA Paper Trade OPENED**\n"
                      f"• Side: {side} FUT\n"
                      f"• Actual: {actual} | Consensus: {consensus}\n"
                      f"• Surprise: {surprise:+.1f} Bcf\n"
                      f"• Entry Price: ₹{underlying:.2f}\n"
                      f"• SL: ₹{sl_underlying:.2f} | Tgt: ₹{target_underlying:.2f}")
        except Exception:
            pass
        return {"action": "EXECUTED", "trade_id": trade_id, "reason": "EVENT trade opened"}
        
    return None

def check_ng_eia_exits_every_2_min() -> None:
    """Check exits for open EIA EVENT trades."""
    open_trade = get_open_paper_trade("NATURALGAS")
    if not open_trade or open_trade.get("setup_type") != "NG_EVENT":
        return
        
    from src.fetchers.router import fetch_option_chain
    oc = fetch_option_chain("NATURALGAS")
    underlying = oc.get("underlying_price") if oc else None
    if not underlying or underlying <= 0:
        return
        
    sl_ul = float(open_trade.get("sl_underlying") or 0)
    tgt_ul = float(open_trade.get("target_underlying") or 0)
    side = open_trade["side"]
    entry_und = float(open_trade["entry_underlying"])
    
    # ATR Stop loss & trailing SL logic
    # Trail to BE after +1R
    hit_sl = side == "BUY" and underlying <= sl_ul
    hit_sl = hit_sl or (side == "SELL" and underlying >= sl_ul)
    
    # Target check
    hit_target = side == "BUY" and underlying >= tgt_ul
    hit_target = hit_target or (side == "SELL" and underlying <= tgt_ul)
    
    # Trailing stop: check if we reached +1R and trail to breakeven
    r_dist = abs(entry_und - sl_ul)
    if r_dist > 0:
        r_current = (entry_und - underlying) / r_dist if side == "SELL" else (underlying - entry_und) / r_dist
        if r_current >= 1.0:
            # Trailed stop is breakeven
            if side == "BUY" and underlying <= entry_und:
                hit_sl = True
            elif side == "SELL" and underlying >= entry_und:
                hit_sl = True
                
    # Hard stop at 21:30 IST
    now_ist = datetime.now(IST)
    is_time_stop = now_ist.time() >= datetime.strptime("21:30", "%H:%M").time()
    
    exit_hit = hit_target or hit_sl or is_time_stop
    if exit_hit:
        status = "CLOSED"
        reason = ""
        if hit_target:
            status = "CLOSED_TARGET"
            reason = "EIA target hit (+2R)"
        elif hit_sl:
            status = "CLOSED_SL"
            reason = "EIA stop loss hit / breakeven trail hit"
        elif is_time_stop:
            reason = "EIA hard time-stop 21:30 IST"
            
        close_paper_trade(
            open_trade["id"],
            datetime.now(timezone.utc).isoformat(),
            underlying,
            underlying,
            status,
            reason
        )
        log.info("Closed NG EVENT paper trade #%d | reason: %s at price %g",
                 open_trade["id"], reason, underlying)
        try:
            from src.alerts.telegram_dispatcher import send_text
            send_text(f"🛑 **NG EVENT Paper Trade CLOSED**\n"
                      f"• Trade ID: #{open_trade['id']}\n"
                      f"• Reason: {reason}\n"
                      f"• Price: ₹{underlying:.2f}")
        except Exception:
            pass
