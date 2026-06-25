"""
LLM Enrichment Engine v2.0 — Deep AI Brain Integration

Phase 1: Comprehensive prompt with all available market data.
Phase 2: AI trade decision influence (advisory / boost_only / full).
Phase 3: AI exit advisor for open trades.

The AI receives the full scan context including:
  - OI data (totals, changes, PCR, max pain, support/resistance)
  - Chart OHLC (1H/3H candles + sentiment)
  - Alert summary (counts by type and severity)
  - Rule engine output (verdict, confidence, bull/bear forces)
  - News headlines + sentiment direction
  - Open trade status (if any)
  - Historical scan trend
"""

import json
import logging
import os
import re
from datetime import datetime

import pytz
from pydantic import BaseModel, Field

from config.symbol_classes import get_symbol_class

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None

log = logging.getLogger(__name__)

_IST = pytz.timezone("Asia/Kolkata")


# ── Response Schemas ─────────────────────────────────────────────────────


class LLMTradeVerdict(BaseModel):
    """
    Action-oriented trade verdict for traders.
    Structured for quick decision-making with specific levels.
    """

    # Decision signal
    action: str = Field(
        description="GO_LONG, GO_SHORT, or NO_TRADE — clear executable signal"
    )
    confidence: int = Field(
        description=(
            "Confidence 0-100. DERIVE from evidence agreement — do not guess:\n"
            "  Count how many of these 3 agree with your action:\n"
            "    (1) Net OI Δ direction (CE vs PE change)\n"
            "    (2) Price action (underlying trend vs key levels)\n"
            "    (3) News/macro direction\n"
            "  3/3 agree → 80-95 | 2/3 → 60-75 | 1/3 → 35-55 | 0/3 → NO_TRADE.\n"
            "  Chart candles (1H/3H) are entry-timing context, NOT a confidence source.\n"
            "  If action=NO_TRADE, set confidence to how strongly staying out is supported."
        )
    )

    # Evidence chain — displayed as structured bullets in the alert
    signal_chain: str = Field(
        description=(
            "Structured 3-line evidence chain. Use EXACTLY this format (each line ≤ 15 words):\n"
            "  Line 1 — OI: [pattern name] — CE Δ [value] vs PE Δ [value] → [BEARISH/BULLISH/NEUTRAL]\n"
            "  Line 2 — Price: [price change]% vs [MP/ATM/S/R level] → [confirms/contradicts/caps]\n"
            "  Line 3 — Chart: 1H [sentiment] + 3H [sentiment] → [entry timing read]\n"
            "Cite only numbers from DATA. No filler words.\n"
            "Good example (short setup):\n"
            "  OI: Call Writing — CE Δ +2.3K vs PE Δ +186 → BEARISH\n"
            "  Price: +0.92% intraday vs MP 6800 → upside capped\n"
            "  Chart: 1H BEAR + 3H BEAR → entry timing aligned\n"
            "Good example (no trade):\n"
            "  OI: Both unwinding — CE Δ −43 vs PE Δ −224 → squaring, no edge\n"
            "  Price: 306.4 within 300–310 range → no directional breakout\n"
            "  Chart: 1H BEAR vs 3H BULL → 1H pullback in 3H trend"
        )
    )

    # Trade specification
    instrument: str = Field(
        description="Exact contract: e.g., 'NIFTY 24500 CE 27Jun' or 'CRUDEOIL 7100 PE 20Jun'"
    )
    entry_trigger: str = Field(
        description="Specific condition to enter: e.g., 'Underlying crosses above 24520' or 'Premium breaks 185'"
    )
    entry_premium_range: str = Field(
        description="Acceptable entry premium range: e.g., '180-195' or 'ATM ± 1 strike'"
    )

    # Risk management
    stop_loss: str = Field(
        description="Exact SL level: 'Premium 140' or 'Underlying 24450' — must be specific number"
    )
    target_1: str = Field(
        description="First profit target: 'Premium 230' or 'Underlying 24600'"
    )
    target_2: str = Field(
        description="Extended target if momentum continues: 'Premium 280' or 'Underlying 24700'"
    )
    risk_reward: str = Field(
        description="Calculated R:R ratio: e.g., '1:1.8' or '1:2.5'"
    )

    # Thesis — ONE sentence bottom-line punchline
    thesis: str = Field(
        description=(
            "ONE punchy sentence, max 20 words. The bottom-line case for the trade. "
            "Start with the OI pattern name, end with the action implication. "
            "Good: 'CE writing at 7000 with PCR 0.71 and dual bearish candles — short the bounce.' "
            "Bad: multi-sentence recap of signal_chain data, or prose paragraphs."
        )
    )
    invalidation: str = Field(
        description="What kills the trade: 'If underlying breaks above 6875' or 'If PCR rises above 1.0'"
    )

    # Context
    risk_rating: str = Field(
        description="LOW, MEDIUM, HIGH — overall risk considering macro events, expiry proximity, volatility"
    )
    catalyst: str = Field(
        description="Upcoming event that could accelerate or invalidate: 'EIA report Thursday 8:30PM IST' or 'No major catalyst'"
    )


class LLMExitAdvice(BaseModel):
    action: str = Field(description="HOLD, TRAIL_SL, CLOSE_EARLY, or EXTEND_TARGET")
    new_sl_premium: float | None = Field(
        default=None, description="New stop-loss premium level, or null if unchanged"
    )
    new_target_premium: float | None = Field(
        default=None, description="New target premium level, or null if unchanged"
    )
    reasoning: str = Field(
        description="1-2 sentence rationale for the exit recommendation"
    )
    urgency: str = Field(
        description="LOW, MEDIUM, or HIGH — how urgently this action should be taken"
    )


class LLMStrategyOptimization(BaseModel):
    suggested_config_changes: dict[str, float | str | int] = Field(
        description="Dictionary of configuration keys and their new recommended values"
    )
    analysis: str = Field(
        description="Brief analysis explaining why these changes were suggested based on trade history"
    )


# ── Client management ────────────────────────────────────────────────────

_client = None


def _get_client(api_key: str):
    global _client
    if _client is None:
        _client = genai.Client(api_key=api_key)
    return _client


# ── Deep prompt construction ─────────────────────────────────────────────


def _summarize_alerts(alerts: list[dict]) -> str:
    """Summarize alerts by type and severity for the AI prompt."""
    if not alerts:
        return "No anomalies detected in this scan."

    by_type: dict[str, dict[str, int]] = {}
    for a in alerts:
        atype = a.get("alert_type", "UNKNOWN")
        sev = a.get("severity", "LOW")
        if atype not in by_type:
            by_type[atype] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        by_type[atype][sev] = by_type[atype].get(sev, 0) + 1

    lines = []
    for atype, sevs in sorted(by_type.items()):
        parts = []
        for s in ("HIGH", "MEDIUM", "LOW"):
            if sevs.get(s, 0) > 0:
                parts.append(f"{sevs[s]} {s}")
        lines.append(f"  - {atype}: {', '.join(parts)}")
    return "\n".join(lines)


def _format_chart_data(chart_indicators: dict | None) -> str:
    """Format chart OHLC data for the AI prompt."""
    if not chart_indicators:
        return "No chart data available."

    lines = []
    for tf in ("1h", "3h"):
        data = chart_indicators.get(tf)
        if not data:
            continue
        ohlc = data.get("ohlc", {})
        prev = data.get("prev_ohlc") or data.get("last_closed_ohlc") or {}
        sentiment = data.get("sentiment", "UNKNOWN")
        lines.append(
            f"  {tf.upper()} Candle: O={ohlc.get('open'):.2f} H={ohlc.get('high'):.2f} L={ohlc.get('low'):.2f} C={ohlc.get('close'):.2f} | Sentiment: {sentiment}"
        )
        if prev:
            lines.append(
                f"  {tf.upper()} Prev:   O={prev.get('open'):.2f} H={prev.get('high'):.2f} L={prev.get('low'):.2f} C={prev.get('close'):.2f}"
            )
    return "\n".join(lines) if lines else "No chart data available."


def _format_news(news_data: dict | None) -> str:
    """Format news data for the AI prompt."""
    if not news_data or not news_data.get("items"):
        return "No news data available for this symbol."

    direction = news_data.get("current_news_direction", "MIXED")
    score = news_data.get("news_score_current", 0)
    count = news_data.get("count_24h", 0)
    items = news_data.get("items", [])[:5]

    lines = [
        f"  News Direction: {direction} (score: {score}) | {count} articles in 24h"
    ]
    for item in items:
        title = item.get("title", "")[:100]
        s = item.get("score", 0)
        tag = "+" if s > 0 else ("-" if s < 0 else "=")
        lines.append(f"  [{tag}] {title}")
    return "\n".join(lines)


