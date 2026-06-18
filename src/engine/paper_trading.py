"""Auto paper-trading engine based on bot verdict + scan context."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

import pytz

from src.engine.paper_plan import (
    build_paper_trade_plan,
    is_bearish_verdict,
    is_bullish_verdict,
)
from src.models.schema import (
    close_paper_trade,
    get_open_paper_trade,
    insert_paper_trade,
    get_latest_snapshots_for_symbol,
    get_open_timeframe_trades,
    get_scan_summary_at_least_1h_old,
    get_today_scan_count,
    get_scan_summary_n_scans_ago,
    get_conn,
)
from src.engine.trade_decision import make_trade_decision
from src.engine.risk_engine import check_risk_limits
from config.settings import LOT_SIZES, DEFAULT_LOTS_PER_TRADE, TIMEFRAME_OI_MIN_DIFF_PCT
from config.symbol_classes import market_window, get_strike_step, get_symbol_class
from config.runtime_config import get_scan_frequency_minutes, get_scan_frequency_nse, get_scan_frequency_mcx


IST = pytz.timezone("Asia/Kolkata")


def _is_market_open(symbol: str) -> bool:
    """Return True only if current IST time is within the symbol's market window."""
    now = datetime.now(IST)
    open_t, close_t, days = market_window(symbol)
    if now.weekday() not in days:
        return False
    from config.holidays import is_market_holiday
    if is_market_holiday(symbol, now):
        return False
    t = now.strftime("%H:%M")
    return open_t <= t <= close_t


def _get_option_premium(
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    option_rows: list[dict] | None = None,
) -> float | None:
    """Fetch current option premium (LTP) from latest snapshot."""
    for row in option_rows or []:
        try:
            if (
                abs(float(row.get("strike") or 0) - strike) < 0.01
                and str(row.get("option_type") or "").upper() == option_type
            ):
                premium = float(row.get("ltp") or 0.0)
                return premium if premium > 0 else None
        except Exception:
            continue
    try:
        snapshots = get_latest_snapshots_for_symbol(symbol, expiry)
        for snap in snapshots:
            if (abs(snap.get("strike", 0) - strike) < 0.01 and
                snap.get("option_type") == option_type):
                return float(snap.get("ltp") or 0.0)
    except Exception:
        pass
    return None


def _calculate_buy_sl_target(entry_premium: float) -> tuple[float, float]:
    """
    Calculate SL and Target in premium terms for BUY option trades.
      SL     = entry * 0.70  (exit if premium drops 30%)
      Target = entry * 1.50  (exit when premium rises 50%)
    """
    if entry_premium <= 0:
        return 0.0, 0.0
    sl     = round(entry_premium * 0.70, 2)
    target = round(entry_premium * 1.50, 2)
    return sl, target


def _calculate_sell_sl_target(entry_premium: float) -> tuple[float, float]:
    """
    Calculate SL and Target in premium terms for SELL option trades.
      SL     = entry * 1.50  (exit if premium rises 50% against us)
      Target = entry * 0.60  (exit when premium decays 40%)
    """
    if entry_premium <= 0:
        return 0.0, 0.0
    sl     = round(entry_premium * 1.50, 2)
    target = round(entry_premium * 0.60, 2)
    return sl, target


def _parse_verdict_and_confidence(intel_text: str) -> tuple[str, int]:
    verdict = ""
    confidence = 0
    m_v = re.search(r"\*Verdict:\s*([^\*]+)\*", intel_text or "")
    if m_v:
        verdict = m_v.group(1).strip()
    m_c = re.search(r"Confidence:\s*(\d+)%", intel_text or "")
    if m_c:
        confidence = int(m_c.group(1))
    return verdict, confidence


def _is_reversal_against_open_trade(open_trade: dict, verdict: str, confidence: int) -> bool:
    if confidence < 70:
        return False
    ot = str(open_trade.get("option_type") or "").upper()
    side = open_trade.get("side") or "BUY"
    if ot == "CE" and side == "BUY" and is_bearish_verdict(verdict):
        return True
    if ot == "PE" and side == "BUY" and is_bullish_verdict(verdict):
        return True
    if ot == "CE" and side == "SELL" and is_bullish_verdict(verdict):
        return True
    if ot == "PE" and side == "SELL" and is_bearish_verdict(verdict):
        return True
    return False


def _trade_plan_from_verdict(verdict: str, confidence: int, ctx: dict) -> dict | None:
    plan = build_paper_trade_plan(verdict, confidence, ctx)
    if not plan:
        return None

    expiry = ctx.get("expiry", "")
    symbol = ctx.get("symbol", "")
    option_rows = ctx.get("option_rows") or []
    strike = float(plan["strike"])
    option_type = str(plan["option_type"])
    side = plan.get("side", "BUY")

    if option_type == "FUT":
        entry_premium = float(plan["entry_underlying"])
        sl_premium = float(plan["sl_underlying"])
        target_premium = float(plan["target_underlying"])
    else:
        entry_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows)

        if not entry_premium or entry_premium <= 0:
            log.warning("%s: paper trade plan aborted — entry option premium unavailable for %s %s strike %s",
                        symbol, option_type, expiry, strike)
            return None

        if side == "SELL":
            sl_premium, target_premium = _calculate_sell_sl_target(entry_premium)
        else:
            sl_premium, target_premium = _calculate_buy_sl_target(entry_premium)

    return {
        **plan,
        "entry_premium": entry_premium,
        "sl_premium": sl_premium,
        "target_premium": target_premium,
    }


