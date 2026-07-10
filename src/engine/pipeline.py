"""
Data Pipeline Orchestrator v2.11
fetch → detect → dedup → digest → alert

Fixes (v2.11):
  - PDM: Pipeline Decision Matrix injected after LLM+ML resolution.
    Composite score, gate pass/fail, and per-signal breakdown appended to
    scan_context, intel, and Telegram digest.

Fixes (v2.10):
  - ADR-007 §3 A3: Async LLM enrichment — entry verdict deferred to background thread.
    Digest v1 sent with rule-derived levels + "thesis pending"; v2 edit adds thesis/invalidation.

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
import threading
from datetime import datetime, timezone

from config.settings import WATCH_SYMBOLS, get_symbol_thresholds, LLM_ENRICHMENT_ASYNC, LLM_ENRICH_TIMEOUT_S, MAX_ANOMALIES_PER_SYMBOL, ANOMALY_MIN_SEVERITY
from config.settings import DISABLE_LLM_ENRICHMENT as _DISABLE_LLM_ENV
from src.alerts.dedup import is_duplicate, record_alert, should_send_zero_signal
from src.alerts.digest import build_digest_wrapper as build_digest
from src.alerts.telegram_dispatcher import send_text, send_text_and_return_id, edit_message_text
from src.engine.anomaly_detector import detect_anomalies
from src.engine.intelligence import generate_intelligence_structured
from src.engine.live_trading import run_live_timeframe_strategy, run_live_trading
from src.engine.paper_trading import _invalidate_pattern_cache

# Phase 2: ML Success Predictor integration (AI_INTELLIGENCE_ROADMAP_v3.0)
from src.engine.scan_cache import update_scan_snapshot
from src.engine.scan_summary import save_scan_summary
from src.fetchers.chart_fetcher import get_chart_fetcher
from src.fetchers.router import fetch_option_chain

# Phase 1: Trade History Analyzer integration (AI_INTELLIGENCE_ROADMAP_v3.0)
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

import json
from pathlib import Path

# Track which calendar dates have already been cleaned to avoid
# repeating the expiry cleanup on every 3-minute scan cycle.
CLEANUP_DATA_FILE = Path("data") / "cleanup_dates.json"

# BUG-005 FIX: Maximum number of dates to retain in the cleanup tracking set.
# Older entries are pruned to prevent unbounded growth of the JSON file.
# 30 days is sufficient — cleanup is idempotent and re-running is harmless.
_CLEANUP_DATES_MAX_ENTRIES = 30

# ── P0-1: Pipeline re-entrancy lock ────────────────────────────────────────────────
# Prevents concurrent runs when pipeline execution exceeds the scheduler sleep
# interval. Without this, double-entry into open positions is possible.
_PIPELINE_LOCK = threading.Lock()


def _load_cleanup_dates() -> set[str]:
    if not CLEANUP_DATA_FILE.exists():
        return set()
    try:
        dates = set(json.loads(CLEANUP_DATA_FILE.read_text()))
        # BUG-005 FIX: Prune old entries on load to prevent unbounded growth
        if len(dates) > _CLEANUP_DATES_MAX_ENTRIES:
            sorted_dates = sorted(dates, reverse=True)
            dates = set(sorted_dates[:_CLEANUP_DATES_MAX_ENTRIES])
            log.debug(
                "Pruned cleanup dates from %d to %d entries",
                len(sorted_dates),
                len(dates),
            )
        return dates
    except Exception:
        return set()


def _save_cleanup_dates(dates: set[str]) -> None:
    # BUG-005 FIX: Enforce max entries before saving to prevent unbounded growth
    if len(dates) > _CLEANUP_DATES_MAX_ENTRIES:
        sorted_dates = sorted(dates, reverse=True)
        dates = set(sorted_dates[:_CLEANUP_DATES_MAX_ENTRIES])
    CLEANUP_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLEANUP_DATA_FILE.write_text(json.dumps(sorted(dates)))


_CLEANUP_DATES = _load_cleanup_dates()


def _check_edge_health_and_trigger_retrain() -> None:
    """
    Phase 3: After a trade closes, check edge health and trigger ML retraining
    if the edge is declining.

    v3.0 FIX #6: Do NOT trigger retrain on the insufficient-data sentinel.
    The early-data branch returns health_score=50, which is < the retrain
    threshold (60) — so the old code fired run_training() on EVERY early
    trade close (wasted calls; train() bails under 30 trades anyway, and
    thrashes once just past it). Only react to a REAL declining edge.
    """
    try:
        from src.intelligence.edge_monitor import get_monitor
        from src.scheduler.ml_training_job import on_edge_health_alert

        monitor = get_monitor()
        overall_health = monitor.check_edge_health()
        if overall_health:
            h = overall_health[0]
            # Only trigger on a REAL trend assessment, not the sentinel
            if h.win_rate_trend not in ("INSUFFICIENT_HISTORY",):
                on_edge_health_alert(h.health_score)
    except ImportError:
        pass  # edge_monitor not yet available
    except Exception:
        log.debug("Edge health check failed gracefully")


def _async_llm_enrich_and_edit(
    symbol: str,
    intel: dict,
    scan_context: dict,
    new_alerts: list,
    news_data: dict,
    fetched_at: str,
    digest_id: str,
    message_id: int,
    paper_trade_report: dict | None,
    live_trade_report: dict | None,
    dedup_suppressed: int,
    intel_text_base: str,
) -> None:
    """
    ADR-007 §3 A3: Background thread to fetch LLM verdict and edit digest message.
    Runs after digest v1 is sent; edits message with v2 containing thesis/invalidation.
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

        _, digest_msg_v2 = build_digest(
            symbol,
            new_alerts,
            fetched_at,
            scan_context=scan_context,
            intelligence_text=intel_text_v2,
            detected_count=len(new_alerts),
            dedup_suppressed_count=dedup_suppressed,
            digest_id=digest_id,
            paper_trade_status=paper_trade_report,
            live_trade_status=live_trade_report,
            llm_verdict=llm_verdict,
        )

        if edit_message_text(message_id, digest_msg_v2):
            log.info("%s: async LLM digest v2 edit successful", symbol)
        else:
            log.debug("%s: async LLM digest v2 edit failed, sending follow-up", symbol)
            send_text(f"🔄 *Updated analysis for {symbol}:*\n\n{thesis_line}")
    except Exception as e:
        log.warning("%s: async LLM enrichment thread failed: %s", symbol, e)



