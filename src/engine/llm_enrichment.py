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
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pydantic import BaseModel, Field

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None

log = logging.getLogger(__name__)


# ── Response Schemas ─────────────────────────────────────────────────────

class LLMTradeVerdict(BaseModel):
    bias: str = Field(description="BULLISH, BEARISH, or NEUTRAL")
    confidence: int = Field(description="Confidence score from 0 to 100")
    strategy: str = Field(description="Suggested Option strategy (e.g., 'Bull Call Spread', 'Stand aside')")
    strike_selection: str = Field(description="Specific contract strikes logic (e.g., 'Buy ATM CE')")
    reasoning: str = Field(description="2-3 sentence rationale synthesising all data points")
    risk_rating: str = Field(description="HIGH, MEDIUM, or LOW — overall risk of taking this trade")
    exit_advice: str = Field(description="Dynamic SL/target suggestion based on current levels")
    news_synthesis: str = Field(description="How news headlines impact the trade thesis (or 'No news data' if unavailable)")


class LLMExitAdvice(BaseModel):
    action: str = Field(description="HOLD, TRAIL_SL, CLOSE_EARLY, or EXTEND_TARGET")
    new_sl_premium: float | None = Field(default=None, description="New stop-loss premium level, or null if unchanged")
    new_target_premium: float | None = Field(default=None, description="New target premium level, or null if unchanged")
    reasoning: str = Field(description="1-2 sentence rationale for the exit recommendation")
    urgency: str = Field(description="LOW, MEDIUM, or HIGH — how urgently this action should be taken")


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


def _build_deep_prompt(
    symbol: str,
    intel: dict,
    scan_context: dict,
    alerts: list[dict] | None = None,
    news_data: dict | None = None,
    open_trade: dict | None = None,
    trade_decision: dict | None = None,
) -> str:
    """Construct a comprehensive prompt feeding the AI all available data."""

    ctx = scan_context or {}

    prompt = f"""You are the Chief Trading Strategist at a proprietary F&O desk specializing in Indian NSE/MCX options.
Your job is to synthesize ALL data below into a single, actionable trade recommendation.

═══════════════════════════════════════════════════════
SYMBOL: {symbol}
═══════════════════════════════════════════════════════

1. RULE ENGINE OUTPUT
   Verdict: {intel.get('verdict_label')} ({intel.get('verdict_desc', '')})
   Bias: {intel.get('bias', 'UNKNOWN')}
   Confidence: {intel.get('confidence', 0)}%
   Trend: {intel.get('trend', 'UNKNOWN')}
   Chart Conflict (1H vs 3H): {intel.get('chart_conflict', False)}
   Days to Expiry: {intel.get('days_to_expiry', -1)}

2. FORCE ANALYSIS
{_format_forces(intel)}

3. OI & LEVELS
   Underlying: {ctx.get('underlying')}
   Prev Underlying: {ctx.get('prev_underlying')}
   Price Change: {ctx.get('price_change_points', 0)} pts ({ctx.get('price_change_pct', 'N/A')}%)
   ATM Strike: {ctx.get('atm_strike')}
   Support (PE OI Wall): {ctx.get('support')}
   Resistance (CE OI Wall): {ctx.get('resistance')}
   Max Pain: {ctx.get('max_pain')}
   PCR: {ctx.get('pcr')}
   Total CE OI: {ctx.get('total_ce_oi', 0):,}
   Total PE OI: {ctx.get('total_pe_oi', 0):,}
   CE OI Change: {ctx.get('ce_oi_change', 0):,}
   PE OI Change: {ctx.get('pe_oi_change', 0):,}

4. CHART DATA (OHLC Candles)
{_format_chart_data(ctx.get('chart_indicators'))}

5. ANOMALY ALERTS
{_summarize_alerts(alerts or [])}

6. NEWS SENTIMENT
{_format_news(news_data)}

7. CURRENT OPEN TRADE
{_format_open_trade(open_trade)}
"""

    if trade_decision:
        prompt += f"""
8. TRADE DECISION ENGINE OUTPUT
   Status: {trade_decision.get('status')}
   Setup Type: {trade_decision.get('setup_type')}
   Reason: {trade_decision.get('reason')}
   Scores: {json.dumps(trade_decision.get('scores', {}), default=str)}
"""

    prompt += """
═══════════════════════════════════════════════════════
INSTRUCTIONS
═══════════════════════════════════════════════════════
- Synthesize ALL the data above. Do not just echo the rule engine verdict.
- If chart and OI signals conflict, explain which signal you trust more and why.
- If news contradicts technical signals, flag the risk clearly.
- Provide a specific, actionable trade plan (not vague suggestions).
- Your exit_advice should reference specific price/premium levels.
- Your risk_rating should account for chart conflict, low confidence, adverse news, and DTE.
- If there is an open trade, your exit_advice should focus on whether to hold, trail, or close it.
"""

    return prompt


