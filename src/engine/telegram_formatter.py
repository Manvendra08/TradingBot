"""
User-Friendly Telegram Message Formatter
Converts technical intelligence into actionable, easy-to-understand messages

DEPRECATED — production uses digest.build_llm_consolidated_digest.
These functions are retained only for offline testing/demo.
Do not add features here.
"""
import logging

log = logging.getLogger(__name__)


def format_user_friendly_message(intel: dict, decision: dict = None, risk_info: dict = None) -> str:
    """
    Generate a user-friendly Telegram message from intelligence and decision data.
    
    Args:
        intel: IntelligenceResult dict with verdict, confidence, trend, etc.
        decision: Trade decision dict with status, setup_type, scores
        risk_info: Risk engine info with limits and current usage
    
    Returns:
        Formatted Telegram message string
    """
    lines = []
    
    symbol = intel.get("symbol", "?")
    verdict = intel.get("verdict_label", "Unknown")
    confidence = int(intel.get("confidence") or 0)
    bias = intel.get("bias", "NEUTRAL")
    trend = intel.get("trend", "")
    
    # ── HEADER ─────────────────────────────────────────────────────────────
    lines.append("=" * 25)
    
    expiry = intel.get("expiry")
    days_to_expiry = intel.get("days_to_expiry", -1)
    if expiry and days_to_expiry >= 0:
        lines.append(f"📊 {symbol} — TRADING SIGNAL (Expiry: {expiry} | {days_to_expiry} DTE)")
    else:
        lines.append(f"📊 {symbol} — TRADING SIGNAL")
        
    lines.append("=" * 25)
    
    # ── MAIN VERDICT (Simple & Clear) ──────────────────────────────────────
    verdict_emoji = intel.get("verdict_emoji", "⚪")
    
    if bias == "BULLISH":
        signal_text = "🟢 BUY SIGNAL"
        signal_color = "Bullish"
    elif bias == "BEARISH":
        signal_text = "🔴 SELL SIGNAL"
        signal_color = "Bearish"
    else:
        signal_text = "⚪ NEUTRAL"
        signal_color = "Neutral"
    
    lines.append("")
    lines.append(f"{signal_text}")
    lines.append(f"Verdict: {verdict}")
    lines.append(f"Confidence: {confidence}% {'🔥' if confidence >= 80 else ('⚡' if confidence >= 65 else '❄️')}")
    
    # ── SIMPLE EXPLANATION ─────────────────────────────────────────────────
    lines.append("")
    lines.append("📝 WHAT'S HAPPENING:")
    
    explanation = _get_simple_explanation(verdict, confidence)
    lines.append(f"  {explanation}")
    
    # ── DECISION ENGINE (Simple Traffic Light) ─────────────────────────────
    if decision:
        lines.append("")
        lines.append("✅ BOT DECISION:")
        
        status = decision.get("status", "BLOCKED")
        setup_type = decision.get("setup_type", "")
        scores = decision.get("scores", {})
        
        if status == "TRIGGERED_CORE":
            lines.append("  Status: ✅ GO AHEAD (High Quality)")
            quality = "HIGH"
        elif status == "TRIGGERED_EXPERIMENTAL":
            lines.append("  Status: 🧪 RISKY (Low Quality)")
            quality = "LOW"
        else:
            lines.append("  Status: ❌ WAIT (Not Ready)")
            quality = "BLOCKED"
        
        # Setup type explanation
        if setup_type == "CONFIRMED_REVERSAL":
            lines.append("  Type: 🔄 Reversal Trade (Counter-trend)")
            lines.append("  Why: Market reversing from previous trend")
        elif setup_type == "TREND_CONTINUATION":
            lines.append("  Type: 📈 Trend Trade (Following trend)")
            lines.append("  Why: Multiple scans confirm same direction")
        elif setup_type == "MOMENTUM_TRADE":
            lines.append("  Type: ⚡ Momentum Trade (Strong confluence)")
            lines.append("  Why: Multiple factors align")
        elif setup_type == "EXPERIMENTAL_SETUP":
            lines.append("  Type: 🧪 Experimental (Research only)")
            lines.append("  Why: Marginal setup, not recommended")
        
        # Score summary (simple)
        lines.append("")
        lines.append("  Score Breakdown:")
        
        conf = scores.get("confidence", 0)
        eq = scores.get("entry_quality", 0)
        ta = scores.get("trend_alignment", 0)
        regime = scores.get("regime_score", 0)
        momentum = scores.get("momentum_score", 0)
        
        # Confidence
        conf_bar = _get_bar(conf, 100)
        lines.append(f"    Confidence: {conf_bar} {conf}%")
        
        # Entry Quality
        eq_bar = _get_bar(eq, 100)
        lines.append(f"    Entry Quality: {eq_bar} {eq}/100")
        
        # Trend Alignment
        ta_bar = _get_bar(ta, 100)
        lines.append(f"    Trend Alignment: {ta_bar} {ta}%")
        
        # Regime
        regime_bar = _get_bar(regime, 100)
        lines.append(f"    Market Regime: {regime_bar} {regime}%")
        
        # Momentum (if available)
        if momentum > 0:
            mom_bar = _get_bar(momentum, 100)
            lines.append(f"    Momentum: {mom_bar} {momentum}%")
    
    # ── RISK CHECK ─────────────────────────────────────────────────────────
    if risk_info:
        lines.append("")
        lines.append("⚠️ RISK CHECK:")
        
        if risk_info.get("blocked"):
            lines.append(f"  ❌ BLOCKED: {risk_info.get('reason', 'Risk limit exceeded')}")
        else:
            open_trades = risk_info.get("open_trades", 0)
            max_trades = risk_info.get("max_trades", 4)
            daily_loss = risk_info.get("daily_loss", 0)
            max_loss = risk_info.get("max_loss", 10000)
            
            lines.append(f"  Open Trades: {open_trades}/{max_trades}")
            lines.append(f"  Daily Loss: ₹{daily_loss:,}/{max_loss:,}")
            
            if open_trades >= max_trades:
                lines.append("  ⚠️ At position limit — close existing trades first")
            if daily_loss >= max_loss * 0.8:
                lines.append("  ⚠️ Approaching daily loss limit")
    
    # ── ACTION PLAN (What to Do) ───────────────────────────────────────────
    lines.append("")
    lines.append("🎯 WHAT TO DO:")
    
    action = _get_action_plan(verdict, confidence, status if decision else "BLOCKED", symbol)
    for action_line in action:
        lines.append(f"  {action_line}")
    
    # ── CHART STATUS ───────────────────────────────────────────────────────
    if intel.get("chart_conflict"):
        lines.append("")
        lines.append("⚠️ CHART WARNING:")
        lines.append("  1H and 3H charts disagree")
        lines.append("  → Reduce position size or wait for alignment")
    
    # ── BROADER TREND ──────────────────────────────────────────────────────
    if trend:
        lines.append("")
        lines.append("📊 MARKET CONTEXT:")
        lines.append(f"  {trend}")
    
    # ── FOOTER ─────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("=" * 25)
    lines.append("⏰ Check back in 5 minutes for next scan")
    lines.append("=" * 25)
    
    return "\n".join(lines)
 
 
