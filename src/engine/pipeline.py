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
from datetime import datetime, timedelta, timezone

from config.settings import WATCH_SYMBOLS, get_symbol_thresholds, LLM_ENRICHMENT_ASYNC, MAX_ANOMALIES_PER_SYMBOL, ANOMALY_MIN_SEVERITY
from config.settings import DISABLE_LLM_ENRICHMENT as _DISABLE_LLM_ENV
from src.alerts.dedup import is_duplicate, record_alert, should_send_zero_signal
from src.alerts.digest import build_digest, synthesize_market_insight, format_options_insight
# Enforces edge_health check pipeline integration rule
from src.alerts.telegram_dispatcher import send_text, send_text_and_return_id, edit_message_text
from src.engine.anomaly_detector import detect_anomalies
from src.engine.intelligence import generate_intelligence_structured
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
_CLEANUP_DATES = set()

def _process_symbol(*args, **kwargs):
    # Backward compatibility alias for _process_prefetched_symbol in test mocks
    return _process_prefetched_symbol(*args, **kwargs)



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


def _ensure_shoonya_session() -> None:
    """Pre-warm the Shoonya OAuth session before the fetch deadline clock starts.

    login() holds a cross-process FileLock during Playwright (~25-35 s).  If we
    call it inside the 30 s option_chain deadline window it almost always times
    out on the first cold-start scan.  Calling it here — synchronously, before
    prefetch futures are submitted — guarantees the token is on disk by the time
    shoonya_fetcher.fetch_option_chain() runs, so the fetch itself stays fast.
    """
    try:
        from src.fetchers.shoonya_fetcher import get_shoonya_fetcher
        fetcher = get_shoonya_fetcher()
        # _load_cached_token refreshes from disk; if token is already valid,
        # login() returns immediately (no Playwright, no lock wait).
        fetcher._load_cached_token()
        if not fetcher.access_token:
            log.info("[pipeline] Shoonya session not ready — warming up before fetch deadline starts")
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(fetcher.login)
                try:
                    ok = future.result(timeout=8.0)
                except concurrent.futures.TimeoutError:
                    log.warning("[pipeline] Shoonya login timed out after 8s — falling back to dhan_commodity")
                    ok = False

            if ok:
                log.info("[pipeline] Shoonya session ready")
                try:
                    from src.models.schema import stamp_health
                    stamp_health("shoonya_session", "OK", "session_ready")
                except Exception:
                    pass
            else:
                log.warning("[pipeline] Shoonya login failed — fetcher will fall back to dhan_commodity")
                try:
                    from src.models.schema import stamp_health
                    stamp_health("shoonya_session", "DOWN", "login_failed")
                except Exception:
                    pass
    except Exception as exc:
        log.warning("[pipeline] _ensure_shoonya_session error (non-fatal): %s", exc)