def _format_macro_context(symbol: str) -> str:
    """
    Inject symbol-specific fundamental & macro context into the LLM prompt.
    This runs regardless of whether live news is available — it gives the LLM
    the structural knowledge of what DRIVES this instrument, so it can flag
    risks even in the absence of fresh headlines.
    """
    base = symbol.upper().strip()

    _MACRO_PREFIX = (
        "  ⚠️ BACKGROUND CONTEXT ONLY — not a directional signal for today.\n"
        "  Do NOT use seasonality or macro narrative to override live OI Δ or price action.\n"
    )

    # ── MCX Commodities ──────────────────────────────────────────────────
    if "NATURALGAS" in base:
        return (
            _MACRO_PREFIX
            + """  Symbol type: MCX Natural Gas Futures (USD-denominated, INR-settled)
  Primary drivers:
    - EIA Weekly Natural Gas Storage Report (every Thursday ~8:30 PM IST)
      → Surprise builds = bearish pressure; surprise draws = bullish spike
    - Henry Hub spot price (US benchmark): MCX closely tracks it with INR/USD multiplier
    - Weather demand: Summer cooling (US/EU) and winter heating drive consumption
    - LNG export demand from US Gulf Coast terminals
    - INR/USD rate: Every 1 rupee depreciation in INR adds ~1.5-2% to MCX price
  Key risk events to flag:
    - EIA report day (Thursday): avoid fresh entries 2h before/after report
    - US weather model updates (Mon/Wed): can move Henry Hub 3-5% intraday
  Seasonality: Jun-Aug = low demand (shoulder season) → structurally bearish bias
  Correlation: Positive with crude (energy complex); negative with renewable output"""
        )

    if "CRUDEOIL" in base:
        return (
            _MACRO_PREFIX
            + """  Symbol type: MCX Crude Oil Futures (Brent/WTI proxy, USD-denominated, INR-settled)
  Primary drivers:
    - EIA Weekly Petroleum Status Report (every Wednesday ~8:00 PM IST)
      → Inventory build = bearish; inventory draw = bullish
    - API Crude Inventory (Tuesday ~4:30 AM IST, unofficial early signal)
    - OPEC+ production quota decisions and compliance rates
    - USD Index (DXY): Strong USD → lower crude; weak USD → higher crude
    - INR/USD rate: Direct multiplier on MCX price (1% INR move = ~1% MCX move)
    - Geopolitical risk premium: Middle East tensions, Russia-Ukraine supply routes
  Key risk events to flag:
    - EIA report Wednesday: major volatility event — flag HIGH risk if trade near report
    - OPEC+ meetings (quarterly): binary risk for trend trades
  Seasonality: Summer driving season (Jun-Aug) supports demand; shoulder in Sep-Oct
  Correlation: Natural Gas (energy complex), DXY (inverse), equities (risk-on)"""
        )

    if "GOLD" in base:
        return (
            _MACRO_PREFIX
            + """  Symbol type: MCX Gold Futures (USD-denominated, INR-settled)
  Primary drivers:
    - US Federal Reserve rate decisions and dot-plot guidance
    - US CPI/PCE inflation prints (monthly) — higher inflation = bullish gold
    - USD Index (DXY): Strong USD = bearish gold; weak USD = bullish gold
    - INR/USD: Weaker INR inflates MCX gold price independently of spot
    - Geopolitical safe-haven demand; central bank gold buying (RBI, PBoC)
  Key risk: Fed FOMC statements, US NFP, CPI day volatility is extreme
  Seasonality: Akshaya Tritiya / Dhanteras / wedding season → INR demand spikes"""
        )

    if "SILVER" in base:
        return (
            _MACRO_PREFIX
            + """  Symbol type: MCX Silver Futures (USD-denominated, INR-settled)
  Primary drivers: Industrial demand (solar panels, EVs), Gold correlation (~0.85)
  Key risk: More volatile than gold; tracks gold direction but amplifies moves 2-3x
  Watch: US manufacturing PMI (industrial demand signal), Gold/Silver ratio extremes"""
        )

    # ── NSE Index Options ────────────────────────────────────────────────
    if "BANKNIFTY" in base:
        return (
            _MACRO_PREFIX
            + """  Symbol type: NSE BANKNIFTY Index Options (INR)
  Primary drivers:
    - RBI Monetary Policy Committee (MPC) — rate decisions & stance (every 2 months)
    - Bank credit growth, NPA cycles, PSU bank disinvestment news
    - FII/DII net flows (daily): Sustained FII selling → index headwind
    - US Fed policy (risk-on/risk-off global sentiment)
    - India VIX: VIX > 20 = elevated uncertainty; VIX < 12 = complacency risk
  BANKNIFTY-specific: Beta ~1.5x vs Nifty; highly sensitive to RBI rate surprises
  Expiry behaviour: Weekly expiry (Thursday) → gamma squeeze risk near ATM last 2 days
  Key risk: RBI policy day, budget day, election results = binary events"""
        )

    if "NIFTY" in base:
        return (
            _MACRO_PREFIX
            + """  Symbol type: NSE NIFTY 50 Index Options (INR)
  Primary drivers:
    - RBI Monetary Policy Committee (MPC) — rate decisions & stance (every 2 months)
    - FII/DII net flows (daily): Sustained FII selling → index headwind
    - US Fed policy, DXY, US equity overnight moves (SGX Nifty pre-market)
    - India macro: GDP, CPI, IIP prints (monthly)
    - India VIX: VIX > 20 = elevated uncertainty; VIX < 12 = complacency risk
  Expiry behaviour: Weekly expiry (Thursday) → gamma squeeze risk near ATM last 2 days
  Key risk: Budget day, election results, RBI policy day = binary events"""
        )

    # ── Generic fallback ─────────────────────────────────────────────────
    return (
        _MACRO_PREFIX
        + """  No specific macro context available for this symbol.
  General reminder: Consider broader market sentiment, FII flows, and any
  scheduled economic events before taking directional positions."""
    )


def _format_open_trade(open_trade: dict | None) -> str:
    """Format open trade status for the AI prompt."""
    if not open_trade:
        return "No open trade for this symbol."

    side = open_trade.get("side", "BUY")
    opt = open_trade.get("option_type", "")
    strike = open_trade.get("strike", "")
    entry_und = open_trade.get("entry_underlying", "")
    entry_prem = open_trade.get("entry_premium", "")
    sl_prem = open_trade.get("sl_premium", "")
    target_prem = open_trade.get("target_premium", "")
    opened_at = open_trade.get("opened_at", "")
    reason = open_trade.get("reason", "")

    return (
        f"  OPEN: {side} {opt} {strike} | Entry: ₹{entry_prem} | SL: ₹{sl_prem} | Target: ₹{target_prem}\n"
        f"  Underlying at entry: {entry_und} | Opened: {opened_at}\n"
        f"  Reason: {reason}"
    )


def _format_forces(intel: dict) -> str:
    """Format bull/bear forces for the AI prompt."""
    bull = intel.get("bull_forces") or []
    bear = intel.get("bear_forces") or []
    lines = []
    if bull:
        lines.append("  Bull Forces:")
        for score, desc in bull:
            lines.append(f"    +{score}: {desc}")
    if bear:
        lines.append("  Bear Forces:")
        for score, desc in bear:
            lines.append(f"    -{score}: {desc}")
    return "\n".join(lines) if lines else "  No force breakdown available."