def _get_simple_explanation(verdict: str, confidence: int) -> str:
    """Get a simple, non-technical explanation of the verdict."""
    
    explanations = {
        "Long Buildup": "Buyers are accumulating positions. Price likely to go up.",
        "Short Buildup": "Sellers are accumulating positions. Price likely to go down.",
        "Short Covering": "Shorts are exiting. Rally may be temporary.",
        "Long Unwinding": "Longs are exiting. Decline may be temporary.",
        "Put Writing": "Smart money is buying puts (bullish). Support building.",
        "Call Writing": "Smart money is buying calls (bearish). Resistance building.",
        "OI Bias Bullish": "Options positioning suggests bullish bias.",
        "OI Bias Bearish": "Options positioning suggests bearish bias.",
        "Sideways": "No clear direction. Market is choppy.",
    }
    
    base = explanations.get(verdict, "Mixed signals detected.")
    
    if confidence >= 85:
        return f"{base} (Very High Confidence)"
    elif confidence >= 70:
        return f"{base} (High Confidence)"
    elif confidence >= 55:
        return f"{base} (Moderate Confidence)"
    else:
        return f"{base} (Low Confidence - Wait for confirmation)"
 
 
def _get_action_plan(verdict: str, confidence: int, status: str, symbol: str = "") -> list[str]:
    """Get actionable steps for the user."""
    
    actions = []
    
    # Status-based action
    if status == "TRIGGERED_CORE":
        actions.append("✅ Bot approved this trade")
        actions.append("→ You can enter if you agree with the setup")
    elif status == "TRIGGERED_EXPERIMENTAL":
        actions.append("🧪 Bot found a marginal setup")
        actions.append("→ Only trade if you're comfortable with higher risk")
    else:
        actions.append("❌ Bot is not ready to trade")
        actions.append("→ Wait for better conditions")
    
    is_natgas = "NATURALGAS" in str(symbol).upper()

    # Verdict-based action
    if "Long" in verdict or "Put" in verdict or "Bullish" in verdict:
        actions.append("")
        actions.append("IF YOU TRADE BULLISH:")
        if is_natgas:
            actions.append("  • Buy Futures (FUT)")
            actions.append("  • Set Stop Loss below support")
            actions.append("  • Target: Resistance level")
        else:
            actions.append("  • Sell Put (PE) at support level (OTM)")
            actions.append("  • Set Stop Loss (Premium): +50% from entry")
            actions.append("  • Target (Premium): -40% from entry (decay)")
    elif "Short" in verdict or "Call" in verdict or "Bearish" in verdict:
        actions.append("")
        actions.append("IF YOU TRADE BEARISH:")
        if is_natgas:
            actions.append("  • Sell Futures (FUT)")
            actions.append("  • Set Stop Loss above resistance")
            actions.append("  • Target: Support level")
        else:
            actions.append("  • Sell Call (CE) at resistance level (OTM)")
            actions.append("  • Set Stop Loss (Premium): +50% from entry")
            actions.append("  • Target (Premium): -40% from entry (decay)")
    
    # Confidence-based action
    if confidence < 55:
        actions.append("")
        actions.append("⚠️ LOW CONFIDENCE:")
        actions.append("  • Wait for next scan to confirm")
        actions.append("  • Or reduce position size")
    
    return actions


