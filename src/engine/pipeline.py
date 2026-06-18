"""
Data Pipeline Orchestrator v2.9
fetch → detect → dedup → digest → alert

Fixes (v2.9):
  - B5: Track when underlying fell back to prev_price (is_fallback=True).
        Pass to save_scan_summary so regime_detector filters these rows out.
  - #9:  AI CLOSE_EARLY now fetches current LTP from option snapshots before
        calling close_paper_trade(); falls back to entry_premium only when
        LTP is unavailable, with a warning log for visibility.
  - #13: Signal dedup key in live_trading no longer includes verdict text;
        pipeline ensures llm_verdict is passed through so live engine uses
        the structured object directly (see live_trading._parse_verdict_and_confidence).
"""
import logging
from datetime import datetime, timezone

from src.fetchers.router import fetch_option_chain
from src.fetchers.chart_fetcher import get_chart_fetcher
from src.models.schema import (
    insert_snapshots,
    insert_underlying_price,
    insert_alert,
    mark_telegram_sent,
    get_previous_underlying,
)
from src.engine.anomaly_detector import detect_anomalies
from src.engine.intelligence import generate_intelligence_structured
from src.engine.paper_trading import run_paper_trading, run_timeframe_strategy
# from src.engine.live_trading import run_live_trading, run_live_timeframe_strategy
from src.engine.scan_summary import save_scan_summary
from src.alerts.dedup import is_duplicate, record_alert, should_send_zero_signal
from src.alerts.digest import build_digest_wrapper as build_digest
from src.alerts.telegram_dispatcher import send_text
from config.settings import WATCH_SYMBOLS, get_symbol_thresholds

log = logging.getLogger(__name__)


def run_pipeline(symbols: list[str] | None = None, force: bool = False) -> None:
    """
    Run the full pipeline for each symbol.

    Args:
        symbols: Override watch list. Defaults to WATCH_SYMBOLS.
        force:   Skip market-hours guard (used by --now CLI flag).
    """
    symbols = symbols or WATCH_SYMBOLS
    fetched_at = datetime.now(timezone.utc).isoformat()
    log.info("Pipeline run started | %s | symbols: %s | force=%s", fetched_at, symbols, force)
    
    # B7: Sync manual Kite direct positions to SQLite for AI Exit Advisor monitoring
    # TODO: sync_direct_kite_positions() not yet implemented
    # try:
    #     from src.engine.live_trading import sync_direct_kite_positions
    #     sync_direct_kite_positions()
    # except Exception:
    #     log.exception("Direct Kite position synchronization failed")

    for symbol in symbols:
        try:
            _process_symbol(symbol, fetched_at)
        except Exception:
            log.exception("Unhandled pipeline error for %s — continuing with next symbol", symbol)
    log.info("Pipeline run complete | %s", fetched_at)


def _get_current_option_ltp(
    symbol: str,
    expiry: str,
    strike: float | None,
    option_type: str | None,
    option_rows: list[dict] | None,
) -> float | None:
    """
    Resolve the current LTP for an option position from the freshly-fetched
    option chain rows.  Returns None when the data is unavailable so callers
    can fall back gracefully.
    """
    if not strike or not option_type:
        return None
    for row in option_rows or []:
        try:
            if (
                abs(float(row.get("strike") or 0) - float(strike)) < 0.01
                and str(row.get("option_type") or "").upper() == str(option_type).upper()
            ):
                ltp = float(row.get("ltp") or 0.0)
                return ltp if ltp > 0 else None
        except Exception:
            continue
    return None


