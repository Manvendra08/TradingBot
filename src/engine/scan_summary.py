"""
Scan Summary Engine — saves one row per scan to scan_summaries table.
Foundation for multi-scan trend analysis (Phase 2+).
"""
from __future__ import annotations

import json
import logging

from src.models.schema import get_conn

log = logging.getLogger(__name__)


def save_scan_summary(
    symbol: str,
    scan_context: dict,
    alerts: list[dict],
    intel: dict,
    digest_id: str,
    fetched_at: str,
) -> None:
    """
    Save one row per scan. intel must be the structured dict from
    generate_intelligence_structured() — not raw Telegram text.
    """
    ctx = scan_context or {}
    verdict_label = intel.get("verdict_label", "")
    confidence = int(intel.get("confidence") or 0)

    chart_data = ctx.get("chart_indicators") or {}
    # chart_data may be keyed by symbol or directly by timeframe
    tf_data = chart_data
    if verdict_label and not any(k in chart_data for k in ("1h", "3h")):
        # symbol-keyed: unwrap first value
        tf_data = next(iter(chart_data.values()), {}) if chart_data else {}
    candle_1h = (tf_data.get("1h") or {}).get("sentiment", "NEUTRAL")
    candle_3h = (tf_data.get("3h") or {}).get("sentiment", "NEUTRAL")

    top = _find_top_signal(alerts)

    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO scan_summaries (
                    symbol, expiry, fetched_at, digest_id,
                    underlying, atm_strike, total_ce_oi, total_pe_oi,
                    ce_oi_change, pe_oi_change, pcr, max_pain, support, resistance,
                    verdict_label, confidence, candle_1h, candle_3h,
                    top_signal_type, top_signal_strike, top_signal_option_type,
                    top_signal_severity, top_signal_oi_pct
                ) VALUES (
                    ?,?,?,?,
                    ?,?,?,?,
                    ?,?,?,?,?,?,
                    ?,?,?,?,
                    ?,?,?,?,?
                )
                """,
                (
                    symbol, ctx.get("expiry"), fetched_at, digest_id,
                    ctx.get("underlying"), ctx.get("atm_strike"),
                    ctx.get("total_ce_oi"), ctx.get("total_pe_oi"),
                    ctx.get("ce_oi_change"), ctx.get("pe_oi_change"),
                    ctx.get("pcr"), ctx.get("max_pain"),
                    ctx.get("support"), ctx.get("resistance"),
                    verdict_label, confidence, candle_1h, candle_3h,
                    top.get("type"), top.get("strike"), top.get("option_type"),
                    top.get("severity"), top.get("oi_pct"),
                ),
            )
        log.info("%s: scan summary saved | verdict=%s conf=%d", symbol, verdict_label, confidence)
    except Exception:
        log.exception("%s: scan summary save failed", symbol)


def _find_top_signal(alerts: list[dict]) -> dict:
    if not alerts:
        return {}
    sev_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

    def _score(a: dict) -> tuple:
        sev = sev_order.get(a.get("severity", "LOW"), 0)
        try:
            detail = json.loads(a.get("detail_json") or "{}")
            oi_pct = abs(float(detail.get("pct_change", 0)))
        except Exception:
            oi_pct = 0.0
        return (sev, oi_pct)

    top = max(alerts, key=_score)
    try:
        detail = json.loads(top.get("detail_json") or "{}")
        oi_pct = abs(float(detail.get("pct_change", 0)))
    except Exception:
        oi_pct = 0.0

    return {
        "type": top.get("alert_type"),
        "strike": top.get("strike"),
        "option_type": top.get("option_type"),
        "severity": top.get("severity"),
        "oi_pct": oi_pct,
    }