def _format_historical_oi(symbol: str) -> str:
    """
    Fetch last 10 scans and format OI/price trend for the LLM prompt.
    Includes price-impact analysis (OI vs price correlation).
    """
    from src.models.schema import get_conn

    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT fetched_at, underlying, pcr, max_pain,
                       ce_oi_change, pe_oi_change, verdict_label, confidence
                FROM scan_summaries
                WHERE symbol = ?
                  AND (is_fallback IS NULL OR is_fallback = 0)
                ORDER BY fetched_at DESC
                LIMIT 10
                """,
                (symbol,),
            ).fetchall()
    except Exception as e:
        log.debug("[llm] Historical OI query failed for %s: %s", symbol, e)
        return "  Historical data unavailable."

    if not rows or len(rows) < 3:
        return "  Insufficient historical data (<3 scans)."

    lines = []
    lines.append(f"  Last {len(rows)} scans (newest first):")

    pcr_vals: list[float] = []
    oi_net: list[int] = []
    price_vals: list[float] = []

    for row in rows:
        fetched_at = row["fetched_at"] or ""
        underlying = float(row["underlying"] or 0)
        pcr = float(row["pcr"] or 0)
        ce_chg = int(row["ce_oi_change"] or 0)
        pe_chg = int(row["pe_oi_change"] or 0)
        verdict = row["verdict_label"] or "N/A"

        # Extract time portion for display (HH:MM)
        time_str = fetched_at[11:16] if len(fetched_at) > 16 else fetched_at

        lines.append(
            f"    {time_str}: Und {underlying:.0f} | PCR {pcr:.2f} | "
            f"CE \u0394{ce_chg:+,} | PE \u0394{pe_chg:+,} | {verdict}"
        )

        if pcr > 0:
            pcr_vals.append(pcr)
        oi_net.append(ce_chg + pe_chg)
        if underlying > 0:
            price_vals.append(underlying)

    # ── Trend summaries ──────────────────────────────────────────────────
    if len(pcr_vals) >= 3:
        pcr_newest = pcr_vals[0]
        pcr_oldest = pcr_vals[-1]
        pcr_dir = (
            "rising"
            if pcr_newest > pcr_oldest + 0.05
            else ("falling" if pcr_newest < pcr_oldest - 0.05 else "stable")
        )
        lines.append(
            f"  PCR Trend: {pcr_dir} ({pcr_oldest:.2f} \u2192 {pcr_newest:.2f})"
        )

    if len(oi_net) >= 3:
        recent_net = sum(oi_net[:3])
        prior_net = sum(oi_net[3:6]) if len(oi_net) >= 6 else 0
        if recent_net > 0 and recent_net > prior_net:
            oi_dir = "accelerating buildup"
        elif recent_net > 0:
            oi_dir = "decelerating buildup"
        elif recent_net < 0 and recent_net < prior_net:
            oi_dir = "accelerating unwinding"
        elif recent_net < 0:
            oi_dir = "decelerating unwinding"
        else:
            oi_dir = "flat"
        lines.append(
            f"  OI Trend: {oi_dir} (recent 3: {recent_net:+,} vs prior 3: {prior_net:+,})"
        )

    # ── Price impact analysis (Fix 3) ────────────────────────────────────
    if len(price_vals) >= 3 and len(oi_net) >= 3:
        price_newest = price_vals[0]
        price_oldest = price_vals[-1]
        price_chg = price_newest - price_oldest
        price_pct = (price_chg / price_oldest) * 100 if price_oldest > 0 else 0.0
        net_oi_5 = sum(oi_net[:5])

        if net_oi_5 > 0 and price_pct > 0.1:
            impact = "OI building + price rising = Long buildup confirmed"
        elif net_oi_5 > 0 and price_pct < -0.1:
            impact = "OI building + price falling = Short buildup confirmed"
        elif net_oi_5 < 0 and price_pct > 0.1:
            impact = "OI unwinding + price rising = Short covering"
        elif net_oi_5 < 0 and price_pct < -0.1:
            impact = "OI unwinding + price falling = Long liquidation"
        elif abs(price_pct) <= 0.1 and abs(net_oi_5) > 0:
            impact = "OI moving but price flat = Consolidation / trap risk"
        else:
            impact = "Mixed signals \u2014 price and OI not aligned"

        lines.append(
            f"  Price Impact: {impact} (\u0394{price_pct:+.2f}% over {len(rows)} scans, "
            f"net OI: {net_oi_5:+,})"
        )

    # ── Verdict persistence ──────────────────────────────────────────────
    verdicts = [row["verdict_label"] for row in rows if row["verdict_label"]]
    if len(verdicts) >= 3:
        from collections import Counter

        vc = Counter(verdicts)
        most_common_label, most_common_count = vc.most_common(1)[0]
        pct = (most_common_count / len(verdicts)) * 100
        lines.append(
            f"  Verdict Persistence: '{most_common_label}' in {most_common_count}/{len(verdicts)} "
            f"scans ({pct:.0f}%)"
        )

    return "\n".join(lines)


def _build_deep_prompt(
    symbol: str,
    intel: dict,
    scan_context: dict,
    alerts: list[dict] | None = None,
    news_data: dict | None = None,
    open_trade: dict | None = None,
    trade_decision: dict | None = None,
) -> str:
    """Construct an action-oriented prompt for structured trade recommendations."""

    ctx = scan_context or {}

    # Calculate RISK FLAGS
    dte = ctx.get("days_to_expiry", 99)
    risk_flags = []
    if dte is not None and int(dte) <= 2:
        risk_flags.append(f"EXPIRY IMMINENT ({dte} DTE)")
    if news_data:
        high_impact = [
            i for i in (news_data.get("items") or []) if abs(i.get("score", 0)) >= 3
        ]
        if high_impact:
            risk_flags.append(f"HIGH-IMPACT NEWS ACTIVE ({len(high_impact)} articles)")

    prompt = f"""You are a professional options trader. Analyse the data below and generate a structured trade plan.

{symbol} | {datetime.now(_IST).strftime("%a %H:%M IST")} | Underlying: {ctx.get("underlying")} | ATM: {ctx.get("atm_strike")} | DTE: {dte}

DATA:
• Verdict : {intel.get("verdict_label")} @ {intel.get("confidence", 0)}% | Trend: {intel.get("trend", "N/A")}
• S/R     : {ctx.get("support")} / {ctx.get("resistance")} | MaxPain: {ctx.get("max_pain")} | PCR: {ctx.get("pcr")}
• OI Δ    : CE {ctx.get("ce_oi_change", 0):+,} | PE {ctx.get("pe_oi_change", 0):+,}
• Price Δ : {ctx.get("price_change_pct", "N/A")}% ({ctx.get("price_change_points", "N/A")} pts)
• Chart   : {_format_chart_data(ctx.get("chart_indicators"))}
• Alerts  : {_summarize_alerts(alerts or [])}
• Risk    : {", ".join(risk_flags) or "None"}

HISTORICAL OI CONTEXT:
{_format_historical_oi(symbol)}
"""

    if news_data and news_data.get("items"):
        prompt += f"NEWS: {_format_news(news_data)}\n"
    else:
        prompt += f"MACRO: {_format_macro_context(symbol)}\n"

    if open_trade:
        prompt += f"OPEN POSITION: {_format_open_trade(open_trade)}\n"

    if trade_decision:
        prompt += f"ENGINE RESULT: {trade_decision.get('status')} — {trade_decision.get('reason', '')}\n"

    # Derive engine direction string for the prompt
    from src.engine.verdict_sets import is_bearish as _is_bear
    from src.engine.verdict_sets import is_bullish as _is_bull

    _vl = intel.get("verdict_label", "")
    _bias_str = (
        "BULLISH" if _is_bull(_vl) else ("BEARISH" if _is_bear(_vl) else "NO_TRADE")
    )
    _bias_rationale = intel.get("verdict_desc") or intel.get("trend") or ""
    _c1 = (
        str(
            ctx.get("chart_indicators", {}).get("1h", {}).get("sentiment", "NEUTRAL")
        ).upper()
        if isinstance(ctx.get("chart_indicators"), dict)
        else "NEUTRAL"
    )
    _c3 = (
        str(
            ctx.get("chart_indicators", {}).get("3h", {}).get("sentiment", "NEUTRAL")
        ).upper()
        if isinstance(ctx.get("chart_indicators"), dict)
        else "NEUTRAL"
    )

    prompt += f"""
ENGINE DECISION (authoritative — you MUST respect this):
• Direction : {_bias_str}
• Pattern   : {_vl}
• Rationale : {_bias_rationale}

OI SEMANTICS (facts, not opinions):
  CE OI rising + price flat/down  = Call Writing   = BEARISH  (resistance — shorts defending topside)
  PE OI rising + price flat/up    = Put Writing    = BULLISH  (support — shorts defending downside)
  Price ↑ + total OI ↑            = Long Buildup   = BULLISH  (fresh longs entering)
  Price ↓ + total OI ↑            = Short Buildup  = BEARISH  (fresh shorts entering)
  CE OI falling faster            = CE unwinding   = BULLISH  (shorts covering calls)
  PE OI falling faster            = PE unwinding   = BEARISH  (longs exiting puts)
  Both sides unwinding            = Squaring / expiry — NO directional edge
  PCR rising                      = more puts → BULLISH skew
  PCR falling                     = fewer puts → BEARISH skew

CHART ROLE (OI trades only — not timeframe strategy):
  3H = dominant trend. 1H = short-term momentum within it.
  1H opposite to completed 3H = pullback entry in 3H direction (not a conflict).
  Both 3H + 1H opposing OI = only genuine conflict.
  Chart candles do NOT override OI engine direction.

