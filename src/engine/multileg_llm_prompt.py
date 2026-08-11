"""
Multi-leg strategy LLM prompt builder.

Builds the prompt that makes the LLM think like an experienced options trader,
using the core engine's data as raw material but reasoning beyond pure math.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


def _format_full_option_chain(
    option_rows: list[dict], atm_strike: float, underlying: float
) -> str:
    """Format ALL option chain strikes (not just ATM±3).

    Groups by strike, shows CE/PE side by side for easy comparison.
    """
    if not option_rows:
        return "  No option chain data available."

    # Group by strike
    strikes: dict[float, dict] = {}
    for row in option_rows:
        s = float(row.get("strike") or 0)
        if s <= 0:
            continue
        if s not in strikes:
            strikes[s] = {}
        opt_type = str(row.get("option_type") or "").upper()
        strikes[s][opt_type] = row

    lines = []
    sorted_strikes = sorted(strikes.keys())
    # Show strikes within a reasonable range of ATM (±20 strikes or all if fewer)
    atm_idx = 0
    for i, s in enumerate(sorted_strikes):
        if s >= atm_strike:
            atm_idx = i
            break
    start = max(0, atm_idx - 15)
    end = min(len(sorted_strikes), atm_idx + 15)

    lines.append(f"  {'Strike':>10}  {'CE LTP':>8}  {'CE OI':>10}  {'CE Δ':>6}  {'CE IV':>6}  │  {'PE LTP':>8}  {'PE OI':>10}  {'PE Δ':>6}  {'PE IV':>6}")
    lines.append(f"  {'─'*10}  {'─'*8}  {'─'*10}  {'─'*6}  {'─'*6}  │  {'─'*8}  {'─'*10}  {'─'*6}  {'─'*6}")

    for s in sorted_strikes[start:end]:
        ce = strikes[s].get("CE", {})
        pe = strikes[s].get("PE", {})
        marker = " ◄ ATM" if abs(s - atm_strike) < 0.01 else ""
        ce_ltp = f"{float(ce.get('ltp') or 0):.1f}" if ce else "  -"
        ce_oi = f"{int(ce.get('oi') or 0):,}" if ce else "  -"
        ce_delta = f"{float(ce.get('delta') or 0):.2f}" if ce else "  -"
        ce_iv = f"{float(ce.get('iv') or 0):.1f}" if ce else "  -"
        pe_ltp = f"{float(pe.get('ltp') or 0):.1f}" if pe else "  -"
        pe_oi = f"{int(pe.get('oi') or 0):,}" if pe else "  -"
        pe_delta = f"{float(pe.get('delta') or 0):.2f}" if pe else "  -"
        pe_iv = f"{float(pe.get('iv') or 0):.1f}" if pe else "  -"
        lines.append(
            f"  {s:>10.0f}  {ce_ltp:>8}  {ce_oi:>10}  {ce_delta:>6}  {ce_iv:>6}  │  {pe_ltp:>8}  {pe_oi:>10}  {pe_delta:>6}  {pe_iv:>6}{marker}"
        )

    if start > 0 or end < len(sorted_strikes):
        lines.append(f"  ... showing {start+1}-{end} of {len(sorted_strikes)} strikes ...")

    return "\n".join(lines)


def _format_iv_summary(option_rows: list[dict], atm_strike: float) -> str:
    """Format IV data: ATM IV, IV skew, IV range."""
    if not option_rows:
        return "  No IV data available."

    atm_iv = 0.0
    ce_ivs = []
    pe_ivs = []

    for row in option_rows:
        iv = float(row.get("iv") or 0)
        if iv <= 0:
            continue
        strike = float(row.get("strike") or 0)
        opt_type = str(row.get("option_type") or "").upper()
        if abs(strike - atm_strike) < 0.01:
            atm_iv = iv
        if opt_type == "CE":
            ce_ivs.append(iv)
        elif opt_type == "PE":
            pe_ivs.append(iv)

    all_ivs = ce_ivs + pe_ivs
    iv_range = f"{min(all_ivs):.1f}% - {max(all_ivs):.1f}%" if all_ivs else "N/A"
    avg_ce = sum(ce_ivs) / len(ce_ivs) if ce_ivs else 0
    avg_pe = sum(pe_ivs) / len(pe_ivs) if pe_ivs else 0
    skew = "put premium > call premium" if avg_pe > avg_ce else "call premium > put premium" if avg_ce > avg_pe else "balanced"

    return (
        f"  ATM IV: {atm_iv:.1f}%\n"
        f"  IV Range: {iv_range}\n"
        f"  Avg CE IV: {avg_ce:.1f}% | Avg PE IV: {avg_pe:.1f}%\n"
        f"  IV Skew: {skew}"
    )


def _format_historical_strategy_performance(symbol: str) -> str:
    """Query past multi-leg trades for performance context."""
    try:
        from src.models.schema import get_read_conn
        with get_read_conn() as conn:
            trades = conn.execute(
                """SELECT strategy_type, status, total_pnl, net_premium, margin_req,
                          opened_at, closed_at
                   FROM multi_leg_trades
                   WHERE symbol=? AND status != 'OPEN'
                   ORDER BY closed_at DESC LIMIT 10""",
                (symbol,),
            ).fetchall()

        if not trades:
            return "  No historical multi-leg trades for this symbol."

        lines = []
        wins = 0
        total = len(trades)
        total_pnl = 0
        for t in trades:
            pnl = float(t["total_pnl"] or 0)
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            lines.append(
                f"  {t['strategy_type'] or 'N/A':20s} | {t['status']:15s} | P&L: ₹{pnl:>10.0f} | Premium: ₹{float(t['net_premium'] or 0):.0f}"
            )
        win_rate = (wins / total * 100) if total else 0
        return (
            f"  Recent trades ({total}):\n" + "\n".join(lines[:5]) +
            f"\n  Win Rate: {win_rate:.0f}% | Avg P&L: ₹{total_pnl/total:.0f}"
        )
    except Exception as e:
        log.debug("Failed to fetch historical strategy performance: %s", e)
        return "  Historical data unavailable."


def _format_open_books(open_books: list[dict] | None) -> str:
    """Format currently open multi-leg books."""
    if not open_books:
        return "  No open multi-leg positions."

    lines = []
    for book in open_books:
        legs = book.get("legs", [])
        leg_strs = [
            f"    {l.get('side','?')} {l.get('option_type','?')} {l.get('strike',0):.0f} @ ₹{float(l.get('entry_premium') or 0):.1f} (Δ={float(l.get('delta') or 0):.2f})"
            for l in legs
        ]
        lines.append(
            f"  Book: {book.get('book_id','?')} | {book.get('strategy_type','?')}\n"
            f"    Net Premium: ₹{float(book.get('net_premium') or 0):.1f} | "
            f"Net Δ: {float(book.get('net_delta') or 0):.2f} | "
            f"P&L: ₹{float(book.get('total_pnl') or 0):.0f}\n"
            + "\n".join(leg_strs)
        )
    return "\n".join(lines)


def build_multileg_prompt(
    symbol: str,
    intel: dict,
    scan_context: dict,
    open_books: list[dict] | None = None,
    news_data: dict | None = None,
    historical_perf: str | None = None,
) -> str:
    """Build the complete multi-leg strategy prompt.

    Makes the LLM think like an experienced options seller who uses the core
    engine's data as raw material but reasons beyond pure mathematical signals.
    """
    underlying = float(scan_context.get("underlying") or 0)
    atm_strike = float(scan_context.get("atm_strike") or 0)
    expiry = scan_context.get("expiry", "")
    dte = int(scan_context.get("dte") or 0)
    option_rows = scan_context.get("option_rows") or []

    verdict_label = intel.get("verdict_label", "N/A")
    confidence = int(intel.get("confidence") or 0)
    support = float(scan_context.get("support") or 0)
    resistance = float(scan_context.get("resistance") or 0)
    max_pain = float(scan_context.get("max_pain") or 0)
    pcr = float(scan_context.get("pcr") or 0)

    # Chart data
    chart = scan_context.get("chart_indicators") or {}
    chart_1h = chart.get("1h", {})
    chart_3h = chart.get("3h", {})
    ohlc_1h = chart_1h.get("ohlc") or {}
    ohlc_3h = chart_3h.get("ohlc") or {}

    # News
    news_section = ""
    if news_data:
        direction = news_data.get("direction", "neutral")
        score = news_data.get("score", 0)
        headlines = news_data.get("headlines", [])[:3]
        news_section = f"\nNEWS: {direction} (score: {score})\n"
        for h in headlines:
            news_section += f"  - {h.get('headline', '')[:80]}\n"

    # Market regime
    regime = scan_context.get("market_regime", "unknown")

    prompt = f"""You are an expert NSE options seller with 15+ years of experience. You sell premium consistently and profit from time decay and IV crush. You think in terms of probability, risk/reward, and portfolio-level risk — not just single-trade math.

