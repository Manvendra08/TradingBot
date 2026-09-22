"""
Multi-leg strategy LLM prompt builder.

Builds the prompt that makes the LLM think like an experienced options trader,
using the core engine's data as raw material but reasoning beyond pure math.
"""
from __future__ import annotations

from datetime import datetime
import logging
import re
from typing import Optional

from config.settings import IST
from config.multileg_strategies import MAX_BOOK_MARGIN, MAX_NET_DELTA

log = logging.getLogger(__name__)


def _resolve_dte_from_expiry(expiry_str: str | None) -> int | None:
    """Calculate days to expiry from date string using IST timezone date."""
    if not expiry_str:
        return None
    cleaned = str(expiry_str).strip().split("T")[0].split()[0]
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            exp_date = datetime.strptime(cleaned, fmt).date()
            today = datetime.now(IST).date()
            return (exp_date - today).days
        except Exception:
            continue
    return None


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
    """Format currently open multi-leg books (verbose — used for exit/adjustment prompts)."""
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


def _format_open_book_summary(open_books: list[dict] | None, margin_cap: float = 7500000.0, delta_cap: float = 0.60) -> str:
    """Format compact open multi-leg books summary (used for entry prompts to avoid token bloat)."""
    if not open_books:
        return "  No open multi-leg positions."

    book_count = len(open_books)
    combined_delta = sum(float(b.get("net_delta") or 0) for b in open_books)
    combined_margin = sum(
        float(b.get("margin_req") or b.get("margin") or 0) for b in open_books
    )
    margin_headroom = max(0.0, margin_cap - combined_margin)
    delta_headroom = max(0.0, delta_cap - abs(combined_delta))

    lines = [
        f"  Open books: {book_count}",
        f"  Combined net delta: {combined_delta:+.2f} (cap {delta_cap:.2f}, headroom {delta_headroom:.2f})",
        f"  Combined margin used: ₹{combined_margin:,.0f} / ₹{margin_cap:,.0f} (headroom ₹{margin_headroom:,.0f})",
    ]
    if margin_headroom < 50000:
        lines.append("  MARGIN WARNING: headroom < ₹50k — new position requires full margin check")
    if delta_headroom < 0.10:
        lines.append("  DELTA WARNING: delta headroom < 0.10 — new position may breach cap")

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
    expiry = str(scan_context.get("expiry") or scan_context.get("current_expiry") or "")
    calc_dte = _resolve_dte_from_expiry(expiry)
    if calc_dte is not None:
        dte = calc_dte
    elif scan_context.get("dte") is not None:
        dte = int(scan_context["dte"])
    elif scan_context.get("days_to_expiry") is not None:
        dte = int(scan_context["days_to_expiry"])
    else:
        dte = 99
    option_rows = scan_context.get("option_rows") or []

    verdict_label = intel.get("verdict_label", "N/A")
    confidence = int(intel.get("confidence") or 0)
    support = float(scan_context.get("support") or 0)
    resistance = float(scan_context.get("resistance") or 0)
    max_pain = float(scan_context.get("max_pain") or 0)
    pcr = float(scan_context.get("pcr") or 0)
    is_mcx = symbol.upper() in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER")
    conf_floor = 72 if is_mcx else 70

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

    # Compact open books summary with margin/delta caps
    open_summary = _format_open_book_summary(open_books, MAX_BOOK_MARGIN, MAX_NET_DELTA)

    # F&O ban check (placeholder - actual check happens downstream but good to inform LLM)
    # In practice, this would come from an API. For now, we'll let downstream validation handle it.

    ce_oi_chg = int(scan_context.get("ce_oi_change") or 0)
    pe_oi_chg = int(scan_context.get("pe_oi_change") or 0)
    px_chg_pts = scan_context.get("price_change_points", "N/A")
    px_chg_pct = scan_context.get("price_change_pct", "N/A")

    # Determine canonical OI flow ground truth from options writers' perspective
    if pe_oi_chg > 0 and ce_oi_chg < 0:
        oi_flow_ground_truth = "BULLISH (PE buildup / put writing + CE unwinding / call short-covering)"
    elif ce_oi_chg > 0 and pe_oi_chg < 0:
        oi_flow_ground_truth = "BEARISH (CE buildup / call writing + PE unwinding / put exit)"
    elif pe_oi_chg > 0 and ce_oi_chg > 0:
        if pe_oi_chg > ce_oi_chg * 1.5:
            oi_flow_ground_truth = f"BULLISH BIAS (PE writing +{pe_oi_chg:,} heavily outpaces CE writing +{ce_oi_chg:,})"
        elif ce_oi_chg > pe_oi_chg * 1.5:
            oi_flow_ground_truth = f"BEARISH BIAS (CE writing +{ce_oi_chg:,} heavily outpaces PE writing +{pe_oi_chg:,})"
        else:
            oi_flow_ground_truth = "NEUTRAL / STRANGLE SETUP (Both CE & PE building in balance; support & resistance narrowing)"
    elif ce_oi_chg < 0 and pe_oi_chg < 0:
        oi_flow_ground_truth = "NEUTRAL / SQUARING OFF (Both CE & PE unwinding; position liquidation)"
    elif pe_oi_chg > 0 and ce_oi_chg == 0:
        oi_flow_ground_truth = "BULLISH (Fresh PE put writing / support floor)"
    elif ce_oi_chg > 0 and pe_oi_chg == 0:
        oi_flow_ground_truth = "BEARISH (Fresh CE call writing / resistance ceiling)"
    elif pe_oi_chg < 0 and ce_oi_chg == 0:
        oi_flow_ground_truth = "BEARISH (PE unwinding / support loss)"
    elif ce_oi_chg < 0 and pe_oi_chg == 0:
        oi_flow_ground_truth = "BULLISH (CE unwinding / short covering)"
    else:
        oi_flow_ground_truth = "MIXED / CONSOLIDATION"

    # Jev System-1 fast read (pre-computed directional bias)
    jev_section = ""
    jev_dir = intel.get("jev_direction") if intel else None
    jev_conv = intel.get("jev_conviction") if intel else None
    if jev_dir and jev_conv is not None:
        jev_section = f"\nJev System-1: direction={jev_dir} conviction={jev_conv:.2f} (fast pre-LLM read — factor into strategy selection bias)"

    prompt = f"""NSE/MCX options seller. Design a multi-leg premium strategy.

{symbol} | ₹{underlying:.2f} | ATM {atm_strike:.0f} | {expiry} (DTE {dte})
Verdict: {verdict_label} {confidence}% | PCR {pcr:.2f} | S={support:.0f} R={resistance:.0f} Pain={max_pain:.0f} | Regime: {regime}
OI Flow: CE Δ {ce_oi_chg:+,} | PE Δ {pe_oi_chg:+,} → {oi_flow_ground_truth}
Price Move: {px_chg_pts} pts ({px_chg_pct}%){jev_section}

OPTIONS MARKET MECHANICS (MANDATORY WRITER GROUND TRUTH — NEVER INVERT):
• PE Buildup (Positive PE OI change) = PUT WRITING / PUT SELLING by institutions establishing support floor → BULLISH. NEVER interpret PE buildup as bearish short positioning!
• CE Unwinding (Negative CE OI change) = CALL SHORT-COVERING / call writers exiting upside risk → BULLISH.
• CE Buildup (Positive CE OI change) = CALL WRITING / CALL SELLING establishing overhead resistance ceiling → BEARISH.
• PE Unwinding (Negative PE OI change) = PUT UNWINDING / support crumbling → BEARISH.
• PCR = Total PE OI / Total CE OI. Rising PCR (e.g. 0.80 → 1.10) reflects heavier Put writing than Call writing → BULLISH accumulation. Lowering PCR (e.g. 1.20 → 0.60) reflects Put unwinding or heavy Call writing → BEARISH.
• In your thesis and rationale, ALWAYS adhere to these mathematical facts.
{commodity_intel}
CHAIN (use only strikes with OI>0 and LTP>0):
{_format_full_option_chain(option_rows, atm_strike, underlying)}

IV: {_format_iv_summary(option_rows, atm_strike)}
Chart: 3H {float(ohlc_3h.get('open',0)):.0f}/{float(ohlc_3h.get('high',0)):.0f}/{float(ohlc_3h.get('low',0)):.0f}/{float(ohlc_3h.get('close',0)):.0f} | 1H {float(ohlc_1h.get('open',0)):.0f}/{float(ohlc_1h.get('high',0)):.0f}/{float(ohlc_1h.get('low',0)):.0f}/{float(ohlc_1h.get('close',0)):.0f}
{news_section}
Open: {open_summary}
History: {historical_perf or _format_historical_strategy_performance(symbol)}

CONSTRAINTS (non-negotiable):
• Price Discovery & Timings: 09:00–09:15 F&O Pre-open call auction (09:00-09:08 entry, 09:08-09:12 matching, 09:12-09:15 buffer) | 15:15 cash continuous close for F&O stocks | 15:15–15:35 CAS price discovery | 15:40 derivative F&O close (extra 10 min window past 15:30 to hedge/adjust). Non-F&O cash trades to 15:30.

• {symbol} F&O ban status: CHECK_DOWNSTREAM → if BANNED, strategy_type="NO_TRADE", legs=[]
• Weekly vs Monthly expiry: {"Weekly" if dte <= 7 else "Monthly"}. Weekly → 2 legs for strangles/straddles/spreads (or 4 legs for defined-risk IRON_CONDOR), tighter 15-20% profit target. Monthly → up to 4 legs, 30-50% target.
• SEBI STT on sell side: ~0.05% on premium for equity options, ~0.125% for commodity. Factor into max_profit estimate if not already netted.
• SPAN margin is dynamic. If estimated margin + existing ₹{sum(float(b.get("margin_req") or b.get("margin") or 0) for b in (open_books or [])):,.0f} > ₹{MAX_BOOK_MARGIN:,.0f} → NO_TRADE.
• Combined delta headroom: {MAX_NET_DELTA - abs(sum(float(b.get("net_delta") or 0) for b in (open_books or []))):.2f} remaining.


TASK: Select best multi-leg strategy — or NO_TRADE. You are selling premium: your edge is IV overpricing realized movement plus theta. If that edge is absent, there is no strategy to pick.

EDGE CHECKS (before choosing legs):
1. Expected move ≈ ATM CE LTP + ATM PE LTP (straddle). Short strikes must sit OUTSIDE spot ± expected move — unless deliberately trading a straddle.
2. IV must pay for the risk: if ATM IV is depressed and OTM credits are thin relative to strike width, prefer defined-risk spreads (IRON_CONDOR, BEAR_CALL_SPREAD, BULL_PUT_SPREAD) over naked shorts.
3. Index weekly at DTE ≤ 1 → defined-risk ONLY (no naked strangle/straddle): gamma is unbounded into expiry.
4. Max pain {max_pain:.0f} is a magnet into expiry — shorts straddling it benefit; shorts fighting it need wider strikes.

Strategy Map:
- Sideways → SHORT_STRADDLE (ATM) or SHORT_STRANGLE (OTM)
- Rangebound+defined → IRON_CONDOR (Wings MUST be sufficiently wide to avoid insurance drag: NIFTY ≥100-200 pts, BANKNIFTY ≥300-500 pts, SENSEX ≥400-800 pts. Never pick buy wings adjacent or too close to sell legs!)
- Bearish+defined → BEAR_CALL_SPREAD | Bullish+defined → BULL_PUT_SPREAD (spread width ≥ 0.5% of spot)
- Bullish+high IV → JADE_LIZARD
- Uncertain → NO_TRADE is always acceptable; a missed trade costs nothing.

MCX Parity (NATURALGAS/CRUDEOIL):
- Deviation >+1.5%: inflated → BEAR_CALL_SPREAD or sell upper CE
- Deviation <-1.5%: discounted → BULL_PUT_SPREAD or sell lower PE
- |Deviation| ≤1.0%: fair value → SHORT_STRANGLE or IRON_CONDOR
- **EIA Report Day / Window**: If EIA inventory release is active/imminent, avoid naked straddles; prefer defined-risk spreads or wider strangle strikes with safe deltas (Δ 0.10 - 0.15). NO_TRADE is always acceptable; a missed trade costs nothing if event risk is elevated or setup is unclear.

### Important Constraints on Legs:
Leg counts: STRADDLE=2 SELL, STRANGLE=2 SELL, CONDOR=4(2 SELL+2 BUY), SPREAD=2, NO_TRADE=legs[]

Liquidity (CRITICAL): Only strikes with OI>0 AND LTP>0. Never use [NO LIQ] strikes.
Strangle: CE strike > {underlying:.0f} (OTM) | PE strike < {underlying:.0f} (OTM). Never ITM.
Straddle: Both CE+PE at ATM {atm_strike:.0f}.
Condor/Spreads: all sold+bought legs liquid.
→ Wing Width & Insurance Guardrail (CRITICAL):
  * For IRON_CONDOR and defined-risk spreads: DO NOT place buy hedge legs too close to short legs.
  * Minimum wing width (buy strike minus sell strike): NIFTY ≥100 pts, BANKNIFTY ≥250 pts, SENSEX ≥400 pts (ideally 500–800 pts).
  * Max Hedge Cost: Total debit spent on BUY wings MUST NOT exceed 65% of gross credit collected from SELL legs (collect ≥35% net premium). Placing wings only 1 strike away consumes 75-80% of premium, resulting in unviable trades!
→ If NO liquid strikes for chosen strategy, emit NO_TRADE. NO_TRADE is always acceptable; a missed trade costs nothing.

Delta target: 0.15-0.30 for OTM sell legs | Max pain={max_pain:.0f} as magnet | S/R for strike anchors.

Risk: Max loss ≤ 3x net premium | Net delta near 0 | Profit target 30-50% max | Don't over-leg.
- Set time decay exit DTE: 0 for weekly index options (hold to expiry day); DTE ≤ 2 for monthly/commodity options.

CONFIDENCE CALIBRATION & ENGINE ALIGNMENT (0-100):
- Execution confidence floor is {conf_floor}%. Any proposed strategy with confidence below {conf_floor}% will abort execution.
- Baseline: Anchor your confidence to the underlying ENGINE conviction ({confidence}%).
- When the engine confidence is high (≥70%) and you identify liquid strikes with viable net premium and safe delta: output confidence ≥ 70% (typically 75-95% commensurate with setup quality).
- If the option chain has poor liquidity, wide bid-ask spreads, or negative risk-reward, downgrade confidence below {conf_floor}% or set strategy_type="NO_TRADE" and legs=[].
- If you genuinely see extreme event risk, data corruption, or lack of premium edge, explicitly set strategy_type="NO_TRADE" with confidence=0 and explain in entry_rationale and thesis.

ARITHMETIC (anti-hallucination — violations invalidate the plan):
- Every leg premium MUST be the exact LTP printed in CHAIN for that strike. A leg whose strike or LTP is not in CHAIN is invalid → NO_TRADE.
- net_premium = Σ(SELL LTPs) − Σ(BUY LTPs). max_profit, max_loss, breakevens must reconcile with net_premium and strike widths. Do not estimate any of these.
- Empty/illiquid chain, incoherent spot vs strikes, or DTE 0 with no theta window → strategy_type="NO_TRADE", legs=[], explain in entry_rationale.

PRE-VERDICT REASONING AUDIT (MANDATORY — POPULATE `reasoning_chain` FIRST):
Execute this 4-step verification sequence before finalizing strategy and legs:
1. OI Flow Audit: Confirm PE and CE OI change against writer rules (PE+ = Put Writing support, CE+ = Call Writing resistance). Does flow justify this trade?
2. Level Audit: Verify spot relative to S/R anchors and Max Pain ({max_pain:.0f}). Ensure short strikes sit safely outside expected move.
3. Catalyst & Expiry Audit: Check DTE ({dte}) and event risks (EIA, OPEC, pin risk).
4. Adversarial Invalidation: Define `structural_invalidation_spot` (the exact spot level that proves this setup wrong).

Output: JSON per LLMMultiLegVerdict schema including reasoning_chain and structural_invalidation_spot. Per-leg rationale specific to that strike; thesis = the setup narrative (why this strategy, these strikes, this edge).
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
    underlying = float(scan_context.get("underlying") or 0)
    book_expiry = (
        book.get("expiry")
        or (legs[0].get("expiry") if legs else None)
        or scan_context.get("expiry")
        or scan_context.get("current_expiry")
        or ""
    )
    calc_dte = _resolve_dte_from_expiry(book_expiry)
    if calc_dte is not None:
        dte = calc_dte
    elif scan_context.get("dte") is not None:
        dte = int(scan_context["dte"])
    elif scan_context.get("days_to_expiry") is not None:
        dte = int(scan_context["days_to_expiry"])
    else:
        dte = 99

    try:
        from config.settings import LOT_SIZES
        lot_size = int(LOT_SIZES.get(symbol.upper().split()[0], 1))
    except Exception:
        lot_size = 1

    leg_lines = []
    from src.engine.trade_plan import is_valid_option_premium
    from src.models.schema import get_latest_option_snapshot
    calc_total_pnl = 0.0
    for l in legs:
        current_premium = 0.0
        strike_val = float(l.get("strike", 0))
        opt_type_val = str(l.get("option_type") or "").upper()
        entry_prem_val = float(l.get("entry_premium") or 0.0)
        leg_expiry = str(l.get("expiry") or book_expiry or "").strip()

        # Step 1: Look up current premium from scan_context option_rows
        found_ltp = None
        for row in scan_context.get("option_rows", []):
            if (abs(float(row.get("strike", 0)) - strike_val) < 0.01
                    and str(row.get("option_type") or "").upper() == opt_type_val):
                ltp_val = float(row.get("ltp") or 0)
                if ltp_val > 0 and (underlying <= 0 or is_valid_option_premium(strike_val, opt_type_val, ltp_val, underlying)):
                    found_ltp = ltp_val
                break

        # Step 2: Query latest DB option snapshot (exact same source as _build_real_leg_exits)
        if found_ltp is None and leg_expiry:
            try:
                snap = get_latest_option_snapshot(symbol, leg_expiry, strike_val, opt_type_val)
                if snap:
                    snap_ltp = float(snap.get("ltp") or 0.0)
                    if snap_ltp > 0 and (underlying <= 0 or is_valid_option_premium(strike_val, opt_type_val, snap_ltp, underlying)):
                        found_ltp = snap_ltp
            except Exception:
                pass

        # Step 3: Check in-memory leg current_premium populated by _calc_multileg_pnl / _update_live_book_pnl
        if found_ltp is not None:
            current_premium = found_ltp
        elif float(l.get("current_premium") or 0.0) > 0 and (
            underlying <= 0 or is_valid_option_premium(strike_val, opt_type_val, float(l.get("current_premium")), underlying)
        ):
            current_premium = float(l.get("current_premium"))
        else:
            # Step 4: Fallback to delta-based estimate aligned with _calc_multileg_pnl()
            if underlying > 0 and book.get("entry_underlying"):
                entry_und = float(book.get("entry_underlying") or underlying)
                und_move = underlying - entry_und
                delta = float(l.get("delta") or 0.25)
                delta_sign = delta if opt_type_val == "CE" else -abs(delta)
                current_premium = max(0.05, entry_prem_val + delta_sign * und_move)
            else:
                current_premium = float(l.get("current_premium") or entry_prem_val or 0.05)

        # Short legs profit as premium decays; long (BUY) legs profit as premium rises.
        direction = 1 if str(l.get("side") or "SELL").upper() == "SELL" else -1
        pnl = direction * (entry_prem_val - current_premium) * int(l.get("lots", 1)) * lot_size
        calc_total_pnl += pnl
        leg_lines.append(
            f"  {l['side']} {l['option_type']} {strike_val:.0f} | "
            f"Entry: ₹{entry_prem_val:.1f} | "
            f"Current: ₹{current_premium:.1f} | "
            f"P&L: ₹{pnl:.0f} | Δ={float(l.get('delta',0)):.2f}"
        )

    # Use explicit book total_pnl if non-zero, otherwise use freshly summed legs P&L + realized
    book_pnl_val = book.get("total_pnl")
    total_pnl = float(book_pnl_val) if book_pnl_val is not None and abs(float(book_pnl_val)) > 0.01 else (calc_total_pnl + float(book.get("realized_pnl") or 0))
    net_premium = float(book.get("net_premium") or 0)
    adjustment_count = int(book.get("adjustment_count") or 0)

    # Calculate physical max profit in rupees for accurate percentage logic
    _pt_lots = max((int(l.get("lots") or 1) for l in legs), default=1)
    max_profit_points = float(book.get("max_profit") or net_premium)
    max_profit_rupees = max(max_profit_points * lot_size * _pt_lots, 1.0)

    profit_target_pct = float(book.get("profit_target_pct") or 0.5)
    stop_loss_pct = float(book.get("stop_loss_pct") or 1.5)
    time_decay_exit_dte = int(book.get("time_decay_exit_dte") or 0)
    # Robust sanity bounds on profit_pct_of_max (-500% to +100% for credit sellers)
    raw_profit_pct = total_pnl / max_profit_rupees
    if raw_profit_pct > 1.05:
        log.warning(
            "[multileg-prompt] %s: book %s anomalous profit_pct_of_max %.1f%% (total_pnl=₹%.0f, max_profit=₹%.0f) — clamped to 100%%",
            symbol, book.get("book_id", "unknown"), raw_profit_pct * 100, total_pnl, max_profit_rupees
        )
    profit_pct_of_max = min(max(raw_profit_pct, -5.0), 1.0)

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

    # Event proximity checks
    event_within_4h = False
    next_catalyst = ""
    hours_to_event = 999
    if symbol.upper().startswith("NATURALGAS"):
        now_ist = datetime.now(IST)
        # EIA is Thursday 20:00 IST
        if now_ist.weekday() == 3:  # Thursday
            eia_hour = 20
            hours_to_event = (eia_hour - now_ist.hour) + (eia_hour == now_ist.hour and now_ist.minute > 0)
            if 0 <= hours_to_event <= 4:
                event_within_4h = True
            next_catalyst = f"EIA Inventory {hours_to_event:.1f}h"
    elif symbol.upper().startswith("CRUDEOIL"):
        # OPEC+ meetings typically Wednesday
        now_ist = datetime.now(IST)
        if now_ist.weekday() == 2:  # Wednesday
            hours_to_event = 16 - now_ist.hour  # approximate
            if 0 <= hours_to_event <= 4:
                event_within_4h = True
            next_catalyst = f"OPEC+ {hours_to_event:.1f}h"
        else:
            next_catalyst = "None imminent"
    else:
        next_catalyst = "None imminent"

    # Pin risk check for weekly expiry (only late afternoon after 14:00 IST when spot is within 0.15% of short strike)
    is_expiry_today = (dte == 0)
    pin_risk_high = False
    if is_expiry_today and is_weekly and now_ist.hour >= 14:
        for l in legs:
            strike = float(l.get("strike", 0))
            if abs(underlying - strike) / underlying <= 0.0015:  # within 0.15%
                pin_risk_high = True
                break

    ce_chg = int(scan_context.get("ce_oi_change") or 0)
    pe_chg = int(scan_context.get("pe_oi_change") or 0)
    if ce_chg != 0 or pe_chg != 0:
        if pe_chg > 0 and ce_chg < 0:
            oi_flow_desc = "BULLISH (PE put writing support + CE short-covering)"
        elif pe_chg > 0 and pe_chg > ce_chg:
            oi_flow_desc = f"BULLISH (PE writing +{pe_chg:,} > CE writing +{ce_chg:,})"
        elif ce_chg > 0 and pe_chg < 0:
            oi_flow_desc = "BEARISH (CE call writing resistance + PE support breakdown)"
        elif ce_chg > 0 and ce_chg > pe_chg:
            oi_flow_desc = f"BEARISH (CE writing +{ce_chg:,} > PE writing +{pe_chg:,})"
        elif ce_chg < 0 and pe_chg < 0:
            oi_flow_desc = "UNWINDING (Both sides closing positions)"
        else:
            oi_flow_desc = "BALANCED"
        oi_flow_str = f" | OI Flow: {oi_flow_desc} (PE+ = Support, CE+ = Resistance)"
    else:
        oi_flow_str = ""

    inval_str = ""
    entry_desc = str(book.get("entry_reason") or book.get("reason") or "")
    inval_m = re.search(r"Invalidation:\s*(?:Spot\s*)?([0-9.]+)", entry_desc)
    if inval_m:
        inval_level = float(inval_m.group(1))
        inval_str = f" | Invalidation Level: ₹{inval_level:.1f}"

    prompt = f"""Managing multi-leg position: {symbol}

