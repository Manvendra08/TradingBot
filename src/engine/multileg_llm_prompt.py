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

        ce_oi_val = int(ce.get("oi") or 0)
        ce_ltp_val = float(ce.get("ltp") or 0)
        ce_vol_val = float(ce.get("volume") or 0)
        if not ce or (ce_oi_val <= 0 and ce_ltp_val <= 0 and ce_vol_val <= 0):
            ce_ltp = "  -"
            ce_oi = "0 [NO LIQ]"
            ce_delta = "  -"
            ce_iv = "  -"
        else:
            ce_ltp = f"{ce_ltp_val:.1f}" if ce_ltp_val > 0 else "  -"
            ce_oi = f"{ce_oi_val:,}" if ce_oi_val > 0 else "0 [NO LIQ]"
            ce_delta = f"{float(ce.get('delta') or 0):.2f}"
            ce_iv = f"{float(ce.get('iv') or 0):.1f}"

        pe_oi_val = int(pe.get("oi") or 0)
        pe_ltp_val = float(pe.get("ltp") or 0)
        pe_vol_val = float(pe.get("volume") or 0)
        if not pe or (pe_oi_val <= 0 and pe_ltp_val <= 0 and pe_vol_val <= 0):
            pe_ltp = "  -"
            pe_oi = "0 [NO LIQ]"
            pe_delta = "  -"
            pe_iv = "  -"
        else:
            pe_ltp = f"{pe_ltp_val:.1f}" if pe_ltp_val > 0 else "  -"
            pe_oi = f"{pe_oi_val:,}" if pe_oi_val > 0 else "0 [NO LIQ]"
            pe_delta = f"{float(pe.get('delta') or 0):.2f}"
            pe_iv = f"{float(pe.get('iv') or 0):.1f}"

        strike_fmt = f"{s:.1f}" if s % 1 != 0 else f"{s:.0f}"
        lines.append(
            f"  {strike_fmt:>10}  {ce_ltp:>8}  {ce_oi:>10}  {ce_delta:>6}  {ce_iv:>6}  │  {pe_ltp:>8}  {pe_oi:>10}  {pe_delta:>6}  {pe_iv:>6}{marker}"
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


def _format_commodity_regime_intelligence(symbol: str, scan_context: dict) -> str:
    """Format specialized commodity, parity divergence, momentum, and event intelligence."""
    base_sym = symbol.upper().split()[0] if symbol else ""
    is_mcx = symbol.upper() in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER") or base_sym in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER")
    if not is_mcx and "ng_regime" not in scan_context and "ng_dev_pct" not in scan_context:
        return ""

    lines = ["## SPECIALIZED COMMODITY & REGIME INTELLIGENCE"]
    
    if base_sym.startswith("NATURALGAS") or "ng_regime" in scan_context or "ng_dev_pct" in scan_context:
        from datetime import datetime
        import pytz
        
        IST = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(IST)
        
        # 1. Parity State
        ng_regime = scan_context.get("ng_regime", "UNKNOWN")
        ng_fv = float(scan_context.get("ng_fv") or 0.0)
        ng_dev = float(scan_context.get("ng_dev_pct") or 0.0)
        parity_bias = "MCX Overvalued vs US Spot — Bearish Parity Edge" if ng_dev > 1.5 else (
            "MCX Undervalued vs US Spot — Bullish Parity Edge" if ng_dev < -1.5 else
            "Aligned with US Fair Value — Mean-Reverting / Rangebound"
        )
        lines.append(f"- Session Regime : {ng_regime}")
        if ng_fv > 0:
            lines.append(f"- Parity Fair Value : ₹{ng_fv:.2f} (Henry Hub Spot parity)")
            lines.append(f"- Parity Deviation  : {ng_dev:+.2f}% ({parity_bias})")
            
        # 2. EIA Inventory Schedule
        is_thu = now_ist.weekday() == 3
        is_eia_window = is_thu and (18 <= now_ist.hour <= 22)
        if is_eia_window:
            eia_status = "ACTIVE TODAY ~8:00 PM IST (Extreme Gamma & Volatility Shock Window — Avoid Naked Straddles)"
        elif is_thu:
            eia_status = "TODAY ~8:00 PM IST (Pre-event positioning)"
        else:
            eia_status = "Thursdays ~8:00 PM IST (Normal theta session)"
        lines.append(f"- EIA Inventory     : {eia_status}")

        # 3. Momentum Engine Status
        try:
            from src.engine.ng_momentum_strategy import check_ng_momentum_entry
            bull_ok, bull_msg = check_ng_momentum_entry("BUY")
            bear_ok, bear_msg = check_ng_momentum_entry("SELL")
            if bull_ok:
                mom_status = "Bullish momentum breakout active"
            elif bear_ok:
                mom_status = "Bearish momentum breakdown active"
            else:
                mom_status = "No momentum breakout — consolidating in range"
        except Exception:
            mom_status = "Momentum engine nominal"
        lines.append(f"- Momentum Engine   : {mom_status}")

        # 4. Weather Context
        weather_dir = scan_context.get("weather_direction", "neutral")
        weather_z = float(scan_context.get("weather_z") or 0.0)
        storm = scan_context.get("weather_gulf_storm", False)
        if weather_dir != "neutral" or weather_z != 0.0 or storm:
            lines.append(f"- Weather Factors   : Direction={weather_dir}, Z-Score={weather_z:+.2f}, Storm={'Active' if storm else 'Inactive'}")

    return "\n" + "\n".join(lines) + "\n"


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

    # News (external headlines are untrusted input — sanitize before prompt injection)
    from src.engine.llm_enrichment import _sanitize_news_text

    news_section = ""
    if news_data:
        direction = news_data.get("current_news_direction", "MIXED")
        score = news_data.get("news_score_current", 0)
        items = news_data.get("items", [])[:3]
        news_section = f"\nNEWS: {direction} (score: {score})\n"
        for item in items:
            title = _sanitize_news_text(item.get("title", ""), max_len=80)
            pub = item.get("published_at", "")
            time_str = pub[:16].replace("T", " ") if pub else "Unknown time"
            news_section += f"  - [{time_str}] {title}\n"

    # Market regime
    regime = scan_context.get("market_regime", "unknown")
    commodity_intel = _format_commodity_regime_intelligence(symbol, scan_context)

    prompt = f"""You are an expert options seller with 15+ years of experience in NSE and MCX derivatives. You sell premium consistently and profit from time decay and IV crush. You think in terms of probability, risk/reward, and portfolio-level risk — not just single-trade math.

## MARKET DATA — {symbol}
Underlying: {underlying:.2f} | ATM: {atm_strike:.0f} | Expiry: {expiry} | DTE: {dte}
Verdict: {verdict_label} | Confidence: {confidence}%
PCR: {pcr:.2f} | Support: {support:.0f} | Resistance: {resistance:.0f} | Max Pain: {max_pain:.0f}
Market Regime: {regime}
{commodity_intel}
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

Analyze the data above and select the BEST multi-leg options strategy.
- For pure naked short strategies (SHORT_STRANGLE, SHORT_STRADDLE), all legs should be SELL.
- For defined-risk strategies (IRON_CONDOR, BEAR_CALL_SPREAD, BULL_PUT_SPREAD, JADE_LIZARD), include BOTH SELL (short income leg) and BUY (long protective wing leg) to limit downside risk.

### Strategy Selection Guide:
- **Calm / Rangebound / Sideways market** → SHORT_STRADDLE (sell ATM CE + ATM PE) or SHORT_STRANGLE (sell OTM CE + OTM PE for wider buffer).
- **Rangebound market + defined risk** → IRON_CONDOR (sell inner OTM CE + PE, buy outer protective CE + PE)
- **Trending / Directional market + defined risk** → BEAR_CALL_SPREAD for bearish (sell inner CE, buy outer CE) or BULL_PUT_SPREAD for bullish (sell inner PE, buy outer PE)
- **Bullish bias + high IV** → JADE_LIZARD (sell OTM PE + sell OTM CE spread)
- **Uncertain direction / Volatile** → IRON_CONDOR (collect from both sides with defined risk wings)
- **Extremely thin liquidity or severe event risk** → Consider NO_TRADE

### Commodity & Parity Tactical Rules (MCX NATURALGAS / CRUDEOIL):
- **Parity Divergence (Deviation > +1.5%)**: MCX premium is inflated relative to Henry Hub fair value. Exploit downside re-pricing using a **BEAR_CALL_SPREAD** (sell OTM CE, buy further OTM CE) or selling upper CE in a strangle.
- **Parity Divergence (Deviation < -1.5%)**: MCX is discounted relative to Henry Hub fair value. Exploit upside convergence using a **BULL_PUT_SPREAD** (sell OTM PE, buy further OTM PE) or selling lower PE.
- **Parity Alignment (|Deviation| ≤ 1.0%)**: Market in fair-value equilibrium. Exploit theta decay with a **SHORT_STRANGLE** (sell OTM CE + sell OTM PE at key support/resistance) or **IRON_CONDOR**.
- **EIA Report Day / Window**: If EIA inventory release is active/imminent, avoid naked straddles; prefer defined-risk spreads or wider strangle strikes with safe deltas (Δ 0.10 - 0.15), or emit **NO_TRADE** if event risk is extreme.

### Important Constraints on Legs:
- **SHORT_STRADDLE**: Exactly 2 SELL legs (1 ATM CE + 1 ATM PE). Never return 1 leg.
- **SHORT_STRANGLE**: Exactly 2 SELL legs (1 OTM CE + 1 OTM PE). Never return 1 leg. Both CE and PE sides are strictly required.
- **IRON_CONDOR**: Exactly 4 legs (2 inner SELL legs [1 CE + 1 PE] + 2 outer protective BUY legs [1 CE + 1 PE]).
- **BEAR_CALL_SPREAD**: Exactly 2 CE legs (1 inner SELL CE + 1 outer protective BUY CE).
- **BULL_PUT_SPREAD**: Exactly 2 PE legs (1 inner SELL PE + 1 outer protective BUY PE).
- **NO_TRADE**: If no clean multi-leg structure fits, set strategy_type="NO_TRADE" and legs=[]. Never emit half-formed single-leg structures under multi-leg strategy names.

### Strike Selection & Strict Liquidity Rules:
- **LIQUIDITY REQUIREMENT**: ONLY select strikes that have active open interest (OI > 0) and positive premium (LTP > 0). NEVER select strikes marked `[NO LIQ]`, `0`, or `-`. Selecting an illiquid strike will cause immediate rejection by the risk engine.
- **SHORT_STRANGLE**:
  * OTM CE strike MUST be strictly ABOVE the underlying spot price ({underlying:.2f}).
  * OTM PE strike MUST be strictly BELOW the underlying spot price ({underlying:.2f}).
  * PE strike < underlying < CE strike. NEVER select In-The-Money (ITM) strikes for a strangle.
  * Both CE and PE strikes MUST have active liquidity (OI > 0 and LTP > 0).
- **SHORT_STRADDLE**: Both CE and PE MUST be at the ATM strike ({atm_strike:.0f}), and both MUST have active liquidity.
- **SPREADS / CONDORS**: All sold and bought legs must have active liquidity (OI > 0 and LTP > 0).
- If liquid strikes meeting the strategy requirements are not available, you MUST set strategy_type="NO_TRADE" and legs=[].
- Sell strikes at or beyond support/resistance levels when available.
- Use max pain as a magnet — strikes near max pain have highest probability of expiring worthless.
- For spreads: width determines max loss — keep width reasonable.
- Delta guidance: 0.15-0.30 for OTM sold strikes (70-85% probability of profit).

### Risk Management:
- Max loss should be ≤ 3x net premium collected
- Net delta should be close to 0 (market-neutral) unless strong directional conviction
- Consider correlation between legs — don't create hidden directional bets
- Set profit target at 30-50% of max profit (don't get greedy)
- Set time decay exit at DTE ≤ 3

### Data Legitimacy & Input Validation:
- Before selecting a strategy, verify that the provided spot price, option chain strikes, and DTE are coherent and liquid.
- If option chain data is empty, missing, has 0 volume/OI across all strikes, or DTE is 0 without a viable theta decay window, you MUST set strategy_type="NO_TRADE" and legs=[] with an explicit explanation in entry_rationale and thesis. Never invent strikes or artificial premiums.

### Output Format:
Return a JSON object matching the LLMMultiLegVerdict schema. Think through each leg carefully with a specific rationale. Your thesis should explain the full setup narrative — why this strategy, why these strikes, what's the edge.
"""
    return prompt