## MARKET DATA — {symbol}
Underlying: {underlying:.2f} | ATM: {atm_strike:.0f} | Expiry: {expiry} | DTE: {dte}
Verdict: {verdict_label} | Confidence: {confidence}%
PCR: {pcr:.2f} | Support: {support:.0f} | Resistance: {resistance:.0f} | Max Pain: {max_pain:.0f}
Market Regime: {regime}

## OPTION CHAIN (all strikes)
{_format_full_option_chain(option_rows, atm_strike, underlying)}

## IV ANALYSIS
{_format_iv_summary(option_rows, atm_strike)}

## CHART DATA
3H: O={float(ohlc_3h.get('open',0)):.0f} H={float(ohlc_3h.get('high',0)):.0f} L={float(ohlc_3h.get('low',0)):.0f} C={float(ohlc_3h.get('close',0)):.0f}
1H: O={float(ohlc_1h.get('open',0)):.0f} H={float(ohlc_1h.get('high',0)):.0f} L={float(ohlc_1h.get('low',0)):.0f} C={float(ohlc_1h.get('close',0)):.0f}
{news_section}

## CURRENT OPEN BOOKS
{_format_open_books(open_books)}

## HISTORICAL PERFORMANCE
{historical_perf or _format_historical_strategy_performance(symbol)}