def _get_bar(value: int, max_val: int = 100) -> str:
    """Generate a simple progress bar."""
    percentage = min(100, max(0, int(value * 100 / max_val)))
    filled = percentage // 10
    empty = 10 - filled
    
    if percentage >= 70:
        color = "🟢"
    elif percentage >= 50:
        color = "🟡"
    else:
        color = "🔴"
    
    bar = "█" * filled + "░" * empty
    return f"{color} {bar}"


# ── ALTERNATIVE: COMPACT FORMAT ────────────────────────────────────────────

def format_compact_message(intel: dict, decision: dict = None) -> str:
    """
    Ultra-compact format for quick scanning (single line per section)
    """
    lines = []
    
    symbol = intel.get("symbol", "?")
    verdict = intel.get("verdict_label", "Unknown")
    confidence = int(intel.get("confidence") or 0)
    bias = intel.get("bias", "NEUTRAL")
    
    # Header
    if bias == "BULLISH":
        signal = "🟢 BUY"
    elif bias == "BEARISH":
        signal = "🔴 SELL"
    else:
        signal = "⚪ WAIT"
    
    expiry = intel.get("expiry")
    days_to_expiry = intel.get("days_to_expiry", -1)
    
    if expiry and days_to_expiry >= 0:
        lines.append(f"{signal} | {symbol} (Exp:{expiry} | {days_to_expiry}DTE) | {verdict} | Conf: {confidence}%")
    else:
        lines.append(f"{signal} | {symbol} | {verdict} | Conf: {confidence}%")
    
    # Decision
    if decision:
        status = decision.get("status", "BLOCKED")
        setup = decision.get("setup_type", "")
        
        if status == "TRIGGERED_CORE":
            status_text = "✅ GO"
        elif status == "TRIGGERED_EXPERIMENTAL":
            status_text = "🧪 RISKY"
        else:
            status_text = "❌ WAIT"
        
        setup_short = {
            "CONFIRMED_REVERSAL": "Reversal",
            "TREND_CONTINUATION": "Trend",
            "MOMENTUM_TRADE": "Momentum",
            "EXPERIMENTAL_SETUP": "Experimental",
        }.get(setup, "")
        
        lines.append(f"Decision: {status_text} | Type: {setup_short}")
        
        # Scores in one line
        scores = decision.get("scores", {})
        score_line = f"Scores: "
        score_line += f"Conf:{scores.get('confidence', 0)}% "
        score_line += f"EQ:{scores.get('entry_quality', 0)} "
        score_line += f"TA:{scores.get('trend_alignment', 0)} "
        score_line += f"Reg:{scores.get('regime_score', 0)}"
        lines.append(score_line)
    
    return "\n".join(lines)