ANALYSIS CHAIN — think through these IN ORDER before generating output:
  Step 1 — OI Pattern : What do CE Δ {ctx.get("ce_oi_change", 0):+,} and PE Δ {ctx.get("pe_oi_change", 0):+,} mean? Apply OI semantics above.
  Step 2 — Price Check: Does price Δ {ctx.get("price_change_pct", 0)}% confirm or contradict the OI signal?
             Is price near support {ctx.get("support")}, resistance {ctx.get("resistance")}, or max pain {ctx.get("max_pain")}?
  Step 3 — History    : Is this pattern new or persistent? (See HISTORICAL OI CONTEXT above)
  Step 4 — Chart Timing: What do 1H ({_c1}) and 3H ({_c3}) say about entry timing?
  Step 5 — Macro/News : Any catalyst (EIA/RBI/OPEC/expiry) that amplifies or reduces the setup?
  Step 6 — Confidence : Count how many of [OI Δ, price action, news/macro] agree → derive score.
             3/3 agree → 80-95 | 2/3 → 60-75 | 1/3 → 35-55 | 0/3 → NO_TRADE
  Step 7 — Action     : MUST match ENGINE DECISION ({_bias_str}). You may downgrade to NO_TRADE. Never flip direction.

YOUR ROLE — execution detail and evidence chain only. Engine decided direction. You provide:
  signal_chain, instrument, entry_trigger, entry_premium_range, stop_loss,
  target_1, target_2, risk_reward, thesis, invalidation, risk_rating, catalyst.

OUTPUT FIELDS (all required):
• action         : GO_LONG | GO_SHORT | NO_TRADE — must match ENGINE DECISION ({_bias_str})
• confidence     : integer 0-100 derived from Step 6 above
• signal_chain   : 3 lines in EXACT format (≤15 words each):
                   Line 1: OI: [pattern] — CE Δ [value] vs PE Δ [value] → [bias]
                   Line 2: Price: [Δ%] vs [MP/ATM/S/R] → [confirms/contradicts/caps]
                   Line 3: Chart: 1H [sentiment] + 3H [sentiment] → [timing read]
• instrument     : "{symbol} <strike> CE/PE/FUT <expiry>" — exact symbol and expiry from DATA
• entry_trigger  : specific condition with a level (e.g., "Underlying holds below 6700 on next scan")
• entry_premium_range: range in rupees (e.g., "70-80") or "ATM ± 1 strike"
• stop_loss      : exact level — "Underlying 6875" or "Premium 95"
• target_1       : first profit level with exact number
• target_2       : extended target with exact number
• risk_reward    : "1:X.X" format calculated from the levels above
• thesis         : ONE sentence max 20 words — OI pattern + bottom-line action implication
                   Good: "CE writing at 7000 with PCR 0.71 and dual bearish candles — short the bounce."
• invalidation   : what kills the trade (specific level or OI condition)
• risk_rating    : LOW | MEDIUM | HIGH
                   HIGH if any RISK flag present or chart genuine conflict (both TFs vs OI).
                   MEDIUM for mixed signals. LOW only when all 3 sources agree.
• catalyst       : nearest upcoming event or "No major catalyst"

RULES:
1. Use ONLY levels from DATA for all numeric fields. Do not invent levels.
2. If NO_TRADE: fill signal_chain with the OI squaring reason, fill instrument/entry_trigger with what WOULD change your view.
3. thesis is the punchline — do not repeat signal_chain content in it."""

    return prompt


def _build_exit_prompt(
    symbol: str,
    open_trade: dict,
    scan_context: dict,
    news_data: dict | None = None,
) -> str:
    """Build a focused prompt for exit/management advice on an open trade."""
    ctx = scan_context or {}

    side = str(open_trade.get("side") or "BUY").upper()
    opt = str(open_trade.get("option_type") or "").upper()
    strike = open_trade.get("strike")
    strike_str = f"{strike}" if strike else ""

    # Derive position direction and option mechanics in explicit terms for the LLM
    if side == "BUY":
        position_desc = f"LONG option position (purchased {opt} {strike_str} contract, paid premium)"
        if opt == "CE":
            pos_direction = "LONG UNDERLYING (Bullish) — profits when underlying RISES"
            behavior = "You benefit when underlying price goes UP. CE premium increases as underlying rises, and decreases as underlying falls."
        elif opt == "PE":
            pos_direction = "SHORT UNDERLYING (Bearish) — profits when underlying FALLS"
            behavior = "You benefit when underlying price goes DOWN. PE premium increases as underlying falls, and decreases as underlying rises."
        else:  # FUT
            pos_direction = "LONG UNDERLYING — profits when underlying RISES"
            behavior = "You benefit when underlying price goes UP."
    else:  # SELL
        position_desc = f"SHORT option position (sold/wrote {opt} {strike_str} contract, collected premium)"
        if opt == "CE":
            pos_direction = "SHORT UNDERLYING (Bearish) — profits when underlying FALLS"
            behavior = "You benefit when underlying price goes DOWN. CE premium decreases as underlying falls (profitable for seller), and increases as underlying rises (unprofitable)."
        elif opt == "PE":
            pos_direction = "LONG UNDERLYING (Bullish) — profits when underlying RISES"
            behavior = "You benefit when underlying price goes UP. PE premium decreases as underlying rises (profitable for seller), and increases as underlying falls (unprofitable)."
        else:  # FUT
            pos_direction = "SHORT UNDERLYING — profits when underlying FALLS"
            behavior = "You benefit when underlying price goes DOWN."

    # Age of position
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        opened_at_str = str(open_trade.get("opened_at") or "")
        if opened_at_str:
            opened_dt = _dt.fromisoformat(opened_at_str.replace("Z", "+00:00"))
            now_utc = _dt.now(_tz.utc)
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=_tz.utc)
            age_min = int((now_utc - opened_dt).total_seconds() / 60)
            age_str = f"({abs(age_min)} min ago)"
        else:
            age_str = ""
    except Exception:
        age_str = ""

    # Expiry and time to settlement calculation
    expiry = open_trade.get("expiry") or ""
    # For FUT positions, use futures_expiry (different schedule from options)
    if opt == "FUT":
        futures_expiry = ctx.get("futures_expiry")
        if not futures_expiry:
            # Compute on the fly if not in context
            try:
                from config.symbol_classes import get_futures_expiry

                futures_expiry = get_futures_expiry(symbol)
            except Exception:
                futures_expiry = None
        if futures_expiry:
            expiry = futures_expiry
    if not expiry:
        expiry = ctx.get("expiry") or ""
    time_left_str = "N/A"
    dte = ctx.get("days_to_expiry")
    if dte is None and expiry:
        try:
            exp_date = _dt.strptime(str(expiry).split(" ")[0], "%Y-%m-%d").date()
            today_ist = _dt.now(_IST).date()
            dte = (exp_date - today_ist).days
        except Exception:
            pass

    if dte is not None:
        if dte == 0:
            sym_upper = symbol.upper()
            is_mcx = any(
                m in sym_upper for m in ["NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"]
            )
            expiry_hour = 23 if is_mcx else 15
            expiry_minute = 30

            now_ist = _dt.now(_IST)
            expiry_dt = now_ist.replace(
                hour=expiry_hour, minute=expiry_minute, second=0, microsecond=0
            )

            diff_seconds = (expiry_dt - now_ist).total_seconds()
            if diff_seconds > 0:
                mins_left = int(diff_seconds / 60)
                time_left_str = f"EXPIRY TODAY: {mins_left} minutes remaining until contract expiry/settlement at {expiry_hour:02d}:{expiry_minute:02d} IST"
            else:
                time_left_str = f"EXPIRY TODAY: Contract expired/settling now (expired at {expiry_hour:02d}:{expiry_minute:02d} IST)"
        elif dte > 0:
            time_left_str = f"{dte} days remaining until expiry"
        else:
            time_left_str = f"Expired {abs(dte)} days ago"

    entry_premium = open_trade.get("entry_premium", "—")
    sl_premium = open_trade.get("sl_premium", "—")
    target_premium = open_trade.get("target_premium", "—")

    entry_underlying = open_trade.get("entry_underlying", "—")
    sl_underlying = open_trade.get("sl_underlying")
    target_underlying = open_trade.get("target_underlying")

    sl_ul_str = f"{sl_underlying}" if sl_underlying is not None else "—"
    tgt_ul_str = f"{target_underlying}" if target_underlying is not None else "—"

    return f"""Trade management decision. Evaluate exit/hold for the EXISTING position below.

TIME: {datetime.now(_IST).strftime("%a %H:%M IST")}

OPEN POSITION (authoritative — evaluate THIS side and its mechanics only):
  Position Type: {position_desc}
  Underlying Direction: {pos_direction}
  Mechanics: {behavior}
  Entry Underlying: {entry_underlying} | SL (Underlying): {sl_ul_str} | Target (Underlying): {tgt_ul_str}
  Entry Premium: ₹{entry_premium} | SL (Premium): ₹{sl_premium} | Target (Premium): ₹{target_premium}
  Opened: {str(open_trade.get("opened_at", ""))[:16]} {age_str}
  Contract Expiry: {expiry} | Time to Expiry: {time_left_str}