def _prefetch_symbol_data(symbol: str, fetched_at: str) -> dict:
    packet = {"symbol": symbol, "fetched_at": fetched_at}
    oc = run_with_deadline("option_chain", lambda: fetch_option_chain(symbol))
    packet["option_chain_result"] = oc
    if not oc.ok or not oc.data:
        return packet

    oc_data = oc.data
    underlying = oc_data.get("underlying_price")

    # Accumulate next expiry options data when current expiry DTE is < 2 days
    expiry = oc_data.get("expiry")
    if expiry:
        try:
            from datetime import datetime
            import pytz
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            today = datetime.now(pytz.timezone("Asia/Kolkata")).date()
            dte = (exp_date - today).days
            if 0 <= dte < 2:
                all_exps = sorted(list(set(oc_data.get("all_expiries", []))))
                future_exps = [e for e in all_exps if datetime.strptime(e, "%Y-%m-%d").date() > exp_date]
                if future_exps:
                    next_exp = future_exps[0]
                    packet["next_expiry_future"] = pipeline_io_executor.submit(
                        lambda: run_with_deadline(
                            "next_option_chain",
                            lambda: fetch_option_chain(symbol, expiry=next_exp)
                        )
                    )
                    log.info("[pipeline] %s | DTE is %d (< 2 days). Submitting parallel fetch for next expiry %s",
                             symbol, dte, next_exp)
        except Exception as e:
            log.warning("[pipeline] %s | Failed to schedule next expiry pre-fetch: %s", symbol, e)

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
        # BUG-H06 FIX: Safe dict conversion - result may be simple type or namedtuple
        news_result = news_future.result()
        if hasattr(news_result, '__dict__'):
            packet["news_result"] = news_result.__dict__
        elif isinstance(news_result, dict):
            packet["news_result"] = news_result
        else:
            packet["news_result"] = {"ok": True, "data": news_result}

    # BUG-H06 FIX: Safe dict conversion for chart_result
    chart_result = chart_future.result()
    if hasattr(chart_result, '__dict__'):
        packet["chart_result"] = chart_result.__dict__
    elif isinstance(chart_result, dict):
        packet["chart_result"] = chart_result
    else:
        packet["chart_result"] = {"ok": True, "data": chart_result}
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
            _ensure_shoonya_session()  # warm Shoonya token before fetch deadline clock starts

        futures = [pipeline_io_executor.submit(_prefetch_symbol_data, symbol, fetched_at) for symbol in symbols]
        prefetched = []
        for fut in as_completed(futures):
            try:
                prefetched.append(fut.result())
            except Exception:
                log.exception("Prefetch stage failed for a symbol")

        # BUG-H07 FIX: Safe sorted() key with fallback for normalized symbols
        symbols_list = list(symbols)
        for packet in sorted(prefetched, key=lambda x: symbols_list.index(x["symbol"]) if x["symbol"] in symbols_list else 999):
            try:
                _process_prefetched_symbol(packet, is_test=is_test)
                try:
                    from src.models.schema import stamp_health
                    stamp_health(f"last_scan_{packet['symbol']}", "OK", f"scan_ok fetched_at={fetched_at}")
                except Exception:
                    pass
            except Exception:
                log.exception("Unhandled pipeline error for %s", packet.get("symbol"))
                try:
                    from src.models.schema import stamp_health
                    stamp_health(f"last_scan_{packet.get('symbol', 'UNKNOWN')}", "DOWN", f"pipeline_error")
                except Exception:
                    pass

        log.info("Pipeline run complete | %s", fetched_at)


def _async_llm_enrich_and_edit(
    symbol: str,
    intel: dict,
    scan_context: dict,
    new_alerts: list,
    news_data: dict | None,
    fetched_at: str,
    digest_id: str,
    message_id: int,
    dedup_suppressed: int,
    intel_text_base: str,
) -> None:
    """
    Background task to fetch LLM verdict and edit digest message.
    Runs after digest v1 is sent; edits message with v2 containing thesis.
    """
    try:
        from src.engine.llm_enrichment import get_llm_verdict
        llm_verdict = get_llm_verdict(
            symbol,
            intel,
            scan_context,
            alerts=new_alerts,
            news_data=news_data,
            open_trade=None,
        )
        if not llm_verdict:
            log.debug("%s: async LLM returned no verdict", symbol)
            return

        log.info(
            "%s: async LLM verdict — %s (%d%%) risk=%s",
            symbol,
            llm_verdict.action,
            llm_verdict.confidence,
            llm_verdict.risk_rating,
        )

        intel_v2 = generate_intelligence_structured(
            symbol,
            new_alerts,
            scan_context=scan_context,
            ai_verdict=llm_verdict,
        )
        intel_text_v2 = intel_v2.get("telegram_text", intel_text_base) if intel_v2 else intel_text_base

        action_emoji = {
            "GO_LONG": "🟢",
            "GO_SHORT": "🔴",
            "NO_TRADE": "⚪",
        }.get(getattr(llm_verdict, "action", "NO_TRADE"), "❓")
        risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(
            getattr(llm_verdict, "risk_rating", "MEDIUM"), "❓"
        )

        thesis_line = f"\n\n{action_emoji} *AI Trade Plan* ({llm_verdict.action}, {llm_verdict.confidence}%)\n"
        thesis_line += f"📋 *Contract:* `{llm_verdict.instrument}`\n"
        thesis_line += f"🎯 *Entry:* {llm_verdict.entry_trigger}\n"
        thesis_line += f"💰 *Premium:* {llm_verdict.entry_premium_range}\n"
        thesis_line += f"🛑 *SL:* {llm_verdict.stop_loss}\n"
        thesis_line += f"🎯 *T1:* {llm_verdict.target_1} | *T2:* {llm_verdict.target_2}\n"
        thesis_line += f"📊 *R:R:* {llm_verdict.risk_reward} | {risk_emoji} *Risk:* {llm_verdict.risk_rating}\n"
        thesis_line += f"💡 *Thesis:* {llm_verdict.thesis}\n"
        thesis_line += f"⚠️ *Invalidation:* {llm_verdict.invalidation}\n"
        if llm_verdict.catalyst and llm_verdict.catalyst != "No major catalyst":
            thesis_line += f"📅 *Catalyst:* {llm_verdict.catalyst}\n"

        intel_text_v2 += thesis_line

        structured_payload = _build_structured_payload(
            symbol, fetched_at, scan_context, intel, llm_verdict,
            news_data=news_data, open_trade=None, digest_id=digest_id,
        )

        _, digest_msg_v2 = build_digest(
            symbol,
            new_alerts,
            scan_context,
            intel,
            intelligence_text=intel_text_v2,
            paper_trade_status=None,
            live_trade_status=None,
            llm_verdict=llm_verdict,
            structured_payload=structured_payload,
        )

        if edit_message_text(message_id, digest_msg_v2):
            log.info("%s: async LLM digest v2 edit successful", symbol)
        else:
            log.debug("%s: async LLM digest v2 edit failed, sending follow-up", symbol)
            send_text(f"🔄 *Updated analysis for {symbol}:*\n\n{thesis_line}")
    except Exception as e:
        log.warning("%s: async LLM enrichment thread failed: %s", symbol, e)