# ── ALTERNATIVE: DETAILED FORMAT ───────────────────────────────────────────

def format_detailed_message(intel: dict, decision: dict = None, scan_context: dict = None) -> str:
    """
    Detailed format with all information (for power users)
    """
    lines = []
    
    symbol = intel.get("symbol", "?")
    verdict = intel.get("verdict_label", "Unknown")
    confidence = int(intel.get("confidence") or 0)
    bias = intel.get("bias", "NEUTRAL")
    
    expiry = intel.get("expiry")
    days_to_expiry = intel.get("days_to_expiry", -1)
    
    # Header
    if expiry and days_to_expiry >= 0:
        lines.append(f"🤖 NSEBOT INTELLIGENCE — {symbol} (Expiry: {expiry} | {days_to_expiry} DTE)")
    else:
        lines.append(f"🤖 NSEBOT INTELLIGENCE — {symbol}")
    lines.append("")
    
    # Signal
    if bias == "BULLISH":
        lines.append("🟢 BULLISH SIGNAL")
    elif bias == "BEARISH":
        lines.append("🔴 BEARISH SIGNAL")
    else:
        lines.append("⚪ NEUTRAL SIGNAL")
    
    lines.append(f"Verdict: {verdict}")
    lines.append(f"Confidence: {confidence}%")
    lines.append("")
    
    # Decision
    if decision:
        status = decision.get("status", "BLOCKED")
        setup = decision.get("setup_type", "")
        reason = decision.get("reason", "")
        
        if status == "TRIGGERED_CORE":
            exec_src = decision.get("execution_source", "")
            badge = f" [TFSS]" if "TFSS" in exec_src else ""
            lines.append(f"✅ TRADE APPROVED{badge} (High Quality)")
        elif status == "TRIGGERED_EXPERIMENTAL":
            exec_src = decision.get("execution_source", "")
            badge = f" [TFSS]" if "TFSS" in exec_src else ""
            lines.append(f"🧪 TRADE APPROVED{badge} (Experimental)")
        else:
            lines.append("❌ TRADE BLOCKED")
        
        lines.append(f"Setup Type: {setup}")
        if decision.get("execution_source"):
            lines.append(f"Execution Engine: {decision.get('execution_source')}")
        lines.append(f"Reason: {reason}")
        lines.append("")
        
        # Detailed scores
        scores = decision.get("scores", {})
        lines.append("SCORE ANALYSIS:")
        
        for key, value in scores.items():
            key_display = {
                "confidence": "Confidence",
                "entry_quality": "Entry Quality",
                "trend_alignment": "Trend Alignment",
                "regime_score": "Regime Score",
                "momentum_score": "Momentum Score",
            }.get(key, key.replace("_", " ").title())
            
            bar = _get_bar(value, 100)
            lines.append(f"  {key_display}: {bar} {value}")
        
        lines.append("")
    
    # Context
    if scan_context:
        lines.append("MARKET CONTEXT:")
        underlying = scan_context.get("underlying", 0)
        support = scan_context.get("support", 0)
        resistance = scan_context.get("resistance", 0)
        pcr = scan_context.get("pcr", 0)
        
        if underlying:
            is_commodity = symbol.upper().split()[0] in {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}
            lbl = "Future" if is_commodity else "Spot"
            fmt = ".2f" if is_commodity else ".0f"
            lines.append(f"  {lbl}: {underlying:{fmt}}")
        if support:
            lines.append(f"  Support: {support:.0f}")
        if resistance:
            lines.append(f"  Resistance: {resistance:.0f}")
        if pcr:
            lines.append(f"  PCR: {pcr:.2f}")
        lines.append("")
    
    # Trend
    trend = intel.get("trend", "")
    if trend:
        lines.append(f"BROADER TREND: {trend}")
        lines.append("")
    
    # Action
    lines.append("NEXT STEPS:")
    if decision and decision.get("status") != "BLOCKED":
        lines.append("  1. Review the setup on your chart")
        lines.append("  2. Confirm entry and exit levels")
        lines.append("  3. Place trade if you agree")
    else:
        lines.append("  1. Wait for next scan")
        lines.append("  2. Monitor for better setup")
    
    return "\n".join(lines)
