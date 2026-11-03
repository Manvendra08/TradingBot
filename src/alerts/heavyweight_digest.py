"""
Heavyweight Net Impact Scan Digest for Telegram & Contributors Parity.

Provides a unified, high-density scan summary across NIFTY, BANK NIFTY, and SENSEX:
- Index Spot, Net Move, and % share driven by top heavyweights
- Breadth (Bullish vs Bearish heavyweights)
- 5-Scan Shift Velocity (scan-to-scan momentum)
- Key Anchor (top puller) and Drag (top dragger)
- Options Strategy Confluence (bullish/bearish spread favorability, wing alerts)
"""

import logging
import time
from typing import Any, Dict, Optional
from datetime import datetime
import pytz

from src.alerts.digest import _esc
from src.engine.index_contributors import get_heavyweights_history, record_heavyweights_scan_snapshot
from src.alerts.telegram_dispatcher import send_text

log = logging.getLogger(__name__)
_IST = pytz.timezone("Asia/Kolkata")

# In-memory deduplication state to prevent sending duplicate alerts for the same scan timestamp
_LAST_DISPATCHED_SCAN_TIME: str = ""

_HW_CONF_CACHE: Dict[str, Any] = {}
_HW_CONF_CACHE_TIME: float = 0.0


def compute_heavyweights_confluence(history: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Calculate consolidated market breadth, shift velocity, and strategy guidance
    across top heavyweights for NIFTY, BANKNIFTY, and SENSEX.
    """
    if history is None:
        history = get_heavyweights_history(limit=5)
    if not history:
        return {}

    total_bullish = 0
    total_bearish = 0
    total_hw_shift = 0.0
    nifty_trend = "NEUTRAL"
    banknifty_trend = "NEUTRAL"

    indices_to_check = [
        ("NIFTY", "NIFTY 50"),
        ("BANKNIFTY", "BANK NIFTY"),
        ("SENSEX", "SENSEX")
    ]

    for idx_key, _ in indices_to_check:
        idx_data = history.get(idx_key)
        if not idx_data:
            continue
        hw_impact = float(idx_data.get("total_heavyweight_impact") or 0.0)
        bull_count = int(idx_data.get("bullish_count") or 0)
        bear_count = int(idx_data.get("bearish_count") or 0)
        total_bullish += bull_count
        total_bearish += bear_count

        hws = idx_data.get("heavyweights", [])
        shift_pts = sum(float(s.get("shift_pts", 0.0) or 0.0) for s in hws)
        total_hw_shift += shift_pts

        if idx_key == "NIFTY":
            nifty_trend = "BULL" if hw_impact > 15 else ("BEAR" if hw_impact < -15 else "NEUTRAL")
        elif idx_key == "BANKNIFTY":
            banknifty_trend = "BULL" if hw_impact > 30 else ("BEAR" if hw_impact < -30 else "NEUTRAL")

    # Breadth assessment
    if total_bullish >= 18 and total_bearish <= 7:
        breadth_eval = "BROAD-BASED BULLISH (Heavyweights in sync)"
    elif total_bearish >= 18 and total_bullish <= 7:
        breadth_eval = "BROAD-BASED BEARISH (Heavyweight selling pressure)"
    elif nifty_trend != banknifty_trend and nifty_trend != "NEUTRAL" and banknifty_trend != "NEUTRAL":
        breadth_eval = f"SECTOR DIVERGENCE (Nifty {nifty_trend} vs Bank Nifty {banknifty_trend})"
    else:
        breadth_eval = f"MIXED / CHOPPY ({total_bullish} Bullish vs {total_bearish} Bearish)"

    # Shift velocity assessment
    if total_hw_shift > 25.0:
        impulse_eval = f"ACCELERATING UPWARD (+{total_hw_shift:.1f} pts across heavyweights)"
    elif total_hw_shift < -25.0:
        impulse_eval = f"DECELERATING / PROFIT-TAKING ({total_hw_shift:.1f} pts across heavyweights)"
    else:
        impulse_eval = f"RANGE-BOUND COMPRESSION (5-scan shift {total_hw_shift:+.1f} pts)"

    # Actionable options strategy translation & directional vetoes
    veto_bear_calls = False
    veto_bull_puts = False
    if "BULLISH" in breadth_eval and total_hw_shift >= 0:
        strategy_eval = "Favors Bull Put Spreads | Iron Condor Call wing tested | Veto Bear Call Spreads"
        veto_bear_calls = True
    elif "BEARISH" in breadth_eval and total_hw_shift <= 0:
        strategy_eval = "Favors Bear Call Spreads | Iron Condor Put wing tested | Veto Bull Put Spreads"
        veto_bull_puts = True
    elif "DIVERGENCE" in breadth_eval:
        strategy_eval = "Avoid unhedged directional spreads | Favor Iron Condors with wider wings"
    else:
        strategy_eval = "Range-bound | Neutral Iron Condors & Strangles favored | Monitor short deltas"

    return {
        "breadth": breadth_eval,
        "velocity": impulse_eval,
        "strategy": strategy_eval,
        "total_bullish": total_bullish,
        "total_bearish": total_bearish,
        "total_hw_shift": total_hw_shift,
        "nifty_trend": nifty_trend,
        "banknifty_trend": banknifty_trend,
        "veto_bear_calls": veto_bear_calls,
        "veto_bull_puts": veto_bull_puts,
    }


def get_heavyweights_confluence(limit: int = 5) -> Dict[str, Any]:
    """
    Cached accessor for current heavyweights regime confluence (30s TTL).
    Used by Decision Matrix, Multi-Leg Validator, and LLM Prompt builders.
    """
    global _HW_CONF_CACHE, _HW_CONF_CACHE_TIME
    now = time.time()
    if _HW_CONF_CACHE and (now - _HW_CONF_CACHE_TIME) < 30.0:
        return _HW_CONF_CACHE
    try:
        history = get_heavyweights_history(limit=limit)
        conf = compute_heavyweights_confluence(history)
        _HW_CONF_CACHE = conf
        _HW_CONF_CACHE_TIME = now
        return conf
    except Exception as exc:
        log.debug("[heavyweight_digest] Error computing heavyweights confluence: %s", exc)
        return {}


def format_heavyweight_scan_digest(history: Optional[Dict[str, Any]] = None) -> str:
    """
    Format a concise, combined Telegram message summarizing top heavyweights impact
    across NIFTY 50, BANK NIFTY, and SENSEX.
    """
    if history is None:
        history = get_heavyweights_history(limit=5)

    freq_min = history.get("scan_frequency_minutes", 30)

    # Derive scan timestamp from latest available index header
    latest_ts = ""
    for idx_key in ("NIFTY", "BANKNIFTY", "SENSEX"):
        scans = history.get(idx_key, {}).get("scans", [])
        if scans:
            latest_ts = scans[-1].get("display_time", "")
            if latest_ts:
                break
    if not latest_ts:
        latest_ts = datetime.now(_IST).strftime("%d %b %I:%M %p")

    lines: list[str] = [
        f"🏛️ *HEAVYWEIGHTS NET IMPACT* [{_esc(latest_ts)} | {_esc(freq_min)}m]",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    total_bullish = 0
    total_bearish = 0
    total_hw_shift = 0.0
    nifty_trend = "NEUTRAL"
    banknifty_trend = "NEUTRAL"

    indices_to_render = [
        ("NIFTY", "NIFTY 50"),
        ("BANKNIFTY", "BANK NIFTY"),
        ("SENSEX", "SENSEX")
    ]

    for idx_key, display_label in indices_to_render:
        idx_data = history.get(idx_key)
        if not idx_data:
            continue

        spot = float(idx_data.get("spot_price") or 0.0)
        idx_chg_pct = float(idx_data.get("index_change_pct") or 0.0)
        sign = "+" if idx_chg_pct >= 0 else ""
        hw_impact = float(idx_data.get("total_heavyweight_impact") or 0.0)
        hw_sign = "+" if hw_impact >= 0 else ""
        share_pct = float(idx_data.get("heavyweight_share_pct") or 0.0)
        bull_count = int(idx_data.get("bullish_count") or 0)
        bear_count = int(idx_data.get("bearish_count") or 0)

        total_bullish += bull_count
        total_bearish += bear_count

        hws = idx_data.get("heavyweights", [])
        scan_count = idx_data.get("scan_count", len(idx_data.get("scans", [])))
        shift_pts = sum(float(s.get("shift_pts", 0.0) or 0.0) for s in hws)
        total_hw_shift += shift_pts

        if idx_key == "NIFTY":
            nifty_trend = "BULL" if hw_impact > 15 else ("BEAR" if hw_impact < -15 else "NEUTRAL")
        elif idx_key == "BANKNIFTY":
            banknifty_trend = "BULL" if hw_impact > 30 else ("BEAR" if hw_impact < -30 else "NEUTRAL")

        if scan_count >= 2:
            shift_sign = "+" if shift_pts >= 0 else ""
            shift_icon = "▲" if shift_pts > 0.5 else ("▼" if shift_pts < -0.5 else "—")
            shift_text = f"*{shift_sign}{shift_pts:.1f} pts* {shift_icon}"
        else:
            shift_text = "Initial Scan"

        # Anchors & Drags
        pos_hws = [s for s in hws if float(s.get("point_impact", 0.0)) > 0]
        neg_hws = [s for s in hws if float(s.get("point_impact", 0.0)) < 0]
        top_p = max(pos_hws, key=lambda x: float(x.get("point_impact", 0.0))) if pos_hws else None
        top_d = min(neg_hws, key=lambda x: float(x.get("point_impact", 0.0))) if neg_hws else None

        p_str = f"{_esc(top_p['symbol'])} (+{float(top_p['point_impact']):.1f})" if top_p else "None"
        d_str = f"{_esc(top_d['symbol'])} ({float(top_d['point_impact']):.1f})" if top_d else "None"

        block = (
            f"• *{_esc(display_label)}*: {spot:,.1f} ({sign}{idx_chg_pct:.2f}%)\n"
            f"  HW Impact: *{hw_sign}{hw_impact:.1f} pts* ({share_pct:.0f}% of move)\n"
            f"  Breadth: {bull_count}🟢 {bear_count}🔴 | 5-Scan Shift: {shift_text}\n"
            f"  Anchor: {p_str} | Drag: {d_str}"
        )
        lines.append(block)

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🎯 *OPTIONS REGIME CONFLUENCE*:")

    confluence = compute_heavyweights_confluence(history)
    breadth_eval = confluence.get("breadth", f"MIXED / CHOPPY ({total_bullish} Bullish vs {total_bearish} Bearish)")
    impulse_eval = confluence.get("velocity", f"RANGE-BOUND COMPRESSION (5-scan shift {total_hw_shift:+.1f} pts)")
    strategy_eval = confluence.get("strategy", "Range-bound | Neutral Iron Condors & Strangles favored | Monitor short deltas")

    lines.append(f"• *Breadth*: {_esc(breadth_eval)}")
    lines.append(f"• *Velocity*: {_esc(impulse_eval)}")
    lines.append(f"• *Strategy*: {_esc(strategy_eval)}")

    return "\n".join(lines)


def dispatch_heavyweight_scan_digest(force: bool = False) -> bool:
    """
    Checks recent history, evaluates if a new scan has occurred,
    and dispatches one consolidated Telegram digest message.
    """
    global _LAST_DISPATCHED_SCAN_TIME

    try:
        from config.symbol_classes import is_market_open
        now_dt = datetime.now(_IST)

        # Off-market hours guard (unless forced)
        if not force and not any(is_market_open(idx, now_dt) for idx in ("NIFTY", "BANKNIFTY", "SENSEX")):
            log.debug("[heavyweight_digest] Equity market closed; skipping Telegram digest.")
            return False

        history = get_heavyweights_history(limit=5)
        if not history:
            return False

        # Identify latest scan timestamp from history
        latest_scan_time = ""
        for idx in ("NIFTY", "BANKNIFTY", "SENSEX"):
            scans = history.get(idx, {}).get("scans", [])
            if scans:
                latest_scan_time = scans[-1].get("scan_time", "")
                if latest_scan_time:
                    break

        if not latest_scan_time:
            log.debug("[heavyweight_digest] No scan history rows available to dispatch.")
            return False

        # Deduplication check: do not resend for the exact same scan timestamp
        if not force and latest_scan_time == _LAST_DISPATCHED_SCAN_TIME:
            log.debug("[heavyweight_digest] Scan at %s already dispatched to Telegram. Skipping.", latest_scan_time)
            return False

        message_text = format_heavyweight_scan_digest(history)
        sent = send_text(message_text)

        if sent:
            _LAST_DISPATCHED_SCAN_TIME = latest_scan_time
            log.info("[heavyweight_digest] Dispatched combined heavyweight scan alert to Telegram (%s).", latest_scan_time)
            return True
        else:
            log.warning("[heavyweight_digest] Telegram dispatcher queue failed to accept heavyweight digest.")
            return False

    except Exception as exc:
        log.exception("[heavyweight_digest] Failed to dispatch heavyweight scan digest: %s", exc)
        return False


def record_and_dispatch_heavyweights(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Combined helper: records the heavyweight snapshot into SQLite,
    then dispatches the Telegram digest if a new snapshot was recorded.
    """
    saved = record_heavyweights_scan_snapshot(force_refresh=force_refresh)
    # If a snapshot was saved or force_refresh was requested, dispatch the digest
    if saved or force_refresh:
        dispatch_heavyweight_scan_digest(force=force_refresh)
    return saved
