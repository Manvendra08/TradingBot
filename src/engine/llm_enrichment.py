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
import os
import logging
from datetime import datetime
from pydantic import BaseModel, Field

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None

log = logging.getLogger(__name__)


# ── Response Schemas ─────────────────────────────────────────────────────

class LLMTradeVerdict(BaseModel):
    """
    Action-oriented trade verdict for traders.
    Structured for quick decision-making with specific levels.
    """
    # Decision signal
    action: str = Field(description="GO_LONG, GO_SHORT, or NO_TRADE — clear executable signal")
    confidence: int = Field(description="Confidence 0-100 in this specific setup")
    
    # Trade specification
    instrument: str = Field(description="Exact contract: e.g., 'NIFTY 24500 CE 27Jun' or 'CRUDEOIL 7100 PE 20Jun'")
    entry_trigger: str = Field(description="Specific condition to enter: e.g., 'Underlying crosses above 24520' or 'Premium breaks 185'")
    entry_premium_range: str = Field(description="Acceptable entry premium range: e.g., '180-195' or 'ATM ± 1 strike'")
    
    # Risk management
    stop_loss: str = Field(description="Exact SL level: 'Premium 140' or 'Underlying 24450' — must be specific number")
    target_1: str = Field(description="First profit target: 'Premium 230' or 'Underlying 24600'")
    target_2: str = Field(description="Extended target if momentum continues: 'Premium 280' or 'Underlying 24700'")
    risk_reward: str = Field(description="Calculated R:R ratio: e.g., '1:1.8' or '1:2.5'")
    
    # Thesis and invalidation
    thesis: str = Field(description="1-sentence core thesis: why this trade works NOW")
    invalidation: str = Field(description="What kills the trade: 'If underlying drops below 24400' or 'If PCR falls below 0.8'")
    
    # Context
    risk_rating: str = Field(description="LOW, MEDIUM, HIGH — overall risk considering macro events, expiry proximity, volatility")
    catalyst: str = Field(description="Upcoming event that could accelerate or invalidate: 'EIA report Thursday 8:30PM' or 'No major catalyst'")


class LLMExitAdvice(BaseModel):
    action: str = Field(description="HOLD, TRAIL_SL, CLOSE_EARLY, or EXTEND_TARGET")
    new_sl_premium: float | None = Field(default=None, description="New stop-loss premium level, or null if unchanged")
    new_target_premium: float | None = Field(default=None, description="New target premium level, or null if unchanged")
    reasoning: str = Field(description="1-2 sentence rationale for the exit recommendation")
    urgency: str = Field(description="LOW, MEDIUM, or HIGH — how urgently this action should be taken")


class LLMStrategyOptimization(BaseModel):
    suggested_config_changes: dict[str, float | str | int] = Field(description="Dictionary of configuration keys and their new recommended values")
    analysis: str = Field(description="Brief analysis explaining why these changes were suggested based on trade history")


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
        lines.append(f"  {tf.upper()} Candle: O={ohlc.get('open'):.2f} H={ohlc.get('high'):.2f} L={ohlc.get('low'):.2f} C={ohlc.get('close'):.2f} | Sentiment: {sentiment}")
        if prev:
            lines.append(f"  {tf.upper()} Prev:   O={prev.get('open'):.2f} H={prev.get('high'):.2f} L={prev.get('low'):.2f} C={prev.get('close'):.2f}")
    return "\n".join(lines) if lines else "No chart data available."


