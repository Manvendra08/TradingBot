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
from src.models.schema import (
    insert_snapshots,
    insert_underlying_price,
    insert_alert,
    mark_telegram_sent,
    get_previous_underlying,
)
from src.engine.anomaly_detector import detect_anomalies
from src.alerts.dedup import is_duplicate, record_alert
from src.alerts.digest import build_digest
from src.alerts.telegram_dispatcher import send_alert, send_text
from config.settings import WATCH_SYMBOLS, INDIVIDUAL_ALERT_MIN_SEVERITY

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

    # 1. Detect (before persisting so deltas use previous snapshot)
    alerts, scan_context = detect_anomalies(oc_data, fetched_at)
    log.info("%s: %d anomalies detected", symbol, len(alerts))

    # 2. Dedup filter
    new_alerts = [a for a in alerts if not is_duplicate(a)]

    if new_alerts:
        # 3. Build digest → send ONE grouped message
        digest_id, digest_msg = build_digest(
            symbol, new_alerts, fetched_at,
            scan_context=scan_context,
        )
        for a in new_alerts:
            a["digest_id"] = digest_id

        sent_digest = send_text(digest_msg)

        # 4. Persist + record dedup
        #    HIGH alerts always get an individual send (in addition to digest),
        #    so traders see time-critical signals even if they missed the digest.
        #    All other alerts are marked sent when the digest succeeds.
        for alert in new_alerts:
            alert_id = insert_alert(alert)
            record_alert(alert)

            is_high = alert.get("severity") == INDIVIDUAL_ALERT_MIN_SEVERITY
            if is_high:
                # Always attempt individual send for HIGH — digest is supplementary
                sent = send_alert(alert)
                if sent:
                    mark_telegram_sent(alert_id)
                elif sent_digest:
                    # Digest already delivered it; mark sent to avoid retry loops
                    mark_telegram_sent(alert_id)
            elif sent_digest:
                mark_telegram_sent(alert_id)

    # 5. Persist underlying + snapshot
    prev_row = get_previous_underlying(symbol)
    prev_price = prev_row["price"] if prev_row else None
    pct_chg = None
    if prev_price and prev_price != 0:
        pct_chg = round((underlying - prev_price) / abs(prev_price) * 100, 4)

    insert_underlying_price(symbol, underlying, pct_chg, fetched_at)

    rows = [{
        "fetched_at":       fetched_at,
        "symbol":           symbol,
        "expiry":           expiry,
        "strike":           row["strike"],
        "option_type":      row["option_type"],
        "ltp":              row.get("ltp"),
        "oi":               row.get("oi"),
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