MARKET NOW:
  Underlying {ctx.get("underlying")} | Chg {ctx.get("price_change_points", 0)}pts ({ctx.get("price_change_pct", "N/A")}%)
  PCR {ctx.get("pcr")} | S/R {ctx.get("support")}/{ctx.get("resistance")}
  OI Δ: CE {ctx.get("ce_oi_change", 0):,} | PE {ctx.get("pe_oi_change", 0):,}
  Chart: {_format_chart_data(ctx.get("chart_indicators"))}
NEWS: {_format_news(news_data)}

EVALUATE ONLY these options for the {pos_direction.split(" ")[0]} position:
• HOLD: Thesis intact, underlying moving favourably or consolidating — no change.
• TRAIL_SL: Position profitable — lock in gains by raising SL (provide new_sl_premium as number).
• CLOSE_EARLY: Thesis broken (underlying moving against position, key level breached) — exit now.
  Provide exit reasoning with current premium estimate.
• EXTEND_TARGET: Strong momentum in favour — raise target (provide new_target_premium as number).

CRITICAL EXPIRY RULES:
- If TIME TO EXPIRY shows "EXPIRY TODAY" with less than 60 minutes remaining, you MUST prioritize CLOSE_EARLY or CLOSE to secure profits/minimize losses and prevent physical delivery/cash settlement penalties, unless the target is already hit or the option is completely worthless.
- You must explicitly state the remaining minutes to contract expiry in your reasoning to show awareness.

