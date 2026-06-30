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

def get_atr(ctx: dict) -> Optional[float]:
    """
    Extract ATR-14 from chart_indicators, trying 3h then 1h then any TF.
    
    Args:
        ctx: Scan context dict with 'chart_indicators' key
        
    Returns:
        ATR value as float, or None if unavailable
    """
    chart_indicators = ctx.get("chart_indicators") or {}
    
    # 1. Try structured keys 3h then 1h
    pay_3h = chart_indicators.get("3h") or {}
    pay_1h = chart_indicators.get("1h") or {}
    atr = pay_3h.get("atr_14") or pay_1h.get("atr_14")
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
        
    Returns:
        Tuple of (sl_underlying, target_underlying)
    """
    atr = get_atr(ctx)
    if atr and atr > 0:
        sl_underlying = underlying - 1.5 * atr
        target_underlying = underlying + 2.0 * atr
    else:
        log.warning("calculate_buy_sl_target: Missing ATR data, skipping trade plan creation (strict ATR requirement)")
        return None, None
    
    return round(sl_underlying, 2), round(target_underlying, 2)


def calculate_sell_sl_target(
    entry_premium: float,
    underlying: float,
    ctx: dict,
    step: float = 50.0,
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
        
    Returns:
        Tuple of (sl_underlying, target_underlying)
    """
    atr = get_atr(ctx)
    if atr and atr > 0:
        sl_underlying = underlying + 1.5 * atr
        target_underlying = underlying - 2.0 * atr
    else:
        log.warning("calculate_sell_sl_target: Missing ATR data, skipping trade plan creation (strict ATR requirement)")
        return None, None
    
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
                premium = float(row.get("ltp") or 0.0)
                return premium if premium > 0 else None
        except Exception:
            continue
    
    # Fallback: database snapshots with staleness check (L2)
    try:
        snapshots = get_latest_snapshots_for_symbol(symbol, expiry)
        for snap in snapshots:
            if (abs(snap.get("strike", 0) - strike) < 0.01 and 
                str(snap.get("option_type") or "").upper() == option_type.upper()):
                # L2: Check snapshot freshness before using
                fetched_at_str = snap.get("fetched_at")
                if fetched_at_str:
                    try:
                        fetched_at = datetime.fromisoformat(
                            fetched_at_str.replace("Z", "+00:00")
                        )
                        age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
                        if age_seconds > _DB_PREMIUM_MAX_AGE_SECONDS:
                            log.warning(
                                "%s: DB premium fallback REJECTED — snapshot is %.0f min old "
                                "(max %d min). Strike=%.2f %s. Returning None.",
                                symbol, age_seconds / 60, _DB_PREMIUM_MAX_AGE_SECONDS // 60,
                                strike, option_type,
                            )
                            return None
                    except (ValueError, TypeError):
                        # If we can't parse the timestamp, err on the side of caution
                        log.warning(
                            "%s: Could not parse fetched_at='%s' for DB premium staleness check",
                            symbol, fetched_at_str,
                        )
                        return None
                
                premium = float(snap.get("ltp") or 0.0)
                return premium if premium > 0 else None
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
    
    Args:
        intel_text: Intelligence text from Telegram
        
    Returns:
        Tuple of (verdict_string, confidence_int)
    """
    verdict = ""
    confidence = 0
    
    m_v = re.search(r"\*Verdict:\s*([^\*]+)\*", intel_text or "")
    if m_v:
        verdict = m_v.group(1).strip()
    
    m_c = re.search(r"Confidence:\s*(\d+)%", intel_text or "")
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
        delta = 0.5 if side == "BUY" else 0.3
        
    # 2. Delta-based calculation
    underlying_sl_dist = abs(underlying - sl_underlying)
    underlying_tgt_dist = abs(target_underlying - underlying)
    
    if side == "BUY":
        sl_premium = entry_premium - delta * underlying_sl_dist
        target_premium = entry_premium + delta * underlying_tgt_dist
        # Apply safety bounds to prevent negative or zero premiums
        sl_premium = max(sl_premium, 0.05)
        target_premium = max(target_premium, entry_premium + 0.05)
    else: # SELL
        sl_premium = entry_premium + delta * underlying_sl_dist
        target_premium = entry_premium - delta * underlying_tgt_dist
        # Apply safety bounds to prevent negative or zero premiums
        sl_premium = max(sl_premium, entry_premium + 0.05)
        target_premium = max(target_premium, 0.05)
        
    return round(sl_premium, 2), round(target_premium, 2)