def _maybe_close_open_trade(
    symbol: str,
    underlying: float,
    expiry: str,
    now_iso: str,
    option_rows: list[dict] | None = None,
) -> None:
    """
    Check open trade for SL/target hit and close if triggered.

    P0 fix: Underlying and premium checks are now both evaluated independently
    for non-FUT options. Previously the underlying block returned early before
    premium SL/target could fire on the same poll — meaning options that had
    decayed/exploded past their premium SL were not closed until the spot price
    itself crossed the underlying SL level. Both checks now run unless the
    underlying check definitively closes the trade (in which case return is
    correct and premium check is moot).
    """
    open_trade = get_open_paper_trade(symbol)
    if not open_trade:
        return

    option_type = open_trade.get("option_type")
    strike = float(open_trade.get("strike") or 0.0)
    side = open_trade.get("side") or "BUY"

    target_underlying = float(open_trade.get("target_underlying") or 0.0)
    sl_underlying = float(open_trade.get("sl_underlying") or 0.0)

    exit_premium = None
    if option_type != "FUT":
        exit_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows)
    else:
        exit_premium = underlying

    verdict_label = open_trade.get("verdict_label", "")
    from src.engine.paper_plan import is_bullish_verdict
    if option_type == "CE":
        is_bull = (side == "BUY")
    elif option_type == "PE":
        is_bull = (side == "SELL")
    elif option_type == "FUT":
        is_bull = (side == "BUY")
    else:
        is_bull = is_bullish_verdict(verdict_label)

    # Underlying-based exit — evaluated only when both levels are set.
    # For FUT: this is the only exit mechanism (exit_premium == underlying).
    # For options: only closes if underlying level is definitively hit;
    #              if not hit, fall through to premium check below.
    underlying_closed = False
    if target_underlying > 0 and sl_underlying > 0:
        if is_bull:
            if underlying >= target_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                underlying_closed = True
            elif underlying <= sl_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                underlying_closed = True
        else:
            if underlying <= target_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                underlying_closed = True
            elif underlying >= sl_underlying:
                close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                underlying_closed = True

    if underlying_closed:
        return

    # Premium-based exit for options — always evaluated if underlying did not close the trade.
    # This ensures options that have decayed/spiked past their premium SL/target are closed
    # even when spot price has not yet reached the underlying SL/target level.
    if option_type != "FUT" and exit_premium is not None:
        sl_premium = open_trade.get("sl_premium")
        target_premium = open_trade.get("target_premium")
        if sl_premium is not None and target_premium is not None:
            if side == "SELL":
                if exit_premium <= float(target_premium):
                    close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                    return
                elif exit_premium >= float(sl_premium):
                    close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                    return
            else:
                if exit_premium >= float(target_premium):
                    close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_TARGET", "target hit")
                    return
                elif exit_premium <= float(sl_premium):
                    close_paper_trade(open_trade["id"], now_iso, underlying, exit_premium, "CLOSED_SL", "stop loss hit")
                    return