def _format_roll_candidates(option_rows: list[dict], underlying: float) -> str:
    """Liquid OTM strikes the LLM may roll a tested leg into."""
    if not option_rows or underlying <= 0:
        return "  No option chain available — do not propose ADJUST."

    ce: list[str] = []
    pe: list[str] = []
    for row in sorted(option_rows, key=lambda r: float(r.get("strike") or 0)):
        strike = float(row.get("strike") or 0)
        ltp = float(row.get("ltp") or 0)
        oi = int(row.get("oi") or 0)
        if strike <= 0 or ltp <= 0 or oi <= 0:
            continue
        opt = str(row.get("option_type") or "").upper()
        line = f"    {strike:.0f} @ ₹{ltp:.1f} (OI {oi:,}, Δ={float(row.get('delta') or 0):.2f})"
        if opt == "CE" and strike > underlying:
            ce.append(line)
        elif opt == "PE" and strike < underlying:
            pe.append(line)

    if not ce and not pe:
        return "  No liquid OTM strikes — do not propose ADJUST."

    out = []
    if ce:
        out.append("  OTM CE (roll a tested CE up into one of these):")
        out.extend(ce[:8])
    if pe:
        out.append("  OTM PE (roll a tested PE down into one of these):")
        out.extend(pe[-8:])
    return "\n".join(out)