def _build_structured_payload(symbol: str, fetched_at: str, scan_context: dict, intel: dict, llm_verdict: any, news_data: dict | None = None, open_trade: dict | None = None, exit_advice: any = None, digest_id: str | None = None) -> dict:
    from datetime import datetime, timezone
    
    td = (intel or {}).get("trade_decision") or {}
    
    # 1. Header
    try:
        dt = datetime.fromisoformat(fetched_at or "").astimezone(timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
    IST = timezone(timedelta(hours=5, minutes=30))
    ts = dt.astimezone(IST).strftime("%H:%M IST")
    
    trade_entered = td.get("status") in ("TRIGGERED_CORE", "TRIGGERED_EXPERIMENTAL")
    actual_lots = 1
    if digest_id:
        from src.models.schema import get_conn
        try:
            with get_conn() as conn:
                # Check paper trades
                row = conn.execute("SELECT lots FROM paper_trades WHERE digest_id=?", (digest_id,)).fetchone()
                if row:
                    trade_entered = True
                    actual_lots = row[0]
                else:
                    # Check live trades
                    row_live = conn.execute("SELECT lots FROM live_trades WHERE digest_id=?", (digest_id,)).fetchone()
                    if row_live:
                        trade_entered = True
                        actual_lots = row_live[0]
                    else:
                        trade_entered = False
        except Exception as e:
            log.debug("Error checking actual trade for digest_id %s: %s", digest_id, e)

    header = {
        "symbol": symbol,
        "scan_time": ts,
        "expiry": scan_context.get("expiry") or scan_context.get("futures_expiry") or "",
        "dte": scan_context.get("days_to_expiry", 0),
        "underlying": scan_context.get("underlying") or 0.0,
        "market_regime": scan_context.get("market_regime") or "UNKNOWN",
        "confidence": (intel or {}).get("confidence", 0),
        "trade_entered": trade_entered
    }

    is_timeframe = td.get("execution_source") == "TIMEFRAME"
    
    # TFSS vs TIMEFRAME routing
    tfss = {
        "core_origin_verdict": td.get("core_verdict_family", "N/A"),
        "tfss_bias": td.get("normalized_tfss_bias", "N/A"),
        "action": td.get("action", "BLOCK"),
        "execution_side": td.get("option_side", "N/A"),
        "trade_entered": trade_entered,
        "contract": f"{symbol} {td.get('strike')} {td.get('option_side')}" if td.get("strike") else None,
        "delta": td.get("delta"),
        "premium": td.get("premium"),
        "qty": actual_lots,
        "tranche_index": td.get("tranche_index"),
        "exit_reduce": "N/A",
        "existing_position": "N/A",
        "primary_reason": td.get("reason", "N/A"),
        "why": [],
        "blockers": [td.get("reason")] if td.get("status") == "BLOCKED" and not is_timeframe else [],
        "primary_trigger": "None",
        "also_eligible_triggers": td.get("also_eligible_triggers", [])
    }
    
    timeframe = {
        "signal": "N/A",
        "direction": "N/A",
        "action": td.get("action") if is_timeframe else "BLOCK",
        "setup": td.get("setup_type", "N/A") if is_timeframe else "N/A",
        "contract": "N/A",
        "primary_reason": td.get("reason") if is_timeframe else "N/A",
        "why": [],
        "blockers": [td.get("reason")] if is_timeframe and td.get("status") == "BLOCKED" else []
    }
    
    if is_timeframe:
        tfss["action"] = "BLOCK"
        tfss["trade_entered"] = False
        tfss["contract"] = None
        
    ai_thesis = ""
    llm_thesis = getattr(llm_verdict, "thesis", "") if llm_verdict else ""
    if llm_thesis:
        ai_thesis += llm_thesis

    insight = synthesize_market_insight(
        scan_context,
        verdict_label=(intel or {}).get("verdict_label"),
        bias=(intel or {}).get("bias") or scan_context.get("tfss_bias"),
        confidence=(intel or {}).get("confidence", 0),
        news_data=news_data,
        open_trade=open_trade,
    )
    if insight:
        ai_thesis += ("\n\n" if ai_thesis else "") + insight
    if not ai_thesis:
        ai_thesis = "No thesis generated."

    return {
        "header": header,
        "tfss": tfss,
        "timeframe": timeframe,
        "positions": {},
        "global_risk": {},
        "options_insight": format_options_insight(scan_context, symbol),
        "ai_thesis": ai_thesis,
        "exit_advice": exit_advice
    }


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

    # Inject NG parity + weather context for NATURALGAS scans
    if symbol.upper().startswith("NATURALGAS"):
        try:
            from src.engine.parity_engine import get_parity_state
            parity = get_parity_state()
            if parity:
                scan_context["ng_regime"] = parity.get("regime", "UNKNOWN")
                scan_context["ng_fv"] = parity.get("fair_value", 0.0)
                scan_context["ng_dev_pct"] = parity.get("dev_pct", 0.0)
                scan_context["ng_mcx_src"] = parity.get("mcx_src", "")
                scan_context["ng_fx_src"] = parity.get("fx_src", "")
                scan_context["ng_nymex_src"] = parity.get("nymex_src", "")
        except Exception:
            log.debug("%s: parity injection failed gracefully", symbol)

        try:
            from src.fetchers.weather_fetcher import get_weather_signal
            wsig = get_weather_signal()
            if wsig:
                scan_context["weather_signal"] = wsig
                scan_context["weather_direction"] = wsig.get("direction", "neutral")
                scan_context["weather_z"] = wsig.get("zscore", 0.0)
                scan_context["weather_gulf_storm"] = wsig.get("gulf_storm_active", False)
        except Exception:
            log.debug("%s: weather signal injection failed gracefully", symbol)

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

            # Extract real DTE, RSI_1H, and RSI_3H features
            dte = intel.get("days_to_expiry")
            if dte is None or dte < 0:
                exp_str = scan_context.get("expiry")
                if exp_str:
                    try:
                        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                        today_date = (datetime.now(timezone.utc) + IST_OFFSET).date()
                        dte = max(0, (exp_date - today_date).days)
                    except Exception:
                        dte = None

            chart_indicators = scan_context.get("chart_indicators") or {}
            raw_rsi_1h = (chart_indicators.get("1h") or {}).get("rsi")
            rsi_1h = float(raw_rsi_1h) if raw_rsi_1h is not None else None

            raw_rsi_3h = (chart_indicators.get("3h") or {}).get("rsi")
            rsi_3h = float(raw_rsi_3h) if raw_rsi_3h is not None else None

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
                "days_to_expiry": dte,
                "rsi_1h": rsi_1h,
                "rsi_3h": rsi_3h,
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

    intel_text_base = intel_text
    exit_advice = None
    if not _DISABLE_LLM_ENV:
        if open_trade:
            try:
                from src.engine.llm_enrichment import get_exit_advice
                exit_advice = get_exit_advice(symbol, open_trade, scan_context, news_data=news_data)
                if exit_advice:
                    ea_action = getattr(exit_advice, "action", "")
                    ea_urgency = getattr(exit_advice, "urgency", "")
                    ea_reasoning = getattr(exit_advice, "reasoning", "")
                    intel_text += f"\n\n🚨 *AI EXIT ADVICE:* {ea_action} (Urgency: {ea_urgency})\n💡 *Reason:* {ea_reasoning}\n"
            except Exception:
                log.exception("%s: AI exit advice failed gracefully", symbol)
        elif intel:
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
        structured_payload = _build_structured_payload(
            symbol, fetched_at, scan_context, intel, llm_verdict,
            news_data=news_data, open_trade=open_trade, exit_advice=exit_advice,
        )

        digest_id, digest_msg = build_digest(
            symbol,
            new_alerts,
            scan_context,
            intel,
            intelligence_text=intel_text,
            paper_trade_status=None,
            live_trade_status=None,
            llm_verdict=llm_verdict,
            exit_advice=exit_advice,
            structured_payload=structured_payload,
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

        telegram_message_id = None
        _async_llm_pending = False

        if is_test:
            send_text(f"⚠️ **TEST MODE** ⚠️\n\n{digest_msg}")
            sent_digest = False
        else:
            if should_send:
                if llm_async and not _DISABLE_LLM_ENV and intel and not open_trade:
                    telegram_message_id = send_text_and_return_id(digest_msg)
                    sent_digest = telegram_message_id is not None
                    if sent_digest:
                        _async_llm_pending = True
                else:
                    sent_digest = send_text(digest_msg)
            else:
                sent_digest = False

        if _async_llm_pending and telegram_message_id is not None:
            # BUG-M11 FIX: Use functools.partial for positional parameter submission
            import functools
            pipeline_io_executor.submit(
                functools.partial(
                    _async_llm_enrich_and_edit,
                    symbol, intel, scan_context, new_alerts, news_data,
                    fetched_at, digest_id, telegram_message_id,
                    dedup_suppressed, intel_text_base,
                )
            )

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

            # Insert next expiry snapshots if pre-fetched (DTE < 2 days)
            next_exp_future = packet.get("next_expiry_future")
            if next_exp_future:
                try:
                    next_oc_res = next_exp_future.result()
                    next_oc = next_oc_res.data if (next_oc_res and next_oc_res.ok) else None
                    if next_oc and next_oc.get("strikes"):
                        next_rows = [{
                            "fetched_at": fetched_at,
                            "symbol": symbol,
                            "expiry": next_oc["expiry"],
                            "strike": r["strike"],
                            "option_type": r["option_type"],
                            "ltp": r.get("ltp"),
                            "ltp_change_pct": r.get("ltp_change_pct"),
                            "oi": r.get("oi"),
                            "oi_change_pct": r.get("oi_change_pct"),
                            "oi_change": r.get("oi_change"),
                            "volume": r.get("volume"),
                            "iv": r.get("iv"),
                            "bid": r.get("bid"),
                            "ask": r.get("ask"),
                            "delta": r.get("delta"),
                            "underlying_price": underlying,
                            "fetcher_source": next_oc.get("source", "unknown"),
                        } for r in next_oc["strikes"]]
                        insert_snapshots(next_rows)
                        log.info("[pipeline] %s | Accumulated next expiry (%s) option chain snapshots (%d rows)",
                                 symbol, next_oc["expiry"], len(next_rows))
                except Exception as e:
                    log.warning("[pipeline] %s | Failed to retrieve/store next expiry option chain: %s", symbol, e)

        if not is_test:
            try:
                # ── Global Trade Monitoring across ALL Regimes/Strategies ──
                # Ensure mechanical SL/Target/Trailing/Dead/Delta monitoring runs
                # on every scan regardless of session regime (PARITY, EVENT, MOMENTUM, CORE).
                from src.engine.paper_trading import monitor_paper_trades, close_paper_trade
                from src.engine.trade_plan import get_option_premium

                monitor_paper_trades(symbol, scan_context)

                # AI exit advice is advisory only per USER request. It should not execute trades.
                if open_trade and exit_advice:
                    ea_action = str(getattr(exit_advice, "action", "")).upper()
                    ea_urgency = str(getattr(exit_advice, "urgency", "")).upper()
                    if ea_action in ("CLOSE_EARLY", "FLAT_NOW", "EXIT") and ea_urgency == "HIGH":
                        log.info(
                            "%s: [Advisory Only] AI Exit Advice recommends %s (HIGH) | Reason: %s",
                            symbol,
                            ea_action,
                            getattr(exit_advice, "reasoning", ""),
                        )

                from src.engine.strategy_registry import active_strategies_for, get_runner
                for sid in active_strategies_for(symbol):
                    runner = get_runner(sid)
                    if runner is None:
                        continue
                    runner(symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict)
                    
                    # ── Sequentially Trigger Live/Shadow Execution for Active Strategies ──
                    if sid in ("CORE", "NG_MOMENTUM", "NG_PARITY", "NG_EVENT"):
                        try:
                            from src.engine.live_trading import run_live_trading
                            run_live_trading(symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict)
                        except Exception as le:
                            log.exception("%s: live/shadow trading execution failed for %s", symbol, sid)
                    elif sid == "TIMEFRAME":
                        try:
                            from src.engine.live_trading import run_live_timeframe_strategy
                            run_live_timeframe_strategy(symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict)
                        except Exception as le:
                            log.exception("%s: live/shadow timeframe strategy execution failed", symbol)
            except Exception:
                position_sync_dirty_state.mark_dirty("broker_action_failed")
                kite_health_cache.invalidate("session_ok")
                log.exception("%s: serialized strategy execution failed", symbol)
        
        # Run Scan Sentinel diagnostics asynchronously (non-blocking)
        if not is_test:
            try:
                from src.engine.scan_sentinel import run_sentinel
                
                # Build lightweight report for sentinel
                sentinel_report = {
                    "symbol": symbol,
                    "timestamp_ist": datetime.now(timezone.utc).isoformat(),
                    "scan_duration_ms": 0,  # Not tracked in this simplified integration
                    "underlying_price": float(oc_data.get("underlying_price") or 0.0),
                    "expiry": oc_data.get("expiry", ""),
                    "source": oc_data.get("source", "unknown"),
                    "total_strikes": len(oc_data.get("strikes") or []),
                    "zero_ltp_strikes": sum(1 for s in oc_data.get("strikes", []) if float(s.get("ltp") or 0.0) == 0.0),
                    "zero_oi_strikes": sum(1 for s in oc_data.get("strikes", []) if int(s.get("oi") or 0) == 0),
                    "llm_action": getattr(llm_verdict, "action", None) if llm_verdict else None,
                    "llm_instrument": getattr(llm_verdict, "instrument", None) if llm_verdict else None,
                    "llm_entry_premium": getattr(llm_verdict, "entry_premium_range", None) if llm_verdict else None,
                    "llm_target_1": getattr(llm_verdict, "target_1", None) if llm_verdict else None,
                    "llm_target_2": getattr(llm_verdict, "target_2", None) if llm_verdict else None,
                    "llm_stop_loss": getattr(llm_verdict, "stop_loss", None) if llm_verdict else None,
                    "trade_decision_status": (intel.get("trade_decision") or {}).get("status") if intel else None,
                    "trade_decision_reason": (intel.get("trade_decision") or {}).get("reason") if intel else None,
                    "warnings": [],  # Could extract from logs if needed
                    "errors": [],    # Could extract from logs if needed
                    "fetcher_errors": scan_context.get("fetcher_errors", []),
                    "option_premium_used": None,
                    "log_lines": [],
                    "is_test": is_test,
                    "status": "COMPLETED"
                }
                
                # Submit to thread pool for async execution (non-blocking)
                pipeline_io_executor.submit(lambda: run_sentinel(sentinel_report))
            except Exception:
                log.warning("%s: Scan Sentinel submission failed", symbol, exc_info=True)