def _build_exit_prompt(
    symbol: str,
    open_trade: dict,
    scan_context: dict,
    news_data: dict | None = None,
) -> str:
    """Build a focused prompt for exit/management advice on an open trade."""
    ctx = scan_context or {}

    return f"""You are an options trade management specialist. Evaluate whether to HOLD, TRAIL_SL, CLOSE_EARLY, or EXTEND_TARGET.

OPEN TRADE:
{_format_open_trade(open_trade)}

CURRENT MARKET:
  Underlying: {ctx.get('underlying')}
  Price Change: {ctx.get('price_change_points', 0)} pts ({ctx.get('price_change_pct', 'N/A')}%)
  PCR: {ctx.get('pcr')}
  Support: {ctx.get('support')} | Resistance: {ctx.get('resistance')}

CHART:
{_format_chart_data(ctx.get('chart_indicators'))}

NEWS:
{_format_news(news_data)}

RULES:
- HOLD: Market conditions still support the trade thesis. No action needed.
- TRAIL_SL: Trade is in profit — suggest moving SL to lock gains. Provide new_sl_premium.
- CLOSE_EARLY: Market conditions have changed against the trade. Close now to limit loss or lock profit.
- EXTEND_TARGET: Strong momentum continues — suggest raising target. Provide new_target_premium.
- Set urgency to HIGH only if immediate action is needed (e.g., sharp adverse move).
"""


# ── Gemini API calls ─────────────────────────────────────────────────────

import time

_VERDICT_CACHE = {}
_EXIT_CACHE = {}
_API_QUOTA_EXHAUSTED_UNTIL = 0.0

