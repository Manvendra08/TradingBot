"""
Data Pipeline Orchestrator v2.11-safe-io
fetch → detect → dedup → digest → alert

Safe parallelism branch goals:
- Single-flight run_pipeline lock (fail-closed on duplicate ticks)
- Bounded parallel fetch only, serialized commit boundary
- Intra-symbol parallel chart/news I/O with deadlines
- Async LLM default with frozen deterministic v1 decision
- NSE news bypass at deterministic input stage
- IP refresh off critical path
"""

import logging
import threading
from concurrent.futures import as_completed
from datetime import datetime, timezone

from config.settings import WATCH_SYMBOLS, get_symbol_thresholds, LLM_ENRICHMENT_ASYNC, MAX_ANOMALIES_PER_SYMBOL, ANOMALY_MIN_SEVERITY
from config.settings import DISABLE_LLM_ENRICHMENT as _DISABLE_LLM_ENV
from src.alerts.dedup import is_duplicate, record_alert, should_send_zero_signal
from src.alerts.digest import build_digest_wrapper as build_digest
from src.alerts.telegram_dispatcher import send_text, send_text_and_return_id, edit_message_text
from src.engine.anomaly_detector import detect_anomalies
from src.engine.intelligence import generate_intelligence_structured
from src.engine.live_trading import run_live_timeframe_strategy, run_live_trading
from src.engine.paper_trading import _invalidate_pattern_cache
from src.engine.pipeline_concurrency import single_flight_gate, serialized_commit_gate, pipeline_io_executor
from src.engine.provider_parallel import run_with_deadline
from src.engine.runtime_caches import (
    KITE_HEALTH_TTL_S,
    POSITION_RECONCILE_TTL_S,
    kite_health_cache,
    position_sync_cache,
    position_sync_dirty_state,
)
from src.engine.scan_cache import update_scan_snapshot
from src.engine.scan_summary import save_scan_summary
from src.fetchers.chart_fetcher import get_chart_fetcher
from src.fetchers.router import fetch_option_chain
from src.intelligence.history_analyzer import IST_OFFSET, get_analyzer
from src.models.schema import (
    get_previous_underlying,
    insert_alert,
    insert_snapshots,
    insert_underlying_price,
    mark_telegram_sent,
)
from src.utils.ip_monitor import check_ip_changed

log = logging.getLogger(__name__)

NSE_NEWS_BYPASS_SYMBOLS = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}


def _refresh_ip_async() -> None:
    try:
        res = check_ip_changed()
        if res:
            old_ip, new_ip = res
            send_text(
                f"🌐 **ISP IP Address Changed**\nOld: `{old_ip}`\nNew: `{new_ip}`\n\nPlease review broker allowlist settings if applicable."
            )
    except Exception as exc:
        log.warning("Async IP refresh failed: %s", exc)


def _maybe_sync_positions(force_reason: str | None = None) -> None:
    dirty, reason = position_sync_dirty_state.consume()
    if force_reason:
        dirty = True
        reason = force_reason
    heartbeat = position_sync_cache.get("heartbeat")
    if not dirty and heartbeat:
        return
    try:
        from src.engine.live_trading import sync_direct_kite_positions
        sync_direct_kite_positions()
        position_sync_dirty_state.clear()
        position_sync_cache.put("heartbeat", {"reason": reason}, POSITION_RECONCILE_TTL_S)
        log.info("Position sync completed: %s", reason)
    except Exception:
        position_sync_dirty_state.mark_dirty("sync_failed")
        log.exception("Direct Kite position synchronization failed")


def _ensure_kite_health() -> None:
    cached = kite_health_cache.get("session_ok")
    if cached:
        return
    try:
        from src.engine.live_trading import get_kite_client
        kite = get_kite_client()
        if kite is None:
            raise RuntimeError("kite client unavailable")
        kite_health_cache.put("session_ok", True, KITE_HEALTH_TTL_S)
    except Exception:
        kite_health_cache.invalidate("session_ok")
        try:
            from src.services.zerodha_auto_login import auto_login_kite
            result = auto_login_kite(force=False)
            if result.get("success"):
                kite_health_cache.put("session_ok", True, KITE_HEALTH_TTL_S)
            else:
                log.warning("Kite auto-login failed: %s", result.get("message", ""))
        except Exception:
            kite_health_cache.invalidate("session_ok")
            log.exception("Kite connectivity check failed")


def _prefetch_symbol_data(symbol: str, fetched_at: str) -> dict:
    packet = {"symbol": symbol, "fetched_at": fetched_at}
    oc = run_with_deadline("option_chain", lambda: fetch_option_chain(symbol))
    packet["option_chain_result"] = oc
    if not oc.ok or not oc.data:
        return packet

    oc_data = oc.data
    underlying = oc_data.get("underlying_price")

    chart_future = pipeline_io_executor.submit(
        lambda: run_with_deadline(
            "chart",
            lambda: get_chart_fetcher().fetch(symbol, reference_price=underlying) or {},
        )
    )

    skip_news = symbol.upper().strip().split()[0] in NSE_NEWS_BYPASS_SYMBOLS
    if skip_news:
        packet["news_result"] = {"ok": True, "data": None, "bypassed": True}
    else:
        def _fetch_news():
            from src.fetchers.news_fetcher import fetch_news
            return fetch_news(symbol)
        news_future = pipeline_io_executor.submit(lambda: run_with_deadline("news", _fetch_news))
        packet["news_result"] = news_future.result().__dict__

    packet["chart_result"] = chart_future.result().__dict__
    packet["oc_data"] = oc_data
    return packet