URGENCY: HIGH only for immediate adverse threat (sharp move against position, SL imminently breached, or contract expiring in less than 30 minutes).
"""


# ── Gemini API calls ─────────────────────────────────────────────────────

import time

_VERDICT_CACHE = {}
_EXIT_CACHE = {}
_API_QUOTA_EXHAUSTED_UNTIL = 0.0
_CONSECUTIVE_FAILURES = 0
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN = 300.0  # 5 minutes
_CIRCUIT_OPEN_UNTIL = 0.0
_PROVIDER_COOLDOWN_UNTIL: dict[str, float] = {}


def _provider_cooldown_key(provider: dict) -> str:
    return f"{provider.get('env_key')}:{provider.get('model')}"


def _parse_retry_after_seconds(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"try again in (\d+)m([\d.]+)s", text, re.I)
    if m:
        return int(m.group(1)) * 60.0 + float(m.group(2))
    m = re.search(r"try again in ([\d.]+)s", text, re.I)
    if m:
        return float(m.group(1))
    return None


def _register_provider_failure(
    provider: dict, status_code: int, body: str, now: float
) -> None:
    key = _provider_cooldown_key(provider)
    body_l = (body or "").lower()
    if status_code == 402:
        _PROVIDER_COOLDOWN_UNTIL[key] = now + 86400.0
        log.info("[llm] %s credit exhausted — 24h cooldown", provider.get("name"))
        return
    if status_code == 429:
        retry = _parse_retry_after_seconds(body) or 300.0
        if "tokens per day" in body_l or "tpd" in body_l:
            retry = max(retry, 3600.0)
        _PROVIDER_COOLDOWN_UNTIL[key] = now + retry
        log.info("[llm] %s rate-limited — cooldown %.0fs", provider.get("name"), retry)


def _max_tokens_for_purpose(purpose: str) -> int:
    from config.settings import (
        LLM_MAX_TOKENS_EOD,
        LLM_MAX_TOKENS_FORMATTING,
        LLM_MAX_TOKENS_LIVE,
    )

    if purpose == "eod_review":
        return LLM_MAX_TOKENS_EOD
    if purpose == "formatting":
        return LLM_MAX_TOKENS_FORMATTING
    return LLM_MAX_TOKENS_LIVE


def _call_llm_api(
    symbol: str,
    prompt: str,
    response_schema=None,
    deadline: float | None = None,
    purpose: str | None = None,
) -> BaseModel | None:
    """Call LLM APIs in order of reasoning and output quality using available providers
    (OpenRouter, Groq, OpenCode, Gemini). If a model fails on one provider, we fallback
    to another provider hosting the same model group or a fast alternative.

    Args:
        deadline: Unix timestamp by which we must finish. Each model attempt uses
                  remaining_time as its HTTP timeout, so we never overshoot.
        purpose: Routing classification ('live_verdict', 'eod_review', 'formatting')
    """
    global \
        _API_QUOTA_EXHAUSTED_UNTIL, \
        _CONSECUTIVE_FAILURES, \
        _CIRCUIT_OPEN_UNTIL, \
        _PROVIDER_COOLDOWN_UNTIL
    schema = response_schema or LLMTradeVerdict
    now = time.time()

    from config.settings import DISABLE_LLM_ENRICHMENT

    if DISABLE_LLM_ENRICHMENT:
        log.debug("[llm] DISABLE_LLM_ENRICHMENT=true — skipping %s", symbol)
        return None

    # Circuit breaker: If we've had too many consecutive failures, pause LLM calls
    if _CIRCUIT_OPEN_UNTIL > now:
        log.warning(
            "[llm] Circuit breaker OPEN for %s (cooldown ends in %.0fs)",
            symbol,
            _CIRCUIT_OPEN_UNTIL - now,
        )
        return None

    def _remaining() -> float:
        """Seconds left before deadline, or large number if no deadline."""
        if deadline is None:
            return 15.0
        return max(5.0, deadline - time.time())

    import requests
    from urllib3.util.retry import Retry

    from src.utils.tls_adapter import ResilientTLSAdapter

    schema_json = json.dumps(schema.model_json_schema())
    system_prompt = (
        "Options trading analyst. Respond with valid JSON matching this schema exactly.\n"
        "Rules: Complete English only. No abbreviations (use 'underlying' not 'und', 'target' not 'tgt'). "
        "Specific numbers required. No vague language.\n"
        f"Schema:\n{schema_json}"
    )

    # Configure retry strategy for transient network errors
    retry_strategy = Retry(
        total=0,
        connect=0,
        read=0,
        status=1,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["POST"],
        raise_on_status=False,
    )
    adapter = ResilientTLSAdapter(max_retries=retry_strategy)
    adapter.SSL_RETRY_ATTEMPTS = 1
    session = requests.Session()
    session.mount("https://", adapter)

    # Resolve routing classification
    if purpose is None:
        if schema == LLMStrategyOptimization:
            purpose = "eod_review"
        elif schema in (LLMTradeVerdict, LLMExitAdvice):
            purpose = "live_verdict"
        else:
            purpose = "formatting"

    # Route model pipeline based on purpose
    if purpose == "eod_review":
        FREE_MODEL_PIPELINE = [
            {
                "model_group": "nemotron-eod-review",
                "providers": [
                    {
                        "name": "OpenRouter (Nemotron 3 Ultra 550B Free)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                    },
                    {
                        "name": "OpenRouter (Nemotron 3 Super 120B Free)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "nvidia/nemotron-3-super-120b-a12b:free",
                    },
                    {
                        "name": "OpenCode (Nemotron 3 Ultra Free)",
                        "env_key": "OPENCODE_API_KEY",
                        "url": "https://opencode.ai/zen/v1/chat/completions",
                        "model": "nemotron-3-ultra-free",
                    },
                    {
                        "name": "OpenCode (Nemotron 3 Super Free)",
                        "env_key": "OPENCODE_API_KEY",
                        "url": "https://opencode.ai/zen/v1/chat/completions",
                        "model": "nemotron-3-super-free",
                    },
                ],
            }
        ]
    elif purpose == "formatting":
        FREE_MODEL_PIPELINE = [
            {
                "model_group": "qwen-coder-formatting",
                "providers": [
                    {
                        "name": "OpenRouter (Qwen 3 Coder Free)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "qwen/qwen3-coder:free",
                    },
                    {
                        "name": "OpenRouter (Qwen 3 Next 80B Free)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "qwen/qwen3-next-80b-a3b-instruct:free",
                    },
                    {
                        "name": "Groq (Qwen 3 32B)",
                        "env_key": "GROQ_API_KEY",
                        "url": "https://api.groq.com/openai/v1/chat/completions",
                        "model": "qwen/qwen3-32b",
                    },
                    {
                        "name": "Groq (Qwen 3.6 27B)",
                        "env_key": "GROQ_API_KEY",
                        "url": "https://api.groq.com/openai/v1/chat/completions",
                        "model": "qwen/qwen3.6-27b",
                    },
                    {
                        "name": "Groq (Qwen 2.5 Coder 32B)",
                        "env_key": "GROQ_API_KEY",
                        "url": "https://api.groq.com/openai/v1/chat/completions",
                        "model": "qwen-2.5-coder-32b",
                    },
                    {
                        "name": "OpenRouter (Qwen 2.5 Coder 32B Free)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "qwen/qwen-2.5-coder-32b-instruct:free",
                    },
                    {
                        "name": "OpenRouter (Qwen 2.5 Coder 32B)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "qwen/qwen-2.5-coder-32b-instruct",
                    },
                ],
            }
        ]
    else:  # live_verdict — symbol-aware routing
        _base = symbol.upper().strip().split()[0]
        _class = get_symbol_class(_base)
        _is_mcx = _class == "MCX_COMMODITY"

        # Common tail — shared by both index and commodity routing
        _tail_pipeline = [
            {
                "model_group": "groq-reasoning",
                "providers": [
                    {
                        "name": "Groq (GPT-OSS 120B)",
                        "env_key": "GROQ_API_KEY",
                        "url": "https://api.groq.com/openai/v1/chat/completions",
                        "model": "openai/gpt-oss-120b",
                    },
                    {
                        "name": "Groq (Llama 3.3 70B)",
                        "env_key": "GROQ_API_KEY",
                        "url": "https://api.groq.com/openai/v1/chat/completions",
                        "model": "llama-3.3-70b-versatile",
                    },
                    {
                        "name": "Groq (Llama 4 Scout)",
                        "env_key": "GROQ_API_KEY",
                        "url": "https://api.groq.com/openai/v1/chat/completions",
                        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                    },
                ],
            },
            {
                "model_group": "openrouter-free-pool",
                "providers": [
                    {
                        "name": "OpenRouter (Llama 3.3 70B Free)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "meta-llama/llama-3.3-70b-instruct:free",
                    },
                    {
                        "name": "OpenRouter (Qwen 3 32B Free)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "qwen/qwen3-32b:free",
                    },
                    {
                        "name": "OpenRouter (Llama 3.2 3B Free)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "meta-llama/llama-3.2-3b-instruct:free",
                    },
                    {
                        "name": "OpenRouter (Nemotron 3 Super 120B Free)",
                        "env_key": "OPENROUTER_API_KEY",
                        "url": "https://openrouter.ai/api/v1/chat/completions",
                        "model": "nvidia/nemotron-3-super-120b-a12b:free",
                    },
                ],
            },
            {
                "model_group": "gemini-sdk",
                "providers": [
                    {
                        "name": "Gemini SDK (2.5 Flash)",
                        "env_key": "GEMINI_API_KEY",
                        "model": "gemini-2.5-flash",
                        "use_gemini_sdk": True,
                    },
                    {
                        "name": "Gemini SDK (2.0 Flash)",
                        "env_key": "GEMINI_API_KEY",
                        "model": "gemini-2.0-flash",
                        "use_gemini_sdk": True,
                    },
                ],
            },
        ]

        # GitHub Models — used in both paths at different priority
        _github_models = {
            "model_group": "github-models-free",
            "providers": [
                {
                    "name": "GitHub Models (GPT-4o-mini)",
                    "env_key": "GITHUB_TOKEN",
                    "url": "https://models.inference.ai.azure.com/chat/completions",
                    "model": "gpt-4o-mini",
                },
                {
                    "name": "GitHub Models (Llama 3.3 70B)",
                    "env_key": "GITHUB_TOKEN",
                    "url": "https://models.inference.ai.azure.com/chat/completions",
                    "model": "Llama-3.3-70B-Instruct",
                },
            ],
        }

        if _is_mcx:
            # MCX: OpenRouter GPT-OSS → GitHub → Groq → OpenRouter pool → Gemini SDK
            FREE_MODEL_PIPELINE = [
                {
                    "model_group": "openrouter-gpt-oss-free",
                    "providers": [
                        {
                            "name": "OpenRouter (GPT-OSS 120B Free)",
                            "env_key": "OPENROUTER_API_KEY",
                            "url": "https://openrouter.ai/api/v1/chat/completions",
                            "model": "openai/gpt-oss-120b:free",
                        },
                    ],
                },
                _github_models,
                *_tail_pipeline,
            ]
        else:
            # NSE/BSE indices: GitHub → Groq → OpenRouter pool → Gemini SDK
            FREE_MODEL_PIPELINE = [
                _github_models,
                *_tail_pipeline,
            ]

    max_tokens = _max_tokens_for_purpose(purpose)

    # Iterate through the prioritized pipeline
    for group in FREE_MODEL_PIPELINE:
        for provider in group["providers"]:
            key_name = provider["env_key"]
            api_key = os.environ.get(key_name)
            if not api_key:
                continue

            cooldown_key = _provider_cooldown_key(provider)
            cooldown_until = _PROVIDER_COOLDOWN_UNTIL.get(cooldown_key, 0.0)
            if cooldown_until > now:
                log.debug(
                    "[llm] Skipping %s — cooldown %.0fs remaining",
                    provider.get("name"),
                    cooldown_until - now,
                )
                continue

            remaining = _remaining()
            if deadline and time.time() >= deadline - 3:
                log.warning("[llm] Deadline reached, skipping remaining models")
                break

            if provider.get("use_gemini_sdk"):
                if not genai or now < _API_QUOTA_EXHAUSTED_UNTIL:
                    continue
                try:
                    log.info(
                        "[llm] Trying Gemini SDK model %s (%.0fs remaining)",
                        provider["model"],
                        remaining,
                    )
                    c = _get_client(api_key)
                    response = c.models.generate_content(
                        model=provider["model"],
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=schema,
                            temperature=0.2,
                        ),
                    )
                    result = schema.model_validate_json(response.text)
                    log.info(
                        "[llm] %s OK via Gemini SDK (%s)",
                        schema.__name__,
                        provider["model"],
                    )
                    _CONSECUTIVE_FAILURES = 0
                    return result
                except Exception as inner_e:
                    err = str(inner_e)
                    log.info("[llm] Gemini %s failed: %s", provider["model"], err[:200])
                    if "429" in err or "RESOURCE_EXHAUSTED" in err.upper():
                        log.warning(
                            "[llm] Gemini model hit quota. 10-min cooldown activated."
                        )
                        _API_QUOTA_EXHAUSTED_UNTIL = now + 600.0
                        _PROVIDER_COOLDOWN_UNTIL[cooldown_key] = now + 600.0
                continue

            # Handle OpenAI-compatible HTTP POST requests
            try:
                log.info(
                    "[llm] Trying %s via %s (%.0fs remaining)",
                    provider["model"],
                    provider["name"],
                    remaining,
                )

                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Connection": "close",
                }
                json_payload = {
                    "model": provider["model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                }
                if provider["name"].startswith("OpenRouter"):
                    headers["HTTP-Referer"] = "https://github.com/nsebot"
                    headers["X-Title"] = "NSEBOT Trading Engine"
                    json_payload["provider"] = {"allow_fallbacks": False}

                resp = session.post(
                    provider["url"],
                    headers=headers,
                    json=json_payload,
                    timeout=min(
                        remaining, 12.0
                    ),  # Hard cap: 12s per model so ≥2 models fit in 30s budget
                )
                if resp.status_code == 200:
                    resp_json = resp.json()
                    if "choices" not in resp_json:
                        err_msg = resp_json.get("error", {}).get("message") or str(
                            resp_json
                        )
                        log.info(
                            "[llm] %s (%s) returned 200 but error payload: %s",
                            provider["name"],
                            provider["model"],
                            err_msg[:200],
                        )
                        _register_provider_failure(provider, 429, err_msg, now)
                        continue
                    choices = resp_json["choices"]
                    if not choices:
                        log.info(
                            "[llm] %s (%s) returned empty choices",
                            provider["name"],
                            provider["model"],
                        )
                        continue
                    message = choices[0].get("message")
                    if not message:
                        log.info(
                            "[llm] %s (%s) returned choices but no message object",
                            provider["name"],
                            provider["model"],
                        )
                        continue
                    raw_content = message.get("content")
                    if raw_content is None:
                        log.info(
                            "[llm] %s (%s) returned null content",
                            provider["name"],
                            provider["model"],
                        )
                        continue
                    parsed = _extract_json(raw_content)
                    if isinstance(parsed, list):
                        if len(parsed) == 1 and isinstance(parsed[0], dict):
                            parsed = parsed[0]
                            log.debug(
                                "[llm] %s returned array — unwrapped single-element list",
                                provider["name"],
                            )
                        else:
                            raise ValueError(
                                f"{provider['name']} returned unexpected array with {len(parsed)} items"
                            )
                    result = schema.model_validate(parsed)
                    log.info(
                        "[llm] %s OK via %s (%s)",
                        schema.__name__,
                        provider["name"],
                        provider["model"],
                    )
                    _CONSECUTIVE_FAILURES = 0
                    return result
                if resp.status_code == 429:
                    log.warning(
                        "[llm] %s (%s) returned 429 (Too Many Requests/Quota Exceeded). Payload: %s",
                        provider["name"],
                        provider["model"],
                        resp.text[:250],
                    )
                    _register_provider_failure(provider, 429, resp.text, now)
                elif resp.status_code == 402:
                    log.info(
                        "[llm] %s (%s) failed: status=402 (insufficient credits). Payload: %s",
                        provider["name"],
                        provider["model"],
                        resp.text[:200],
                    )
                    _register_provider_failure(provider, 402, resp.text, now)
                else:
                    log.info(
                        "[llm] %s (%s) failed: status=%d %s",
                        provider["name"],
                        provider["model"],
                        resp.status_code,
                        resp.text[:200],
                    )
            except Exception as ex:
                log.info(
                    "[llm] %s (%s) exception: %s",
                    provider["name"],
                    provider["model"],
                    str(ex)[:200],
                )

    # Track consecutive failures and activate circuit breaker
    _CONSECUTIVE_FAILURES += 1
    if _CONSECUTIVE_FAILURES >= _CIRCUIT_BREAKER_THRESHOLD:
        _CIRCUIT_OPEN_UNTIL = now + _CIRCUIT_BREAKER_COOLDOWN
        log.error(
            "[llm] Circuit breaker ACTIVATED after %d failures. Pausing LLM calls for %.0fs.",
            _CONSECUTIVE_FAILURES,
            _CIRCUIT_BREAKER_COOLDOWN,
        )

    log.warning(
        "[llm] All LLM providers exhausted for %s (failures: %d)",
        symbol,
        _CONSECUTIVE_FAILURES,
    )
    return None


# ── Public API ───────────────────────────────────────────────────────────


def _get_option_premium_for_instrument(
    symbol: str, expiry: str, instrument: str, option_rows: list[dict]
) -> float | None:
    if not instrument:
        return None
    # Match strike and option type, e.g. "NIFTY 24500 CE 27Jun" or "NATURALGAS 310 PE 24Jun"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(CE|PE)", instrument, re.IGNORECASE)
    if not m:
        return None
    try:
        strike = float(m.group(1))
    except ValueError:
        return None
    opt_type = m.group(2).upper()

    for row in option_rows or []:
        if (
            abs(float(row.get("strike") or 0.0) - strike) < 0.01
            and str(row.get("option_type")).upper() == opt_type
        ):
            return float(row.get("ltp") or 0.0)
    return None


def _round_echoed_numbers(text: str, runaway_word_cap: int = 90) -> str:
    """
    Light cleanup for model free-text (thesis, reasoning):
      • Rounds 3+ decimal floats the model echoes from raw data (PCR 1.1156 → 1.12).
      • Does NOT collapse to one sentence — multi-sentence thesis is intentional.
      • Caps only pathological runaway output (> runaway_word_cap words) as a safety net,
        trimming to the last complete sentence within the cap.
    """
    if not text:
        return text
    text = re.sub(
        r"\b(\d+\.\d{3,})\b", lambda m: f"{float(m.group(1)):.2f}", text
    ).strip()
    words = text.split()
    if len(words) > runaway_word_cap:
        clipped = " ".join(words[:runaway_word_cap])
        # back off to the last sentence terminator so we don't cut mid-clause
        cut = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
        text = clipped[: cut + 1] if cut > 0 else clipped.rstrip(",;:") + "."
    return text


def _extract_json(raw: str) -> dict:
    """
    D1: Tolerant JSON extraction — handles markdown fences, leading prose,
    and invalid control characters that cause free-tier model parse failures.
    """
    raw = raw.strip()
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|```\s*$", "", raw, flags=re.MULTILINE).strip()
    # Grab outermost {...} when surrounding prose is present
    if not raw.startswith("{"):
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
    # Remove invalid control characters (causes "Invalid control character" parse failures)
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", raw)
    return json.loads(raw)


def _enforce_engine_alignment(
    result: LLMTradeVerdict,
    symbol: str,
    intel: dict,
) -> LLMTradeVerdict:
    """
    B2: Hard guard — LLM action MUST match engine bias.
    Direct direction flip (GO_LONG on a BEARISH engine, GO_SHORT on a BULLISH engine)
    → forced NO_TRADE with HIGH risk.  Downgrading to NO_TRADE is allowed;
    flipping is not, regardless of model or prompt.
    """
    from src.engine.verdict_sets import is_bearish, is_bullish

    vl = (intel or {}).get("verdict_label", "")
    engine_bias = "NEUTRAL"
    if is_bullish(vl):
        engine_bias = "BULLISH"
    elif is_bearish(vl):
        engine_bias = "BEARISH"

    if engine_bias == "NEUTRAL":
        return result  # engine itself has no direction — nothing to enforce

    action = result.action or "NO_TRADE"
    llm_bias = {"GO_LONG": "BULLISH", "GO_SHORT": "BEARISH", "NO_TRADE": "NEUTRAL"}.get(
        action, "NEUTRAL"
    )

    if llm_bias != "NEUTRAL" and llm_bias != engine_bias:
        log.warning(
            "[llm] %s: engine/LLM direction conflict — engine=%s (%s), LLM=%s. "
            "Forcing NO_TRADE (LLM may not flip engine direction).",
            symbol,
            engine_bias,
            vl,
            action,
        )
        update = {
            "action": "NO_TRADE",
            "risk_rating": "HIGH",
            "thesis": (
                f"Engine/AI direction conflict: engine={engine_bias} ({vl}), "
                f"AI={action}. Standing aside — do not trade against OI engine."
            ),
        }
        return (
            result.model_copy(update=update)
            if hasattr(result, "model_copy")
            else result.copy(update=update)
        )

    return result


def _sanitize_llm_verdict(
    result: LLMTradeVerdict,
    symbol: str,
    scan_context: dict,
) -> LLMTradeVerdict:
    """
    Post-process LLM verdict:
    1. Override symbol name in instrument field with the actual scanned symbol.
    2. Override expiry in instrument with the scan's expiry (nearest valid).
    3. Validate action/option-type consistency; downgrade to NO_TRADE on mismatch.
    """
    if result is None:
        return result

    # 1. Replace symbol in instrument string
    instr = result.instrument or ""
    # Strip any leading index name and replace with correct symbol
    for known in ("BANKNIFTY", "NIFTY", "NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"):
        if instr.upper().startswith(known) and known != symbol.upper():
            instr = symbol.upper() + instr[len(known) :]
            break

    # 2. Replace expiry token — find "DDMon" pattern and replace
    scan_expiry = scan_context.get("expiry") or ""  # "YYYY-MM-DD"
    if scan_expiry:
        try:
            from datetime import datetime as _dt

            exp_dt = _dt.strptime(scan_expiry, "%Y-%m-%d")
            exp_token = exp_dt.strftime("%d%b")
            # Remove any leading zero if day is single digit, e.g. "03Jul" -> "3Jul"
            if exp_token.startswith("0"):
                exp_token = exp_token[1:]
            instr = re.sub(r"\d{1,2}[A-Za-z]{3}", exp_token, instr)
        except Exception:
            pass

    # 3. Action / option-type consistency check
    if hasattr(result, "model_copy"):
        result = result.model_copy(update={"instrument": instr})
    else:
        result = result.copy(update={"instrument": instr})

    return result


def get_llm_verdict(
    symbol: str,
    intel_dict: dict,
    scan_context: dict,
    alerts: list[dict] | None = None,
    news_data: dict | None = None,
    open_trade: dict | None = None,
    trade_decision: dict | None = None,
) -> LLMTradeVerdict | None:
    """
    Generate a comprehensive AI trade verdict using all available market data.

    Phase 1: Deep context prompt feeding AI everything the bot knows.
    Executes with a 30-second timeout to prevent pipeline stalls.
    Supports in-memory caching to save tokens and prevent 429 quota exhaustion.
    """
    has_keys = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("OPENCODE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GITHUB_TOKEN")
    )
    if not has_keys:
        return None

    # Check cache first
    now = time.time()
    deadline = now + 75.0  # 75-second budget for the entire call
    current_underlying = float(scan_context.get("underlying") or 0.0)
    is_triggering = (
        trade_decision and "TRIGGERED" in str(trade_decision.get("status", "")).upper()
    )

    # Calculate DTE and store in scan_context
    # For MCX commodities, use futures_expiry (futures expire on a different schedule than options)
    _sym_base = symbol.upper().strip().split()[0]
    _is_mcx_sym = _sym_base in {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}
    if _is_mcx_sym and scan_context.get("futures_expiry"):
        expiry_val = scan_context.get("futures_expiry")
    else:
        expiry_val = scan_context.get("expiry")
    dte = 7
    if expiry_val:
        try:
            from datetime import date
            from datetime import datetime as _dt

            today_ist = _dt.now(_IST).date()
            if "-" in expiry_val:
                exp_dt = _dt.strptime(expiry_val, "%Y-%m-%d").date()
                dte = (exp_dt - today_ist).days
        except Exception:
            pass
    scan_context["days_to_expiry"] = dte

    cached = _VERDICT_CACHE.get(symbol)
    if (
        not is_triggering
        and cached
        and current_underlying > 0
        and cached["underlying"] > 0
    ):
        time_elapsed = now - cached["timestamp"]
        price_moved_pct = (
            abs(current_underlying - cached["underlying"]) / cached["underlying"]
        )
        verdict_same = cached["verdict_label"] == intel_dict.get("verdict_label")
        confidence_same = cached["confidence"] == intel_dict.get("confidence")

        # DTE-aware TTL
        if dte <= 1:
            ttl = 300.0  # 5 min
        elif dte <= 3:
            ttl = 600.0  # 10 min
        else:
            ttl = 1800.0  # 30 min

        # Check premium moved pct
        prem_moved_pct = 0.0
        cached_prem = cached.get("entry_premium", 0.0)
        if cached_prem > 0:
            option_rows = scan_context.get("option_rows") or []
            current_prem = _get_option_premium_for_instrument(
                symbol,
                scan_context.get("expiry", ""),
                cached["verdict"].instrument,
                option_rows,
            )
            if current_prem:
                prem_moved_pct = abs(current_prem - cached_prem) / cached_prem

        if (
            time_elapsed < ttl
            and price_moved_pct < 0.002
            and prem_moved_pct < 0.10
            and verdict_same
            and confidence_same
        ):
            log.debug(
                "[llm] Reusing cached LLM verdict for %s (age: %.1fs)",
                symbol,
                time_elapsed,
            )
            return cached["verdict"]

    prompt = _build_deep_prompt(
        symbol,
        intel_dict,
        scan_context,
        alerts=alerts,
        news_data=news_data,
        open_trade=open_trade,
        trade_decision=trade_decision,
    )
    try:
        result = _call_llm_api(symbol, prompt, LLMTradeVerdict, deadline=deadline)
        if result:
            # Override symbol/expiry, validate action/option-type consistency
            result = _sanitize_llm_verdict(result, symbol, scan_context)
            # B2: Hard guard — engine direction is non-negotiable
            result = _enforce_engine_alignment(result, symbol, intel_dict)
            # Round echoed decimals (PCR 1.1156 → 1.12); keep multi-sentence thesis intact
            _clean_thesis = _round_echoed_numbers(
                result.thesis or "", runaway_word_cap=90
            )
            if _clean_thesis != (result.thesis or ""):
                result = (
                    result.model_copy(update={"thesis": _clean_thesis})
                    if hasattr(result, "model_copy")
                    else result.copy(update={"thesis": _clean_thesis})
                )

            # Store in cache with current premium
            entry_prem = 0.0
            if result.instrument:
                option_rows = scan_context.get("option_rows") or []
                opt_prem = _get_option_premium_for_instrument(
                    symbol,
                    scan_context.get("expiry", ""),
                    result.instrument,
                    option_rows,
                )
                if opt_prem:
                    entry_prem = opt_prem

            _VERDICT_CACHE[symbol] = {
                "timestamp": now,
                "verdict_label": intel_dict.get("verdict_label"),
                "confidence": intel_dict.get("confidence"),
                "underlying": current_underlying,
                "entry_premium": entry_prem,
                "verdict": result,
            }
        return result
    except Exception as e:
        log.error("[llm] Unexpected error in get_llm_verdict for %s: %s", symbol, e)
        return None


def get_exit_advice(
    symbol: str,
    open_trade: dict,
    scan_context: dict,
    news_data: dict | None = None,
) -> LLMExitAdvice | None:
    """
    Phase 3: AI exit advisor for open trades.
    Returns dynamic SL/target adjustment recommendations.
    Supports in-memory caching to save tokens and prevent 429 quota exhaustion.
    """
    has_keys = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("OPENCODE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GITHUB_TOKEN")
    )
    if not has_keys:
        return None

    # Check cache first
    now = time.time()
    deadline = now + 75.0  # 75-second budget for the entire call
    current_underlying = float(scan_context.get("underlying") or 0.0)
    trade_id = open_trade.get("id")

    cached = _EXIT_CACHE.get(symbol)
    if (
        cached
        and trade_id == cached["trade_id"]
        and current_underlying > 0
        and cached["underlying"] > 0
    ):
        time_elapsed = now - cached["timestamp"]
        price_moved_pct = (
            abs(current_underlying - cached["underlying"]) / cached["underlying"]
        )

        # Cache is valid for 15 minutes if underlying price moved less than 0.2%
        if time_elapsed < 900.0 and price_moved_pct < 0.002:
            log.debug(
                "[llm] Reusing cached LLM exit advice for %s (age: %.1fs)",
                symbol,
                time_elapsed,
            )
            return cached["advice"]

    prompt = _build_exit_prompt(symbol, open_trade, scan_context, news_data)
    try:
        result = _call_llm_api(symbol, prompt, LLMExitAdvice, deadline=deadline)
        if result:
            _clean_reason = _round_echoed_numbers(
                result.reasoning or "", runaway_word_cap=60
            )
            if _clean_reason != (result.reasoning or ""):
                result = (
                    result.model_copy(update={"reasoning": _clean_reason})
                    if hasattr(result, "model_copy")
                    else result.copy(update={"reasoning": _clean_reason})
                )
            _EXIT_CACHE[symbol] = {
                "timestamp": now,
                "trade_id": trade_id,
                "underlying": current_underlying,
                "advice": result,
            }
        return result
    except Exception as e:
        log.error("[llm] Unexpected error in get_exit_advice for %s: %s", symbol, e)
        return None


def get_strategy_optimization_advice(
    trades: list[dict],
) -> LLMStrategyOptimization | None:
    """
    Review batch of closed trades to find systematic errors and suggest config tuning.
    'trades' should be a list of dicts from paper_trades or live_trades table.
    """
    if not trades:
        return None

    # Token-saving compression: Sym|Side|Verdict|Conf|PnL|Status
    summary_lines = []
    for t in trades:
        pnl = round(float(t.get("pnl_rupees") or 0))
        # Abbreviate status
        status_map = {
            "CLOSED_SL": "SL",
            "CLOSED_TARGET": "TGT",
            "CLOSED_MANUAL": "MAN",
            "AI_CLOSE_EARLY": "AI_EX",
        }
        stat = status_map.get(t.get("status", ""), "??")

        line = f"{t.get('symbol')}|{t.get('side')}|{t.get('verdict_label')}|{t.get('confidence_score')}%|{pnl}|{stat}"
        summary_lines.append(line)

    trade_data = "\n".join(summary_lines)

    prompt = f"""You are a Quantitative Strategy Optimizer. Review the following trade history summary to optimize the bot's risk and entry parameters.