def _call_gemini_api(symbol: str, prompt: str, response_schema=None) -> BaseModel | None:
    global _API_QUOTA_EXHAUSTED_UNTIL
    schema = response_schema or LLMTradeVerdict
    now = time.time()

    # 1. Try Gemini (Primary)
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key and genai and now >= _API_QUOTA_EXHAUSTED_UNTIL:
        # Fallback list of models: gemini-2.5-flash (primary), gemini-2.0-flash (fallback)
        models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash"]
        try:
            all_failed_429 = True
            c = _get_client(api_key)
            for model_name in models_to_try:
                try:
                    log.info("[llm] Attempting Gemini call with model: %s", model_name)
                    response = c.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=schema,
                            temperature=0.2,
                        )
                    )
                    result = schema.model_validate_json(response.text)
                    log.info("[llm] %s verdict generated for %s using model: %s", schema.__name__, symbol, model_name)
                    return result
                except Exception as inner_e:
                    err_str = str(inner_e)
                    if "429" not in err_str and "RESOURCE_EXHAUSTED" not in err_str.upper():
                        all_failed_429 = False
                    log.warning("[llm] Model %s failed for %s: %s", model_name, symbol, inner_e)
                    continue

            if all_failed_429:
                log.warning("[llm] All Gemini models failed with 429 RESOURCE_EXHAUSTED. Activating 10-minute API cooldown.")
                _API_QUOTA_EXHAUSTED_UNTIL = now + 600.0

        except Exception as e:
            log.warning("[llm] Gemini API client initialization failed for %s: %s", symbol, e)
    else:
        if not api_key or not genai:
            log.debug("[llm] Skipping Gemini (missing API key or SDK)")
        else:
            log.debug("[llm] Skipping Gemini due to active quota cooldown")

    # 2. Try Groq (Secondary Fallback)
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        groq_models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "gemma2-9b-it"]
        schema_json = json.dumps(schema.model_json_schema())
        system_prompt = (
            "You are a professional trading analyst. "
            "You MUST respond with a valid JSON object matching this JSON Schema. "
            "Make sure all field types and values match the schema definition exactly. "
            f"JSON Schema:\n{schema_json}"
        )
        import requests
        for model in groq_models:
            try:
                log.info("[llm] Attempting Groq call with model: %s", model)
                headers = {
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type": "application/json"
                }
                body = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2
                }
                resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=15)
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    result = schema.model_validate_json(content)
                    log.info("[llm] %s successfully generated via Groq model: %s", schema.__name__, model)
                    return result
                else:
                    log.warning("[llm] Groq model %s failed: status=%d error=%s", model, resp.status_code, resp.text)
            except Exception as ge:
                log.warning("[llm] Groq model %s failed with exception: %s", model, ge)
                continue

    # 3. Try OpenRouter (Last Fallback)
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if openrouter_key:
        openrouter_models = [
            "openrouter/free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen3-coder:free"
        ]
        schema_json = json.dumps(schema.model_json_schema())
        system_prompt = (
            "You are a professional trading analyst. "
            "You MUST respond with a valid JSON object matching this JSON Schema. "
            "Make sure all field types and values match the schema definition exactly. "
            f"JSON Schema:\n{schema_json}"
        )
        import requests
        for model in openrouter_models:
            try:
                log.info("[llm] Attempting OpenRouter call with model: %s", model)
                headers = {
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/Manvendra08/TradingBot",
                    "X-Title": "NSEBOT F&O"
                }
                body = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2
                }
                resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body, timeout=15)
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    result = schema.model_validate_json(content)
                    log.info("[llm] %s successfully generated via OpenRouter model: %s", schema.__name__, model)
                    return result
                else:
                    log.warning("[llm] OpenRouter model %s failed: status=%d error=%s", model, resp.status_code, resp.text)
            except Exception as ore:
                log.warning("[llm] OpenRouter model %s failed with exception: %s", model, ore)
                continue

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
    Executes with a 20-second timeout to prevent pipeline stalls.
    Supports in-memory caching to save tokens and prevent 429 quota exhaustion.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    groq_key = os.environ.get("GROQ_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key and not groq_key and not openrouter_key:
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
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call_gemini_api, symbol, prompt, LLMTradeVerdict)
            result = future.result(timeout=20.0)
            if result:
                # Update cache on success
                _VERDICT_CACHE[symbol] = {
                    "timestamp": now,
                    "verdict_label": intel_dict.get("verdict_label"),
                    "confidence": intel_dict.get("confidence"),
                    "underlying": current_underlying,
                    "verdict": result
                }
            return result
    except TimeoutError:
        log.warning("[llm] Gemini API timed out after 20s for %s", symbol)
        return None
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
    api_key = os.environ.get("GEMINI_API_KEY")
    groq_key = os.environ.get("GROQ_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key and not groq_key and not openrouter_key:
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
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call_gemini_api, symbol, prompt, LLMExitAdvice)
            result = future.result(timeout=15.0)
            if result:
                # Update cache on success
                _EXIT_CACHE[symbol] = {
                    "timestamp": now,
                    "trade_id": trade_id,
                    "underlying": current_underlying,
                    "advice": result
                }
            return result
    except TimeoutError:
        log.warning("[llm] Exit advice timed out after 15s for %s", symbol)
        return None
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

    api_key = os.environ.get("GEMINI_API_KEY")
    groq_key = os.environ.get("GROQ_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key and not groq_key and not openrouter_key:
        log.warning("[llm] Strategy optimization skipped: No LLM API key configured.")
        return None

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            # Reusing the existing API caller with the optimization schema
            future = executor.submit(_call_gemini_api, "portfolio", prompt, LLMStrategyOptimization)
            result = future.result(timeout=30.0)
            if result:
                log.info("[llm] Portfolio optimization generated with %d suggestions", 
                         len(result.suggested_config_changes))
            return result
    except Exception as e:
        log.error("[llm] Strategy optimization call failed: %s", e)
        return None