def _format_news(news_data: dict | None) -> str:
    """Format news data for the AI prompt."""
    if not news_data or not news_data.get("items"):
        return "No news data available for this symbol."

    direction = news_data.get("current_news_direction", "MIXED")
    score = news_data.get("news_score_current", 0)
    count = news_data.get("count_24h", 0)
    items = news_data.get("items", [])[:5]

    lines = [f"  News Direction: {direction} (score: {score}) | {count} articles in 24h"]
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

    # ── MCX Commodities ──────────────────────────────────────────────────
    if "NATURALGAS" in base:
        return """  Symbol type: MCX Natural Gas Futures (USD-denominated, INR-settled)
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

    if "CRUDEOIL" in base:
        return """  Symbol type: MCX Crude Oil Futures (Brent/WTI proxy, USD-denominated, INR-settled)
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

    if "GOLD" in base:
        return """  Symbol type: MCX Gold Futures (USD-denominated, INR-settled)
  Primary drivers:
    - US Federal Reserve rate decisions and dot-plot guidance
    - US CPI/PCE inflation prints (monthly) — higher inflation = bullish gold
    - USD Index (DXY): Strong USD = bearish gold; weak USD = bullish gold
    - INR/USD: Weaker INR inflates MCX gold price independently of spot
    - Geopolitical safe-haven demand; central bank gold buying (RBI, PBoC)
  Key risk: Fed FOMC statements, US NFP, CPI day volatility is extreme
  Seasonality: Akshaya Tritiya / Dhanteras / wedding season → INR demand spikes"""

    if "SILVER" in base:
        return """  Symbol type: MCX Silver Futures (USD-denominated, INR-settled)
  Primary drivers: Industrial demand (solar panels, EVs), Gold correlation (~0.85)
  Key risk: More volatile than gold; tracks gold direction but amplifies moves 2-3x
  Watch: US manufacturing PMI (industrial demand signal), Gold/Silver ratio extremes"""

    # ── NSE Index Options ────────────────────────────────────────────────
    if "BANKNIFTY" in base:
        return """  Symbol type: NSE BANKNIFTY Index Options (INR)
  Primary drivers:
    - RBI Monetary Policy Committee (MPC) — rate decisions & stance (every 2 months)
    - Bank credit growth, NPA cycles, PSU bank disinvestment news
    - FII/DII net flows (daily): Sustained FII selling → index headwind
    - US Fed policy (risk-on/risk-off global sentiment)
    - India VIX: VIX > 20 = elevated uncertainty; VIX < 12 = complacency risk
  BANKNIFTY-specific: Beta ~1.5x vs Nifty; highly sensitive to RBI rate surprises
  Expiry behaviour: Weekly expiry (Thursday) → gamma squeeze risk near ATM last 2 days
  Key risk: RBI policy day, budget day, election results = binary events"""

    if "NIFTY" in base:
        return """  Symbol type: NSE NIFTY 50 Index Options (INR)
  Primary drivers:
    - RBI Monetary Policy Committee (MPC) — rate decisions & stance (every 2 months)
    - FII/DII net flows (daily): Sustained FII selling → index headwind
    - US Fed policy, DXY, US equity overnight moves (SGX Nifty pre-market)
    - India macro: GDP, CPI, IIP prints (monthly)
    - India VIX: VIX > 20 = elevated uncertainty; VIX < 12 = complacency risk
  Expiry behaviour: Weekly expiry (Thursday) → gamma squeeze risk near ATM last 2 days
  Key risk: Budget day, election results, RBI policy day = binary events"""

    # ── Generic fallback ─────────────────────────────────────────────────
    return """  No specific macro context available for this symbol.
  General reminder: Consider broader market sentiment, FII flows, and any
  scheduled economic events before taking directional positions."""


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
        pcr_dir = "rising" if pcr_newest > pcr_oldest + 0.05 else (
            "falling" if pcr_newest < pcr_oldest - 0.05 else "stable"
        )
        lines.append(f"  PCR Trend: {pcr_dir} ({pcr_oldest:.2f} \u2192 {pcr_newest:.2f})")

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
        lines.append(f"  OI Trend: {oi_dir} (recent 3: {recent_net:+,} vs prior 3: {prior_net:+,})")

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

    prompt = f"""Deliver a TRADE PLAN with specific levels. No analysis prose.

{symbol} | {datetime.now().strftime("%a %H:%M")} | Underlying: {ctx.get('underlying')} | ATM: {ctx.get('atm_strike')}

DATA:
• Verdict: {intel.get('verdict_label')} @ {intel.get('confidence', 0)}% | Trend: {intel.get('trend', 'N/A')}
• S/R: {ctx.get('support')}/{ctx.get('resistance')} | MaxPain: {ctx.get('max_pain')} | PCR: {ctx.get('pcr')}
• OIΔ CE:{ctx.get('ce_oi_change', 0):,} PE:{ctx.get('pe_oi_change', 0):,}
• Chart: {_format_chart_data(ctx.get('chart_indicators'))}
• Alerts: {_summarize_alerts(alerts or [])}

HISTORICAL:
{_format_historical_oi(symbol)}
"""

    if news_data and news_data.get("items"):
        prompt += f"NEWS: {_format_news(news_data)}\n"
    else:
        prompt += f"MACRO: {_format_macro_context(symbol)}\n"

    if open_trade:
        prompt += f"POSITION: {_format_open_trade(open_trade)}\n"

    if trade_decision:
        prompt += f"ENGINE: {trade_decision.get('status')} — {trade_decision.get('reason', '')}\n"

    prompt += """OUTPUT FIELDS (all required, specific numbers):
• action: GO_LONG (buy CE/sell PE) | GO_SHORT (buy PE/sell CE) | NO_TRADE
• confidence: 0-100
• instrument: "NIFTY 24500 CE 27Jun" format
• entry_trigger: exact condition (e.g., "Underlying breaks 24520 with volume")
• entry_premium_range: "180-195"
• stop_loss: exact level ("Premium 140" or "Underlying 24450")
• target_1: first profit level
• target_2: extended target
• risk_reward: "1:1.8" format
• thesis: one sentence why NOW
• invalidation: what kills the trade
• risk_rating: LOW | MEDIUM | HIGH (HIGH if macro event <2h, <2 DTE, or chart conflict)
• catalyst: upcoming event or "None"

RULES: Reference actual levels from data. If NO_TRADE, fill what WOULD trigger. Pick stronger signal on conflict."""

    return prompt


