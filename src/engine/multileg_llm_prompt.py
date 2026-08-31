"""
Multi-leg strategy LLM prompt builder.

Builds the prompt that makes the LLM think like an experienced options trader,
using the core engine's data as raw material but reasoning beyond pure math.
"""
from __future__ import annotations

from datetime import datetime
import logging
from typing import Optional

from config.settings import IST

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
    # Show strikes within a reasonable range of ATM (±10 strikes per AGENTS.md)
    atm_idx = 0
    for i, s in enumerate(sorted_strikes):
        if s >= atm_strike:
            atm_idx = i
            break
    start = max(0, atm_idx - 10)
    end = min(len(sorted_strikes), atm_idx + 10)

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
            
        # 2. EIA Inventory Schedule (Only active on Thursdays or near event)
        is_thu = now_ist.weekday() == 3
        if is_thu:
            if 18 <= now_ist.hour <= 22:
                lines.append("- EIA Inventory     : ACTIVE TODAY ~8:00 PM IST (Extreme Gamma Shock Window — Avoid Naked Straddles)")
            elif now_ist.hour >= 17:
                lines.append("- EIA Inventory     : TODAY ~8:00 PM IST (Pre-event positioning)")

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

        # 4. Weather Context (Updated daily at 5 PM IST)
        if now_ist.hour >= 17:
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

    prompt = f"""NSE/MCX options seller. Design a multi-leg premium strategy.

{symbol} | ₹{underlying:.2f} | ATM {atm_strike:.0f} | {expiry} (DTE {dte})
Verdict: {verdict_label} {confidence}% | PCR {pcr:.2f} | S={support:.0f} R={resistance:.0f} Pain={max_pain:.0f} | Regime: {regime}
{commodity_intel}
CHAIN (use only strikes with OI>0 and LTP>0):
{_format_full_option_chain(option_rows, atm_strike, underlying)}

IV: {_format_iv_summary(option_rows, atm_strike)}
Chart: 3H {float(ohlc_3h.get('open',0)):.0f}/{float(ohlc_3h.get('high',0)):.0f}/{float(ohlc_3h.get('low',0)):.0f}/{float(ohlc_3h.get('close',0)):.0f} | 1H {float(ohlc_1h.get('open',0)):.0f}/{float(ohlc_1h.get('high',0)):.0f}/{float(ohlc_1h.get('low',0)):.0f}/{float(ohlc_1h.get('close',0)):.0f}
{news_section}
Open: {_format_open_books(open_books)}
History: {historical_perf or _format_historical_strategy_performance(symbol)}

TASK: Select best multi-leg strategy — or NO_TRADE. You are selling premium: your edge is IV overpricing realized movement plus theta. If that edge is absent, there is no strategy to pick.

EDGE CHECKS (before choosing legs):
1. Expected move ≈ ATM CE LTP + ATM PE LTP (straddle). Short strikes must sit OUTSIDE spot ± expected move — unless deliberately trading a straddle.
2. IV must pay for the risk: if ATM IV is depressed and OTM credits are thin relative to strike width, skip naked shorts — defined-risk or NO_TRADE.
3. Index weekly at DTE ≤ 1 → defined-risk ONLY (no naked strangle/straddle): gamma is unbounded into expiry.
4. Max pain {max_pain:.0f} is a magnet into expiry — shorts straddling it benefit; shorts fighting it need wider strikes.

Strategy Map:
- Sideways → SHORT_STRADDLE (ATM) or SHORT_STRANGLE (OTM)
- Rangebound+defined → IRON_CONDOR (wings 1-3 strikes beyond shorts)
- Bearish+defined → BEAR_CALL_SPREAD | Bullish+defined → BULL_PUT_SPREAD
- Bullish+high IV → JADE_LIZARD
- Uncertain → IRON_CONDOR | No liquidity or no edge → NO_TRADE

MCX Parity (NATURALGAS/CRUDEOIL):
- Deviation >+1.5%: inflated → BEAR_CALL_SPREAD or sell upper CE
- Deviation <-1.5%: discounted → BULL_PUT_SPREAD or sell lower PE
- |Deviation| ≤1.0%: fair value → SHORT_STRANGLE or IRON_CONDOR
- **EIA Report Day / Window**: If EIA inventory release is active/imminent, avoid naked straddles; prefer defined-risk spreads or wider strangle strikes with safe deltas (Δ 0.10 - 0.15), or emit **NO_TRADE** if event risk is extreme.

### Important Constraints on Legs:
Leg counts: STRADDLE=2 SELL, STRANGLE=2 SELL, CONDOR=4(2 SELL+2 BUY), SPREAD=2, NO_TRADE=legs[]

Liquidity (CRITICAL): Only strikes with OI>0 AND LTP>0. Never use [NO LIQ] strikes.
Strangle: CE strike > {underlying:.0f} (OTM) | PE strike < {underlying:.0f} (OTM). Never ITM.
Straddle: Both CE+PE at ATM {atm_strike:.0f}.
Condor/Spreads: all sold+bought legs liquid.
→ No liquid strikes: strategy_type="NO_TRADE", legs=[]

Delta target: 0.15-0.30 for OTM sell legs | Max pain={max_pain:.0f} as magnet | S/R for strike anchors.

Risk: Max loss ≤ 3x net premium | Net delta near 0 | Profit target 30-50% max | Don't over-leg.
- Set time decay exit at DTE ≤ 3

ARITHMETIC (anti-hallucination — violations invalidate the plan):
- Every leg premium MUST be the exact LTP printed in CHAIN for that strike. A leg whose strike or LTP is not in CHAIN is invalid → NO_TRADE.
- net_premium = Σ(SELL LTPs) − Σ(BUY LTPs). max_profit, max_loss, breakevens must reconcile with net_premium and strike widths. Do not estimate any of these.
- Empty/illiquid chain, incoherent spot vs strikes, or DTE 0 with no theta window → strategy_type="NO_TRADE", legs=[], explain in entry_rationale.

Output: JSON per LLMMultiLegVerdict schema. Per-leg rationale specific to that strike; thesis = the setup narrative (why this strategy, these strikes, this edge).
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

    max_adj_reached = adjustment_count >= 3
    roll_targets_str = (
        "Max adjustments (3/3) reached. No further adjustments allowed."
        if max_adj_reached
        else _format_roll_candidates(scan_context.get("option_rows") or [], underlying)
    )
    decision_options = "HOLD | CLOSE" if max_adj_reached else "HOLD | ADJUST | CLOSE"
    adj_rule = (
        f"- {adjustment_count}/3 adjustments done → MAX REACHED. You MUST choose HOLD or CLOSE. ADJUST is strictly disallowed."
        if max_adj_reached
        else f"- {adjustment_count}/3 adjustments done → CLOSE or HOLD (if max hit)"
    )

    prompt = f"""Managing multi-leg position: {symbol}

