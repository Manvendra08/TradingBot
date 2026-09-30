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
            
        closes = df["Close"].squeeze()
        # Calculate 20-period EMA
        ema20 = closes.ewm(span=20, adjust=False).mean()
        
        last_close = float(closes.iloc[-1].item() if hasattr(closes.iloc[-1], "item") else closes.iloc[-1])
        last_ema = float(ema20.iloc[-1].item() if hasattr(ema20.iloc[-1], "item") else ema20.iloc[-1])
        
        if last_close > last_ema:
            return "BULLISH"
        elif last_close < last_ema:
            return "BEARISH"
    except Exception as e:
        log.warning("Failed to check NYMEX 1H trend from yfinance: %s", e)
    return "NEUTRAL"

def check_ng_momentum_entry(
    side: str,
    scan_context: dict | None = None,
    intel: dict | None = None
) -> tuple[bool, str]:
    """
    Evals momentum strategy gates:
    - NYMEX 1H trend alignment.
    - Position limit & Daily loss cap.
    - Active EIA macro stance calibrated against real-time price trend and OI data.
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

    # Check active weekly EIA macro stance to prevent fighting structural deficit/surplus trends,
    # but give decisive weightage to real-time price movement and OI positioning.
    try:
        from src.engine.ng_macro_context import get_active_ng_macro_context
        macro_ctx = get_active_ng_macro_context()
        macro_stance = macro_ctx.get("macro_stance", "NEUTRAL_BALANCED")
        squeeze_risk = bool(macro_ctx.get("is_rollover_squeeze_risk", False))

        is_conflict = (
            (side == "SELL" and macro_stance == "BULLISH_TIGHTENING") or
            (side == "BUY" and macro_stance == "BEARISH_LOOSENING")
        )

        if is_conflict:
            # 1. Resolve real-time OI and technical context
            verdict_label = ""
            conf = 0
            candle_1h = ""
            candle_3h = ""
            ce_oi_chg = 0
            pe_oi_chg = 0

            if intel and isinstance(intel, dict):
                verdict_label = str(intel.get("verdict_label") or "").upper()
                conf = int(intel.get("confidence") or 0)
            if scan_context and isinstance(scan_context, dict):
                if not verdict_label:
                    verdict_label = str(scan_context.get("verdict_label") or "").upper()
                if not conf:
                    conf = int(scan_context.get("engine_confidence") or 0)
                candle_1h = str(scan_context.get("candle_1h") or "").upper()
                candle_3h = str(scan_context.get("candle_3h") or "").upper()
                ce_oi_chg = int(scan_context.get("ce_oi_change") or 0)
                pe_oi_chg = int(scan_context.get("pe_oi_change") or 0)

            # Fallback to latest scan summary if context not supplied
            if not verdict_label:
                try:
                    with get_conn() as conn:
                        row = conn.execute(
                            "SELECT verdict_label, confidence, candle_1h, candle_3h, ce_oi_change, pe_oi_change "
                            "FROM scan_summaries WHERE symbol='NATURALGAS' ORDER BY id DESC LIMIT 1"
                        ).fetchone()
                        if row:
                            verdict_label = str(row["verdict_label"] or "").upper()
                            conf = int(row["confidence"] or 0)
                            candle_1h = str(row["candle_1h"] or "").upper()
                            candle_3h = str(row["candle_3h"] or "").upper()
                            ce_oi_chg = int(row["ce_oi_change"] or 0)
                            pe_oi_chg = int(row["pe_oi_change"] or 0)
                except Exception:
                    pass

            # 2. Evaluate Price Action & OI conviction
            oi_confirms = False
            price_confirms = False

            if side == "SELL":
                # Price confirmed: NYMEX is BEARISH (already checked) + MCX candle is BEARISH
                price_confirms = (candle_1h == "BEARISH" or candle_3h == "BEARISH" or nymex_trend == "BEARISH")
                # OI confirmed: Short Buildup, Call Writing, Long Unwinding, or Call OI added > Put OI
                is_bearish_verdict = any(k in verdict_label for k in ("SHORT", "CALL WRITING", "LONG UNWINDING", "BEARISH"))
                is_bearish_flows = (ce_oi_chg > 0 and pe_oi_chg <= 0) or (ce_oi_chg - pe_oi_chg > 1000)
                oi_confirms = (is_bearish_verdict and conf >= 40) or (is_bearish_flows and conf >= 30)
            elif side == "BUY":
                # Price confirmed: NYMEX is BULLISH (already checked) + MCX candle is BULLISH
                price_confirms = (candle_1h == "BULLISH" or candle_3h == "BULLISH" or nymex_trend == "BULLISH")
                # OI confirmed: Long Buildup, Put Writing, Short Covering, or Put OI added > Call OI
                is_bullish_verdict = any(k in verdict_label for k in ("LONG", "PUT WRITING", "SHORT COVERING", "BULLISH"))
                is_bullish_flows = (pe_oi_chg > 0 and ce_oi_chg <= 0) or (pe_oi_chg - ce_oi_chg > 1000)
                oi_confirms = (is_bullish_verdict and conf >= 40) or (is_bullish_flows and conf >= 30)

            # 3. Rollover squeeze risk check: Front-month expiry squeeze risk takes precedence
            if side == "SELL" and macro_stance == "BULLISH_TIGHTENING" and squeeze_risk:
                log.warning("NG Momentum Entry Blocked: Selling into front-month rollover squeeze risk & BULLISH_TIGHTENING stance.")
                return False, "EIA_MACRO_ROLLOVER_SQUEEZE_RISK"

            # 4. If price action and OI confirm trend momentum, OVERRIDE the weekly EIA stance
            if price_confirms and oi_confirms:
                log.info(
                    "NG Momentum: Active weekly EIA stance %s OVERRIDDEN by price action (NYMEX=%s, MCX_1H=%s, 3H=%s) "
                    "and OI data (verdict='%s', conf=%d, CE_OI_chg=%+d, PE_OI_chg=%+d)",
                    macro_stance, nymex_trend, candle_1h, candle_3h, verdict_label, conf, ce_oi_chg, pe_oi_chg
                )
            else:
                log.warning(
                    "NG Momentum Entry Blocked: Side=%s conflicts with active weekly EIA stance %s "
                    "(insufficient price/OI conviction to override: verdict='%s', conf=%d, price_confirms=%s, oi_confirms=%s)",
                    side, macro_stance, verdict_label, conf, price_confirms, oi_confirms
                )
                return False, f"EIA_MACRO_CONFLICT_{macro_stance}"
    except Exception as e:
        log.debug("NG Momentum Macro Stance check error: %s", e)
        
    return True, "PASSED"

def check_ng_weekend_flat() -> None:
    """Force closes open Natural Gas positions past Friday 23:00 IST."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() == 4 and now_ist.time() >= datetime.strptime("23:00", "%H:%M").time():
        open_trade = get_open_paper_trade("NATURALGAS")
        if open_trade:
            log.info("Friday Weekend Flat rule: Force closing open NATURALGAS trade #%s", open_trade["id"])
            from src.fetchers.router import fetch_option_chain
            trade_expiry = open_trade.get("expiry")
            oc = fetch_option_chain("NATURALGAS", expiry=trade_expiry)
            underlying = oc.get("underlying_price") if oc else open_trade["entry_underlying"]
            
            exit_premium = None
            if open_trade.get("option_type") != "FUT":
                from src.engine.trade_plan import get_option_premium
                exit_premium = get_option_premium(
                    "NATURALGAS",
                    trade_expiry,
                    float(open_trade.get("strike") or 0.0),
                    open_trade.get("option_type") or "PE",
                    option_rows=oc.get("options") if oc else None,
                    underlying_price=underlying,
                )
            else:
                exit_premium = underlying
            
            close_paper_trade(
                open_trade["id"],
                datetime.now(timezone.utc).isoformat(),
                underlying,
                exit_premium,
                "CLOSED_MANUAL",
                "Friday Weekend Flat protection"
            )
            
            try:
                from src.alerts.telegram_dispatcher import send_text
                send_text(f"🛑 **NG Friday Weekend Flat Close**\n"
                          f"• Closed trade #{open_trade['id']} at ₹{underlying:.2f} before weekend market close.")
            except Exception:
                pass
