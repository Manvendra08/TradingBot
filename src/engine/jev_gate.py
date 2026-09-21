"""
Jev Fast Gate  (System-1 pre-LLM conviction filter)
====================================================
Runs a lightweight TypeSafe AI System-1 call (~70-500 ms, $0.042/M tokens)
*before* the heavy Claude/OmniRouter enrichment call.

If Jev reports low conviction (``tradeable_setup`` Noul probability < threshold),
the caller can skip heavy LLM enrichment entirely — saving latency and cost.

Public API::

    from src.engine.jev_gate import jev_fast_gate, JevResult

    result = jev_fast_gate(symbol, scan_context, intel, news_data)
    if not result.proceed:
        # skip heavy LLM
        ...
    # result.direction  → "BULLISH" | "BEARISH" | "NEUTRAL" | None
    # result.conviction → float 0.0-1.0
    # result.skipped    → True if Jev was not called (key not set, etc.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Minimum System-1 conviction to allow heavy LLM enrichment.
# Below this value the scan is treated as low-conviction and LLM is skipped.
_CONVICTION_FLOOR = 0.35

# Intel keys that indicate a strong OI-engine signal.
# When these are present we trust the OI engine and always proceed.
_STRONG_OI_KEYS = ("oi_buildup", "breakout_direction", "reversal_signal")


@dataclass(slots=True)
class JevResult:
    proceed: bool          # True → run heavy LLM; False → skip
    direction: str | None  # BULLISH | BEARISH | NEUTRAL
    conviction: float      # System-1 tradeable_setup probability (0.0-1.0)
    skipped: bool          # True when Jev was not called at all


def _build_state_text(
    symbol: str,
    scan_context: dict[str, Any],
    intel: dict[str, Any] | None,
    news_data: dict[str, Any] | None,
) -> str:
    """Compose a concise market state string for the System-1 prompt."""
    parts: list[str] = [f"Symbol: {symbol}"]

    underlying = (
        scan_context.get("underlying")
        or scan_context.get("underlying_price")
        or (scan_context.get("oc_data") or {}).get("underlying_price")
    )
    if underlying:
        parts.append(f"Spot: {underlying}")

    pcr = scan_context.get("pcr") or scan_context.get("put_call_ratio")
    if pcr:
        parts.append(f"PCR: {pcr:.2f}" if isinstance(pcr, float) else f"PCR: {pcr}")

    if intel:
        td = intel.get("trade_decision") or intel.get("direction")
        if td:
            parts.append(f"OI direction: {td}")
        conf = intel.get("confidence")
        if conf is not None:
            parts.append(f"OI confidence: {conf}%")
        sentiment = intel.get("sentiment") or intel.get("oi_sentiment")
        if sentiment:
            parts.append(f"Sentiment: {sentiment}")

    diagnostics = scan_context.get("diagnostics") or {}
    max_oi = diagnostics.get("max_oi_delta_pct")
    if max_oi is not None:
        parts.append(f"Max OI delta: {max_oi:.1f}%" if isinstance(max_oi, float) else f"Max OI delta: {max_oi}%")

    if news_data and not (news_data or {}).get("bypassed"):
        headlines = news_data.get("headlines") or news_data.get("articles") or []
        if headlines:
            top = headlines[0]
            title = top.get("title") or top.get("headline") or str(top)
            parts.append(f"Top news: {title[:120]}")

    return ". ".join(parts) + "."


def jev_fast_gate(
    symbol: str,
    scan_context: dict[str, Any],
    intel: dict[str, Any] | None,
    news_data: dict[str, Any] | None,
    conviction_floor: float = _CONVICTION_FLOOR,
) -> JevResult:
    """Run Jev System-1 pre-LLM gate and return a JevResult.

    Always returns a JevResult — never raises.  If Jev is unavailable
    (key not set, timeout, error), ``skipped=True`` and ``proceed=True``
    so the pipeline behaves as if Jev doesn't exist.

    Strong OI-engine signals bypass the conviction floor: if the intel dict
    already contains a decisive direction we trust the OI engine and always
    proceed regardless of Jev's answer.
    """
    from config.settings import TYPESAFE_API_KEY  # late import

    if not TYPESAFE_API_KEY:
        return JevResult(proceed=True, direction=None, conviction=1.0, skipped=True)

    # Strong OI signal override — always proceed, no need to ask Jev
    if intel:
        for key in _STRONG_OI_KEYS:
            if intel.get(key):
                log.debug("jev: strong OI signal '%s' on %s — bypassing Jev gate", key, symbol)
                return JevResult(proceed=True, direction=intel.get("trade_decision"), conviction=1.0, skipped=True)

    try:
        from src.services.typesafe_client import evaluate_system_one

        state = _build_state_text(symbol, scan_context, intel, news_data)
        questions: dict[str, Any] = {
            "tradeable_setup": {
                "type": "noul",
                "instructions": "Does this market state represent a high-probability tradeable setup worth detailed options analysis?",
            },
            "direction": {
                "type": "choice",
                "instructions": "What is the dominant near-term market direction indicated by the data?",
                "criteria": {
                    "BULLISH": "Upward momentum, call buying or put writing support",
                    "BEARISH": "Downward momentum, put buying or call writing resistance",
                    "NEUTRAL": "Rangebound, consolidation, or mixed signals",
                },
            },
        }

        answers = evaluate_system_one(state, questions, timeout=3.0)
        if answers is None:
            log.debug("jev: no answer for %s — proceeding with LLM", symbol)
            return JevResult(proceed=True, direction=None, conviction=1.0, skipped=True)

        ts_answer = answers.get("tradeable_setup") or {}
        direction_answer = answers.get("direction") or {}

        # TypeSafe NoulAnswer: {"type": "noul", "noul": 0.56} -> probability 0.0 to 1.0
        ts_prob = float(ts_answer.get("noul") if "noul" in ts_answer else (ts_answer.get("probability") or 0.5))
        # TypeSafe ChoiceAnswer: {"type": "choice", "choice": "BEARISH", "confidence": 0.99, ...}
        direction_val = direction_answer.get("choice") or direction_answer.get("value")

        conviction = ts_prob
        proceed = conviction >= conviction_floor

        log.info(
            "jev: %s tradeable conviction=%.2f (floor=%.2f) direction=%s → %s",
            symbol, conviction, conviction_floor, direction_val,
            "PROCEED" if proceed else "SKIP_LLM",
        )
        return JevResult(proceed=proceed, direction=direction_val, conviction=conviction, skipped=False)

    except Exception as exc:  # noqa: BLE001
        log.debug("jev: gate error for %s: %s — proceeding with LLM", symbol, exc)
        return JevResult(proceed=True, direction=None, conviction=1.0, skipped=True)