def build_multileg_exit_prompt(
    symbol: str,
    book: dict,
    legs: list[dict],
    scan_context: dict,
    intel: dict,
) -> str:
    """Build prompt for multi-leg book exit/adjustment decisions."""
    underlying = float(scan_context.get("underlying") or 0)
    dte = int(scan_context.get("dte") or scan_context.get("days_to_expiry") or 0)

    try:
        from config.settings import LOT_SIZES
        lot_size = int(LOT_SIZES.get(symbol.upper().split()[0], 1))
    except Exception:
        lot_size = 1

    leg_lines = []
    for l in legs:
        current_premium = 0.0
        # Look up current premium from option_rows
        for row in scan_context.get("option_rows", []):
            if (abs(float(row.get("strike", 0)) - float(l["strike"])) < 0.01
                    and row.get("option_type") == l["option_type"]):
                current_premium = float(row.get("ltp") or 0)
                break
        # Short legs profit as premium decays; long (BUY) legs profit as premium rises.
        direction = 1 if str(l.get("side") or "SELL").upper() == "SELL" else -1
        pnl = direction * (float(l["entry_premium"]) - current_premium) * int(l.get("lots", 1)) * lot_size
        leg_lines.append(
            f"  {l['side']} {l['option_type']} {l['strike']:.0f} | "
            f"Entry: ₹{float(l['entry_premium']):.1f} | "
            f"Current: ₹{current_premium:.1f} | "
            f"P&L: ₹{pnl:.0f} | Δ={float(l.get('delta',0)):.2f}"
        )

    total_pnl = float(book.get("total_pnl") or 0)
    net_premium = float(book.get("net_premium") or 0)
    adjustment_count = int(book.get("adjustment_count") or 0)
    max_profit = float(book.get("max_profit") or net_premium)
    profit_target_pct = float(book.get("profit_target_pct") or 0.5)
    stop_loss_pct = float(book.get("stop_loss_pct") or 1.5)
    time_decay_exit_dte = int(book.get("time_decay_exit_dte") or 0)
    profit_pct_of_max = (total_pnl / max_profit) if max_profit > 0 else 0.0

    is_weekly = symbol in ("NIFTY", "BANKNIFTY", "SENSEX")
    now_ist = datetime.now(IST)
    current_time_str = now_ist.strftime("%H:%M IST")

    prompt = f"""You are managing an open multi-leg short options position on {symbol}.

## POSITION
Book ID: {book.get('book_id', 'N/A')}
Strategy: {book.get('strategy_type', 'N/A')}
Net Premium Collected: ₹{net_premium:.1f}
Net P&L: ₹{total_pnl:.0f} ({profit_pct_of_max:+.0%} of max profit ₹{max_profit:.0f})
Adjustments Done: {adjustment_count}
DTE: {dte} | Current Time: {current_time_str} | Instrument Type: {'Weekly Index Option' if is_weekly else 'Commodity Option'}

## BOOK EXIT PLAN (set at entry — these are the authoritative thresholds)
Profit target: {profit_target_pct:.0%} of max profit
Stop loss: {stop_loss_pct:.0%} of net premium
Time-decay exit rule: {'Exit after 1:00 PM IST on Expiry Day (DTE=0). DTE 1 or 2 is standard holding territory.' if is_weekly else f'Time-decay exit DTE: {time_decay_exit_dte}'}

## LEGS
{chr(10).join(leg_lines)}

## CURRENT MARKET
Underlying: {underlying:.2f}
Verdict: {intel.get('verdict_label', 'N/A')} | Confidence: {intel.get('confidence', 0)}%

## LIQUID ROLL CANDIDATES (ADJUST must use a strike from this list — never invent one)
{_format_roll_candidates(scan_context.get("option_rows") or [], underlying)}

## YOUR TASK

Decide: HOLD, ADJUST, or CLOSE the book.

- **HOLD**: Position is within acceptable risk, time decay working in your favor
- **ADJUST**: Roll a tested leg to a further OTM strike, or add/remove a leg to improve risk profile
- **CLOSE**: Book profit (if target hit), cut loss (if stop hit), or exit due to changing conditions

Rules (use the BOOK EXIT PLAN values above — do not substitute your own thresholds):
- If net P&L ≥ the profit target shown above → CLOSE.
- If loss exceeds the stop loss shown above → CLOSE.
- **Time Decay & Expiry Timing**:
  * For weekly index options ({symbol}): DTE = 1, 2, or 3 is normal holding territory to harvest theta. Do NOT exit for time decay on DTE ≥ 1. Only exit for time decay on Expiry Day (DTE == 0) after 1:00 PM IST (13:00) to protect against 0-DTE gamma risk.
  * For commodity options: If DTE ≤ the time-decay exit DTE ({time_decay_exit_dte}) → CLOSE.
- If any leg delta > 0.50 → the market is testing that side heavily; prefer ADJUST (roll that leg further OTM) over CLOSE if the untested side is still safe.
- Adjustments already done: {adjustment_count}. Hard cap is 3 per book — if {adjustment_count} ≥ 3, ADJUST is not available; choose HOLD or CLOSE.
- Don't adjust just to avoid realizing a loss.

Output (JSON matching the LLMMultiLegExit schema):
- action: HOLD | ADJUST | CLOSE
- reasoning: 1-3 sentences citing the actual P&L, delta, or DTE numbers shown above
- urgency: LOW | MEDIUM | HIGH
- adjustment: required ONLY when action=ADJUST — give close_strike, close_option_type,
  new_strike, new_option_type, new_side, rationale. Set null for HOLD and CLOSE.
  An ADJUST with no adjustment object will be discarded and treated as HOLD.
"""
    return prompt