def run_paper_trading(symbol: str, scan_context: dict, digest_id: str, intel: dict, ai_verdict=None) -> dict | None:
    """
    intel: structured dict from generate_intelligence_structured().
    Accepts legacy str for backward-compat (extension_bridge etc.).
    """
    if isinstance(intel, str):
        verdict, confidence = _parse_verdict_and_confidence(intel)
        intel = {"verdict_label": verdict, "confidence": confidence}

    now_iso = datetime.now(timezone.utc).isoformat()
    underlying = float((scan_context or {}).get("underlying") or 0.0)
    expiry = (scan_context or {}).get("expiry", "")
    verdict = intel.get("verdict_label", "")
    confidence = int(intel.get("confidence") or 0)

    if underlying <= 0:
        return None

    if not _is_market_open(symbol):
        log.debug("%s: paper-trading skipped — outside market hours", symbol)
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    option_rows = list((scan_context or {}).get("option_rows") or [])
    prev_open_trade = get_open_paper_trade(symbol)
    _maybe_close_open_trade(symbol, underlying, expiry, now_iso, option_rows)
    current_open_trade = get_open_paper_trade(symbol)

    if prev_open_trade and not current_open_trade:
        with get_conn() as conn:
            closed_trade = conn.execute("SELECT * FROM paper_trades WHERE id=?", (prev_open_trade["id"],)).fetchone()
            if closed_trade:
                closed_trade = dict(closed_trade)
                return {
                    "action": "CLOSED",
                    "trade": closed_trade,
                    "reason": f"Closed via exit logic: {closed_trade.get('reason') or 'SL/Target hit'} (P&L: \u20b9{closed_trade.get('pnl_rupees', 0.0):,.2f})"
                }

    if current_open_trade and _is_reversal_against_open_trade(current_open_trade, verdict, confidence):
        strike = float(current_open_trade.get("strike") or 0.0)
        option_type = str(current_open_trade.get("option_type") or "")
        exit_premium = _get_option_premium(symbol, expiry, strike, option_type, option_rows) if strike > 0 else None
        trade_id = current_open_trade["id"]
        close_paper_trade(
            trade_id, now_iso, underlying, exit_premium, "CLOSED_MANUAL",
            f"reversal: verdict={verdict} conf={confidence}",
        )
        current_open_trade = None
        with get_conn() as conn:
            closed_trade = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            if closed_trade:
                closed_trade = dict(closed_trade)
                return {
                    "action": "CLOSED",
                    "trade": closed_trade,
                    "reason": f"Closed on opposite reversal signal: verdict={verdict} (P&L: \u20b9{closed_trade.get('pnl_rupees', 0.0):,.2f})"
                }

    if current_open_trade:
        return {"action": "HELD", "trade": current_open_trade}

    ctx = {**(scan_context or {}), "symbol": symbol, "expiry": expiry, "option_rows": option_rows}

    decision = make_trade_decision(symbol, intel, ctx, ai_verdict=ai_verdict)
    if decision["status"] == "BLOCKED":
        log.info("%s: paper trade blocked by decision engine — %s", symbol, decision["reason"])
        return {"action": "BLOCKED_DECISION", "reason": decision["reason"]}

    risk_ok, risk_reason = check_risk_limits(symbol, decision.get("setup_type"))
    if not risk_ok:
        log.info("%s: paper trade blocked by risk engine — %s", symbol, risk_reason)
        return {"action": "BLOCKED_RISK", "reason": risk_reason}

    plan = _trade_plan_from_verdict(verdict, confidence, ctx)
    if not plan:
        return {"action": "BLOCKED_PLAN", "reason": "No valid trade plan (e.g. missing option premium/strikes)"}

    lots = DEFAULT_LOTS_PER_TRADE
    scores = decision.get("scores") or {}

    # ---------------------------------------------------------------------------
    # FIX #13: Signal dedup key — verdict text removed.
    #
    # Old key: {symbol}:{opt_type}:{strike}:{verdict}:{date}
    # Problem: LLM paraphrasing ('STRONG BULLISH' vs 'BULLISH BREAKOUT') on the
    # same underlying/strike produced two distinct keys, bypassing INSERT OR IGNORE
    # dedup and opening duplicate positions on the same signal.
    #
    # New key: {symbol}:{opt_type}:{strike}:{date}:paper
    # Dedup is now purely structural — same symbol + instrument + date = same signal.
    # ':paper' suffix namespaces away from live_trading keys which use ':live'.
    # ---------------------------------------------------------------------------
    today_date = datetime.now(IST).strftime("%Y%m%d")
    option_type_key = plan.get("option_type", "")
    strike_key = int(plan.get("strike") or 0)
    signal_key = f"{symbol}:{option_type_key}:{strike_key}:{today_date}:paper"

    trade_data = {
        "opened_at":             now_iso,
        "symbol":                symbol,
        "expiry":                expiry,
        "verdict_label":         plan["verdict_label"],
        "side":                  plan.get("side", "BUY"),
        "option_type":           plan["option_type"],
        "strike":                plan["strike"],
        "entry_underlying":      plan["entry_underlying"],
        "entry_premium":         plan.get("entry_premium"),
        "sl_underlying":         plan["sl_underlying"],
        "sl_premium":            plan.get("sl_premium"),
        "target_underlying":     plan["target_underlying"],
        "target_premium":        plan.get("target_premium"),
        "lots":                  lots,
        "status":                "OPEN",
        "reason":                f"auto | {decision['reason']}",
        "digest_id":             digest_id,
        "trade_status":          decision["status"],
        "setup_type":            decision["setup_type"],
        "decision_reason":       decision["reason"],
        "confidence_score":      scores.get("confidence"),
        "entry_quality_score":   scores.get("entry_quality"),
        "trend_alignment_score": scores.get("trend_alignment"),
        "regime_score":          scores.get("regime_score"),
        "signal_key":            signal_key,
    }

    # P1 fix: INSERT OR IGNORE silently drops duplicate rows and still returns
    # lastrowid=0 (or the prior rowid). Check lastrowid explicitly so the caller
    # can distinguish a real insert from a dedup-blocked one.
    inserted_id = insert_paper_trade(trade_data)
    if not inserted_id:
        log.warning("%s: paper trade INSERT skipped — duplicate signal_key=%s", symbol, signal_key)
        return {
            "action":     "DEDUP_SKIPPED",
            "reason":     f"Duplicate signal already recorded today (signal_key={signal_key})",
            "signal_key": signal_key,
        }

    return {
        "action":     "EXECUTED",
        "trade":      trade_data,
        "setup_type": decision["setup_type"],
        "reason":     decision["reason"],
        "lots":       lots,
    }