Book {book.get('book_id', 'N/A')} | {book.get('strategy_type', 'N/A')}
Credit: ₹{net_premium * lot_size * _pt_lots:,.0f} | P&L: ₹{total_pnl:.0f} ({profit_pct_of_max:+.0%} of max ₹{max_profit_rupees:,.0f})
Adjustments: {adjustment_count}/3 | DTE {dte} | {current_time_str} | {'Weekly' if is_weekly else 'Commodity'}

EXIT PLAN (authoritative):
Profit: {profit_target_pct:.0%} max | Stop: {stop_loss_pct:.0%} credit | Time: {('EXPIRY TODAY: Exit after 13:00 IST' if dte == 0 else f'Hold to expiry day (DTE {dte} > 0)') if is_weekly else f'DTE {time_decay_exit_dte}'}{inval_str}

LEGS:
{chr(10).join(leg_lines)}

MARKET: ₹{underlying:.2f} | {intel.get('verdict_label', 'N/A')} {intel.get('confidence', 0)}%{oi_flow_str}

ROLL TARGETS:
{roll_targets_str}

EXIT DISCIPLINE (Indian context):
• Price Discovery Timings: 15:15 cash continuous trading ends for F&O stocks; CAS runs 15:15–15:35; derivatives trade until 15:40 IST (use 15:30–15:40 for hedging/adjustments).
• If profit_pct ≥ 50%: CLOSE is preferred. Only ADJUST if both legs delta < 0.10 AND DTE ≥ 5 AND no event in next 24h.
• {symbol} pin risk: {'HIGH — expiry day after 14:00 IST and spot within 0.15% of short strike' if pin_risk_high else 'normal (early session / safe distance)'}.
• {'Do NOT ADJUST within 4h of catalyst — close or hold only.' if event_within_4h else 'No event window restriction.'}
• Weekly index (NIFTY/BANKNIFTY/SENSEX): On expiry day (DTE 0) BEFORE 13:00 IST, do NOT close for time decay or pin risk unless profit target, stop loss, or structural invalidation is breached. Pin risk exits apply only after 14:00 IST when spot is within 0.15% of a short strike.


DECISION: {decision_options}

Rules (use EXIT PLAN above):
- P&L ≥ profit target → CLOSE
- P&L ≤ stop loss → CLOSE
- Invalidation level breached by underlying → CLOSE immediately (thesis invalidated)
- Time decay exit: close ONLY if DTE ≤ time exit or on weekly expiry day after 13:00 IST (never close for time on DTE > 0)
{adj_rule}
- One side tested (delta spike) → {'CLOSE (max adjustments reached)' if max_adj_reached else 'ADJUST (roll OTM) if <3 adjustments, else CLOSE'}
- Both sides tested → CLOSE (strangle broken)

JSON:
{{"decision":"{'HOLD|CLOSE' if max_adj_reached else 'HOLD|ADJUST|CLOSE'}","urgency":"LOW|MEDIUM|HIGH","reasoning":"why","target_legs":[{{"option_type":"CE|PE","strike":num}}],"adjustments":[{{"action":"ADD|CLOSE","option_type":"CE|PE","strike":num,"reason":"why"}}]}}

Note: ADJUST requires adjustment object. Null for HOLD/CLOSE.
"""
    return prompt