## YOUR TASK

Analyze the data above and select the BEST multi-leg short options strategy. All legs must be SELL (you are the seller/writer of options).

### Strategy Selection Guide:
- **Calm / Rangebound / Sideways market** → SHORT_STRADDLE (sell ATM CE + ATM PE for pure theta decay and low gamma risk) or SHORT_STRANGLE (sell OTM CE + OTM PE for wider buffer). This is the IDEAL environment for short delta-neutral premium selling!
- **Rangebound market + elevated IV** → IRON_CONDOR (sell both sides, defined risk) or SHORT_STRANGLE (higher premium, undefined risk)
- **Trending market + high IV** → SHORT_STRANGLE skewed with trend, or directional credit spread (BEAR_CALL_SPREAD for bearish, BULL_PUT_SPREAD for bullish)
- **High IV + neutral** → SHORT_STRADDLE (maximum premium capture, high theta)
- **Bullish bias + high IV** → JADE_LIZARD (sell CE spread + sell naked PE)
- **Uncertain direction / Volatile** → IRON_CONDOR (collect from both sides, widest breakeven)
- **Extremely thin liquidity or severe event risk** → Consider NO_TRADE

### Important Constraints on Legs:
- **SHORT_STRADDLE**: Exactly 2 SELL legs (1 ATM CE + 1 ATM PE at the same strike).
- **SHORT_STRANGLE**: Exactly 2 SELL legs (1 OTM CE + 1 OTM PE).
- **IRON_CONDOR**: Exactly 4 SELL legs (2 inner short strikes + 2 outer protection legs) OR use SHORT_STRANGLE if only proposing 2 legs. Do NOT output a 2-leg IRON_CONDOR.
- **BEAR_CALL_SPREAD / BULL_PUT_SPREAD**: Exactly 2 SELL legs.