def _build_exit_prompt(
    symbol: str,
    open_trade: dict,
    scan_context: dict,
    news_data: dict | None = None,
) -> str:
    """Build a focused prompt for exit/management advice on an open trade."""
    ctx = scan_context or {}

    return f"""Trade management decision. Complete English, specific premium levels.

TIME: {datetime.now().strftime("%a %H:%M")}
POSITION: {_format_open_trade(open_trade)}
MARKET: Underlying {ctx.get('underlying')} | Chg {ctx.get('price_change_points', 0)}pts ({ctx.get('price_change_pct', 'N/A')}%) | PCR {ctx.get('pcr')} | S/R {ctx.get('support')}/{ctx.get('resistance')}
CHART: {_format_chart_data(ctx.get('chart_indicators'))}
NEWS: {_format_news(news_data)}

ACTION OPTIONS:
• HOLD: Thesis intact, no change needed
• TRAIL_SL: Profitable — lock gains (provide new_sl_premium)
• CLOSE_EARLY: Thesis broken — exit now (provide exit premium in reasoning)
• EXTEND_TARGET: Strong momentum — raise target (provide new_target_premium)

URGENCY: HIGH only for immediate threat (sharp adverse move, key level breach). Otherwise LOW/MEDIUM.
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

def _call_llm_api(symbol: str, prompt: str, response_schema=None, deadline: float | None = None) -> BaseModel | None:
    """Call LLM APIs in priority order: OpenRouter (primary) → Gemini (Fallback 1) → Groq (Fallback 2).
    
    Args:
        deadline: Unix timestamp by which we must finish. Each model attempt uses
                  remaining_time as its HTTP timeout, so we never overshoot.
    """
    global _API_QUOTA_EXHAUSTED_UNTIL, _CONSECUTIVE_FAILURES, _CIRCUIT_OPEN_UNTIL
    schema = response_schema or LLMTradeVerdict
    now = time.time()

    # Circuit breaker: If we've had too many consecutive failures, pause LLM calls
    if _CIRCUIT_OPEN_UNTIL > now:
        log.warning("[llm] Circuit breaker OPEN for %s (cooldown ends in %.0fs)", symbol, _CIRCUIT_OPEN_UNTIL - now)
        return None

    def _remaining() -> float:
        """Seconds left before deadline, or large number if no deadline."""
        if deadline is None:
            return 15.0
        return max(5.0, deadline - time.time())

    import requests
    from requests.adapters import HTTPAdapter
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
        total=1,  # Reduced retries to fail fast
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"],
    )
    adapter = ResilientTLSAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)

    # ── PRIMARY: OpenRouter free tier — openrouter/free model ─────────────
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if openrouter_key:
        remaining = _remaining()
        if not (deadline and time.time() >= deadline - 3):
            try:
                log.info("[llm] PRIMARY → OpenRouter openrouter/free (%.0fs remaining)", remaining)
                resp = session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {openrouter_key}",
                        "Content-Type": "application/json",
                        "Connection": "close",
                        "HTTP-Referer": "https://github.com/nsebot",
                        "X-Title": "NSEBOT Trading Engine"
                    },
                    json={
                        "model": "openrouter/free",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.2,
                    },
                    timeout=min(remaining, 15.0),
                )
                if resp.status_code == 200:
                    raw_content = resp.json()["choices"][0]["message"]["content"]
                    # Defensive: some free models return [{...}] instead of {...}
                    parsed = json.loads(raw_content)
                    if isinstance(parsed, list):
                        if len(parsed) == 1 and isinstance(parsed[0], dict):
                            parsed = parsed[0]
                            log.debug("[llm] OpenRouter returned array — unwrapped single-element list")
                        else:
                            raise ValueError(f"OpenRouter returned unexpected array with {len(parsed)} items")
                    result = schema.model_validate(parsed)
                    log.info("[llm] %s OK via OpenRouter openrouter/free", schema.__name__)
                    _CONSECUTIVE_FAILURES = 0
                    return result
                log.warning("[llm] OpenRouter failed: status=%d %s", resp.status_code, resp.text[:200])
            except Exception as oe:
                log.warning("[llm] OpenRouter exception: %s", str(oe)[:200])

    # ── FALLBACK 1: Gemini free tier — 1500 req/day ───────────────────────
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key and genai and now >= _API_QUOTA_EXHAUSTED_UNTIL:
        gemini_models = [
            "gemini-2.5-flash",   # Best free Gemini model
            "gemini-2.0-flash",   # Stable alternative
        ]
        try:
            all_429 = True
            c = _get_client(gemini_key)
            for idx, model_name in enumerate(gemini_models):
                remaining = _remaining()
                if deadline and time.time() >= deadline - 3:
                    log.warning("[llm] Skipping %s — deadline reached", model_name)
                    break
                tier = "FALLBACK-1" if idx == 0 else f"GEMINI-FALLBACK-{idx + 1}"
                try:
                    log.info("[llm] %s → %s (%.0fs remaining)", tier, model_name, remaining)
                    response = c.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=schema,
                            temperature=0.2,
                        ),
                    )
                    result = schema.model_validate_json(response.text)
                    log.info("[llm] %s OK via Gemini %s for %s", schema.__name__, model_name, symbol)
                    _CONSECUTIVE_FAILURES = 0
                    return result
                except Exception as inner_e:
                    err = str(inner_e)
                    if "429" not in err and "RESOURCE_EXHAUSTED" not in err.upper():
                        all_429 = False
                    log.warning("[llm] Gemini %s failed for %s: %s", model_name, symbol, err[:200])
            if all_429:
                log.warning("[llm] All Gemini models hit quota. 10-min cooldown activated.")
                _API_QUOTA_EXHAUSTED_UNTIL = now + 600.0
        except Exception as e:
            log.warning("[llm] Gemini client init failed for %s: %s", symbol, e)
    elif not gemini_key or not genai:
        log.debug("[llm] Gemini skipped (no API key or SDK)")
    else:
        log.debug("[llm] Gemini skipped — quota cooldown active until %.0fs", _API_QUOTA_EXHAUSTED_UNTIL - now)

    # ── FALLBACK 2: Groq free tier — 14,400+ req/day, ultra-fast inference ──
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        groq_models = [
            "llama-3.3-70b-versatile",                   # Best quality
            "llama-3.1-8b-instant",                      # Ultra-fast fallback
            "llama3-8b-8192",                            # Lightest, max rate limits
        ]
        for idx, model in enumerate(groq_models):
            # Skip attempt if deadline already passed
            remaining = _remaining()
            if deadline and time.time() >= deadline - 3:
                log.warning("[llm] Skipping %s — deadline reached", model)
                break

            tier = "FALLBACK-2" if idx == 0 else f"GROQ-FALLBACK-{idx}"
            try:
                log.info("[llm] Groq %s → %s (%.0fs remaining)", tier, model, remaining)
                resp = session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {groq_key}",
                        "Content-Type": "application/json",
                        "Connection": "close"
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.2,
                    },
                    timeout=min(remaining, 15.0),  # use remaining time, cap at 15s
                )
                if resp.status_code == 200:
                    raw_content = resp.json()["choices"][0]["message"]["content"]
                    # Defensive: some models return [{...}] instead of {...}
                    parsed = json.loads(raw_content)
                    if isinstance(parsed, list):
                        if len(parsed) == 1 and isinstance(parsed[0], dict):
                            parsed = parsed[0]
                            log.debug("[llm] Groq returned array — unwrapped single-element list")
                        else:
                            raise ValueError(f"Groq returned unexpected array with {len(parsed)} items")
                    result = schema.model_validate(parsed)
                    log.info("[llm] %s OK via Groq %s", schema.__name__, model)
                    _CONSECUTIVE_FAILURES = 0
                    return result
                log.warning("[llm] Groq %s failed: status=%d %s", model, resp.status_code, resp.text[:200])
            except Exception as ge:
                log.warning("[llm] Groq %s exception: %s", model, str(ge)[:200])

    # Track consecutive failures and activate circuit breaker
    _CONSECUTIVE_FAILURES += 1
    if _CONSECUTIVE_FAILURES >= _CIRCUIT_BREAKER_THRESHOLD:
        _CIRCUIT_OPEN_UNTIL = now + _CIRCUIT_BREAKER_COOLDOWN
        log.error("[llm] Circuit breaker ACTIVATED after %d failures. Pausing LLM calls for %.0fs.", 
                  _CONSECUTIVE_FAILURES, _CIRCUIT_BREAKER_COOLDOWN)

    log.warning("[llm] All LLM providers exhausted for %s (failures: %d)", symbol, _CONSECUTIVE_FAILURES)
    return None


# ── Public API ───────────────────────────────────────────────────────────

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
    if not os.environ.get("OPENROUTER_API_KEY") and not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GROQ_API_KEY"):
        return None

    # Check cache first
    now = time.time()
    current_underlying = float(scan_context.get("underlying") or 0.0)
    is_triggering = trade_decision and "TRIGGERED" in str(trade_decision.get("status", "")).upper()
    
    cached = _VERDICT_CACHE.get(symbol)
    if not is_triggering and cached and current_underlying > 0 and cached["underlying"] > 0:
        time_elapsed = now - cached["timestamp"]
        price_moved_pct = abs(current_underlying - cached["underlying"]) / cached["underlying"]
        verdict_same = cached["verdict_label"] == intel_dict.get("verdict_label")
        confidence_same = cached["confidence"] == intel_dict.get("confidence")
        
        # Cache is valid for 30 minutes if rule engine verdict and confidence are the same,
        # and underlying price moved less than 0.2%
        if time_elapsed < 1800.0 and price_moved_pct < 0.002 and verdict_same and confidence_same:
            log.debug("[llm] Reusing cached LLM verdict for %s (age: %.1fs)", symbol, time_elapsed)
            return cached["verdict"]

    prompt = _build_deep_prompt(
        symbol, intel_dict, scan_context,
        alerts=alerts,
        news_data=news_data,
        open_trade=open_trade,
        trade_decision=trade_decision,
    )
    try:
        result = _call_llm_api(symbol, prompt, LLMTradeVerdict)
        if result:
            _VERDICT_CACHE[symbol] = {
                "timestamp": now,
                "verdict_label": intel_dict.get("verdict_label"),
                "confidence": intel_dict.get("confidence"),
                "underlying": current_underlying,
                "verdict": result
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
    if not os.environ.get("OPENROUTER_API_KEY") and not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GROQ_API_KEY"):
        return None

    # Check cache first
    now = time.time()
    current_underlying = float(scan_context.get("underlying") or 0.0)
    trade_id = open_trade.get("id")
    
    cached = _EXIT_CACHE.get(symbol)
    if cached and trade_id == cached["trade_id"] and current_underlying > 0 and cached["underlying"] > 0:
        time_elapsed = now - cached["timestamp"]
        price_moved_pct = abs(current_underlying - cached["underlying"]) / cached["underlying"]
        
        # Cache is valid for 15 minutes if underlying price moved less than 0.2%
        if time_elapsed < 900.0 and price_moved_pct < 0.002:
            log.debug("[llm] Reusing cached LLM exit advice for %s (age: %.1fs)", symbol, time_elapsed)
            return cached["advice"]

    prompt = _build_exit_prompt(symbol, open_trade, scan_context, news_data)
    try:
        result = _call_llm_api(symbol, prompt, LLMExitAdvice)
        if result:
            _EXIT_CACHE[symbol] = {
                "timestamp": now,
                "trade_id": trade_id,
                "underlying": current_underlying,
                "advice": result
            }
        return result
    except Exception as e:
        log.error("[llm] Unexpected error in get_exit_advice for %s: %s", symbol, e)
        return None

def get_strategy_optimization_advice(trades: list[dict]) -> LLMStrategyOptimization | None:
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
        status_map = {"CLOSED_SL": "SL", "CLOSED_TARGET": "TGT", "CLOSED_MANUAL": "MAN", "AI_CLOSE_EARLY": "AI_EX"}
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

    if not os.environ.get("OPENROUTER_API_KEY") and not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GROQ_API_KEY"):
        log.warning("[llm] Strategy optimization skipped: No LLM API key configured.")
        return None

    try:
        result = _call_llm_api("portfolio", prompt, LLMStrategyOptimization)
        if result:
            log.info("[llm] Portfolio optimization generated with %d suggestions",
                     len(result.suggested_config_changes))
        return result
    except Exception as e:
        log.error("[llm] Strategy optimization call failed: %s", e)
        return None