def run_pipeline(symbols: list[str] | None = None, force: bool = False, is_test: bool = False) -> None:
    symbols = symbols or WATCH_SYMBOLS
    fetched_at = datetime.now(timezone.utc).isoformat()

    with single_flight_gate.acquire_or_skip("run_pipeline") as acquired:
        if not acquired:
            return

        log.info("Pipeline run started | %s | symbols=%s | force=%s | is_test=%s", fetched_at, symbols, force, is_test)
        pipeline_io_executor.submit(_refresh_ip_async)

        if not is_test:
            _maybe_sync_positions()
            _ensure_kite_health()

        futures = [pipeline_io_executor.submit(_prefetch_symbol_data, symbol, fetched_at) for symbol in symbols]
        prefetched = []
        for fut in as_completed(futures):
            try:
                prefetched.append(fut.result())
            except Exception:
                log.exception("Prefetch stage failed for a symbol")

        for packet in sorted(prefetched, key=lambda x: symbols.index(x["symbol"])):
            try:
                _process_prefetched_symbol(packet, is_test=is_test)
            except Exception:
                log.exception("Unhandled pipeline error for %s", packet.get("symbol"))

        log.info("Pipeline run complete | %s", fetched_at)


def _process_prefetched_symbol(packet: dict, is_test: bool = False) -> None:
    symbol = packet["symbol"]
    fetched_at = packet["fetched_at"]
    oc_result = packet.get("option_chain_result")
    if not oc_result or not oc_result.ok or not packet.get("oc_data"):
        log.error("No data for %s — skipping", symbol)
        if not is_test:
            send_text(f"⚠️ **NSEBOT ALERT**: all fetchers failed for `{symbol}` at scan interval.")
        return

    oc_data = packet["oc_data"]
    chart_payload = packet.get("chart_result", {})
    if chart_payload.get("ok"):
        oc_data["chart_indicators"] = chart_payload.get("data") or {}
    else:
        oc_data["chart_indicators"] = {}

    news_payload = packet.get("news_result", {})
    news_data = None if news_payload.get("bypassed") else news_payload.get("data")

    prev_row = get_previous_underlying(symbol)
    prev_price = prev_row["price"] if prev_row else None
    underlying = oc_data.get("underlying_price")
    is_fallback = False
    if underlying is None:
        underlying = prev_price or 0.0
        oc_data["underlying_price"] = underlying
        is_fallback = True

    alerts, scan_context = detect_anomalies(
        oc_data,
        fetched_at,
        chart_indicators=oc_data.get("chart_indicators"),
        override_thresholds=get_symbol_thresholds(symbol),
    )
    scan_context["option_rows"] = list(oc_data.get("strikes") or [])

    try:
        update_scan_snapshot(symbol, scan_context)
    except Exception:
        log.debug("%s: scan snapshot caching failed gracefully", symbol)

    sev_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    min_sev = sev_order.get(ANOMALY_MIN_SEVERITY, 1)
    alerts = [a for a in alerts if sev_order.get(a.get("severity", "LOW"), 2) <= min_sev]
    if len(alerts) > MAX_ANOMALIES_PER_SYMBOL:
        alerts = sorted(alerts, key=lambda a: sev_order.get(a.get("severity", "LOW"), 2))[:MAX_ANOMALIES_PER_SYMBOL]

    new_alerts = [a for a in alerts if not is_duplicate(a)]
    dedup_suppressed = max(0, len(alerts) - len(new_alerts))

    intel = generate_intelligence_structured(symbol, new_alerts, scan_context=scan_context)
    intel_text = intel.get("telegram_text", "") if intel else f"⚠️ Intelligence generation failed for {symbol}"

    if intel:
        try:
            analyzer = get_analyzer()
            ist_hour = (datetime.now(timezone.utc) + IST_OFFSET).hour
            intel["trade_dna"] = analyzer.get_trade_dna_match({
                "symbol": symbol,
                "verdict_label": intel.get("verdict_label"),
                "confidence": intel.get("confidence", 0),
                "ist_hour": ist_hour,
            })
        except Exception:
            log.debug("%s: Trade DNA lookup failed gracefully", symbol)

        try:
            from src.intelligence.ml_predictor import get_predictor
            ml_prediction = get_predictor().predict({
                "symbol": symbol,
                "confidence": intel.get("confidence", 0),
                "verdict_label": intel.get("verdict_label"),
                "price_change_pct": scan_context.get("price_change_pct"),
                "pcr": scan_context.get("pcr"),
                "ce_oi_change": scan_context.get("ce_oi_change"),
                "pe_oi_change": scan_context.get("pe_oi_change"),
                "underlying": scan_context.get("underlying"),
                "support": scan_context.get("support"),
                "resistance": scan_context.get("resistance"),
                "max_pain": scan_context.get("max_pain"),
                "chart_conflict": intel.get("chart_conflict"),
                "days_to_expiry": None,
                "rsi_1h": None,
                "rsi_3h": None,
                "regime": scan_context.get("market_regime"),
                "opened_at": scan_context.get("fetched_at"),
            })
            if ml_prediction:
                intel["ml_prediction"] = {
                    "success_probability": ml_prediction.success_probability,
                    "confidence_level": ml_prediction.confidence_level,
                    "top_factors": ml_prediction.top_factors,
                    "model_version": ml_prediction.model_version,
                    "training_samples": ml_prediction.training_samples,
                }
        except Exception:
            log.debug("%s: ML prediction failed gracefully", symbol)

    from config.runtime_config import load_runtime_config
    rconf = load_runtime_config()
    llm_async = rconf.get("llm_enrichment_async", True)
    llm_verdict = None
    open_trade = None

    try:
        from src.models.schema import get_open_paper_trade
        open_trade = get_open_paper_trade(symbol)
        if open_trade:
            open_trade = dict(open_trade)
    except Exception:
        log.debug("%s: could not fetch open trade", symbol)

    deterministic_v1 = {
        "symbol": symbol,
        "intel": intel,
        "trade_decision": (intel or {}).get("trade_decision"),
        "engine_confidence": (intel or {}).get("confidence", 0),
        "news_used": False if news_payload.get("bypassed") else bool(news_data),
    }

    if not _DISABLE_LLM_ENV and intel and not open_trade:
        if llm_async:
            intel_text += "\n💡 *Thesis:* ⏳ Pending async analysis...\n"
        else:
            try:
                from src.engine.llm_enrichment import get_llm_verdict
                llm_verdict = get_llm_verdict(symbol, intel, scan_context, alerts=new_alerts, news_data=news_data, open_trade=None)
                if llm_verdict:
                    intel_text += f"\n\n💡 *Thesis:* {getattr(llm_verdict, 'thesis', '')}\n"
            except Exception:
                log.exception("%s: AI enrichment failed gracefully", symbol)

    with serialized_commit_gate.section(f"commit:{symbol}"):
        digest_id, digest_msg = build_digest(
            symbol,
            new_alerts,
            fetched_at,
            scan_context=scan_context,
            intelligence_text=intel_text,
            detected_count=len(alerts),
            dedup_suppressed_count=dedup_suppressed,
            digest_id=None,
            paper_trade_status=None,
            live_trade_status=None,
            llm_verdict=llm_verdict,
        )

        if intel:
            scan_context["trade_decision"] = intel.get("trade_decision")
            scan_context["engine_confidence"] = intel.get("confidence", 0)
            scan_context["deterministic_v1"] = deterministic_v1

        if not is_test:
            try:
                save_scan_summary(
                    symbol,
                    scan_context,
                    new_alerts,
                    intel,
                    digest_id,
                    fetched_at,
                    is_fallback=is_fallback,
                    llm_verdict=llm_verdict,
                )
            except Exception:
                log.exception("%s: scan summary save failed", symbol)

        should_send = bool(new_alerts)
        if not should_send:
            diag = (scan_context or {}).get("diagnostics", {})
            max_oi = float(diag.get("max_oi_delta_pct") or 0)
            if dedup_suppressed > 0:
                should_send = should_send_zero_signal(symbol)
            elif max_oi >= 1.0:
                should_send = True
            else:
                should_send = should_send_zero_signal(symbol)

        if is_test:
            send_text(f"⚠️ **TEST MODE** ⚠️\n\n{digest_msg}")
            sent_digest = False
        else:
            sent_digest = send_text(digest_msg) if should_send else False

        if not is_test:
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
                "fetched_at": fetched_at,
                "symbol": symbol,
                "expiry": oc_data["expiry"],
                "strike": row["strike"],
                "option_type": row["option_type"],
                "ltp": row.get("ltp"),
                "ltp_change_pct": row.get("ltp_change_pct"),
                "oi": row.get("oi"),
                "oi_change_pct": row.get("oi_change_pct"),
                "oi_change": row.get("oi_change"),
                "volume": row.get("volume"),
                "iv": row.get("iv"),
                "bid": row.get("bid"),
                "ask": row.get("ask"),
                "delta": row.get("delta"),
                "underlying_price": underlying,
                "fetcher_source": oc_data.get("source", "unknown"),
            } for row in oc_data["strikes"]]
            insert_snapshots(rows)

        if not is_test:
            try:
                from src.engine.strategy_registry import active_strategies_for
                active_strats = active_strategies_for(symbol)
                if "CORE" in active_strats:
                    run_live_trading(symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict)
                if "TIMEFRAME" in active_strats:
                    run_live_timeframe_strategy(symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict)
            except Exception:
                position_sync_dirty_state.mark_dirty("broker_action_failed")
                kite_health_cache.invalidate("session_ok")
                log.exception("%s: serialized strategy execution failed", symbol)