### Strike Selection Principles:
- Sell strikes at or beyond support/resistance levels
- Use max pain as a magnet — strikes near max pain have highest probability of expiring worthless
- For spreads: width determines max loss — keep width reasonable (1-3% of underlying for indices)
- Delta guidance: 0.15-0.30 for OTM sold strikes (70-85% probability of profit)
- Avoid strikes with unusually low OI (thin liquidity)

### Risk Management:
- Max loss should be ≤ 3x net premium collected
- Net delta should be close to 0 (market-neutral) unless strong directional conviction
- Consider correlation between legs — don't create hidden directional bets
- Set profit target at 30-50% of max profit (don't get greedy)
- Set time decay exit at DTE ≤ 3

### Output Format:
Return a JSON object matching the LLMMultiLegVerdict schema. Think through each leg carefully with a specific rationale. Your thesis should explain the full setup narrative — why this strategy, why these strikes, what's the edge.
"""
    return prompt


def build_multileg_exit_prompt(
    symbol: str,
    book: dict,
    legs: list[dict],
    scan_context: dict,
    intel: dict,
) -> str:
    """Build prompt for multi-leg book exit/adjustment decisions."""
    underlying = float(scan_context.get("underlying") or 0)
    dte = int(scan_context.get("dte") or 0)

    leg_lines = []
    for l in legs:
        current_premium = 0.0
        # Look up current premium from option_rows
        for row in scan_context.get("option_rows", []):
            if (abs(float(row.get("strike", 0)) - float(l["strike"])) < 0.01
                    and row.get("option_type") == l["option_type"]):
                current_premium = float(row.get("ltp") or 0)
                break
        pnl = (float(l["entry_premium"]) - current_premium) * int(l.get("lots", 1)) * 75
        leg_lines.append(
            f"  {l['side']} {l['option_type']} {l['strike']:.0f} | "
            f"Entry: ₹{float(l['entry_premium']):.1f} | "
            f"Current: ₹{current_premium:.1f} | "
            f"P&L: ₹{pnl:.0f} | Δ={float(l.get('delta',0)):.2f}"
        )

    total_pnl = float(book.get("total_pnl") or 0)
    net_premium = float(book.get("net_premium") or 0)
    adjustment_count = int(book.get("adjustment_count") or 0)

    prompt = f"""You are managing an open multi-leg short options position on {symbol}.

## POSITION
Book ID: {book.get('book_id', 'N/A')}
Strategy: {book.get('strategy_type', 'N/A')}
Net Premium Collected: ₹{net_premium:.1f}
Net P&L: ₹{total_pnl:.0f}
Adjustments Done: {adjustment_count}
DTE: {dte}

## LEGS
{chr(10).join(leg_lines)}

## CURRENT MARKET
Underlying: {underlying:.2f}
Verdict: {intel.get('verdict_label', 'N/A')} | Confidence: {intel.get('confidence', 0)}%

## YOUR TASK

Decide: HOLD, ADJUST, or CLOSE the book.

- **HOLD**: Position is within acceptable risk, time decay working in your favor
- **ADJUST**: Roll a tested leg to a further OTM strike, or add/remove a leg to improve risk profile
- **CLOSE**: Book profit (if target hit), cut loss (if stop hit), or exit due to changing conditions

Rules:
- If net P&L ≥ 50% of max profit → strongly consider closing
- If any leg delta > 0.50 → the market is testing that side heavily
- If DTE ≤ 3 → close remaining legs regardless of P&L (gamma risk too high)
- Max 2 adjustments per book (avoid over-trading)
- Don't adjust just to avoid realizing a loss

Return a JSON object with: action (HOLD/ADJUST/CLOSE), reasoning, and if ADJUSTING: the specific adjustment (which leg to close, what new leg to open).
"""
    return prompt