Book {book.get('book_id', 'N/A')} | {book.get('strategy_type', 'N/A')}
Credit: ₹{net_premium:.1f} | P&L: ₹{total_pnl:.0f} ({profit_pct_of_max:+.0%} of max ₹{max_profit:.0f})
Adjustments: {adjustment_count}/3 | DTE {dte} | {current_time_str} | {'Weekly' if is_weekly else 'Commodity'}

EXIT PLAN (authoritative):
Profit: {profit_target_pct:.0%} max | Stop: {stop_loss_pct:.0%} credit | Time: {'Exit after 1PM IST on expiry day' if is_weekly else f'DTE {time_decay_exit_dte}'}

LEGS:
{chr(10).join(leg_lines)}

MARKET: ₹{underlying:.2f} | {intel.get('verdict_label', 'N/A')} {intel.get('confidence', 0)}%

ROLL TARGETS:
{roll_targets_str}

DECISION: {decision_options}

Rules (use EXIT PLAN above):
- P&L ≥ profit target → CLOSE
- P&L ≤ stop loss → CLOSE
- DTE ≤ time exit → CLOSE (theta done, avoid pin)
{adj_rule}
- One side tested (delta spike) → {'CLOSE (max adjustments reached)' if max_adj_reached else 'ADJUST (roll OTM) if <3 adjustments, else CLOSE'}
- Both sides tested → CLOSE (strangle broken)

JSON:
{{"decision":"{'HOLD|CLOSE' if max_adj_reached else 'HOLD|ADJUST|CLOSE'}","urgency":"LOW|MEDIUM|HIGH","reasoning":"why","target_legs":[{{"option_type":"CE|PE","strike":num}}],"adjustments":[{{"action":"ADD|CLOSE","option_type":"CE|PE","strike":num,"reason":"why"}}]}}

Note: ADJUST requires adjustment object. Null for HOLD/CLOSE.
"""
    return prompt
