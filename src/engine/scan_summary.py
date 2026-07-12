"""
Scan Summary Engine — saves one row per scan to scan_summaries table.
Foundation for multi-scan trend analysis (Phase 2+).

B5 fix: accept is_fallback kwarg; route through insert_scan_summary() which
        sets the is_fallback column so regime_detector can exclude stale rows.
"""
from __future__ import annotations

import logging

from src.models.schema import insert_scan_summary as _db_insert_scan_summary

log = logging.getLogger(__name__)


def save_scan_summary(
    symbol: str,
    scan_context: dict,
    alerts: list[dict],
    intel: dict,
    digest_id: str,
    fetched_at: str,
    *,
    is_fallback: bool = False,
    llm_verdict: dict | None = None,
) -> None:
    """
    Save one row per scan. intel must be the structured dict from
    generate_intelligence_structured() — not raw Telegram text.

    is_fallback: True when the underlying price is a stale fallback value
                 (no live price was available). These rows are excluded from
                 regime classification to prevent regime poisoning.
    """
    ctx = scan_context or {}
    verdict_label = intel.get("verdict_label", "")
    confidence = int(intel.get("confidence") or 0)

    if llm_verdict:
        action = llm_verdict.get("action") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "action", "")
        if action and action not in ("NO_TRADE", "NEUTRAL"):
            llm_conf = llm_verdict.get("confidence") if isinstance(llm_verdict, dict) else getattr(llm_verdict, "confidence", 0)
            if llm_conf:
                confidence = int(llm_conf)

    chart_data = ctx.get("chart_indicators") or {}
    if isinstance(chart_data, dict) and not any(k in chart_data for k in ("1h", "3h")):
        chart_data = next(iter(chart_data.values()), {}) if chart_data else {}
    candle_1h = (chart_data.get("1h") or {}).get("sentiment", "NEUTRAL")
    candle_3h = (chart_data.get("3h") or {}).get("sentiment", "NEUTRAL")

    top = _find_top_signal(alerts)

    summary = {
        "symbol":                symbol,
        "expiry":                ctx.get("expiry"),
        "fetched_at":            fetched_at,
        "digest_id":             digest_id,
        "underlying":            ctx.get("underlying"),
        "atm_strike":            ctx.get("atm_strike"),
        "total_ce_oi":           ctx.get("total_ce_oi"),
        "total_pe_oi":           ctx.get("total_pe_oi"),
        "ce_oi_change":          ctx.get("ce_oi_change"),
        "pe_oi_change":          ctx.get("pe_oi_change"),
        "pcr":                   ctx.get("pcr"),
        "max_pain":              ctx.get("max_pain"),
        "support":               ctx.get("support"),
        "resistance":            ctx.get("resistance"),
        "verdict_label":         verdict_label,
        "confidence":            confidence,
        "candle_1h":             candle_1h,
        "candle_3h":             candle_3h,
        "top_signal_type":       top.get("type"),
        "top_signal_strike":     top.get("strike"),
        "top_signal_option_type": top.get("option_type"),
        "top_signal_severity":   top.get("severity"),
        "top_signal_oi_pct":     top.get("oi_pct"),
        "trend_bias":            ctx.get("trend_bias"),
        "trend_strength":        ctx.get("trend_strength"),
        "market_regime":         ctx.get("market_regime"),
        # TFSS v4 execution metadata (plan §4.11)
        "execution_source":      ctx.get("_execution_source", ""),
        "tfss_bias":             ctx.get("_tfss_bias", ""),
        "tfss_execution_side":   ctx.get("_tfss_execution_side", ""),
        "tfss_persistence":      ctx.get("_tfss_persistence", ""),
    }

    try:
        _db_insert_scan_summary(summary, is_fallback=is_fallback)
        log.info("%s: scan summary saved | verdict=%s conf=%d fallback=%s",
                 symbol, verdict_label, confidence, is_fallback)
    except Exception:
        log.exception("%s: scan summary save failed", symbol)


def _find_top_signal(alerts: list[dict]) -> dict:
    import json
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
        import json as _json
        detail = _json.loads(top.get("detail_json") or "{}")
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