def run_pipeline(
    symbols: list[str] | None = None, force: bool = False, is_test: bool = False
) -> None:
    """
    Run the full pipeline for each symbol.

    Args:
        symbols: Override watch list. Defaults to WATCH_SYMBOLS.
        force:   Skip market-hours guard (used by --now CLI flag).
        is_test: Run in dry-run test mode without writing to DB or sending Telegram alerts.
    """
    symbols = symbols or WATCH_SYMBOLS

    # P0-1: Non-blocking lock acquisition — if another pipeline run is still
    # active, skip this interval entirely rather than risking double-entry.
    if not _PIPELINE_LOCK.acquire(blocking=False):
        log.warning(
            "Pipeline re-entrancy: previous run still active — skipping this interval"
        )
        return

    fetched_at = datetime.now(timezone.utc).isoformat()
    log.info(
        "Pipeline run started | %s | symbols: %s | force=%s | is_test=%s",
        fetched_at,
        symbols,
        force,
        is_test,
    )

    # Check for ISP IP address change and alert user if it changed.
    if not is_test:
        try:
            ip_change = check_ip_changed()
            if ip_change:
                old_ip, new_ip = ip_change
                msg = (
                    f"🌐 **ISP IP Address Changed**\n"
                    f"Old: `{old_ip}`\n"
                    f"New: `{new_ip}`\n\n"
                    f"Your internet connection IP has changed. If using a broker API "
                    f"with IP whitelisting, you may need to update the whitelist."
                )
                send_text(msg)
                log.warning("ISP IP changed: %s → %s", old_ip, new_ip)
        except Exception:
            log.exception("IP change check failed during pipeline startup")

    # At start of scan, check if previous day was an expiry day for any symbol
    if not is_test:
        try:
            from datetime import timedelta

            import pytz

            from src.models.schema import get_conn

            IST = pytz.timezone("Asia/Kolkata")
            today_ist = datetime.now(IST).date()
            today_str = today_ist.strftime("%Y-%m-%d")

            # Run expiry-data cleanup at most once per calendar day (persistent across restarts).
            if today_str in _CLEANUP_DATES:
                log.debug("Expiry cleanup already done for %s, skipping", today_str)
            else:
                _CLEANUP_DATES.add(today_str)
                _save_cleanup_dates(_CLEANUP_DATES)
                yesterday_str = (today_ist - timedelta(days=1)).strftime("%Y-%m-%d")

                with get_conn() as conn:
                    # Find if yesterday_str exists as an expiry in option_chain_snapshots
                    expired_symbols = [
                        r[0]
                        for r in conn.execute(
                            "SELECT DISTINCT symbol FROM option_chain_snapshots WHERE expiry = ?",
                            (yesterday_str,),
                        ).fetchall()
                    ]

                    if expired_symbols:
                        log.info(
                            "Previous day (%s) was expiry day for: %s. Cleaning up expired contract data.",
                            yesterday_str,
                            expired_symbols,
                        )
                        for sym in expired_symbols:
                            c_del = conn.execute(
                                "DELETE FROM option_chain_snapshots WHERE symbol = ? AND expiry <= ?",
                                (sym, yesterday_str),
                            )
                            log.info(
                                "Deleted %d expired snapshots for %s",
                                c_del.rowcount,
                                sym,
                            )

                    # General cleanup: delete all snapshots where expiry is in the past
                    c_past = conn.execute(
                        "DELETE FROM option_chain_snapshots WHERE expiry < ?",
                        (today_str,),
                    )
                    if c_past.rowcount > 0:
                        log.info(
                            "General cleanup: deleted %d older snapshots",
                            c_past.rowcount,
                        )
        except Exception:
            log.exception(
                "Error checking/deleting expired contract data at start of scan"
            )

    # B7/L3: Sync manual Kite direct positions to SQLite for AI Exit Advisor monitoring.
    if not is_test:
        try:
            from src.engine.live_trading import sync_direct_kite_positions

            sync_direct_kite_positions()
        except Exception:
            log.exception("Direct Kite position synchronization failed")

    # ── Kite connectivity check — auto-login if no valid session ─────────────────
    if not is_test:
        try:
            from src.engine.live_trading import get_kite_client

            kite = get_kite_client()
            if kite is None:
                log.warning("Kite client not available — attempting auto-login")
                try:
                    from src.services.zerodha_auto_login import auto_login_kite

                    result = auto_login_kite(force=False)
                    action = result.get("action", "UNKNOWN")
                    if result.get("success"):
                        log.info("Kite auto-login succeeded: %s", action)
                    else:
                        log.warning(
                            "Kite auto-login failed: %s — %s",
                            action,
                            result.get("message", ""),
                        )
                except Exception as login_err:
                    log.exception(
                        "Kite auto-login attempt raised exception: %s", login_err
                    )
            else:
                log.debug("Kite client is available — session active")
        except Exception:
            log.exception("Kite connectivity check failed")

    try:
        for symbol in symbols:
            try:
                _process_symbol(symbol, fetched_at, is_test=is_test)
            except Exception:
                log.exception(
                    "Unhandled pipeline error for %s — continuing with next symbol", symbol
                )
                # OPS Agent: stamp failure
                try:
                    from src.models.schema import stamp_health
                    stamp_health(f"last_scan_{symbol}", "DOWN", f"pipeline_error at {fetched_at}")
                except Exception:
                    pass
    finally:
        _PIPELINE_LOCK.release()
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
                and str(row.get("option_type") or "").upper()
                == str(option_type).upper()
            ):
                ltp = float(row.get("ltp") or 0.0)
                return ltp if ltp > 0 else None
        except Exception:
            continue
    return None


