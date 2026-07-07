"""
Unified trade planning and SL/Target calculation module.

This module provides a single source of truth for:
- SL/Target calculations (ATR-based)
- Option premium resolution
- Verdict parsing
- Common trade planning helpers

Both paper_trading.py and live_trading.py import from here to ensure
identical behavior and prevent divergence.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from src.models.schema import get_latest_snapshots_for_symbol

log = logging.getLogger(__name__)

# L2: Maximum age in seconds for DB snapshot fallback premiums.
# Snapshots older than this are rejected to prevent stale premium usage.
_DB_PREMIUM_MAX_AGE_SECONDS = 15 * 60  # 15 minutes


# ---------------------------------------------------------------------------
# ATR Extraction
# ---------------------------------------------------------------------------


def get_atr(ctx: dict, preferred_tf: str = "3h") -> Optional[float]:
    """
    Extract ATR-14 from chart_indicators, trying preferred_tf first.

    Args:
        ctx: Scan context dict with 'chart_indicators' key
        preferred_tf: Timeframe preference ('3h' or '1h')

    Returns:
        ATR value as float, or None if unavailable
    """
    chart_indicators = ctx.get("chart_indicators") or {}

    # Unwrap symbol-keyed wrapper if present: {"NATURALGAS": {"1h": ..., "3h": ...}}
    if not any(k in chart_indicators for k in ("1h", "3h", "atr_14")):
        for key, val in chart_indicators.items():
            if isinstance(val, dict) and any(k in val for k in ("1h", "3h", "atr_14")):
                chart_indicators = val
                break

    # 1. Try structured keys based on preference
    other_tf = "1h" if preferred_tf == "3h" else "3h"
    pay_pref = chart_indicators.get(preferred_tf) or {}
    pay_other = chart_indicators.get(other_tf) or {}
    atr = pay_pref.get("atr_14") or pay_other.get("atr_14")
    if atr is not None:
        return float(atr)

    # 2. Try any other timeframe under chart_indicators
    for tf, payload in chart_indicators.items():
        if isinstance(payload, dict) and payload.get("atr_14") is not None:
            return float(payload["atr_14"])

    # 3. Fallback: flat structure directly under chart_indicators
    if "atr_14" in chart_indicators:
        return float(chart_indicators["atr_14"])

    return None


# ---------------------------------------------------------------------------
# SL / Target Calculation — ATR-based (Unified)
# ---------------------------------------------------------------------------


def calculate_buy_sl_target(
    entry_premium: float,
    underlying: float,
    ctx: dict,
    step: float = 50.0,
    option_type: str = "CE",
    preferred_tf: str = "3h",
) -> tuple[float, float]:
    """
    ATR-based SL/Target for BUY legs.

    Uses ATR(14) on the underlying for stop-loss and target distances.
    Falls back to 2-step underlying distance when ATR unavailable.

    Args:
        entry_premium: Entry premium (used for context, not calculation)
        underlying: Current underlying price
        ctx: Scan context with chart_indicators
        step: Strike step size for fallback calculation
        option_type: CE/PE — PE inverts direction (profit when underlying falls)

    Returns:
        Tuple of (sl_underlying, target_underlying)
    """
    atr = get_atr(ctx, preferred_tf)
    if option_type == "PE":
        # PE: profit when underlying falls
        if atr and atr > 0:
            sl_underlying = underlying + 1.5 * atr
            target_underlying = underlying - 2.0 * atr
        else:
            log.warning(
                "calculate_buy_sl_target: Missing ATR data, using step-based fallback (step=%.1f)",
                step,
            )
            sl_underlying = underlying + 2.0 * step
            target_underlying = underlying - 2.0 * step
    else:
        # CE/FUT: profit when underlying rises
        if atr and atr > 0:
            sl_underlying = underlying - 1.5 * atr
            target_underlying = underlying + 2.0 * atr
        else:
            log.warning(
                "calculate_buy_sl_target: Missing ATR data, using step-based fallback (step=%.1f)",
                step,
            )
            sl_underlying = underlying - 2.0 * step
            target_underlying = underlying + 2.0 * step

    return round(sl_underlying, 2), round(target_underlying, 2)


def calculate_sell_sl_target(
    entry_premium: float,
    underlying: float,
    ctx: dict,
    step: float = 50.0,
    option_type: str = "CE",
    preferred_tf: str = "3h",
) -> tuple[float, float]:
    """
    ATR-based SL/Target for SELL legs.

    Uses ATR(14) on the underlying for stop-loss and target distances.
    Falls back to 2-step underlying distance when ATR unavailable.

    Args:
        entry_premium: Entry premium (used for context, not calculation)
        underlying: Current underlying price
        ctx: Scan context with chart_indicators
        step: Strike step size for fallback calculation
        option_type: CE/PE — PE inverts direction (profit when underlying rises)

    Returns:
        Tuple of (sl_underlying, target_underlying)
    """
    atr = get_atr(ctx, preferred_tf)
    if option_type == "PE":
        # PE: profit when underlying rises
        if atr and atr > 0:
            sl_underlying = underlying - 1.5 * atr
            target_underlying = underlying + 2.0 * atr
        else:
            log.warning(
                "calculate_sell_sl_target: Missing ATR data, using step-based fallback (step=%.1f)",
                step,
            )
            sl_underlying = underlying - 2.0 * step
            target_underlying = underlying + 2.0 * step
    else:
        # CE/FUT: profit when underlying falls
        if atr and atr > 0:
            sl_underlying = underlying + 1.5 * atr
            target_underlying = underlying - 2.0 * atr
        else:
            log.warning(
                "calculate_sell_sl_target: Missing ATR data, using step-based fallback (step=%.1f)",
                step,
            )
            sl_underlying = underlying + 2.0 * step
            target_underlying = underlying - 2.0 * step

    return round(sl_underlying, 2), round(target_underlying, 2)


# ---------------------------------------------------------------------------
# Option Premium Resolution
# ---------------------------------------------------------------------------


def get_option_premium(
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    option_rows: list[dict] | None = None,
) -> float | None:
    """
    Fetch current option premium (LTP) from option chain rows or database snapshots.

    Tries option_rows first (live data), then falls back to database snapshots.

    L2 fix: DB fallback rejects snapshots older than _DB_PREMIUM_MAX_AGE_SECONDS
    (15 minutes) to prevent stale premium usage in trade planning.

    Args:
        symbol: Trading symbol (e.g., "NIFTY")
        expiry: Expiry date string
        strike: Strike price
        option_type: "CE" or "PE"
        option_rows: List of option chain row dicts with 'strike', 'option_type', 'ltp'

    Returns:
        Premium as float, or None if unavailable or stale
    """
    # Try option_rows first (current scan data — always fresh)
    for row in option_rows or []:
        try:
            if (
                abs(float(row.get("strike") or 0) - strike) < 0.01
                and str(row.get("option_type") or "").upper() == option_type.upper()
            ):
                # Reject completely dead options to prevent placeholder/stale premiums
                # Only reject if volume AND oi are explicitly present and both 0
                vol = row.get("volume")
                oi = row.get("oi")
                if vol is not None and oi is not None:
                    if int(vol) == 0 and int(oi) == 0:
                        log.warning(
                            "%s: get_option_premium — strike=%.2f %s has 0 volume and 0 OI. Rejecting premium.",
                            symbol,
                            strike,
                            option_type,
                        )
                        return None
                ltp_raw = row.get("ltp")
                if ltp_raw is not None:
                    try:
                        return float(ltp_raw)
                    except ValueError:
                        return None
                return None
        except Exception:
            continue

    # Fallback: database snapshots with staleness check (L2)
    try:
        snapshots = get_latest_snapshots_for_symbol(symbol, expiry)
        for snap in snapshots:
            if (
                abs(snap.get("strike", 0) - strike) < 0.01
                and str(snap.get("option_type") or "").upper() == option_type.upper()
            ):
                # Reject completely dead options to prevent placeholder/stale premiums
                # Only reject if volume AND oi are explicitly present and both 0
                vol = snap.get("volume")
                oi = snap.get("oi")
                if vol is not None and oi is not None:
                    if int(vol) == 0 and int(oi) == 0:
                        log.warning(
                            "%s: get_option_premium (DB fallback) — strike=%.2f %s has 0 volume and 0 OI. Rejecting premium.",
                            symbol,
                            strike,
                            option_type,
                        )
                        return None
                # L2: Check snapshot freshness before using
                fetched_at_str = snap.get("fetched_at")
                if fetched_at_str:
                    try:
                        fetched_at = datetime.fromisoformat(
                            fetched_at_str.replace("Z", "+00:00")
                        )
                        age_seconds = (
                            datetime.now(timezone.utc) - fetched_at
                        ).total_seconds()
                        if age_seconds > _DB_PREMIUM_MAX_AGE_SECONDS:
                            log.warning(
                                "%s: DB premium fallback REJECTED — snapshot is %.0f min old "
                                "(max %d min). Strike=%.2f %s. Returning None.",
                                symbol,
                                age_seconds / 60,
                                _DB_PREMIUM_MAX_AGE_SECONDS // 60,
                                strike,
                                option_type,
                            )
                            return None
                    except (ValueError, TypeError):
                        # If we can't parse the timestamp, err on the side of caution
                        log.warning(
                            "%s: Could not parse fetched_at='%s' for DB premium staleness check",
                            symbol,
                            fetched_at_str,
                        )
                        return None

                ltp_raw = snap.get("ltp")
                if ltp_raw is not None:
                    try:
                        return float(ltp_raw)
                    except ValueError:
                        return None
                return None
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Verdict Parsing
# ---------------------------------------------------------------------------


def parse_verdict_and_confidence(intel_text: str) -> tuple[str, int]:
    """
    Extract verdict and confidence from intelligence text.

    Parses Telegram message format:
    - *Verdict: LONG BUILDUP*
    - Confidence: 85%

    BUG-023 FIX: Made regex more flexible:
    - Case-insensitive matching for "Verdict" and "Confidence"
    - Optional asterisk wrapping (single or double)
    - Tolerates extra whitespace

    Args:
        intel_text: Intelligence text from Telegram

    Returns:
        Tuple of (verdict_string, confidence_int)
    """
    verdict = ""
    confidence = 0

    if not intel_text:
        return verdict, confidence

    # BUG-023 FIX: Case-insensitive, tolerant of single/double asterisks
    m_v = re.search(
        r"\*{1,2}\s*Verdict:\s*([^\*\n]+)\*{1,2}", intel_text, re.IGNORECASE
    )
    if m_v:
        verdict = m_v.group(1).strip()
    else:
        # Fallback: try without asterisks, non-greedy on line boundary
        m_v2 = re.search(r"Verdict:\s*([A-Z][A-Z _]{1,30})(?=\s*\n|\s*$|\s*\*)", intel_text, re.IGNORECASE)
        if m_v2:
            verdict = m_v2.group(1).strip()

    # BUG-023 FIX: Case-insensitive confidence matching
    m_c = re.search(r"Confidence:\s*(\d+)\s*%?", intel_text, re.IGNORECASE)
    if m_c:
        confidence = int(m_c.group(1))

    return verdict, confidence


# ---------------------------------------------------------------------------
# Premium Conversion Helpers
# ---------------------------------------------------------------------------


def convert_underlying_sl_to_premium(
    underlying: float,
    sl_underlying: float,
    target_underlying: float,
    entry_premium: float,
    side: str,
    option_type: str,
    strike: float | None = None,
    option_rows: list[dict] | None = None,
) -> tuple[float, float]:
    """
    Convert underlying-based SL/Target to premium equivalents for GTT/polling.

    For FUT: premium = underlying (1:1)
    For options: delta-based conversion based on underlying price distances.

    Args:
        underlying: Current underlying price
        sl_underlying: Stop-loss in underlying terms
        target_underlying: Target in underlying terms
        entry_premium: Entry premium
        side: "BUY" or "SELL"
        option_type: "FUT", "CE", or "PE"
        strike: Option strike price (optional)
        option_rows: Option chain rows (optional, to extract delta)

    Returns:
        Tuple of (sl_premium, target_premium)
    """
    if option_type == "FUT":
        return sl_underlying, target_underlying

    if underlying <= 0:
        # Fallback: fixed multipliers
        if side == "SELL":
            sl_premium = round(entry_premium * 1.50, 2)
            target_premium = round(entry_premium * 0.60, 2)
        else:
            sl_premium = round(entry_premium * 0.70, 2)
            target_premium = round(entry_premium * 1.50, 2)
        return sl_premium, target_premium

    # 1. Resolve delta
    delta = None
    if strike is not None and option_rows:
        for row in option_rows:
            try:
                if (
                    abs(float(row.get("strike") or 0) - strike) < 0.01
                    and str(row.get("option_type") or "").upper() == option_type.upper()
                ):
                    d_val = row.get("delta")
                    if d_val is not None:
                        d_val = abs(float(d_val))
                        if 0.05 <= d_val <= 0.95:
                            delta = d_val
                            break
            except Exception:
                continue

    if delta is None:
        delta = 0.25 if side == "BUY" else 0.20

    # 2. Delta-based calculation
    underlying_sl_dist = abs(underlying - sl_underlying)
    underlying_tgt_dist = abs(target_underlying - underlying)

    if side == "BUY":
        sl_premium = entry_premium - delta * underlying_sl_dist
        target_premium = entry_premium + delta * underlying_tgt_dist
        # Apply safety bounds to prevent negative or zero premiums
        sl_premium = max(sl_premium, 0.05)
        target_premium = max(target_premium, entry_premium + 0.05)
    else:  # SELL
        sl_premium = entry_premium + delta * underlying_sl_dist
        target_premium = entry_premium - delta * underlying_tgt_dist
        # Apply safety bounds to prevent negative or zero premiums
        sl_premium = max(sl_premium, entry_premium + 0.05)
        target_premium = max(target_premium, 0.05)

    return round(sl_premium, 2), round(target_premium, 2)