def _process_symbol(symbol: str, fetched_at: str) -> None:
    log.info("Processing %s ...", symbol)

    oc_data = fetch_option_chain(symbol)
    if not oc_data:
        log.error("No data for %s — skipping", symbol)
        try:
            send_text(f"⚠️ **NSEBOT ALERT**: ALL data fetchers failed for symbol `{symbol}` at scan interval. Price tracking and strategy execution skipped.")
        except Exception:
            log.exception("Failed to send fetch-failure Telegram alert for %s", symbol)
        return

    underlying = oc_data["underlying_price"]
    expiry     = oc_data["expiry"]
    source     = oc_data.get("source", "unknown")
    prev_row   = get_previous_underlying(symbol)
    prev_price = prev_row["price"] if prev_row else None

    # B5: flag when we're using a stale fallback price so regime_detector can ignore the row
    is_fallback = False
    if underlying is None:
        underlying = prev_price or 0.0
        oc_data["underlying_price"] = underlying
        is_fallback = True
        log.warning("%s: underlying price is None, falling back to prev_price: %s", symbol, underlying)

    # 1a. Fetch chart data server-side (Chrome-free)
    try:
        chart_data = get_chart_fetcher().fetch(symbol, reference_price=underlying) or {}
        oc_data["chart_indicators"] = chart_data
        if chart_data:
            log.debug("%s: chart_indicators injected from chart_fetcher", symbol)
        else:
            log.warning("%s: chart_fetcher returned empty chart dict — continuing without chart", symbol)
    except Exception:
        oc_data["chart_indicators"] = {}
        log.exception("%s: chart_fetcher crashed — continuing without chart data", symbol)

    # 1b. Detect anomalies
    symbol_thresholds = get_symbol_thresholds(symbol)
    alerts, scan_context = detect_anomalies(
        oc_data,
        fetched_at,
        chart_indicators=oc_data.get("chart_indicators"),
        override_thresholds=symbol_thresholds,
    )
    scan_context["option_rows"] = list(oc_data.get("strikes") or [])
    log.info("%s: %d anomalies detected", symbol, len(alerts))

    # 2. Dedup filter
    new_alerts = [a for a in alerts if not is_duplicate(a)]
    dedup_suppressed = max(0, len(alerts) - len(new_alerts))
    if dedup_suppressed:
        log.info(
            "%s: detected=%d | new=%d | dedup_suppressed=%d",
            symbol, len(alerts), len(new_alerts), dedup_suppressed,
        )

    # 3. Build digest
    intel = generate_intelligence_structured(symbol, new_alerts, scan_context=scan_context)
    intel_text = intel["telegram_text"]

    # 3a. Fetch news data for AI context
    news_data = None
    try:
        from src.fetchers.news_fetcher import fetch_news
        news_data = fetch_news(symbol)
        if news_data and news_data.get("count_24h", 0) > 0:
            log.info("%s: news fetched — %d articles, direction: %s",
                     symbol, news_data["count_24h"], news_data.get("current_news_direction"))
    except Exception:
        log.debug("%s: news fetch unavailable", symbol)

    # 3b. Check for open paper trade (context for AI)
    open_trade = None
    try:
        from src.models.schema import get_open_paper_trade
        open_trade = get_open_paper_trade(symbol)
        if open_trade:
            open_trade = dict(open_trade)
    except Exception:
        log.debug("%s: could not fetch open trade for AI context", symbol)

    # 3c. AI Enrichment (Gemini — Deep Context)
    from src.engine.llm_enrichment import get_llm_verdict
    from config.settings import DISABLE_LLM_ENRICHMENT
    llm_verdict = None
    
    if DISABLE_LLM_ENRICHMENT:
        log.info("%s: LLM enrichment disabled (DISABLE_LLM_ENRICHMENT=true)", symbol)
    else:
        try:
            llm_verdict = get_llm_verdict(
                symbol, intel, scan_context,
                alerts=new_alerts,
                news_data=news_data,
                open_trade=open_trade,
            )
            if llm_verdict:
                log.info("%s: AI verdict — %s (%d%%) risk=%s | Strategy: %s",
                         symbol, llm_verdict.bias, llm_verdict.confidence,
                         llm_verdict.risk_rating, llm_verdict.strategy)
                intel_text += f"\n\n🧠 *AI Verdict* ({llm_verdict.bias}, {llm_verdict.confidence}%)\n"
                intel_text += f"Strategy: {llm_verdict.strategy}\n"
                intel_text += f"Target: {llm_verdict.strike_selection}\n"
                intel_text += f"Risk: {llm_verdict.risk_rating}\n"
                if llm_verdict.news_synthesis and llm_verdict.news_synthesis != "No news data":
                    intel_text += f"📰 {llm_verdict.news_synthesis}\n"
                if llm_verdict.exit_advice:
                    intel_text += f"🚶 Exit: {llm_verdict.exit_advice}\n"
                intel_text += f"_{llm_verdict.reasoning}_\n"
        except Exception:
            log.exception("%s: AI enrichment failed gracefully", symbol)

    # 3d. AI Exit Advisor — evaluate open paper trades
    try:
        from config.runtime_config import load_runtime_config
        rconf = load_runtime_config()
        ai_exit_advisor_enabled = rconf.get("live_ai_exit_advisor_enabled", False)
        if ai_exit_advisor_enabled and open_trade:
            from src.engine.llm_enrichment import get_exit_advice
            exit_advice = get_exit_advice(symbol, open_trade, scan_context, news_data)
            if exit_advice:
                log.info("%s: AI exit advice — %s (urgency=%s): %s",
                         symbol, exit_advice.action, exit_advice.urgency, exit_advice.reasoning)
                if exit_advice.action == "TRAIL_SL" and exit_advice.new_sl_premium is not None:
                    from src.models.schema import get_conn
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE paper_trades SET sl_premium=? WHERE id=? AND status='OPEN'",
                            (exit_advice.new_sl_premium, open_trade["id"]),
                        )
                    log.info("%s: AI trailed SL to %.2f", symbol, exit_advice.new_sl_premium)
                    intel_text += f"\n🤖 *AI Trail*: SL moved to ₹{exit_advice.new_sl_premium:.2f} — {exit_advice.reasoning}\n"
                elif exit_advice.action == "CLOSE_EARLY" and exit_advice.urgency == "HIGH":
                    # FIX #9: use real current LTP, not entry_premium, to avoid P&L = 0
                    from src.models.schema import close_paper_trade
                    option_rows = scan_context.get("option_rows") or []
                    current_ltp = _get_current_option_ltp(
                        symbol,
                        open_trade.get("expiry"),
                        open_trade.get("strike"),
                        open_trade.get("option_type"),
                        option_rows,
                    )
                    if current_ltp is not None:
                        exit_premium = current_ltp
                    else:
                        # Genuine fallback: LTP unavailable (e.g. expiry day, illiquid strike)
                        exit_premium = float(open_trade.get("entry_premium") or 0.0)
                        log.warning(
                            "%s: AI CLOSE_EARLY — current LTP unavailable for "
                            "strike=%.2f %s; falling back to entry_premium=%.2f "
                            "(P&L will be zero — check snapshot coverage)",
                            symbol,
                            open_trade.get("strike", 0),
                            open_trade.get("option_type", ""),
                            exit_premium,
                        )
                    close_paper_trade(
                        open_trade["id"],
                        datetime.now(timezone.utc).isoformat(),
                        scan_context.get("underlying", 0),
                        exit_premium,
                        "AI_CLOSE_EARLY",
                        f"AI exit: {exit_advice.reasoning}",
                    )
                    log.info(
                        "%s: AI closed trade early at LTP=%.2f — %s",
                        symbol, exit_premium, exit_advice.reasoning,
                    )
                    intel_text += f"\n🤖 *AI Close* @ ₹{exit_premium:.2f}: {exit_advice.reasoning}\n"
    except Exception:
        log.debug("%s: AI exit advisor failed gracefully", symbol)


    import uuid
    digest_id = str(uuid.uuid4())[:8]
    paper_trade_report = None
    try:
        pt_report = run_paper_trading(symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict)
        tf_report = run_timeframe_strategy(symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict)
        if pt_report and pt_report.get("action") in ("EXECUTED", "CLOSED"):
            paper_trade_report = pt_report
        elif tf_report and tf_report.get("action") in ("EXECUTED", "CLOSED"):
            paper_trade_report = tf_report
        else:
            paper_trade_report = pt_report or tf_report
    except Exception:
        log.exception("%s: paper-trading engine failed", symbol)

    live_trade_report = None
    # TODO: Live trading functions not yet implemented
    # try:
    #     lt_report = run_live_trading(symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict)
    #     lt_tf_report = run_live_timeframe_strategy(symbol, scan_context, digest_id, intel)
    #     if lt_report and lt_report.get("action") in ("EXECUTED", "CLOSED"):
    #         live_trade_report = lt_report
    #     elif lt_tf_report and lt_tf_report.get("action") in ("EXECUTED", "CLOSED"):
    #         live_trade_report = lt_tf_report
    #     else:
    #         live_trade_report = lt_report or lt_tf_report
    # except Exception:
    #     log.exception("%s: live-trading engine failed", symbol)

    digest_id, digest_msg = build_digest(
        symbol, new_alerts, fetched_at,
        scan_context=scan_context,
        intelligence_text=intel_text,
        detected_count=len(alerts),
        dedup_suppressed_count=dedup_suppressed,
        digest_id=digest_id,
        paper_trade_status=paper_trade_report,
        live_trade_status=live_trade_report,
        llm_verdict=llm_verdict,
    )
    for a in new_alerts:
        a["digest_id"] = digest_id

    # 3b. Save scan summary — pass is_fallback so regime_detector can exclude stale rows
    try:
        save_scan_summary(symbol, scan_context, new_alerts, intel, digest_id, fetched_at,
                          is_fallback=is_fallback)
    except Exception:
        log.exception("%s: scan summary save failed", symbol)

    # 4. Send logic
    should_send = bool(new_alerts)
    if not should_send:
        diag    = (scan_context or {}).get("diagnostics", {})
        max_oi  = float(diag.get("max_oi_delta_pct") or 0)
        if dedup_suppressed > 0:
            if should_send_zero_signal(symbol):
                should_send = True
            else:
                log.info("%s: duplicate-only scan suppressed by cooldown", symbol)
        elif max_oi >= 1.0:
            should_send = True
        elif should_send_zero_signal(symbol):
            should_send = True
        else:
            log.info("%s: suppressed flat zero-signal scan (max_oi=%.2f%%)", symbol, max_oi)

    if should_send:
        sent_digest = send_text(digest_msg)
    else:
        sent_digest = False

    # 5. Persist + record dedup
    if new_alerts:
        for alert in new_alerts:
            alert_id = insert_alert(alert)
            record_alert(alert)
            if sent_digest:
                mark_telegram_sent(alert_id)

    pct_chg = None
    if underlying is not None and prev_price and prev_price != 0:
        pct_chg = round((underlying - prev_price) / abs(prev_price) * 100, 4)

    insert_underlying_price(symbol, underlying, pct_chg, fetched_at)

    rows = [{
        "fetched_at":       fetched_at,
        "symbol":           symbol,
        "expiry":           expiry,
        "strike":           row["strike"],
        "option_type":      row["option_type"],
        "ltp":              row.get("ltp"),
        "ltp_change_pct":   row.get("ltp_change_pct"),
        "oi":               row.get("oi"),
        "oi_change_pct":    row.get("oi_change_pct"),
        "oi_change":        row.get("oi_change"),
        "volume":           row.get("volume"),
        "iv":               row.get("iv"),
        "bid":              row.get("bid"),
        "ask":              row.get("ask"),
        "delta":            row.get("delta"),
        "underlying_price": underlying,
        "fetcher_source":   source,
    } for row in oc_data["strikes"]]
    insert_snapshots(rows)
    log.info("%s: persisted %d rows (source: %s)", symbol, len(rows), source)