TRADE HISTORY (Symbol|Side|Verdict|Conf|PnL|Status):
{trade_data}

TARGET PARAMETERS TO TUNE:
- live_min_confidence_core (0-100): Entry threshold for safe trades.
- live_max_concurrent_positions (1-5): Risk limit.
- live_ai_decision_mode: 'advisory' (Human must confirm), 'boost_only' (AI promotes marginal setups), 'full' (AI can veto/approve).
- live_ai_min_confidence_boost (0-100): Bar for AI promotion.
- live_ai_min_confidence_veto (0-100): Bar for AI veto.

INSTRUCTIONS:
1. Identify if specific symbols or verdicts (e.g. 'Short Covering') are consistently losing money.
2. If win rate is high (>80%) but PnL is low, suggest increasing max concurrent positions.
3. If confidence scores for losses are high (>90), suggest increasing the min_confidence threshold.
4. If AI decision mode is 'advisory' and performance is good, suggest 'boost_only'. If performance is poor, suggest 'full' or 'advisory' with higher veto.
5. Provide a JSON response with 'suggested_config_changes' mapping keys to new values.
6. Ensure suggested values are within reasonable bounds (e.g., confidence 0-100, positions 1-5).
"""

    has_keys = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("OPENCODE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GITHUB_TOKEN")
    )
    if not has_keys:
        log.warning("[llm] Strategy optimization skipped: No LLM API key configured.")
        return None

    deadline = time.time() + 30.0
    try:
        result = _call_llm_api(
            "portfolio", prompt, LLMStrategyOptimization, deadline=deadline
        )
        if result:
            log.info(
                "[llm] Portfolio optimization generated with %d suggestions",
                len(result.suggested_config_changes),
            )
        return result
    except Exception as e:
        log.error("[llm] Strategy optimization call failed: %s", e)
        return None