def _parse_llm_sl(exit_advice: str, fallback: float | None) -> float | None:
    import re
    if not exit_advice:
        return fallback
    m = re.search(r'SL\s+(?:at\s+)?[₹]?([\d.]+)', exit_advice, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return fallback


def run_timeframe_strategy(symbol: str, scan_context: dict, digest_id: str, intel: dict, ai_verdict=None) -> dict | None:
    """Run secondary timeframe trading strategy (3h Entry / 1h Exit) based on completed candle crossovers."""
    if not _is_market_open(symbol):
        return {"action": "SKIPPED_MARKET_CLOSED", "reason": "Outside market hours"}

    ctx = scan_context or {}
    underlying = float(ctx.get("underlying") or 0.0)
    if underlying <= 0:
        return None

    if get_symbol_class(symbol) == "MCX_COMMODITY":
        scan_freq = get_scan_frequency_mcx()
    else:
        scan_freq = get_scan_frequency_nse()
    fetched_at = ctx.get("fetched_at") or datetime.now(timezone.utc).isoformat()
    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        today_scans = get_today_scan_count(symbol, fetched_at)
        current_scan_idx = today_scans + 1
        if current_scan_idx % scans_needed != 0:
            log.info("%s: Timeframe strategy skipped — scan %d is not a 1-hour boundary (run every %d scans for %d-min scan freq)",
                     symbol, current_scan_idx, scans_needed, scan_freq)
            return {"action": "SKIPPED_TIMEFRAME_BOUNDARY", "reason": f"Skipped scan {current_scan_idx} (run every {scans_needed} scans)"}

    open_trades_before = get_open_timeframe_trades(symbol)

    chart_indicators = ctx.get("chart_indicators") or {}
    tf_data = chart_indicators
    if not any(k in chart_indicators for k in ("1h", "3h")):
        tf_data = next(iter(chart_indicators.values()), {}) if chart_indicators else {}

    pay_3h = tf_data.get("3h")
    pay_1h = tf_data.get("1h")
    if not pay_3h or not pay_1h:
        log.warning("%s: Timeframe strategy skipped — missing 3h/1h chart data", symbol)
        return {"action": "SKIPPED_NO_CHART_DATA", "reason": "Missing 3h/1h chart data"}

    ohlc_3h = pay_3h.get("ohlc")
    prev_3h = pay_3h.get("prev_ohlc") or pay_3h.get("last_closed_ohlc")
    ohlc_1h = pay_1h.get("ohlc")
    prev_1h = pay_1h.get("prev_ohlc") or pay_1h.get("last_closed_ohlc")

    if not ohlc_3h or not prev_3h or not ohlc_1h or not prev_1h:
        log.warning("%s: Timeframe strategy skipped — incomplete 3h/1h candle data", symbol)
        return {"action": "SKIPPED_INCOMPLETE_CANDLES", "reason": "Incomplete 3h/1h candle data"}

    c_3h_close = float(ohlc_3h["close"])
    p_3h_high = float(prev_3h["high"])
    p_3h_low = float(prev_3h["low"])

    c_1h_close = float(ohlc_1h["close"])
    p_1h_high = float(prev_1h["high"])
    p_1h_low = float(prev_1h["low"])

    current_ce = ctx.get("total_ce_oi")
    current_pe = ctx.get("total_pe_oi")

    if current_ce is None or current_pe is None:
        log.warning("%s: Timeframe strategy skipped — missing total OI data", symbol)
        return {"action": "SKIPPED_NO_OI", "reason": "Missing total OI data"}

    if scan_freq in (15, 30):
        scans_needed = 60 // scan_freq
        older = get_scan_summary_n_scans_ago(symbol, scans_needed)
        if not older:
            log.warning("%s: Timeframe strategy skipped — insufficient scan history (%d scans ago)", symbol, scans_needed)
            return {"action": "SKIPPED_INSUFFICIENT_HISTORY", "reason": f"Insufficient scan history ({scans_needed} scans ago)"}
    else:
        older = get_scan_summary_at_least_1h_old(symbol, fetched_at)
        if not older:
            log.warning("%s: Timeframe strategy skipped — insufficient 1h scan history", symbol)
            return {"action": "SKIPPED_INSUFFICIENT_HISTORY", "reason": "Insufficient 1h scan history"}

    prev_ce = older["total_ce_oi"]
    prev_pe = older["total_pe_oi"]
    ce_diff = current_ce - prev_ce
    pe_diff = current_pe - prev_pe

    # Add a minimum threshold in % terms (Item 2)
    min_diff_pct = TIMEFRAME_OI_MIN_DIFF_PCT
    long_oi_support = (pe_diff - ce_diff) > (prev_pe * min_diff_pct)
    short_oi_support = (ce_diff - pe_diff) > (prev_ce * min_diff_pct)

    # Calculate breakout buffer (Strictly 0.1% of underlying CMP, no ATR dependency)
    breakout_buffer = underlying * 0.001

    log.info("%s: Timeframe strategy | 3h close=%g p3h_high=%g p3h_low=%g (buffer=%g) | 1h close=%g p1h_high=%g p1h_low=%g | pe_diff=%g ce_diff=%g",
             symbol, c_3h_close, p_3h_high, p_3h_low, breakout_buffer, c_1h_close, p_1h_high, p_1h_low, pe_diff, ce_diff)

    # ── 1. EXIT LOGIC ──
    # Check exits first
    open_trades = get_open_timeframe_trades(symbol)
    now_iso = datetime.now(timezone.utc).isoformat()
    bar_end_1h = pay_1h.get("bar_end_utc")

    for trade in open_trades:
        # Get current premium for options
        exit_premium = None
        if trade["option_type"] in ("CE", "PE"):
            exit_premium = _get_option_premium(symbol, ctx.get("expiry", ""), trade["strike"], trade["option_type"], ctx.get("option_rows"))

        # Update max favorable R in database
        r_current = 0.0
        if trade["option_type"] in ("CE", "PE"):
            entry_prem = float(trade.get("entry_premium") or 0.0)
            sl_prem = float(trade.get("sl_premium") or 0.0)
            side = trade.get("side") or "BUY"
            if side == "SELL":
                if sl_prem > entry_prem and exit_premium:
                    r_current = (entry_prem - float(exit_premium)) / (sl_prem - entry_prem)
            else:
                if entry_prem > sl_prem and exit_premium:
                    r_current = (float(exit_premium) - entry_prem) / (entry_prem - sl_prem)
        else:
            # Futures
            entry_und = float(trade.get("entry_underlying") or 0.0)
            sl_und = float(trade.get("sl_underlying") or 0.0)
            if trade.get("side", "BUY") == "BUY":
                if entry_und > sl_und:
                    r_current = (underlying - entry_und) / (entry_und - sl_und)
            else:
                if sl_und > entry_und:
                    r_current = (entry_und - underlying) / (sl_und - entry_und)
        
        max_fav = max(float(trade.get("max_favorable_r") or 0.0), r_current)
        with get_conn() as conn:
            conn.execute("UPDATE paper_trades SET max_favorable_r=? WHERE id=?", (max_fav, trade["id"]))

        # LLM reversal exit check (Gate D)
        if ai_verdict is not None:
            if isinstance(ai_verdict, dict):
                bias = ai_verdict.get("bias", "NEUTRAL")
                confidence = ai_verdict.get("confidence", 0)
            else:
                bias = getattr(ai_verdict, "bias", "NEUTRAL")
                confidence = getattr(ai_verdict, "confidence", 0)
            
            if confidence >= 70:
                if (trade["verdict_label"] == "LONG" and bias == "BEARISH") or \
                   (trade["verdict_label"] == "SHORT" and bias == "BULLISH"):
                    close_paper_trade(
                        trade["id"],
                        now_iso,
                        underlying,
                        exit_premium if trade["option_type"] in ("CE", "PE") else underlying,
                        "LLM_REVERSAL",
                        f"LLM sentiment reversal: bias {bias} opposes {trade['verdict_label']} trade with confidence {confidence}%"
                    )
                    log.info("%s: Closed open TIMEFRAME trade (id=%d) due to LLM sentiment reversal (%s, confidence=%d%%)", 
                             symbol, trade["id"], bias, confidence)
                    continue

        # 2a. Options SL hit (Item 2)
        if trade["option_type"] in ("CE", "PE"):
            sl_prem = trade.get("sl_premium")
            side = trade.get("side") or "BUY"
            is_sl_hit = False
            if exit_premium and sl_prem:
                if side == "SELL":
                    is_sl_hit = (exit_premium >= float(sl_prem))
                else:
                    is_sl_hit = (exit_premium <= float(sl_prem))

            # Check underlying-based SL if set (e.g. by LLM override)
            sl_und = trade.get("sl_underlying")
            is_und_sl_hit = False
            if sl_und:
                sl_und_val = float(sl_und)
                if trade["verdict_label"] == "LONG" and underlying <= sl_und_val:
                    is_und_sl_hit = True
                elif trade["verdict_label"] == "SHORT" and underlying >= sl_und_val:
                    is_und_sl_hit = True

            if is_sl_hit or is_und_sl_hit:
                reason = f"Options SL hit: premium {exit_premium} vs SL {sl_prem} ({side})" if is_sl_hit else f"Options underlying SL hit: spot {underlying} vs SL {sl_und_val}"
                close_paper_trade(
                    trade["id"],
                    now_iso,
                    underlying,
                    exit_premium,
                    "CLOSED_SL",
                    reason,
                )
                log.info("%s: Closed open TIMEFRAME trade (id=%d) on Options SL", symbol, trade["id"])
                continue
        # 2b. Futures SL hit (Item 2)
        else:
            sl_und = trade.get("sl_underlying")
            if sl_und:
                sl_und_val = float(sl_und)
                if trade["verdict_label"] == "LONG" and underlying <= sl_und_val:
                    close_paper_trade(
                        trade["id"],
                        now_iso,
                        underlying,
                        underlying,
                        "CLOSED_SL",
                        f"Futures SL hit: underlying {underlying} <= SL {sl_und_val}",
                    )
                    log.info("%s: Closed open TIMEFRAME trade (id=%d) on Futures SL", symbol, trade["id"])
                    continue
                elif trade["verdict_label"] == "SHORT" and underlying >= sl_und_val:
                    close_paper_trade(
                        trade["id"],
                        now_iso,
                        underlying,
                        underlying,
                        "CLOSED_SL",
                        f"Futures SL hit: underlying {underlying} >= SL {sl_und_val}",
                    )
                    log.info("%s: Closed open TIMEFRAME trade (id=%d) on Futures SL", symbol, trade["id"])
                    continue

        # Check Dead Trade exit (Item 3)
        opened_dt = datetime.fromisoformat(trade["opened_at"].replace("Z", "+00:00"))
        if bar_end_1h:
            bar_end_dt = datetime.fromisoformat(bar_end_1h.replace("Z", "+00:00"))
            time_diff = (bar_end_dt - opened_dt).total_seconds()
            if time_diff >= 3.0 * 3600 - 60: # 3 completed 1H candles
                if max_fav < 0.5:
                    close_paper_trade(
                        trade["id"],
                        now_iso,
                        underlying,
                        exit_premium if trade["option_type"] in ("CE", "PE") else underlying,
                        "Dead Trade",
                        f"Dead trade exit: 3 hours passed, max favorable R {max_fav:.2f} < 0.5",
                    )
                    log.info("%s: Closed open TIMEFRAME trade (id=%d) on dead-trade exit", symbol, trade["id"])
                    continue

        # Exit Long trade (Crossover) (Item 1)
        if trade["option_type"] in ("CE", "FUT") and trade["verdict_label"] == "LONG":
            if bar_end_1h and trade["opened_at"] < bar_end_1h:
                if c_1h_close < p_1h_low:
                    crossover_size = p_1h_low - c_1h_close
                    # Exit immediately if move is large (2x breakout_buffer), otherwise require OI support
                    if crossover_size > 2 * breakout_buffer:
                        close_paper_trade(
                            trade["id"],
                            now_iso,
                            underlying,
                            exit_premium if trade["option_type"] == "CE" else underlying,
                            "TF-1H-Cross",
                            f"timeframe exit | Large reversal move ({crossover_size:.2f} > 2x buffer {2 * breakout_buffer:.2f})",
                        )
                        log.info("%s: Closed open TIMEFRAME LONG trade (id=%d) on large 1H close reversal", symbol, trade["id"])
                    elif short_oi_support:
                        close_paper_trade(
                            trade["id"],
                            now_iso,
                            underlying,
                            exit_premium if trade["option_type"] == "CE" else underlying,
                            "TF-1H-Cross",
                            f"timeframe exit | 1H close {c_1h_close:.2f} < p1H_low {p_1h_low:.2f} + Short OI bias",
                        )
                        log.info("%s: Closed open TIMEFRAME LONG trade (id=%d) on 1H close crossover with OI bias", symbol, trade["id"])

        # Exit Short trade (Crossover) (Item 1)
        elif trade["option_type"] in ("PE", "FUT") and trade["verdict_label"] == "SHORT":
            if bar_end_1h and trade["opened_at"] < bar_end_1h:
                if c_1h_close > p_1h_high:
                    crossover_size = c_1h_close - p_1h_high
                    # Exit immediately if move is large (2x breakout_buffer), otherwise require OI support
                    if crossover_size > 2 * breakout_buffer:
                        close_paper_trade(
                            trade["id"],
                            now_iso,
                            underlying,
                            exit_premium if trade["option_type"] == "PE" else underlying,
                            "TF-1H-Cross",
                            f"timeframe exit | Large reversal move ({crossover_size:.2f} > 2x buffer {2 * breakout_buffer:.2f})",
                        )
                        log.info("%s: Closed open TIMEFRAME SHORT trade (id=%d) on large 1H close reversal", symbol, trade["id"])
                    elif long_oi_support:
                        close_paper_trade(
                            trade["id"],
                            now_iso,
                            underlying,
                            exit_premium if trade["option_type"] == "PE" else underlying,
                            "TF-1H-Cross",
                            f"timeframe exit | 1H close {c_1h_close:.2f} > p1H_high {p_1h_high:.2f} + Long OI bias",
                        )
                        log.info("%s: Closed open TIMEFRAME SHORT trade (id=%d) on 1H close crossover with OI bias", symbol, trade["id"])

    open_trades_after = get_open_timeframe_trades(symbol)
    closed_trade_id = None
    for pt in open_trades_before:
        if pt["id"] not in [ct["id"] for ct in open_trades_after]:
            closed_trade_id = pt["id"]
            break

    if closed_trade_id:
        with get_conn() as conn:
            closed = conn.execute("SELECT * FROM paper_trades WHERE id=?", (closed_trade_id,)).fetchone()
            if closed:
                closed = dict(closed)
                return {
                    "action": "CLOSED",
                    "trade": closed,
                    "reason": f"Timeframe exit: {closed.get('reason')} (P&L: ₹{closed.get('pnl_rupees', 0.0):,.2f})"
                }

    # ── 2. ENTRY LOGIC ──
    # Check if we already opened a trade in this 3h bar window for same signal/direction (Item 5)
    bar_end_3h = pay_3h.get("bar_end_utc")
    if not bar_end_3h:
        return None

    # Check direction
    is_long_trigger = c_3h_close > p_3h_high + breakout_buffer and long_oi_support
    is_short_trigger = c_3h_close < p_3h_low - breakout_buffer and short_oi_support

    if not is_long_trigger and not is_short_trigger:
        return None

    direction = "LONG" if is_long_trigger else "SHORT"

    # LLM entry gates (Gate A and Gate B)
    if ai_verdict is not None:
        if isinstance(ai_verdict, dict):
            bias = ai_verdict.get("bias", "NEUTRAL")
            confidence = ai_verdict.get("confidence", 0)
            risk_rating = ai_verdict.get("risk_rating", "LOW")
        else:
            bias = getattr(ai_verdict, "bias", "NEUTRAL")
            confidence = getattr(ai_verdict, "confidence", 0)
            risk_rating = getattr(ai_verdict, "risk_rating", "LOW")

        # Gate A: Bias alignment check
        if confidence >= 65:
            if (direction == "LONG" and bias == "BEARISH") or (direction == "SHORT" and bias == "BULLISH"):
                log.info("%s: Timeframe %s entry blocked by LLM bias alignment (bias=%s, confidence=%d%%)", symbol, direction, bias, confidence)
                return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry blocked: LLM bias alignment (bias={bias}, confidence={confidence}%)"}

        # Gate B: Risk rating check
        if risk_rating == "HIGH":
            log.info("%s: Timeframe %s entry blocked by LLM risk rating (risk_rating=HIGH)", symbol, direction)
            return {"action": "BLOCKED_PLAN", "reason": "Timeframe entry blocked: LLM risk rating is HIGH"}

    signal_key = f"{symbol}:TIMEFRAME:3H:{direction}:{bar_end_3h}"

    # Check unique signal_key
    with get_conn() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM paper_trades WHERE signal_key=?",
            (signal_key,)
        ).fetchone()["c"]
        if cnt > 0:
            log.debug("%s: Timeframe strategy entry skipped — duplicate signal key %s", symbol, signal_key)
            return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry skipped: duplicate signal key {signal_key}"}

    # Check risk limits (with setup_type='TIMEFRAME')
    risk_ok, risk_reason = check_risk_limits(symbol, "TIMEFRAME")
    if not risk_ok:
        log.info("%s: Timeframe trade blocked by risk engine — %s", symbol, risk_reason)
        return {"action": "BLOCKED_RISK", "reason": f"Timeframe entry skipped: {risk_reason}"}

    # Pyramid validation (Item 9)
    # Re-fetch open trades to count
    open_trades = get_open_timeframe_trades(symbol)
    if len(open_trades) >= 3:
        log.info("%s: Timeframe entry skipped — maximum pyramid level (3) reached", symbol)
        return {"action": "BLOCKED_PLAN", "reason": "Timeframe entry skipped: maximum pyramid level (3) reached"}

    if len(open_trades) > 0:
        # Must be in the same direction
        if any(t["verdict_label"] != direction for t in open_trades):
            log.info("%s: Timeframe entry skipped — cannot pyramid in opposite direction", symbol)
            return {"action": "BLOCKED_PLAN", "reason": "Timeframe entry skipped: cannot pyramid in opposite direction"}

        # At least one existing trade must be profitable
        any_profitable = False
        for t in open_trades:
            if t["option_type"] in ("CE", "PE"):
                t_exit = _get_option_premium(symbol, ctx.get("expiry", ""), t["strike"], t["option_type"], ctx.get("option_rows"))
                t_side = t.get("side") or "BUY"
                if t_exit:
                    if t_side == "SELL":
                        is_profitable = t_exit < float(t.get("entry_premium") or 0.0)
                    else:
                        is_profitable = t_exit > float(t.get("entry_premium") or 0.0)
                    if is_profitable:
                        any_profitable = True
                        break
            else:
                # Futures
                if t["verdict_label"] == "LONG" and underlying > float(t["entry_underlying"]):
                    any_profitable = True
                    break
                elif t["verdict_label"] == "SHORT" and underlying < float(t["entry_underlying"]):
                    any_profitable = True
                    break
        if not any_profitable:
            log.info("%s: Timeframe entry skipped — no profitable open trades to pyramid", symbol)
            return {"action": "BLOCKED_PLAN", "reason": "Timeframe entry skipped: no profitable open trades to pyramid"}

    # Sizing scaling
    pyramid_level = len(open_trades) + 1
    lot_multiplier = 1.0
    if pyramid_level == 2:
        lot_multiplier = 0.75
    elif pyramid_level == 3:
        lot_multiplier = 0.50

    lots = max(1, round(DEFAULT_LOTS_PER_TRADE * lot_multiplier))

    expiry = ctx.get("expiry", "")
    step = float(get_strike_step(symbol) or 1)
    atm = ctx.get("atm_strike") or round(underlying / step) * step
    is_natgas = "NATURALGAS" in symbol

    if direction == "LONG":
        if is_natgas:
            opt_type = "FUT"
            strike = atm
            entry_premium = underlying
            sl_premium = None
            sl_underlying = float(ohlc_3h["low"])
            if underlying - sl_underlying < underlying * 0.003:
                sl_underlying = underlying - underlying * 0.003
        else:
            opt_type = "CE"
            strike = atm - 4 * step
            entry_premium = _get_option_premium(symbol, expiry, strike, "CE", ctx.get("option_rows"))
            if not entry_premium or entry_premium <= 0:
                log.warning("%s: timeframe long entry skipped — option premium unavailable for CE strike %g", symbol, strike)
                return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry skipped: option premium unavailable for CE strike {strike}"}
            sl_premium = entry_premium * 0.75
            sl_underlying = None

        if ai_verdict is not None:
            if isinstance(ai_verdict, dict):
                exit_advice = ai_verdict.get("exit_advice", "")
            else:
                exit_advice = getattr(ai_verdict, "exit_advice", "")
            sl_underlying = _parse_llm_sl(exit_advice, sl_underlying)

        trade_data = {
            "opened_at": now_iso,
            "symbol": symbol,
            "expiry": expiry,
            "verdict_label": "LONG",
            "side": "BUY",
            "option_type": opt_type,
            "strike": strike,
            "entry_underlying": underlying,
            "entry_premium": entry_premium,
            "sl_underlying": sl_underlying,
            "sl_premium": sl_premium,
            "lots": lots,
            "status": "OPEN",
            "reason": f"timeframe entry | 3H close {c_3h_close:.2f} > p3H_high {p_3h_high:.2f} | level {pyramid_level}",
            "digest_id": digest_id,
            "trade_status": "TRIGGERED_TIMEFRAME",
            "setup_type": "TIMEFRAME",
            "signal_key": signal_key,
            "pyramid_level": pyramid_level,
            "max_favorable_r": 0.0,
        }
        insert_paper_trade(trade_data)
        log.info("%s: Timeframe Strategy LONG entry triggered! Strike %g Premium %g Lots %d (Level %d)", symbol, strike, entry_premium, lots, pyramid_level)
        return {
            "action": "EXECUTED",
            "trade": trade_data,
            "setup_type": "TIMEFRAME",
            "reason": f"timeframe entry | level {pyramid_level}",
            "lots": lots
        }

    elif direction == "SHORT":
        if is_natgas:
            opt_type = "FUT"
            strike = atm
            entry_premium = underlying
            sl_premium = None
            sl_underlying = float(ohlc_3h["high"])
            if sl_underlying - underlying < underlying * 0.003:
                sl_underlying = underlying + underlying * 0.003
        else:
            opt_type = "PE"
            strike = atm + 4 * step
            entry_premium = _get_option_premium(symbol, expiry, strike, "PE", ctx.get("option_rows"))
            if not entry_premium or entry_premium <= 0:
                log.warning("%s: timeframe short entry skipped — option premium unavailable for PE strike %g", symbol, strike)
                return {"action": "BLOCKED_PLAN", "reason": f"Timeframe entry skipped: option premium unavailable for PE strike {strike}"}
            sl_premium = entry_premium * 1.25
            sl_underlying = None

        if ai_verdict is not None:
            if isinstance(ai_verdict, dict):
                exit_advice = ai_verdict.get("exit_advice", "")
            else:
                exit_advice = getattr(ai_verdict, "exit_advice", "")
            sl_underlying = _parse_llm_sl(exit_advice, sl_underlying)

        trade_data = {
            "opened_at": now_iso,
            "symbol": symbol,
            "expiry": expiry,
            "verdict_label": "SHORT",
            "side": "BUY",
            "option_type": opt_type,
            "strike": strike,
            "entry_underlying": underlying,
            "entry_premium": entry_premium,
            "sl_underlying": sl_underlying,
            "sl_premium": sl_premium,
            "lots": lots,
            "status": "OPEN",
            "reason": f"timeframe entry | 3H close {c_3h_close:.2f} < p3H_low {p_3h_low:.2f} | level {pyramid_level}",
            "digest_id": digest_id,
            "trade_status": "TRIGGERED_TIMEFRAME",
            "setup_type": "TIMEFRAME",
            "signal_key": signal_key,
            "pyramid_level": pyramid_level,
            "max_favorable_r": 0.0,
        }
        insert_paper_trade(trade_data)
        log.info("%s: Timeframe Strategy SHORT entry triggered! Strike %g Premium %g Lots %d (Level %d)", symbol, strike, entry_premium, lots, pyramid_level)
        return {
            "action": "EXECUTED",
            "trade": trade_data,
            "setup_type": "TIMEFRAME",
            "reason": f"timeframe entry | level {pyramid_level}",
            "lots": lots
        }

    return None