def _process_symbol(symbol: str, fetched_at: str, is_test: bool = False) -> None:
    from src.engine.scan_sentinel import ScanRunRecorder, run_sentinel
    
    oc_data = None
    scan_context = {}
    intel = None
    llm_verdict = None
    exit_advice = None
    
    with ScanRunRecorder(symbol) as recorder:
        try:
            results = {}
            _process_symbol_inner(symbol, fetched_at, is_test, results)
            oc_data = results.get("oc_data")
            scan_context = results.get("scan_context", {})
            intel = results.get("intel")
            llm_verdict = results.get("llm_verdict")
            exit_advice = results.get("exit_advice")
        finally:
            if oc_data is None:
                oc_data = {"underlying_price": 0.0, "expiry": "", "source": "failed", "strikes": []}
            recorder.finalize(oc_data, scan_context, intel, llm_verdict, exit_advice, is_test)
            if recorder.report:
                run_sentinel(recorder.report)

def _process_symbol_inner(symbol: str, fetched_at: str, is_test: bool = False, results: dict = None) -> None:
    if results is None:
        results = {}
    log.info("Processing %s ...", symbol)

    import sys

    from config.symbol_classes import is_market_open

    if not is_market_open(symbol):
        if not is_test and "pytest" not in sys.modules:
            log.info(
                "%s: Market is closed. Forcing dry-run (skip database save) for this symbol.",
                symbol,
            )
            is_test = True

    oc_data = fetch_option_chain(symbol)
    results["oc_data"] = oc_data
    if not oc_data:
        log.error("No data for %s — skipping", symbol)
        # OPS Agent: stamp failure
        try:
            from src.models.schema import stamp_health
            stamp_health(f"last_scan_{symbol}", "DOWN", f"fetch_failed at {fetched_at}")
        except Exception:
            pass
        if not is_test:
            try:
                send_text(
                    f"⚠️ **NSEBOT ALERT**: ALL data fetchers failed for symbol `{symbol}` at scan interval. Price tracking and strategy execution skipped."
                )
            except Exception:
                log.exception(
                    "Failed to send fetch-failure Telegram alert for %s", symbol
                )
        return

    underlying = oc_data["underlying_price"]
    expiry = oc_data["expiry"]
    source = oc_data.get("source", "unknown")
    prev_row = get_previous_underlying(symbol)
    prev_price = prev_row["price"] if prev_row else None

    # B5: flag when we're using a stale fallback price so regime_detector can ignore the row
    is_fallback = False
    if underlying is None:
        underlying = prev_price or 0.0
        oc_data["underlying_price"] = underlying
        is_fallback = True
        log.warning(
            "%s: underlying price is None, falling back to prev_price: %s",
            symbol,
            underlying,
        )



    # 1a. Fetch chart data server-side (Chrome-free)
    try:
        chart_data = get_chart_fetcher().fetch(symbol, reference_price=underlying) or {}
        oc_data["chart_indicators"] = chart_data
        if chart_data:
            log.debug("%s: chart_indicators injected from chart_fetcher", symbol)
        else:
            log.warning(
                "%s: chart_fetcher returned empty chart dict — continuing without chart",
                symbol,
            )
    except Exception:
        oc_data["chart_indicators"] = {}
        log.exception(
            "%s: chart_fetcher crashed — continuing without chart data", symbol
        )

    # 1b. Detect anomalies
    symbol_thresholds = get_symbol_thresholds(symbol)
    alerts, scan_context = detect_anomalies(
        oc_data,
        fetched_at,
        chart_indicators=oc_data.get("chart_indicators"),
        override_thresholds=symbol_thresholds,
    )
    scan_context["option_rows"] = list(oc_data.get("strikes") or [])
    results["scan_context"] = scan_context

    # Phase 1: Natural Gas Parity Logging & context injection
    if str(symbol).upper().startswith("NATURALGAS"):
        try:
            from src.engine.parity_engine import get_parity_state
            from src.engine.ng_session_router import get_ng_regime
            import pytz
            from datetime import datetime

            parity_state = get_parity_state(underlying, mcx_age_sec=0)
            now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
            regime, ng_reason = get_ng_regime(now_ist)

            # Inject into scan_context so LLM and digest can access it
            scan_context["ng_regime"] = regime
            scan_context["ng_dev_pct"] = parity_state.dev_pct
            scan_context["ng_fair_value"] = parity_state.fair_value

            if not is_test:
                from dataclasses import asdict
                from src.models.schema import insert_ng_parity_log
                log_data = asdict(parity_state)
                log_data["timestamp"] = fetched_at
                log_data["ng_regime"] = regime

                insert_ng_parity_log(log_data)
                log.info(
                    "NATURALGAS parity logged: regime=%s, dev_pct=%.2f%%",
                    regime,
                    parity_state.dev_pct,
                )
        except Exception as e:
            log.exception("NATURALGAS parity calculation failed")

    # Phase 2: Cache scan snapshot for ML prediction dashboard endpoint
    # This ensures the dashboard endpoint can hydrate full feature context
    # before predicting, producing identical results to the pipeline.
    try:
        update_scan_snapshot(symbol, scan_context)
    except Exception:
        log.debug("%s: scan snapshot caching failed gracefully", symbol)
    # Inject futures expiry (different from option chain expiry for MCX/NSE)
    try:
        from config.symbol_classes import get_futures_expiry
        from src.fetchers.dhan_commodity_fetcher import _get_open_futures_expiry

        near_fut_expiry = get_futures_expiry(symbol)
        open_fut_expiry = _get_open_futures_expiry(symbol)

        if open_fut_expiry:
            scan_context["futures_expiry"] = open_fut_expiry
        else:
            opt_expiry_str = oc_data.get("expiry")
            if opt_expiry_str and near_fut_expiry:
                try:
                    opt_dt = datetime.strptime(opt_expiry_str, "%Y-%m-%d").date()
                    near_fut_dt = datetime.strptime(near_fut_expiry, "%Y-%m-%d").date()
                    if (opt_dt.year > near_fut_dt.year) or (
                        opt_dt.year == near_fut_dt.year
                        and opt_dt.month > near_fut_dt.month
                    ):
                        from datetime import timedelta

                        next_fut_expiry = get_futures_expiry(
                            symbol, ref_date=near_fut_dt + timedelta(days=1)
                        )
                        scan_context["futures_expiry"] = next_fut_expiry
                    else:
                        scan_context["futures_expiry"] = near_fut_expiry
                except Exception:
                    scan_context["futures_expiry"] = near_fut_expiry
            else:
                scan_context["futures_expiry"] = near_fut_expiry
    except Exception:
        log.exception("Error calculating futures expiry in pipeline")
        scan_context["futures_expiry"] = None

    # Inject Index Heavyweight Sentiment if applicable
    scan_context["index_weights_sentiment"] = None
    if symbol in ("NIFTY", "BANKNIFTY", "SENSEX"):
        try:
            from src.engine.index_weights import calculate_index_momentum

            scan_context["index_weights_sentiment"] = calculate_index_momentum(symbol)
            log.info(
                "%s: index weights momentum calculated: %.3f%%",
                symbol,
                scan_context["index_weights_sentiment"].get("weighted_momentum"),
            )
        except Exception:
            log.exception(
                "%s: Failed to calculate index weights momentum in pipeline", symbol
            )
        except Exception:
            log.exception("%s: Failed to calculate index weights momentum in pipeline", symbol)

    log.info("%s: %d anomalies detected", symbol, len(alerts))

    # 1b. Severity filter + cap
    _sev_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    min_sev = _sev_order.get(ANOMALY_MIN_SEVERITY, 1)
    alerts = [a for a in alerts if _sev_order.get(a.get("severity", "LOW"), 2) <= min_sev]
    if len(alerts) > MAX_ANOMALIES_PER_SYMBOL:
        alerts = sorted(alerts, key=lambda a: _sev_order.get(a.get("severity", "LOW"), 2))[:MAX_ANOMALIES_PER_SYMBOL]
        log.info("%s: capped to %d anomalies after severity filter", symbol, MAX_ANOMALIES_PER_SYMBOL)

    # 2. Dedup filter
    new_alerts = [a for a in alerts if not is_duplicate(a)]
    dedup_suppressed = max(0, len(alerts) - len(new_alerts))
    if dedup_suppressed:
        log.info(
            "%s: detected=%d | new=%d | dedup_suppressed=%d",
            symbol,
            len(alerts),
            len(new_alerts),
            dedup_suppressed,
        )

    # 3. Build digest
    try:
        intel = generate_intelligence_structured(
            symbol, new_alerts, scan_context=scan_context
        )
        results["intel"] = intel
    except Exception as e:
        log.exception(
            "%s: intelligence generation failed with exception: %s",
            symbol,
            str(e)[:500],
        )
        intel = None

    if not intel:
        log.warning("%s: intel is None, using fallback digest", symbol)
        intel_text = f"⚠️ Intelligence generation failed for {symbol}"
    else:
        intel_text = intel.get("telegram_text", "")

    # 3ai. Phase 1: Trade DNA match — find similar historical trades
    if intel:
        try:
            analyzer = get_analyzer()  # singleton, no per-scan cost
            ist_hour = (datetime.now(timezone.utc) + IST_OFFSET).hour
            dna_context = {
                "symbol": symbol,
                "verdict_label": intel.get("verdict_label"),
                "confidence": intel.get("confidence", 0),
                "ist_hour": ist_hour,
            }
            dna_match = analyzer.get_trade_dna_match(dna_context)
            intel["trade_dna"] = dna_match
            if dna_match.get("match_found"):
                log.info(
                    "%s: Trade DNA — %d similar trades, historical WR=%.1f%%, avg PnL=₹%.0f",
                    symbol,
                    dna_match["similar_trades"],
                    dna_match["historical_win_rate"] * 100,
                    dna_match["avg_pnl"],
                )
        except Exception:
            log.debug("%s: Trade DNA lookup failed gracefully", symbol)

    # 3aii. Phase 2: ML Success Predictor — get P(trade profitable)
    # v2.2 FIX: Use singleton instead of re-instantiating per scan cycle.
    # TradeSuccessPredictor.__init__() calls _load_model() which loads XGBoost
    # from disk (~50-100ms) and SHAP TreeExplainer init (~50-200ms). At 5 symbols
    # every 3 minutes, that's 1-2.5 seconds of disk I/O per cycle.
    if intel:
        try:
            from src.intelligence.ml_predictor import get_predictor

            ml_predictor = get_predictor()  # Module-level singleton, loaded once
            ml_prediction = ml_predictor.predict(
                {
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
                    "days_to_expiry": None,  # Will be computed from expiry in scan_context
                    "rsi_1h": None,  # From chart indicators
                    "rsi_3h": None,  # From chart indicators
                    "regime": scan_context.get("market_regime"),
                    # v2.0 FIX: Pass opened_at for correct time features
                    "opened_at": scan_context.get("fetched_at"),
                }
            )
            if ml_prediction:
                intel["ml_prediction"] = {
                    "success_probability": ml_prediction.success_probability,
                    "confidence_level": ml_prediction.confidence_level,
                    "top_factors": ml_prediction.top_factors,
                    "model_version": ml_prediction.model_version,
                    "training_samples": ml_prediction.training_samples,
                }
                log.info(
                    "[ML] %s: P(success) = %.1f%% (confidence: %s)",
                    symbol,
                    ml_prediction.success_probability * 100,
                    ml_prediction.confidence_level,
                )
        except ImportError:
            log.debug(
                "%s: ML predictor not available (xgboost/sklearn not installed)", symbol
            )
        except Exception:
            log.debug("%s: ML prediction failed gracefully", symbol)

    # 3a. Fetch news data for AI context
    # NSE index symbols (NIFTY/BANKNIFTY) use FII/DII positioning from
    # fii_positioning table as the directional sentiment signal — not news articles.
    # News is still fetched for MCX commodities (NATURALGAS, CRUDEOIL, etc.).
    news_data = None
    _norm = symbol.upper().strip().split()[0]
    _skip_news_for_fii = _norm in ("NIFTY", "BANKNIFTY")
    if _skip_news_for_fii:
        log.debug("%s: skipping news fetch — FII/DII positioning used instead", symbol)
    else:
        try:
            from src.fetchers.news_fetcher import fetch_news

            news_data = fetch_news(symbol)
            if news_data and news_data.get("count_24h", 0) > 0:
                log.info(
                    "%s: news fetched — %d articles, direction: %s",
                    symbol,
                    news_data["count_24h"],
                    news_data.get("current_news_direction"),
                )
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

    # 3c. AI Enrichment (LLM — Deep Context)
    # ADR-007 §3 A3: Async enrichment — entry verdict deferred to background thread if LLM_ENRICHMENT_ASYNC=True
    from config.runtime_config import load_runtime_config
    rconf = load_runtime_config()
    is_async_mode = rconf.get("llm_enrichment_async", LLM_ENRICHMENT_ASYNC)

    DISABLE_LLM_ENRICHMENT = _DISABLE_LLM_ENV
    intel_text_base = intel_text
    llm_verdict = None
    exit_advice = None
    _async_llm_pending = False

    if DISABLE_LLM_ENRICHMENT:
        log.info("%s: LLM enrichment disabled (DISABLE_LLM_ENRICHMENT=true)", symbol)
    elif intel:
        try:
            if open_trade:
                log.debug(
                    "%s: open position exists — skipping LLM entry verdict, exit advisor will run",
                    symbol,
                )
            elif is_async_mode:
                _async_llm_pending = True
                intel_text += "\n💡 *Thesis:* ⏳ Pending async analysis...\n"
                log.debug("%s: async LLM enrichment mode — verdict deferred to background thread", symbol)
            else:
                from src.engine.llm_enrichment import get_llm_verdict
                llm_verdict = get_llm_verdict(
                    symbol,
                    intel,
                    scan_context,
                    alerts=new_alerts,
                    news_data=news_data,
                    open_trade=None,
                )
                results["llm_verdict"] = llm_verdict
                if llm_verdict:
                    intel = generate_intelligence_structured(
                        symbol,
                        new_alerts,
                        scan_context=scan_context,
                        ai_verdict=llm_verdict,
                    )
                    results["intel"] = intel
                    intel_text = intel.get("telegram_text", "") if intel else intel_text

                    log.info(
                        "%s: AI verdict — %s (%d%%) risk=%s | Instrument: %s",
                        symbol,
                        llm_verdict.action,
                        llm_verdict.confidence,
                        llm_verdict.risk_rating,
                        llm_verdict.instrument,
                    )

                    action_emoji = {
                        "GO_LONG": "🟢",
                        "GO_SHORT": "🔴",
                        "NO_TRADE": "⚪",
                    }.get(llm_verdict.action, "❓")
                    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(
                        llm_verdict.risk_rating, "❓"
                    )

                    intel_text += f"\n\n{action_emoji} *AI Trade Plan* ({llm_verdict.action}, {llm_verdict.confidence}%)\n"
                    intel_text += f"📋 *Contract:* `{llm_verdict.instrument}`\n"
                    intel_text += f"🎯 *Entry:* {llm_verdict.entry_trigger}\n"
                    intel_text += f"💰 *Premium:* {llm_verdict.entry_premium_range}\n"
                    intel_text += f"🛑 *SL:* {llm_verdict.stop_loss}\n"
                    intel_text += (
                        f"🎯 *T1:* {llm_verdict.target_1} | *T2:* {llm_verdict.target_2}\n"
                    )
                    intel_text += f"📊 *R:R:* {llm_verdict.risk_reward} | {risk_emoji} *Risk:* {llm_verdict.risk_rating}\n"
                    intel_text += f"💡 *Thesis:* {llm_verdict.thesis}\n"
                    intel_text += f"⚠️ *Invalidation:* {llm_verdict.invalidation}\n"
                    if llm_verdict.catalyst and llm_verdict.catalyst != "No major catalyst":
                        intel_text += f"📅 *Catalyst:* {llm_verdict.catalyst}\n"
        except Exception:
            log.exception("%s: AI enrichment failed gracefully", symbol)

    # 3d. AI Exit Advisor — evaluate open paper trades
    try:
        from config.runtime_config import load_runtime_config

        rconf = load_runtime_config()
        ai_exit_advisor_enabled = rconf.get("live_ai_exit_advisor_enabled", False)
        if (
            ai_exit_advisor_enabled
            and open_trade
            and not is_test
            and is_market_open(symbol)
        ):
            from src.engine.llm_enrichment import get_exit_advice

            exit_advice = get_exit_advice(symbol, open_trade, scan_context, news_data)
            results["exit_advice"] = exit_advice
            if exit_advice:
                log.info(
                    "%s: AI exit advice — %s (urgency=%s): %s",
                    symbol,
                    exit_advice.action,
                    exit_advice.urgency,
                    exit_advice.reasoning,
                )
                if (
                    exit_advice.action == "TRAIL_SL"
                    and exit_advice.new_sl_premium is not None
                ):
                    from src.models.schema import get_conn

                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE paper_trades SET sl_premium=? WHERE id=? AND status='OPEN'",
                            (exit_advice.new_sl_premium, open_trade["id"]),
                        )
                    log.info(
                        "%s: AI trailed SL to %.2f", symbol, exit_advice.new_sl_premium
                    )
                    intel_text += f"\n🤖 *AI Trail*: SL moved to ₹{exit_advice.new_sl_premium:.2f} — {exit_advice.reasoning}\n"
                elif (
                    exit_advice.action == "CLOSE_EARLY"
                    and exit_advice.urgency == "HIGH"
                ):
                    # FIX #9 + M4: use real current LTP only. If LTP is unavailable,
                    # SKIP the close entirely — closing at entry_premium forces P&L=0
                    # regardless of whether the trade was actually profitable.
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
                        close_paper_trade(
                            open_trade["id"],
                            datetime.now(timezone.utc).isoformat(),
                            scan_context.get("underlying", 0),
                            exit_premium,
                            "AI_CLOSE_EARLY",
                            f"AI exit: {exit_advice.reasoning}",
                        )
                        _invalidate_pattern_cache()  # Phase 1: refresh patterns after close
                        try:
                            from src.scheduler.ml_training_job import on_trade_closed

                            on_trade_closed()
                        except Exception:
                            pass  # Phase 2: increment ML retraining counter
                        # Phase 3: Check edge health after trade close
                        _check_edge_health_and_trigger_retrain()
                        log.info(
                            "%s: AI closed trade early at LTP=%.2f — %s",
                            symbol,
                            exit_premium,
                            exit_advice.reasoning,
                        )
                        intel_text += f"\n🤖 *AI Close* @ ₹{exit_premium:.2f}: {exit_advice.reasoning}\n"
                    else:
                        # M4: Skip CLOSE_EARLY when LTP unavailable — don't force zero-P&L exit
                        log.warning(
                            "%s: AI CLOSE_EARLY SKIPPED — current LTP unavailable for "
                            "strike=%.2f %s. Will retry next scan instead of closing at "
                            "entry_premium (which would force P&L=0).",
                            symbol,
                            open_trade.get("strike", 0),
                            open_trade.get("option_type", ""),
                        )
                        intel_text += f"\n🤖 *AI Close deferred*: LTP unavailable for {open_trade.get('option_type')} {open_trade.get('strike')} — will retry next scan\n"
    except Exception:
        log.debug("%s: AI exit advisor failed gracefully", symbol)

    # ────────────────────────────────────────────────────────────────────────
    # 3e. Pipeline Decision Matrix (PDM)
    # Runs after all signal sources are resolved (OI, PCR, chart, regime,
    # ML prediction, LLM verdict, Trade DNA).  Produces an auditable composite
    # GO/NO-GO decision with per-signal breakdown.
    # ────────────────────────────────────────────────────────────────────────
    pdm_result = None
    try:
        from src.engine.decision_matrix import evaluate as pdm_evaluate

        pdm_result = pdm_evaluate(
            symbol=symbol,
            new_alerts=new_alerts,
            scan_context=scan_context,
            intel=intel,
            llm_verdict=llm_verdict,
        )

        # Inject serialisable summary into scan_context for scan_summary + LLM prompts
        scan_context["decision_matrix"] = {
            "direction":        pdm_result.direction,
            "composite_score":  pdm_result.composite_score,
            "strength":         pdm_result.strength,
            "confidence_band":  pdm_result.confidence_band,
            "gate_pass":        pdm_result.gate_pass,
            "gate_reason":      pdm_result.gate_reason,
            "signals": [
                {
                    "name":          s.name,
                    "raw_score":     s.raw_score,
                    "weight":        s.weight,
                    "weighted_score":s.weighted_score,
                    "detail":        s.detail,
                }
                for s in pdm_result.signals
            ],
        }

        # Mirror into intel so downstream digest/LLM prompts can reference it
        if intel:
            intel["decision_matrix"] = scan_context["decision_matrix"]

        # Append Telegram block when gate fails (operator needs to see WHY)
        # or when confidence is HIGH (strong signal worth surfacing)
        if not pdm_result.gate_pass or pdm_result.confidence_band == "HIGH":
            intel_text += pdm_result.telegram_block

    except Exception:
        log.debug("%s: Pipeline Decision Matrix failed gracefully", symbol)

    import uuid

    digest_id = str(uuid.uuid4())[:8]
    paper_trade_report = None
    live_trade_report = None
    if is_test:
        log.info("%s: [dry-run] Skipping paper and live trading executions", symbol)
    else:
        try:
            from src.engine.strategy_registry import active_strategies_for, get_runner

            # Registry-driven dispatch (replaces hardcoded run_paper_trading +
            # run_timeframe_strategy calls). Order follows KNOWN_STRATEGY_IDS
            # (CORE, TIMEFRAME, TFSS) so CORE continues to take precedence over
            # TIMEFRAME on a tie — identical to the previous `pt_report or tf_report`
            # fallback behaviour below.
            strategy_reports: dict = {}
            for sid in active_strategies_for(symbol):
                runner = get_runner(sid)
                if runner is None:
                    continue
                strategy_reports[sid] = runner(
                    symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict
                )

            paper_trade_report = None
            for report in strategy_reports.values():
                if report and report.get("action") in ("EXECUTED", "CLOSED"):
                    paper_trade_report = report
                    break
            if paper_trade_report is None:
                paper_trade_report = next(
                    (r for r in strategy_reports.values() if r), None
                )

            # Phase 2: Trigger ML retraining counter when a trade closes
            # on_trade_closed() increments the trade counter and triggers
            # training if 20+ new trades have accumulated since last training.
            if paper_trade_report and paper_trade_report.get("action") == "CLOSED":
                try:
                    from src.scheduler.ml_training_job import on_trade_closed

                    on_trade_closed()
                except Exception:
                    log.debug("%s: ML training counter increment failed", symbol)
                # Phase 3: Check edge health after trade close and trigger
                # ML retraining if the edge is declining (health < 60).
                _check_edge_health_and_trigger_retrain()
        except Exception:
            log.exception("%s: paper-trading engine failed", symbol)

        try:
            lt_report = None
            lt_tf_report = None
            active_strats = active_strategies_for(symbol)
            if "CORE" in active_strats:
                lt_report = run_live_trading(
                    symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict
                )
            if "TIMEFRAME" in active_strats:
                lt_tf_report = run_live_timeframe_strategy(
                    symbol, scan_context, digest_id, intel, ai_verdict=llm_verdict
                )
            if lt_report and lt_report.get("action") in ("EXECUTED", "CLOSED"):
                live_trade_report = lt_report
            elif lt_tf_report and lt_tf_report.get("action") in ("EXECUTED", "CLOSED"):
                live_trade_report = lt_tf_report
            else:
                live_trade_report = lt_report or lt_tf_report
        except Exception:
            log.exception("%s: live-trading engine failed", symbol)

    # Simulate paper trade status in test mode or when strategy is disabled but decision is triggered
    if paper_trade_report is None and llm_verdict:
        td = (intel or {}).get("trade_decision") or {}
        td_status = td.get("status")
        if td_status in ("TRIGGERED", "TRIGGERED_EXPERIMENTAL"):
            def gv(key, default=""):
                if isinstance(llm_verdict, dict):
                    return llm_verdict.get(key, default)
                return getattr(llm_verdict, key, default) if llm_verdict else default

            instr = gv("instrument") or symbol
            opt = ""
            if "PE" in instr.upper():
                opt = "PE"
            elif "CE" in instr.upper():
                opt = "CE"
            elif "FUT" in instr.upper():
                opt = "FUT"

            strike_val = None
            import re
            clean_instr = re.sub(r"\b\d+\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\b", "", instr, flags=re.IGNORECASE)
            strike_m = re.search(r"\b(\d+(?:\.\d+)?)\b", clean_instr)
            if strike_m:
                try:
                    strike_val = float(strike_m.group(1))
                except:
                    pass

            entry_premium = None
            range_str = gv("entry_premium_range") or ""
            if "-" in range_str:
                try:
                    entry_premium = float(range_str.split("-")[0].strip())
                except:
                    pass
            elif range_str:
                try:
                    entry_premium = float(range_str.strip())
                except:
                    pass

            sl_val = None
            sl_str = gv("stop_loss") or ""
            is_premium_sl = "PREMIUM" in sl_str.upper()
            m = re.search(r"(\d+(?:\.\d+)?)", sl_str)
            if m:
                try:
                    sl_val = float(m.group(1))
                except:
                    pass

            t1_val = None
            t1_str = gv("target_1") or ""
            is_premium_t1 = "PREMIUM" in t1_str.upper()
            m = re.search(r"(\d+(?:\.\d+)?)", t1_str)
            if m:
                try:
                    t1_val = float(m.group(1))
                except:
                    pass

            from src.engine.strategy_registry import active_strategies_for
            active_strats = active_strategies_for(symbol)

            if is_test:
                action_type = "DRY_RUN_EXECUTED"
            elif not active_strats:
                action_type = "WOULD_EXECUTE"
            else:
                action_type = None

            if action_type:
                paper_trade_report = {
                    "action": action_type,
                    "trade": {
                        "option_type": opt,
                        "strike": strike_val,
                        "side": "BUY",
                        "entry_premium": entry_premium,
                        "sl_premium": sl_val if is_premium_sl else None,
                        "sl_underlying": sl_val if not is_premium_sl else None,
                        "target_premium": t1_val if is_premium_t1 else None,
                        "target_underlying": t1_val if not is_premium_t1 else None,
                    },
                    "reason": td.get("reason") or "AI Override triggered"
                }

    if intel:
        scan_context["trade_decision"] = intel.get("trade_decision")
        # Inject engine OI confidence so digest shows the rule-engine confidence,
        # not the LLM confidence (which may be 0 when LLM self-selects NO_TRADE).
        scan_context["engine_confidence"] = intel.get("confidence", 0)

    digest_id, digest_msg = build_digest(
        symbol,
        new_alerts,
        fetched_at,
        scan_context=scan_context,
        intelligence_text=intel_text,
        detected_count=len(alerts),
        dedup_suppressed_count=dedup_suppressed,
        digest_id=digest_id,
        paper_trade_status=paper_trade_report,
        live_trade_status=live_trade_report,
        llm_verdict=llm_verdict,
        exit_advice=exit_advice,
    )
    for a in new_alerts:
        a["digest_id"] = digest_id

    # 3b. Save scan summary
    if is_test:
        log.info("%s: [dry-run] Skipping scan summary save", symbol)
    else:
        try:
            if intel:
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
            else:
                log.warning(
                    "%s: skipping scan summary save due to missing intelligence data",
                    symbol,
                )
        except Exception:
            log.exception("%s: scan summary save failed", symbol)

    # 4. Send logic
    should_send = bool(new_alerts)
    if not should_send:
        diag = (scan_context or {}).get("diagnostics", {})
        max_oi = float(diag.get("max_oi_delta_pct") or 0)
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
            log.info(
                "%s: suppressed flat zero-signal scan (max_oi=%.2f%%)", symbol, max_oi
            )

    if is_test:
        digest_msg = f"⚠️ **TEST MODE** ⚠️\n\n{digest_msg}"
        log.info(
            "%s: [dry-run] Sending test digest to Telegram/Discord:\n%s",
            symbol,
            digest_msg,
        )
        send_text(digest_msg)
        sent_digest = False
        telegram_message_id = None
    else:
        if should_send:
            if _async_llm_pending:
                telegram_message_id = send_text_and_return_id(digest_msg)
                sent_digest = telegram_message_id is not None
                if sent_digest:
                    log.info("%s: digest v1 sent (msg_id=%d), launching async LLM thread", symbol, telegram_message_id)
                    thread = threading.Thread(
                        target=_async_llm_enrich_and_edit,
                        kwargs=dict(
                            symbol=symbol,
                            intel=intel,
                            scan_context=scan_context,
                            new_alerts=new_alerts,
                            news_data=news_data,
                            fetched_at=fetched_at,
                            digest_id=digest_id,
                            message_id=telegram_message_id,
                            paper_trade_report=paper_trade_report,
                            live_trade_report=live_trade_report,
                            dedup_suppressed=dedup_suppressed,
                            intel_text_base=intel_text_base,
                        ),
                        daemon=True,
                    )
                    thread.start()
                else:
                    log.warning("%s: digest v1 send failed, async LLM enrichment skipped", symbol)
            else:
                sent_digest = send_text(digest_msg)
                telegram_message_id = None
        else:
            sent_digest = False
            telegram_message_id = None

    # 5. Persist + record dedup
    if is_test:
        log.info("%s: [dry-run] Skipping alert and snapshot DB records", symbol)
    else:
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

        rows = [
            {
                "fetched_at": fetched_at,
                "symbol": symbol,
                "expiry": expiry,
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
                "fetcher_source": source,
            }
            for row in oc_data["strikes"]
        ]
        insert_snapshots(rows)
        log.info("%s: persisted %d rows (source: %s)", symbol, len(rows), source)
        # OPS Agent: stamp last scan per symbol
        try:
            from src.models.schema import stamp_health
            stamp_health(f"last_scan_{symbol}", "OK", f"source={source} price={underlying}")
        except Exception:
            pass

        # Fetch and save next-expiry data when DTE <= 2
        if is_test:
            log.info("%s: [dry-run] Skipping next-expiry fetching/saving", symbol)
        else:
            all_expiries = oc_data.get("all_expiries", [])
            if expiry and all_expiries:
                try:
                    # Explicit import to avoid scoping issues in Python 3.11+
                    from datetime import datetime as dt_class

                    import pytz

                    exp_date = dt_class.strptime(expiry, "%Y-%m-%d").date()
                    IST = pytz.timezone("Asia/Kolkata")
                    today_ist = dt_class.now(IST).date()
                    dte = (exp_date - today_ist).days

                    if 0 <= dte <= 2:
                        try:
                            idx = all_expiries.index(expiry)
                        except ValueError:
                            # Expiry not in list, skip next-expiry fetch
                            log.debug(
                                "%s: Expiry %s not found in all_expiries; skipping next-expiry fetch",
                                symbol,
                                expiry,
                            )
                        else:
                            if idx + 1 < len(all_expiries):
                                next_expiry = all_expiries[idx + 1]
                                log.info(
                                    "%s: Active expiry %s has DTE = %d. Fetching next-expiry %s data to DB.",
                                    symbol,
                                    expiry,
                                    dte,
                                    next_expiry,
                                )

                                next_oc_data = fetch_option_chain(
                                    symbol, expiry=next_expiry
                                )
                                if next_oc_data and next_oc_data.get("strikes"):
                                    next_underlying = next_oc_data["underlying_price"]
                                    next_source = next_oc_data.get("source", "unknown")

                                    next_rows = [
                                        {
                                            "fetched_at": fetched_at,
                                            "symbol": symbol,
                                            "expiry": next_expiry,
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
                                            "underlying_price": next_underlying,
                                            "fetcher_source": next_source,
                                        }
                                        for row in next_oc_data["strikes"]
                                    ]

                                    num_inserted = insert_snapshots(next_rows)
                                    log.info(
                                        "%s: Next-expiry (%s) saved: %d rows inserted",
                                        symbol,
                                        next_expiry,
                                        num_inserted,
                                    )
                except Exception as next_exc:
                    log.warning(
                        "%s: Failed to fetch/save next-expiry data: %s",
                        symbol,
                        next_exc,
                    )
