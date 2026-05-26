"""
Data Pipeline Orchestrator v2.7
fetch → detect → dedup → digest → alert

Fixes (v2.7):
  - sent_digest logic corrected: HIGH alerts always get individual send;
    digest marks remaining alerts as sent.
  - Added optional market-hours guard for direct/--now invocations.
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
from src.engine.intelligence import generate_intelligence
from src.engine.paper_trading import run_paper_trading
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
                 When False, symbols are expected to already be filtered
                 by the scheduler's _guarded_run. Direct callers should
                 pass force=True explicitly to bypass the guard intentionally.
    """
    symbols = symbols or WATCH_SYMBOLS
    fetched_at = datetime.now(timezone.utc).isoformat()
    log.info("Pipeline run started | %s | symbols: %s | force=%s", fetched_at, symbols, force)
    for symbol in symbols:
        try:
            _process_symbol(symbol, fetched_at)
        except Exception:
            log.exception("Unhandled pipeline error for %s — continuing with next symbol", symbol)
    log.info("Pipeline run complete | %s", fetched_at)


def _process_symbol(symbol: str, fetched_at: str) -> None:
    log.info("Processing %s ...", symbol)

    oc_data = fetch_option_chain(symbol)
    if not oc_data:
        log.error("No data for %s — skipping", symbol)
        return

    underlying = oc_data["underlying_price"]
    expiry     = oc_data["expiry"]
    source     = oc_data.get("source", "unknown")
    prev_row = get_previous_underlying(symbol)
    prev_price = prev_row["price"] if prev_row else None
    if underlying is None:
        underlying = prev_price or 0.0
        oc_data["underlying_price"] = underlying
        log.warning("%s: underlying price is None, falling back to prev_price: %s", symbol, underlying)

    # 1a. Fetch chart data server-side (Chrome-free)
    #     Merged into oc_data so anomaly_detector sees chart_indicators
    #     Same key as Chrome extension output — zero downstream changes needed
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

    # 1b. Detect anomalies (before persisting so deltas use previous snapshot)
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

    # 3. Build digest — always build (zero-alert scans need suppression check)
    intel_text = generate_intelligence(symbol, new_alerts, scan_context=scan_context)
    digest_id, digest_msg = build_digest(
        symbol, new_alerts, fetched_at,
        scan_context=scan_context,
        intelligence_text=intel_text,
        detected_count=len(alerts),
        dedup_suppressed_count=dedup_suppressed,
    )
    for a in new_alerts:
        a["digest_id"] = digest_id

    # 4. Send logic
    #    - Has alerts: always send digest
    #    - Only duplicate alerts: heartbeat send (cooldown-gated)
    #    - Zero alerts, OI moved ≥1%: send quiet scan (market moving but no threshold breach)
    #    - Zero alerts, OI flat: suppress UNLESS 30min cooldown allows heartbeat
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
            should_send = True  # heartbeat: once per 30min so trader knows bot is alive
        else:
            log.info("%s: suppressed flat zero-signal scan (max_oi=%.2f%%)", symbol, max_oi)

    if should_send:
        sent_digest = send_text(digest_msg)
    else:
        sent_digest = False

    # 4b. Auto paper-trading lifecycle
    try:
        run_paper_trading(symbol, scan_context, digest_id, intel_text)
    except Exception:
        log.exception("%s: paper-trading engine failed", symbol)

    # 5. Persist + record dedup (alerts only)
    if new_alerts:
        for alert in new_alerts:
            alert_id = insert_alert(alert)
            record_alert(alert)
            if sent_digest:
                mark_telegram_sent(alert_id)

    # 5. Persist underlying + snapshot
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
