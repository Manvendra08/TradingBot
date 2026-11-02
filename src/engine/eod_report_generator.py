"""
EOD Report Generator — Market Close Strategic Intelligence Report (16:00 IST / 4:00 PM)
Combines Firecrawl web search (closing bell, FII/DII flows, macro news) with quantitative
OI data from the internal scanning engine.
Synthesizes a high-level executive memorandum in SteadyAlpha Strategic Format, renders an A4 PDF,
and dispatches both the summary and document to Telegram.
"""

import asyncio
import re
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytz
from pydantic import BaseModel, Field

from src.fetchers.firecrawl_eod import fetch_firecrawl_eod_data
from src.models.schema import get_conn
from config.settings import LOT_SIZES

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")
REPORTS_DIR = Path("docs/reports")


# ── Pydantic schema for structured LLM output (Compact Mobile Summary) ───────

class EODMarketReport(BaseModel):
    title: str = Field(description="Exact title: 'Indian Market Close EOD Report — DD Mon YYYY'")
    index_summary: str = Field(
        description=(
            "One line per index. Include spot, % change, and key sector/market driver from web closing wrap. "
            "Example: 'NIFTY 23431 (-0.4%) | IT (+0.6%) gains, Energy drags | Long Unwinding'"
        )
    )
    oi_derivatives_analysis: str = Field(
        description=(
            "One line per index. Format: SYMBOL | CEΔ PEΔ | PCR X.XX | MP ±pts | SIGNAL. "
            "Use M suffix for millions (e.g. -26.4M). State flow direction in 3 words max "
            "(e.g. 'Put unwinding dominant'). No paragraphs."
        )
    )
    bot_performance: str = Field(
        description=(
            "Institutional risk overview or regime status in 2 lines. "
            "Example: 'Market Regime: DISTRIBUTION / LONG UNWINDING | VIX: Elevated | Capital Flow: Institutional Outflows'."
        )
    )
    key_levels_tomorrow: str = Field(
        description=(
            "One line per index: SYMBOL Sup-XXXX Res-XXXX MP-XXXX Bias: BULLISH/BEARISH/NEUTRAL "
            "with one-word reason (e.g. 'PCR 1.52'). One-line watchout below."
        )
    )
    macro_and_watchlist: str = Field(
        description=(
            "2-3 lines max: (1) Web market wrap / driver in ≤12 words. "
            "(2) FII/DII activity from web if available. "
            "(3) Tomorrow watch: 2-3 bullet points with specific levels/strikes."
        )
    )


# ── Data collection helpers ─────────────────────────────────────────────────

def _get_symbol_pct_change(symbol: str) -> float | None:
    """Fetch the latest price % change for a symbol from underlying_price table."""
    with get_conn() as conn:
        try:
            row = conn.execute(
                """
                SELECT pct_change FROM underlying_price
                WHERE symbol = ? AND pct_change IS NOT NULL
                ORDER BY fetched_at DESC LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
            if row and row["pct_change"] is not None:
                return float(row["pct_change"])
        except Exception as e:
            log.debug("[eod_report] Could not fetch pct_change for %s: %s", symbol, e)
    return None


def _get_latest_fii_dii_flows() -> tuple[float | None, float | None, str | None]:
    """Fetch latest available FII & DII net cash flows (in Cr) and report date."""
    with get_conn() as conn:
        try:
            row = conn.execute(
                """
                SELECT fii_cash_net, dii_cash_net, report_date
                FROM fii_positioning
                ORDER BY report_date DESC LIMIT 1
                """
            ).fetchone()
            if row:
                return (
                    float(row["fii_cash_net"]) if row["fii_cash_net"] is not None else None,
                    float(row["dii_cash_net"]) if row["dii_cash_net"] is not None else None,
                    str(row["report_date"]),
                )
        except Exception as e:
            log.debug("[eod_report] Could not fetch fii_positioning: %s", e)
    return None, None, None


def _extract_pct_from_text(text: str, symbol: str) -> float | None:
    """Extract percentage change mentioned near symbol in LLM or web text."""
    if not text:
        return None
    pattern = rf"{symbol}[^.\r\n]*?([+-]?\d+\.?\d*)\s*%"
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _get_today_scan_data() -> list[dict]:
    """Fetch the latest scan row per symbol today with all quantitative fields."""
    now_ist = datetime.now(IST)
    today_start_utc = (
        now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
        .isoformat()
    )

    summaries = []
    with get_conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT s.symbol, s.underlying, s.pcr, s.max_pain,
                       s.verdict_label, s.confidence,
                       s.ce_oi_change, s.pe_oi_change,
                       s.total_ce_oi, s.total_pe_oi,
                       s.atm_strike, s.support, s.resistance,
                       s.top_signal_type, s.top_signal_strike,
                       s.top_signal_option_type, s.top_signal_severity,
                       s.trend_bias, s.market_regime, s.candle_1h, s.candle_3h,
                       s.fetched_at
                FROM scan_summaries s
                INNER JOIN (
                    SELECT symbol, MAX(fetched_at) AS max_fa
                    FROM scan_summaries
                    WHERE fetched_at >= ?
                    GROUP BY symbol
                ) latest ON s.symbol = latest.symbol AND s.fetched_at = latest.max_fa
                ORDER BY s.symbol
                """,
                (today_start_utc,),
            ).fetchall()
            summaries = [dict(r) for r in rows]
        except Exception as e:
            log.error("[eod_report] Failed to query scan_summaries: %s", e)
    return summaries


def _get_today_trade_performance() -> dict:
    """Aggregate today's paper/multi-leg trade performance (used for quantitative context)."""
    result = {
        "ml_opened": 0,
        "ml_closed": 0,
        "ml_open": 0,
        "ml_total_pnl": 0.0,
        "ml_closed_pnl": 0.0,
        "ml_books": [],
        "pt_opened": 0,
        "pt_closed": 0,
        "pt_total_pnl": 0.0,
    }
    with get_conn() as conn:
        try:
            now_ist = datetime.now(IST)
            today_start_utc = (
                now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
                .astimezone(timezone.utc)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
            ml_rows = conn.execute(
                """
                SELECT symbol, structure, strategy_type, status, total_pnl,
                       net_premium, entry_underlying, exit_underlying, entry_reason
                FROM multi_leg_trades
                WHERE opened_at >= ? OR date(opened_at) = date('now')
                ORDER BY opened_at DESC
                """,
                (today_start_utc,),
            ).fetchall()
            for r in ml_rows:
                d = dict(r)
                result["ml_opened"] += 1
                if d["status"] in ("CLOSED", "CLOSED_SL", "CLOSED_TARGET", "CLOSED_AI_EXIT", "CLOSED_FRIDAY"):
                    result["ml_closed"] += 1
                    result["ml_closed_pnl"] += d.get("total_pnl") or 0.0
                else:
                    result["ml_open"] += 1
                result["ml_total_pnl"] += d.get("total_pnl") or 0.0
                result["ml_books"].append(d)
        except Exception as e:
            log.error("[eod_report] Failed to query multi_leg_trades: %s", e)

        try:
            pt_rows = conn.execute(
                """
                SELECT symbol, option_type, strike, side, status,
                       pnl_rupees, entry_premium, exit_premium, verdict_label
                FROM paper_trades
                WHERE date(opened_at) = date('now')
                """,
            ).fetchall()
            for r in pt_rows:
                d = dict(r)
                result["pt_opened"] += 1
                if d["status"] not in ("OPEN",):
                    result["pt_closed"] += 1
                    result["pt_total_pnl"] += d.get("pnl_rupees") or 0.0
        except Exception as e:
            log.error("[eod_report] Failed to query paper_trades: %s", e)

    return result


def _build_scan_text(summaries: list[dict]) -> str:
    """Convert scan data into a dense LLM-readable narrative."""
    if not summaries:
        return "No scan data available for today."

    lines = []
    for s in summaries:
        sym = s.get("symbol", "?")
        spot = s.get("underlying")
        pcr = s.get("pcr")
        pain = s.get("max_pain")
        verdict = s.get("verdict_label", "Unknown")
        conf = s.get("confidence", 0)
        ce_chg = s.get("ce_oi_change")
        pe_chg = s.get("pe_oi_change")
        tot_ce = s.get("total_ce_oi")
        tot_pe = s.get("total_pe_oi")
        atm = s.get("atm_strike")
        sup = s.get("support")
        res = s.get("resistance")
        top_sig = s.get("top_signal_type")
        top_strike = s.get("top_signal_strike")
        top_ot = s.get("top_signal_option_type")
        top_sev = s.get("top_signal_severity")
        candle_1h = s.get("candle_1h")
        candle_3h = s.get("candle_3h")

        def fmt(v, decimals=0):
            if v is None:
                return "N/A"
            if decimals:
                return f"{v:,.{decimals}f}"
            return f"{v:,.0f}"

        def oi_sign(v):
            if v is None:
                return "N/A"
            prefix = "+" if v > 0 else ""
            return f"{prefix}{v:,.0f}"

        line = (
            f"\n[{sym}]\n"
            f"  Spot: {fmt(spot, 1)} | ATM: {fmt(atm)} | Max Pain: {fmt(pain)}\n"
            f"  Support: {fmt(sup)} | Resistance: {fmt(res)}\n"
            f"  OI Engine Verdict: {verdict} ({conf}% conviction)\n"
            f"  PCR: {pcr} | Total CE OI: {fmt(tot_ce)} | Total PE OI: {fmt(tot_pe)}\n"
            f"  CE OI Change: {oi_sign(ce_chg)} | PE OI Change: {oi_sign(pe_chg)}\n"
        )
        if top_sig:
            line += f"  Top Signal: {top_sig} @ {top_strike} {top_ot} ({top_sev})\n"
        if candle_1h or candle_3h:
            line += f"  1H Candle: {candle_1h or 'N/A'} | 3H Candle: {candle_3h or 'N/A'}\n"
        lines.append(line)

    return "\n".join(lines)


def _build_trade_text(perf: dict) -> str:
    """Convert trade performance dict into concise LLM-readable summary."""
    lines = []
    ml = perf

    if ml["ml_opened"] > 0:
        closed_pnl = ml["ml_closed_pnl"]
        pnl_sign = "+" if closed_pnl >= 0 else ""
        lines.append(
            f"Multi-leg books today: {ml['ml_opened']} opened | "
            f"{ml['ml_closed']} closed (P&L: {pnl_sign}Rs {closed_pnl:,.0f}) | "
            f"{ml['ml_open']} still open"
        )
        for b in ml["ml_books"][:8]:
            sym = b.get("symbol", "?")
            struct = b.get("structure", "?")
            status = b.get("status", "?")
            pnl = b.get("total_pnl") or 0.0
            spot_entry = b.get("entry_underlying")
            pnl_sign = "+" if pnl >= 0 else ""
            lines.append(
                f"  {sym} {struct} — {status} | Entry spot {spot_entry} | P&L {pnl_sign}Rs {pnl:,.0f}"
            )
    else:
        lines.append("No multi-leg books opened today.")

    if ml["pt_opened"] > 0:
        lines.append(
            f"Single-leg paper trades: {ml['pt_opened']} opened | "
            f"{ml['pt_closed']} closed | P&L: Rs {ml['pt_total_pnl']:,.0f}"
        )

    return "\n".join(lines) if lines else "No trading activity today."


def _format_firecrawl_items(items: list[dict], max_snippet: int = 800) -> str:
    """Format Firecrawl search items into a dense text block for LLM consumption."""
    if not items:
        return "No data available."
    lines = []
    for item in items:
        title = item.get("title", "Untitled")
        snippet = (item.get("snippet") or item.get("description") or "")[:max_snippet].strip()
        url = item.get("url", "")
        lines.append(f"• {title} ({url})\n  {snippet}")
    return "\n".join(lines)


# ── Telegram formatting ─────────────────────────────────────────────────────

def _format_telegram_report(report: EODMarketReport, date_display: str) -> str:
    """Format structured report as a compact Telegram message."""
    parts = [
        f"📊 *EOD Market Intelligence — {date_display}*",
        "",
        "📈 *Market Close & Drivers*",
        report.index_summary,
        "",
        "📉 *OI & Derivatives Flow*",
        report.oi_derivatives_analysis,
        "",
        "🎯 *Strategic Battleground Levels*",
        report.key_levels_tomorrow,
        "",
        "🌐 *Macro Drivers & Opening Watchlist*",
        report.macro_and_watchlist,
        "",
        "📄 *Full Executive SteadyAlpha PDF generated & attached below.*",
    ]
    return "\n".join(parts)


def _send_telegram_chunked(message: str, chunk_size: int = 3800) -> None:
    """Send long Telegram messages in chunks to avoid 4096-char API limit."""
    from src.alerts.telegram_dispatcher import send_text_and_return_id
    lines = message.split("\n")
    current_chunk: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > chunk_size and current_chunk:
            try:
                msg_id = send_text_and_return_id("\n".join(current_chunk))
                log.info("[eod_report] Dispatched chunk to Telegram (msg_id=%s)", msg_id)
            except Exception as exc:
                log.warning("[eod_report] Telegram dispatch failed: %s", exc)
            current_chunk = []
            current_len = 0
        current_chunk.append(line)
        current_len += line_len

    if current_chunk:
        try:
            msg_id = send_text_and_return_id("\n".join(current_chunk))
            log.info("[eod_report] Dispatched chunk to Telegram (msg_id=%s)", msg_id)
        except Exception as exc:
            log.warning("[eod_report] Telegram dispatch failed: %s", exc)


# ── SteadyAlpha HTML & PDF Generation ────────────────────────────────────────

REPORT_LOT_SIZES = LOT_SIZES


def _fmt_oi(val: float) -> str:
    sign = "+" if val > 0 else ""
    if abs(val) >= 1_000_000:
        return f"{sign}{val / 1_000_000:.2f}M"
    elif abs(val) >= 1_000:
        return f"{sign}{val / 1_000:.1f}K"
    return f"{sign}{val:,.0f}"


def _fmt_oi_with_lots(val: float, symbol: str, html: bool = False) -> str:
    """Format OI change with underlying share volume and contract lot equivalents."""
    sign = "+" if val > 0 else ""
    lots_sign = "+" if val > 0 else "-"
    lot_size = REPORT_LOT_SIZES.get(symbol.upper(), 65)
    lots = abs(val) / lot_size

    # Underlying shares formatting
    if abs(val) >= 1_000_000:
        shs_str = f"{sign}{val / 1_000_000:.2f}M shs"
    elif abs(val) >= 1_000:
        shs_str = f"{sign}{val / 1_000:.1f}K shs"
    else:
        shs_str = f"{sign}{val:,.0f} shs"

    # Contract lots formatting
    if lots >= 1_000:
        lots_str = f"{lots_sign}{lots / 1_000:.1f}K lots"
    elif lots >= 10:
        lots_str = f"{lots_sign}{lots:.0f} lots"
    else:
        lots_str = f"{lots_sign}{lots:.1f} lots"

    if html:
        return f"{shs_str}<br><span style=\"font-size: 8.5px; opacity: 0.8;\">({lots_str})</span>"
    return f"{shs_str} ({lots_str})"


def _classify_oi_regime(symbol: str, ce_delta: float, pe_delta: float, verdict_label: str = "", confidence: int = 0) -> tuple[str, str]:
    """Dynamically determine derivative regime and CSS badge class based on OI flow direction.

    Returns:
        tuple[str, str]: (label_with_conviction, badge_css_class)
    """
    conf_suffix = f" ({confidence}%)" if confidence > 0 else ""

    # 1. Dual Liquidation: Both Call & Put OI declining
    if ce_delta < 0 and pe_delta < 0:
        if abs(pe_delta) > abs(ce_delta) or (verdict_label and verdict_label.lower() in ("bearish", "short buildup", "low conviction")):
            label = f"Long Unwinding{conf_suffix}"
            badge = "badge-bearish"
        else:
            label = f"Dual Liquidation{conf_suffix}"
            badge = "badge-warning"
    # 2. Dual Buildup: Both Call & Put OI increasing
    elif ce_delta > 0 and pe_delta > 0:
        label = f"Straddle Buildup{conf_suffix}"
        badge = "badge-neutral"
    # 3. Call Buildup & Put Unwinding (Pure Short Buildup)
    elif ce_delta > 0 and pe_delta <= 0:
        label = f"Short Buildup{conf_suffix}"
        badge = "badge-bearish"
    # 4. Put Buildup & Call Unwinding (Pure Long Buildup)
    elif ce_delta <= 0 and pe_delta > 0:
        label = f"Long Buildup{conf_suffix}"
        badge = "badge-bullish"
    else:
        lbl = verdict_label if verdict_label and verdict_label != "Unknown" else "Range Compression"
        label = f"{lbl}{conf_suffix}"
        badge = "badge-neutral"

    return label, badge


def _render_oi_chart_svg(nifty_ce: float, nifty_pe: float, bn_ce: float, bn_pe: float, sensex_ce: float, sensex_pe: float) -> str:
    max_val = max(abs(nifty_ce), abs(nifty_pe), abs(bn_ce), abs(bn_pe), abs(sensex_ce), abs(sensex_pe), 1_000_000)
    w, h = 720, 205
    chart_x, chart_w = 120, 585
    mid_x = chart_x + (chart_w / 2)  # 412.5
    half_w = 205.0  # Leaves 85px margin on both sides for value labels

    def get_bar(val: float, y: float, is_pe: bool) -> tuple[float, float, str, float, str]:
        val_pct = val / max_val
        bar_len = max(abs(val_pct) * half_w, 6.0)
        if val >= 0:
            bar_x = mid_x
            color = "#059669" if is_pe else "#0284c7"
            text_x = mid_x + bar_len + 8
            text_anchor = "start"
        else:
            bar_x = mid_x - bar_len
            color = "#dc2626" if is_pe else "#ea580c"
            text_x = mid_x - bar_len - 8
            text_anchor = "end"
        return bar_x, bar_len, color, text_x, text_anchor

    rows = [
        ("NIFTY 50", nifty_ce, nifty_pe, 36),
        ("BANKNIFTY", bn_ce, bn_pe, 86),
        ("SENSEX", sensex_ce, sensex_pe, 136),
    ]

    svg_bars = []
    for label, ce, pe, y in rows:
        ce_x, ce_w, ce_col, ce_tx, ce_anc = get_bar(ce, y, is_pe=False)
        pe_x, pe_w, pe_col, pe_tx, pe_anc = get_bar(pe, y + 15, is_pe=True)

        svg_bars.append(f"""
        <text x="18" y="{y + 18}" fill="#0f172a" font-size="12" font-weight="700" text-anchor="start" font-family="system-ui, -apple-system, sans-serif">{label}</text>
        <rect x="{ce_x:.1f}" y="{y:.1f}" width="{ce_w:.1f}" height="11" rx="3" fill="{ce_col}"/>
        <text x="{ce_tx:.1f}" y="{y + 9:.1f}" fill="{ce_col}" font-size="10.5" font-weight="700" text-anchor="{ce_anc}" font-family="ui-monospace, monospace">{_fmt_oi(ce)} (CE)</text>
        <rect x="{pe_x:.1f}" y="{y + 15:.1f}" width="{pe_w:.1f}" height="11" rx="3" fill="{pe_col}"/>
        <text x="{pe_tx:.1f}" y="{y + 24:.1f}" fill="{pe_col}" font-size="10.5" font-weight="700" text-anchor="{pe_anc}" font-family="ui-monospace, monospace">{_fmt_oi(pe)} (PE)</text>
        """)

    return f"""<svg width="100%" height="205" viewBox="0 0 {w} {h}" fill="none" xmlns="http://www.w3.org/2000/svg" style="background:#f8fafc; border: 1px solid #e2e8f0; border-radius:6px;">
      <line x1="{mid_x}" y1="20" x2="{mid_x}" y2="170" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="4 4"/>
      <text x="{mid_x}" y="15" fill="#64748b" font-size="10" text-anchor="middle" font-weight="700" font-family="system-ui, -apple-system, sans-serif">0 Net Change Axis</text>
      <text x="{chart_x + 10}" y="15" fill="#dc2626" font-size="10" text-anchor="start" font-weight="700" font-family="system-ui, -apple-system, sans-serif">◄ Liquidation / Unwinding</text>
      <text x="{chart_x + chart_w - 10}" y="15" fill="#059669" font-size="10" text-anchor="end" font-weight="700" font-family="system-ui, -apple-system, sans-serif">Accumulation / Buildup ►</text>
      {''.join(svg_bars)}
      <g transform="translate(115, 185)">
        <rect x="0" y="0" width="10" height="10" rx="2" fill="#0284c7"/>
        <text x="15" y="9" fill="#334155" font-size="10" font-weight="600" font-family="system-ui, -apple-system, sans-serif">CE +Writing (Res)</text>
        <rect x="135" y="0" width="10" height="10" rx="2" fill="#ea580c"/>
        <text x="150" y="9" fill="#334155" font-size="10" font-weight="600" font-family="system-ui, -apple-system, sans-serif">CE -Unwind (Covering)</text>
        <rect x="290" y="0" width="10" height="10" rx="2" fill="#059669"/>
        <text x="305" y="9" fill="#334155" font-size="10" font-weight="600" font-family="system-ui, -apple-system, sans-serif">PE +Writing (Support)</text>
        <rect x="445" y="0" width="10" height="10" rx="2" fill="#dc2626"/>
        <text x="460" y="9" fill="#334155" font-size="10" font-weight="600" font-family="system-ui, -apple-system, sans-serif">PE -Unwind (Breakdown)</text>
      </g>
    </svg>"""


def _render_pcr_gauges_svg(nifty_pcr: float, bn_pcr: float, sensex_pcr: float) -> str:
    w, h = 720, 180
    bar_x, bar_w = 120, 385
    min_pcr, max_pcr = 0.4, 1.8

    def pcr_to_x(pcr: float) -> float:
        clamped = max(min_pcr, min(max_pcr, pcr))
        return bar_x + ((clamped - min_pcr) / (max_pcr - min_pcr)) * bar_w

    indices = [
        ("NIFTY 50", nifty_pcr, 42),
        ("BANKNIFTY", bn_pcr, 92),
        ("SENSEX", sensex_pcr, 142),
    ]

    rows = []
    for label, pcr, y in indices:
        px = pcr_to_x(pcr)
        if pcr < 0.75:
            zone_color = "#dc2626"
            status = "Oversold Panic"
        elif pcr < 0.95:
            zone_color = "#ea580c"
            status = "Bearish Distr"
        elif pcr <= 1.20:
            zone_color = "#0284c7"
            status = "Neutral Balance"
        else:
            zone_color = "#d97706"
            status = "Call Wall"

        rows.append(f"""
        <text x="18" y="{y + 9}" fill="#0f172a" font-size="12" font-weight="700" text-anchor="start" font-family="system-ui, -apple-system, sans-serif">{label}</text>
        <rect x="{bar_x}" y="{y}" width="{bar_w}" height="10" rx="5" fill="#e2e8f0"/>
        <rect x="{pcr_to_x(0.4):.1f}" y="{y}" width="{pcr_to_x(0.75) - pcr_to_x(0.4):.1f}" height="10" rx="0" fill="#fee2e2"/>
        <rect x="{pcr_to_x(0.75):.1f}" y="{y}" width="{pcr_to_x(0.95) - pcr_to_x(0.75):.1f}" height="10" rx="0" fill="#ffedd5"/>
        <rect x="{pcr_to_x(0.95):.1f}" y="{y}" width="{pcr_to_x(1.20) - pcr_to_x(0.95):.1f}" height="10" rx="0" fill="#e0f2fe"/>
        <rect x="{pcr_to_x(1.20):.1f}" y="{y}" width="{pcr_to_x(1.80) - pcr_to_x(1.20):.1f}" height="10" rx="0" fill="#fef3c7"/>
        <circle cx="{px:.1f}" cy="{y + 5}" r="7" fill="{zone_color}" stroke="#ffffff" stroke-width="2.5"/>
        <text x="{bar_x + bar_w + 14}" y="{y + 9.5}" fill="{zone_color}" font-size="13.5" font-weight="800" font-family="ui-monospace, monospace">{pcr:.4f}</text>
        <text x="{bar_x + bar_w + 80}" y="{y + 9}" fill="#475569" font-size="10.5" font-weight="700" font-family="system-ui, -apple-system, sans-serif">[{status}]</text>
        """)

    return f"""<svg width="100%" height="180" viewBox="0 0 {w} {h}" fill="none" xmlns="http://www.w3.org/2000/svg" style="background:#f8fafc; border: 1px solid #e2e8f0; border-radius:6px;">
      <text x="{pcr_to_x(0.575):.1f}" y="18" fill="#dc2626" font-size="10" text-anchor="middle" font-weight="700" font-family="system-ui, -apple-system, sans-serif">&lt;0.75 Panic</text>
      <text x="{pcr_to_x(0.83):.1f}" y="18" fill="#ea580c" font-size="10" text-anchor="middle" font-weight="700" font-family="system-ui, -apple-system, sans-serif">0.85 Bear</text>
      <text x="{pcr_to_x(1.10):.1f}" y="18" fill="#0284c7" font-size="10" text-anchor="middle" font-weight="700" font-family="system-ui, -apple-system, sans-serif">1.0 Neutral</text>
      <text x="{pcr_to_x(1.50):.1f}" y="18" fill="#d97706" font-size="10" text-anchor="middle" font-weight="700" font-family="system-ui, -apple-system, sans-serif">&gt;1.20 Overbought</text>
      {''.join(rows)}
    </svg>"""


def _render_steadyalpha_html(date_display: str, scan_data: list[dict], fc_data: dict, report_obj: EODMarketReport | None) -> str:
    """Generate professional SteadyAlpha-style executive memorandum HTML with graphs, charts, and editorial narration."""
    # Index lookups
    nifty = next((x for x in scan_data if x.get("symbol") == "NIFTY"), {})
    bnifty = next((x for x in scan_data if x.get("symbol") == "BANKNIFTY"), {})
    sensex = next((x for x in scan_data if x.get("symbol") == "SENSEX"), {})
    natgas = next((x for x in scan_data if x.get("symbol") == "NATURALGAS"), {})

    def _num(d, k, default=0.0):
        val = d.get(k)
        return float(val) if val is not None else default

    nifty_spot = _num(nifty, "underlying", 22700.0)
    nifty_atm = _num(nifty, "atm_strike", 22700.0)
    nifty_mp = _num(nifty, "max_pain", 22700.0)
    nifty_pcr = _num(nifty, "pcr", 1.0)
    nifty_ce_delta = _num(nifty, "ce_oi_change", 0.0)
    nifty_pe_delta = _num(nifty, "pe_oi_change", 0.0)
    nifty_sup = _num(nifty, "support", 22500.0)
    nifty_res = _num(nifty, "resistance", 23000.0)
    nifty_verdict = nifty.get("verdict_label") or "Neutral"
    nifty_conf = int(_num(nifty, "confidence", 50))
    nifty_regime, nifty_badge = _classify_oi_regime("NIFTY", nifty_ce_delta, nifty_pe_delta, nifty_verdict, nifty_conf)

    bn_spot = _num(bnifty, "underlying", 54300.0)
    bn_atm = _num(bnifty, "atm_strike", 54300.0)
    bn_mp = _num(bnifty, "max_pain", 54600.0)
    bn_pcr = _num(bnifty, "pcr", 1.1)
    bn_ce_delta = _num(bnifty, "ce_oi_change", 0.0)
    bn_pe_delta = _num(bnifty, "pe_oi_change", 0.0)
    bn_sup = _num(bnifty, "support", 54200.0)
    bn_res = _num(bnifty, "resistance", 54400.0)
    bn_verdict = bnifty.get("verdict_label") or "Rangebound"
    bn_conf = int(_num(bnifty, "confidence", 50))
    bn_regime, bn_badge = _classify_oi_regime("BANKNIFTY", bn_ce_delta, bn_pe_delta, bn_verdict, bn_conf)

    sensex_spot = _num(sensex, "underlying", 73300.0)
    sensex_atm = _num(sensex, "atm_strike", 73300.0)
    sensex_mp = _num(sensex, "max_pain", 73300.0)
    sensex_pcr = _num(sensex, "pcr", 1.2)
    sensex_ce_delta = _num(sensex, "ce_oi_change", 0.0)
    sensex_pe_delta = _num(sensex, "pe_oi_change", 0.0)
    sensex_sup = _num(sensex, "support", 72000.0)
    sensex_res = _num(sensex, "resistance", 74000.0)
    sensex_verdict = sensex.get("verdict_label") or "Neutral"
    sensex_conf = int(_num(sensex, "confidence", 50))
    sensex_regime, sensex_badge = _classify_oi_regime("SENSEX", sensex_ce_delta, sensex_pe_delta, sensex_verdict, sensex_conf)

    ng_spot = _num(natgas, "underlying", 290.0)
    ng_atm = _num(natgas, "atm_strike", 290.0)
    ng_mp = _num(natgas, "max_pain", 295.0)
    ng_pcr = _num(natgas, "pcr", 1.0)
    ng_ce_delta = _num(natgas, "ce_oi_change", 0.0)
    ng_pe_delta = _num(natgas, "pe_oi_change", 0.0)
    ng_sup = _num(natgas, "support", 285.0)
    ng_res = _num(natgas, "resistance", 295.0)
    ng_verdict = natgas.get("verdict_label") or "Neutral"
    ng_conf = int(_num(natgas, "confidence", 50))
    ng_regime, ng_badge = _classify_oi_regime("NATURALGAS", ng_ce_delta, ng_pe_delta, ng_verdict, ng_conf)

    # Dynamic percentage changes from DB or LLM text
    report_text = report_obj.index_summary if report_obj else ""
    nifty_pct = _get_symbol_pct_change("NIFTY") or _extract_pct_from_text(report_text, "NIFTY")
    bn_pct = _get_symbol_pct_change("BANKNIFTY") or _extract_pct_from_text(report_text, "BANKNIFTY")
    sensex_pct = _get_symbol_pct_change("SENSEX") or _extract_pct_from_text(report_text, "SENSEX")
    ng_pct = _get_symbol_pct_change("NATURALGAS") or _extract_pct_from_text(report_text, "NATURALGAS")

    def _fmt_pct(pct: float | None) -> tuple[str, str, str]:
        if pct is None:
            return "0.00%", "#64748b", '<path d="M0 10 L100 10" stroke="#64748b" stroke-width="2"/>'
        sign = "+" if pct >= 0 else ""
        pct_str = f"{sign}{pct:.2f}%"
        if pct >= 0:
            return pct_str, "text-positive", '<path d="M0 16 L25 12 L50 14 L75 6 L100 2" stroke="#059669" stroke-width="2" stroke-linecap="round"/>'
        return pct_str, "text-negative", '<path d="M0 4 L25 8 L50 6 L75 14 L100 18" stroke="#dc2626" stroke-width="2" stroke-linecap="round"/>'

    n_pct_str, n_pct_cls, n_spark = _fmt_pct(nifty_pct)
    bn_pct_str, bn_pct_cls, bn_spark = _fmt_pct(bn_pct)
    s_pct_str, s_pct_cls, s_spark = _fmt_pct(sensex_pct)
    ng_pct_str, ng_pct_cls, ng_spark = _fmt_pct(ng_pct)

    # Dynamic FII/DII Institutional flows
    fii_cash, dii_cash, fii_date = _get_latest_fii_dii_flows()
    if fii_cash is not None or dii_cash is not None:
        f_val = fii_cash or 0.0
        d_val = dii_cash or 0.0
        net_val = f_val + d_val
        net_sign = "+" if net_val >= 0 else ""
        flow_net_str = f"{net_sign}₹{net_val:,.0f} Cr Net"
        flow_col = "#059669" if net_val >= 0 else "#dc2626"
        fii_str = f"{'+' if f_val >= 0 else ''}₹{f_val:,.0f}Cr"
        dii_str = f"{'+' if d_val >= 0 else ''}₹{d_val:,.0f}Cr"
        fii_cls = "text-positive" if f_val >= 0 else "text-negative"
        dii_cls = "text-positive" if d_val >= 0 else "text-negative"
        total_mag = abs(f_val) + abs(d_val) + 1e-6
        f_bar_pct = max(min(int((abs(f_val) / total_mag) * 100), 85), 15)
        d_bar_pct = 100 - f_bar_pct
    else:
        flow_net_str = "Equilibrium Net"
        flow_col = "#0284c7"
        fii_str = "Neutral"
        dii_str = "Neutral"
        fii_cls = "text-neutral"
        dii_cls = "text-neutral"
        f_bar_pct = 50
        d_bar_pct = 50

    # SVGs
    oi_chart_svg = _render_oi_chart_svg(nifty_ce_delta, nifty_pe_delta, bn_ce_delta, bn_pe_delta, sensex_ce_delta, sensex_pe_delta)
    pcr_gauge_svg = _render_pcr_gauges_svg(nifty_pcr, bn_pcr, sensex_pcr)

    # Dynamic Executive Takeaways
    if report_obj:
        takeaway_items = [
            f"<li><strong>Market Regime & Tone:</strong> {report_obj.bot_performance.replace(chr(10), ' ')}</li>",
            f"<li><strong>Domestic Benchmarks:</strong> {report_obj.index_summary.replace(chr(10), ' • ')}</li>",
            f"<li><strong>Derivatives Flow Dynamics:</strong> {report_obj.oi_derivatives_analysis.replace(chr(10), ' • ')}</li>",
            f"<li><strong>Institutional Catalysts & Watchlist:</strong> {report_obj.macro_and_watchlist.replace(chr(10), ' ')}</li>",
        ]
    else:
        takeaway_items = [
            f"<li><strong>Market Regime: {nifty_regime} across Domestic Benchmarks.</strong> Closing posture across NIFTY 50 ({nifty_spot:,.1f}), BANKNIFTY ({bn_spot:,.1f}), and SENSEX ({sensex_spot:,.1f}) indicates {nifty_verdict.lower()} order flow with PCR at {nifty_pcr:.2f}.</li>",
            f"<li><strong>NIFTY 50 Order Flow:</strong> Spot ({nifty_spot:,.1f}) anchored near <strong>{nifty_mp:,.0f} Max Pain</strong>. Net Call OI change {_fmt_oi_with_lots(nifty_ce_delta, 'NIFTY')} vs Put OI change {_fmt_oi_with_lots(nifty_pe_delta, 'NIFTY')}.</li>",
            f"<li><strong>BANKNIFTY Derivatives Structure:</strong> Spot ({bn_spot:,.1f}) vs Max Pain ({bn_mp:,.0f}) with PCR {bn_pcr:.4f}. Support established at {bn_sup:,.0f} and resistance cluster at {bn_res:,.0f}.</li>",
            f"<li><strong>Institutional Capital Flows:</strong> {flow_net_str} recorded (FII: {fii_str}, DII: {dii_str}).</li>",
        ]
    takeaways_html = "\n      ".join(takeaway_items)

    # SCIR Framework Dynamic Narrative
    if report_obj and report_obj.index_summary:
        scir_situation = f"Benchmark indices concluded the session with {report_obj.index_summary.replace(chr(10), ' | ')}."
    else:
        scir_situation = (
            f"Domestic benchmark indices settled the session with NIFTY at {nifty_spot:,.1f} ({n_pct_str}), "
            f"BANKNIFTY at {bn_spot:,.1f} ({bn_pct_str}), and SENSEX at {sensex_spot:,.1f} ({s_pct_str}). "
            f"The prevailing structural regime reflects {nifty_regime}."
        )

    if report_obj and report_obj.oi_derivatives_analysis:
        scir_complication = f"Derivatives flow deconstruction: {report_obj.oi_derivatives_analysis.replace(chr(10), ' | ')}."
    else:
        scir_complication = (
            f"NIFTY PCR settled at {nifty_pcr:.4f} with net CE delta {_fmt_oi(nifty_ce_delta)} and PE delta {_fmt_oi(nifty_pe_delta)}. "
            f"Spot tracks {abs(nifty_spot - nifty_mp):.0f} points from the Max Pain magnet ({nifty_mp:,.0f}), "
            f"while BANKNIFTY tracks {abs(bn_spot - bn_mp):.0f} points from Max Pain ({bn_mp:,.0f})."
        )

    if report_obj and report_obj.key_levels_tomorrow:
        scir_implication = f"Key pivot and battleground levels: {report_obj.key_levels_tomorrow.replace(chr(10), ' | ')}."
    else:
        scir_implication = (
            f"Key risk boundaries: NIFTY support floor lies at {nifty_sup:,.0f} with overhead resistance at {nifty_res:,.0f}. "
            f"BANKNIFTY support is defined at {bn_sup:,.0f} and resistance at {bn_res:,.0f}. "
            f"A breach of these boundaries will force gamma-neutral dealers to rehedge."
        )

    if "bull" in nifty_verdict.lower() or nifty_pcr >= 1.2:
        scir_resolution = (
            f"Bias favors defending pullbacks into {nifty_sup:,.0f} (NIFTY) and {bn_sup:,.0f} (BANKNIFTY). "
            f"Optimal risk-defined structures favor Bull Put Spreads or credit put ratio underwriting."
        )
    elif "bear" in nifty_verdict.lower() or nifty_pcr < 0.8:
        scir_resolution = (
            f"Bias favors fading counter-trend rallies toward {nifty_res:,.0f} (NIFTY) and {bn_res:,.0f} (BANKNIFTY). "
            f"Deploy Bear Call Spreads to capture premium decay against established resistance ceilings."
        )
    else:
        scir_resolution = (
            f"Rangebound equilibrium dominates. Desks should prioritize defined-risk delta-neutral structures "
            f"(Iron Condors / Short Strangles) centered around Max Pain {nifty_mp:,.0f} (NIFTY) and {bn_mp:,.0f} (BANKNIFTY)."
        )

    # Dynamic Battleground Matrix Rows
    def _bias_badge(verdict: str) -> tuple[str, str]:
        v = verdict.lower()
        if "bull" in v or "put writing" in v or "long buildup" in v:
            return "badge-bullish", f"Bias: Bullish ({verdict})"
        elif "bear" in v or "call writing" in v or "short buildup" in v or "unwind" in v:
            return "badge-bearish", f"Bias: Bearish ({verdict})"
        return "badge-neutral", f"Bias: Neutral ({verdict})"

    n_bg_badge, n_bg_lbl = _bias_badge(nifty_verdict)
    bn_bg_badge, bn_bg_lbl = _bias_badge(bn_verdict)
    s_bg_badge, s_bg_lbl = _bias_badge(sensex_verdict)

    battleground_rows = f"""
        <tr>
          <td rowspan="4"><strong>NIFTY 50</strong><br><span class="badge {n_bg_badge}">{n_bg_lbl}</span></td>
          <td><strong>{nifty_res:,.0f}</strong></td>
          <td>Major Call Resistance</td>
          <td>Primary overhead ceiling cluster. Sell credit call spreads on relief spikes towards this strike.</td>
        </tr>
        <tr>
          <td><strong>{nifty_mp:,.0f}</strong></td>
          <td>ATM / Max Pain Pivot</td>
          <td>Gravitational equilibrium anchor ({nifty_mp:,.0f}). Spot at {nifty_spot:,.1f}. Watch opening auction price acceptance.</td>
        </tr>
        <tr>
          <td><strong>{nifty_sup:,.0f}</strong></td>
          <td>Key Put Support</td>
          <td>Structural Put floor. Sustained break below opens accelerated downside momentum toward next support.</td>
        </tr>
        <tr>
          <td><strong>{max(nifty_sup - 200, 0):,.0f}</strong></td>
          <td>Extended Buffer Floor</td>
          <td>Deep institutional put open interest cushion; high-probability zone for squaring short delta.</td>
        </tr>
        <tr>
          <td rowspan="4"><strong>BANKNIFTY</strong><br><span class="badge {bn_bg_badge}">{bn_bg_lbl}</span></td>
          <td><strong>{bn_res:,.0f}</strong></td>
          <td>Major Call Resistance</td>
          <td>Overhead supply ceiling cluster. Fading rallies here offers attractive risk-reward for defined spreads.</td>
        </tr>
        <tr>
          <td><strong>{bn_mp:,.0f}</strong></td>
          <td>Max Pain Magnet</td>
          <td>Equilibrium anchor ({bn_mp:,.0f}). Spot currently at {bn_spot:,.1f} ({abs(bn_spot - bn_mp):.0f} pts delta).</td>
        </tr>
        <tr>
          <td><strong>{bn_sup:,.0f}</strong></td>
          <td>Key Put Support</td>
          <td>Immediate institutional demand zone. Downside cushion and risk boundary for credit put spreads.</td>
        </tr>
        <tr>
          <td><strong>{max(bn_sup - 500, 0):,.0f}</strong></td>
          <td>Structural Buffer Floor</td>
          <td>Extended support cluster for multi-leg strangle / condor wing placement.</td>
        </tr>
        <tr>
          <td rowspan="3"><strong>SENSEX</strong><br><span class="badge {s_bg_badge}">{s_bg_lbl}</span></td>
          <td><strong>{sensex_res:,.0f}</strong></td>
          <td>Major Resistance</td>
          <td>Overhead call writing cluster. Strong resistance barrier for zero-DTE expiry structures.</td>
        </tr>
        <tr>
          <td><strong>{sensex_mp:,.0f}</strong></td>
          <td>Max Pain Pivot</td>
          <td>Equilibrium anchor ({sensex_mp:,.0f}). Spot at {sensex_spot:,.1f}. Pinning risk high near expiry.</td>
        </tr>
        <tr>
          <td><strong>{sensex_sup:,.0f}</strong></td>
          <td>Key Support Floor</td>
          <td>Structural Put floor ({sensex_sup:,.0f}). Key invalidation level for credit put positions.</td>
        </tr>
    """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SteadyAlpha — Executive Derivatives Intelligence Report</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      background-color: #f8fafc;
      color: #0f172a;
      line-height: 1.5;
      font-size: 12.5px;
      -webkit-font-smoothing: antialiased;
    }}
    .tabular-nums {{ font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
    .document-container {{
      max-width: 1000px;
      margin: 0 auto;
      background: #ffffff;
      padding: 30px 36px;
      border: 1px solid #e2e8f0;
    }}
    .masthead {{
      border-bottom: 2px solid #001a3a;
      padding-bottom: 14px;
      margin-bottom: 18px;
    }}
    .masthead-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }}
    .brand-wrap {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .firm-brand {{
      font-family: Georgia, 'Times New Roman', serif;
      font-size: 22px;
      font-weight: 700;
      color: #001a3a;
      letter-spacing: -0.3px;
    }}
    .practice-group {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 1px;
      color: #0284c7;
      font-weight: 700;
      margin-top: 1px;
    }}
    .doc-meta {{
      text-align: right;
      font-size: 10.5px;
      color: #475569;
      line-height: 1.35;
    }}
    .audit-badge {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: #f0fdf4;
      border: 1px solid #bbf7d0;
      color: #166534;
      font-size: 9px;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 4px;
      margin-top: 3px;
    }}
    .report-title-box {{ margin-top: 8px; }}
    .report-category {{
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      color: #0284c7;
      letter-spacing: 1px;
    }}
    .report-title {{
      font-family: Georgia, 'Times New Roman', serif;
      font-size: 22px;
      color: #001a3a;
      font-weight: 700;
      line-height: 1.25;
      margin-top: 2px;
    }}
    .report-subtitle {{
      font-size: 12px;
      color: #475569;
      margin-top: 2px;
    }}

    /* KPI Dashboard Ribbon */
    .kpi-ribbon {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin: 16px 0 20px 0;
    }}
    .kpi-card {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      padding: 10px 12px;
      border-top: 3px solid #001a3a;
    }}
    .kpi-card.nifty {{ border-top-color: #0284c7; }}
    .kpi-card.bnifty {{ border-top-color: #d97706; }}
    .kpi-card.sensex {{ border-top-color: #dc2626; }}
    .kpi-card.flows {{ border-top-color: #059669; }}
    .kpi-label {{
      font-size: 9.5px;
      font-weight: 700;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .kpi-val {{
      font-size: 17px;
      font-weight: 800;
      color: #001a3a;
      margin: 2px 0;
    }}
    .kpi-sub {{
      font-size: 9.5px;
      color: #475569;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .kpi-spark {{
      margin-top: 5px;
      width: 100%;
      height: 15px;
    }}

    /* Executive Takeaways */
    .executive-takeaways {{
      background-color: #f1f5f9;
      border-left: 4px solid #001a3a;
      padding: 12px 16px;
      margin-bottom: 18px;
      border-radius: 0 6px 6px 0;
    }}
    .takeaways-heading {{
      font-family: Georgia, 'Times New Roman', serif;
      font-size: 12.5px;
      color: #001a3a;
      font-weight: 700;
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .takeaways-list {{ list-style: none; }}
    .takeaways-list li {{
      position: relative;
      padding-left: 14px;
      margin-bottom: 5px;
      font-size: 11px;
      color: #1e293b;
      line-height: 1.45;
    }}
    .takeaways-list li::before {{
      content: "■";
      position: absolute;
      left: 0;
      color: #0284c7;
      font-size: 8px;
      top: 1px;
    }}

    /* SCIR Grid */
    .scir-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-bottom: 16px;
    }}
    .scir-card {{
      background: #ffffff;
      border: 1px solid #e2e8f0;
      padding: 10px 12px;
      border-top: 3px solid #64748b;
      border-radius: 4px;
    }}
    .scir-card.situation {{ border-top-color: #001a3a; }}
    .scir-card.complication {{ border-top-color: #dc2626; }}
    .scir-card.implication {{ border-top-color: #d97706; }}
    .scir-card.resolution {{ border-top-color: #059669; }}
    .scir-label {{
      font-size: 9px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      margin-bottom: 3px;
    }}
    .scir-card.situation .scir-label {{ color: #001a3a; }}
    .scir-card.complication .scir-label {{ color: #dc2626; }}
    .scir-card.implication .scir-label {{ color: #d97706; }}
    .scir-card.resolution .scir-label {{ color: #059669; }}
    .scir-card p {{
      font-size: 10.5px;
      color: #334155;
      line-height: 1.4;
    }}

    /* Exhibits & Tables */
    .exhibit-container {{ margin: 12px 0; break-inside: avoid; }}
    .exhibit-header {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 6px;
      border-bottom: 1.5px solid #001a3a;
      padding-bottom: 3px;
    }}
    .exhibit-tag {{
      font-size: 10px;
      font-weight: 800;
      color: #001a3a;
      text-transform: uppercase;
      letter-spacing: 0.8px;
    }}
    .exhibit-title {{
      font-family: Georgia, 'Times New Roman', serif;
      font-size: 12.5px;
      color: #001a3a;
      font-weight: 700;
    }}
    .exhibit-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 10px;
      text-align: left;
    }}
    .exhibit-table th {{
      background-color: #001a3a;
      color: #ffffff;
      font-weight: 600;
      padding: 4px 6px;
      font-size: 9.5px;
      border: 1px solid #001a3a;
    }}
    .exhibit-table td {{
      padding: 3.5px 6px;
      border-bottom: 1px solid #e2e8f0;
      border-left: 1px solid #f1f5f9;
      border-right: 1px solid #f1f5f9;
      color: #1e293b;
    }}
    .exhibit-table tr:nth-child(even) td {{ background-color: #f8fafc; }}

    /* Badges */
    .badge {{
      display: inline-block;
      padding: 1px 4px;
      border-radius: 2px;
      font-size: 8.5px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .badge-bearish {{ background-color: #fee2e2; color: #991b1b; }}
    .badge-bullish {{ background-color: #dcfce7; color: #166534; }}
    .badge-neutral {{ background-color: #f1f5f9; color: #334155; }}
    .badge-warning {{ background-color: #fef3c7; color: #92400e; }}
    .text-negative {{ color: #dc2626; font-weight: 600; }}
    .text-positive {{ color: #059669; font-weight: 600; }}
    .text-neutral {{ color: #64748b; font-weight: 600; }}

    /* Sector Heatmap Tiles */
    .sector-grid {{
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 6px;
      margin: 8px 0 10px 0;
    }}
    .sector-tile {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 4px;
      padding: 6px;
      text-align: center;
    }}
    .sector-name {{ font-size: 9px; font-weight: 700; color: #1e293b; text-transform: uppercase; }}
    .sector-chg {{ font-size: 11px; font-weight: 800; margin: 1px 0; }}
    .sector-desc {{ font-size: 8px; color: #64748b; line-height: 1.15; }}

    /* Microstructure Insights */
    .insights-box {{
      background: #ffffff;
      border: 1px solid #cbd5e1;
      border-left: 3.5px solid #0284c7;
      padding: 8px 12px;
      margin: 10px 0;
      border-radius: 0 4px 4px 0;
    }}
    .insights-title {{
      font-size: 9.5px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: #0284c7;
      margin-bottom: 2px;
    }}
    .insights-body {{
      font-size: 10px;
      color: #334155;
      line-height: 1.35;
    }}

    /* Report Footer */
    .report-footer {{
      border-top: 1px solid #e2e8f0;
      padding-top: 8px;
      margin-top: 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 9px;
      color: #64748b;
    }}
    .confidential-tag {{
      font-weight: 800;
      color: #001a3a;
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }}

    @media print {{
      @page {{ size: A4 portrait; margin: 6mm 10mm; }}
      body {{ background-color: #ffffff; }}
      .document-container {{ border: none; padding: 0; max-width: 100%; }}
      .avoid-break {{ break-inside: avoid; page-break-inside: avoid; }}
      .page-break {{ page-break-before: always; break-before: always; }}
    }}
  </style>
</head>
<body>

<div class="document-container">

  <!-- MASTHEAD -->
  <header class="masthead">
    <div class="masthead-top">
      <div class="brand-wrap">
        <!-- SteadyAlpha Modern Geometric Crest Logo -->
        <svg width="40" height="40" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect width="48" height="48" rx="10" fill="#001a3a"/>
          <path d="M14 34L24 14L34 34" stroke="#0077c8" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M18 27H30" stroke="#0077c8" stroke-width="3" stroke-linecap="round"/>
          <circle cx="24" cy="14" r="3.5" fill="#f59e0b"/>
          <path d="M24 14L36 24L32 32" stroke="#f59e0b" stroke-width="2" stroke-dasharray="2 2"/>
        </svg>
        <div>
          <div class="firm-brand">SteadyAlpha</div>
          <div class="practice-group">Indian Capital Markets &amp; Quantitative Analytics Practice</div>
        </div>
      </div>
      <div class="doc-meta">
        <div><strong>Date:</strong> {date_display}</div>
        <div><strong>Timeframe:</strong> EOD Market Intelligence (4:00 PM IST)</div>
        <div class="audit-badge">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg>
          SYSTEM QUANT AUDITED
        </div>
      </div>
    </div>
    
    <div class="report-title-box">
      <div class="report-category">Strategic Derivatives Intelligence Memorandum</div>
      <h1 class="report-title">Domestic Equity &amp; Commodities Market Close Analysis</h1>
      <div class="report-subtitle">Quantitative Order Flow Deconstruction, Asymmetric Put Liquidation, and Strategic Expiry Playbook</div>
    </div>
  </header>

  <!-- HIGH-ATTENTION KPI RIBBON -->
  <div class="kpi-ribbon avoid-break">
    <div class="kpi-card nifty">
      <div class="kpi-label">NIFTY 50 SPOT</div>
      <div class="kpi-val tabular-nums">{nifty_spot:,.1f}</div>
      <div class="kpi-sub">
        <span class="{n_pct_cls} tabular-nums">{n_pct_str}</span>
        <span>PCR <strong class="tabular-nums">{nifty_pcr:.2f}</strong></span>
      </div>
      <svg class="kpi-spark" viewBox="0 0 100 20" fill="none">
        {n_spark}
      </svg>
    </div>

    <div class="kpi-card bnifty">
      <div class="kpi-label">BANKNIFTY SPOT</div>
      <div class="kpi-val tabular-nums">{bn_spot:,.1f}</div>
      <div class="kpi-sub">
        <span class="{bn_pct_cls} tabular-nums">{bn_pct_str}</span>
        <span>PCR <strong class="tabular-nums">{bn_pcr:.2f}</strong></span>
      </div>
      <svg class="kpi-spark" viewBox="0 0 100 20" fill="none">
        {bn_spark}
      </svg>
    </div>

    <div class="kpi-card sensex">
      <div class="kpi-label">SENSEX SPOT</div>
      <div class="kpi-val tabular-nums">{sensex_spot:,.1f}</div>
      <div class="kpi-sub">
        <span class="{s_pct_cls} tabular-nums">{s_pct_str}</span>
        <span>PCR <strong class="tabular-nums">{sensex_pcr:.2f}</strong></span>
      </div>
      <svg class="kpi-spark" viewBox="0 0 100 20" fill="none">
        {s_spark}
      </svg>
    </div>

    <div class="kpi-card flows">
      <div class="kpi-label">FII / DII CASH FLOW</div>
      <div class="kpi-val tabular-nums" style="font-size: 15px; color: {flow_col};">{flow_net_str}</div>
      <div class="kpi-sub">
        <span>FII: <strong class="{fii_cls}">{fii_str}</strong></span>
        <span>DII: <strong class="{dii_cls}">{dii_str}</strong></span>
      </div>
      <!-- Mini Flow Bar -->
      <div style="margin-top: 6px; height: 5px; background: #fee2e2; border-radius: 2.5px; display: flex; overflow: hidden;">
        <div style="width: {f_bar_pct}%; background: #dc2626;"></div>
        <div style="width: {d_bar_pct}%; background: #059669;"></div>
      </div>
    </div>
  </div>

  <!-- EXECUTIVE TAKEAWAYS -->
  <div class="executive-takeaways avoid-break">
    <div class="takeaways-heading">Executive Summary &amp; Institutional Posture</div>
    <ul class="takeaways-list">
      {takeaways_html}
    </ul>
  </div>

  <!-- STRATEGIC NARRATIVE (SCIR FRAMEWORK) -->
  <section class="avoid-break" style="margin-bottom: 14px;">
    <div class="exhibit-header">
      <div class="exhibit-tag">Strategic Framework</div>
      <div class="exhibit-title">1.0 SCIR Analytical Narrative</div>
    </div>

    <div class="scir-grid">
      <div class="scir-card situation">
        <div class="scir-label">The Situation</div>
        <p>{scir_situation}</p>
      </div>

      <div class="scir-card complication">
        <div class="scir-label">The Complication</div>
        <p>{scir_complication}</p>
      </div>

      <div class="scir-card implication">
        <div class="scir-label">The Implication</div>
        <p>{scir_implication}</p>
      </div>

      <div class="scir-card resolution">
        <div class="scir-label">The Strategic Resolution</div>
        <p>{scir_resolution}</p>
      </div>
    </div>
  </section>

  <!-- CORE ASSET BENCHMARK & CAPITAL FLOW PERFORMANCE MATRIX -->
  <section class="exhibit-container avoid-break" style="margin: 10px 0 0 0;">
    <div class="exhibit-header">
      <div class="exhibit-tag">Market Coverage</div>
      <div class="exhibit-title">Core Asset Benchmark &amp; Capital Flow Performance Matrix</div>
    </div>
    <div class="sector-grid">
      <div class="sector-tile">
        <div class="sector-name">NIFTY 50</div>
        <div class="sector-chg {n_pct_cls} tabular-nums">{n_pct_str}</div>
        <div class="sector-desc">{nifty_verdict} ({nifty_conf}%)</div>
      </div>
      <div class="sector-tile">
        <div class="sector-name">BANKNIFTY</div>
        <div class="sector-chg {bn_pct_cls} tabular-nums">{bn_pct_str}</div>
        <div class="sector-desc">{bn_verdict} ({bn_conf}%)</div>
      </div>
      <div class="sector-tile">
        <div class="sector-name">SENSEX</div>
        <div class="sector-chg {s_pct_cls} tabular-nums">{s_pct_str}</div>
        <div class="sector-desc">{sensex_verdict} ({sensex_conf}%)</div>
      </div>
      <div class="sector-tile">
        <div class="sector-name">NATURALGAS</div>
        <div class="sector-chg {ng_pct_cls} tabular-nums">{ng_pct_str}</div>
        <div class="sector-desc">{ng_verdict} ({ng_conf}%)</div>
      </div>
      <div class="sector-tile">
        <div class="sector-name">FII / DII NET</div>
        <div class="sector-chg tabular-nums" style="color: {flow_col};">{flow_net_str}</div>
        <div class="sector-desc">FII: {fii_str} | DII: {dii_str}</div>
      </div>
    </div>
  </section>

  <!-- PAGE BREAK FOR CLEAN 2-PAGE SPREAD -->
  <div class="page-break"></div>

  <!-- EXHIBIT 1A: OPEN INTEREST FLOW LANDSCAPE (SVG GRAPH) -->
  <section class="exhibit-container avoid-break" style="margin-top: 10px;">
    <div class="exhibit-header">
      <div class="exhibit-tag">Exhibit 1A</div>
      <div class="exhibit-title">Institutional Open Interest Liquidation &amp; Addition Landscape</div>
    </div>
    {oi_chart_svg}
  </section>

  <!-- EXHIBIT 1B: PCR SENTIMENT GAUGES (SVG GRAPH) -->
  <section class="exhibit-container avoid-break">
    <div class="exhibit-header">
      <div class="exhibit-tag">Exhibit 1B</div>
      <div class="exhibit-title">Derivatives Sentiment Spectrum &amp; Put-Call Ratio Gauges</div>
    </div>
    {pcr_gauge_svg}
  </section>

  <!-- EXHIBIT 1C: DERIVATIVES POSITIONING DECONSTRUCTION TABLE -->
  <section class="exhibit-container avoid-break">
    <div class="exhibit-header">
      <div class="exhibit-tag">Exhibit 1C</div>
      <div class="exhibit-title">Quantitative Option Chain &amp; Derivatives Positioning Deconstruction</div>
    </div>

    <table class="exhibit-table tabular-nums">
      <thead>
        <tr>
          <th>Asset Benchmark</th>
          <th>Spot Close</th>
          <th>ATM Strike</th>
          <th>Max Pain</th>
          <th>PCR</th>
          <th>CE OI Change<br><span style="font-size: 8px; font-weight: 400; opacity: 0.85;">(Shares &amp; Lots)</span></th>
          <th>PE OI Change<br><span style="font-size: 8px; font-weight: 400; opacity: 0.85;">(Shares &amp; Lots)</span></th>
          <th>Structural Verdict</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>NIFTY 50</strong></td>
          <td>{nifty_spot:,.1f}</td>
          <td>{nifty_atm:,.0f}</td>
          <td>{nifty_mp:,.0f}</td>
          <td class="{ 'text-positive' if nifty_pcr >= 1.0 else 'text-negative' }">{nifty_pcr:.4f}</td>
          <td class="{ 'text-positive' if nifty_ce_delta > 0 else 'text-negative' }">{_fmt_oi_with_lots(nifty_ce_delta, 'NIFTY', html=True)}</td>
          <td class="{ 'text-positive' if nifty_pe_delta > 0 else 'text-negative' }">{_fmt_oi_with_lots(nifty_pe_delta, 'NIFTY', html=True)}</td>
          <td><span class="badge {nifty_badge}">{nifty_regime}</span></td>
        </tr>
        <tr>
          <td><strong>BANKNIFTY</strong></td>
          <td>{bn_spot:,.1f}</td>
          <td>{bn_atm:,.0f}</td>
          <td>{bn_mp:,.0f}</td>
          <td class="{ 'text-positive' if bn_pcr >= 1.0 else 'text-negative' }">{bn_pcr:.4f}</td>
          <td class="{ 'text-positive' if bn_ce_delta > 0 else 'text-negative' }">{_fmt_oi_with_lots(bn_ce_delta, 'BANKNIFTY', html=True)}</td>
          <td class="{ 'text-positive' if bn_pe_delta > 0 else 'text-negative' }">{_fmt_oi_with_lots(bn_pe_delta, 'BANKNIFTY', html=True)}</td>
          <td><span class="badge {bn_badge}">{bn_regime}</span></td>
        </tr>
        <tr>
          <td><strong>SENSEX</strong></td>
          <td>{sensex_spot:,.1f}</td>
          <td>{sensex_atm:,.0f}</td>
          <td>{sensex_mp:,.0f}</td>
          <td class="{ 'text-positive' if sensex_pcr >= 1.0 else 'text-negative' }">{sensex_pcr:.4f}</td>
          <td class="{ 'text-positive' if sensex_ce_delta > 0 else 'text-negative' }">{_fmt_oi_with_lots(sensex_ce_delta, 'SENSEX', html=True)}</td>
          <td class="{ 'text-positive' if sensex_pe_delta > 0 else 'text-negative' }">{_fmt_oi_with_lots(sensex_pe_delta, 'SENSEX', html=True)}</td>
          <td><span class="badge {sensex_badge}">{sensex_regime}</span></td>
        </tr>
        <tr>
          <td><strong>NATURALGAS</strong></td>
          <td>{ng_spot:,.2f}</td>
          <td>{ng_atm:,.2f}</td>
          <td>{ng_mp:,.0f}</td>
          <td class="{ 'text-positive' if ng_pcr >= 1.0 else 'text-negative' }">{ng_pcr:.4f}</td>
          <td class="{ 'text-positive' if ng_ce_delta > 0 else 'text-negative' }">{_fmt_oi_with_lots(ng_ce_delta, 'NATURALGAS', html=True)}</td>
          <td class="{ 'text-positive' if ng_pe_delta > 0 else 'text-negative' }">{_fmt_oi_with_lots(ng_pe_delta, 'NATURALGAS', html=True)}</td>
          <td><span class="badge {ng_badge}">{ng_regime}</span></td>
        </tr>
      </tbody>
    </table>
  </section>

  <!-- MICROSTRUCTURE INSIGHTS -->
  <div class="insights-box avoid-break">
    <div class="insights-title">Microstructure Insight: Anatomy of the Derivative Flow Shift</div>
    <div class="insights-body">
      <strong>Order Flow Dynamics:</strong> In NIFTY, institutional repositioning produced CE change {_fmt_oi_with_lots(nifty_ce_delta, 'NIFTY')} and PE change {_fmt_oi_with_lots(nifty_pe_delta, 'NIFTY')} ({nifty_regime}), while BANKNIFTY's PCR ({bn_pcr:.4f}) reflects positioning with Max Pain at {bn_mp:,.0f} and resistance near {bn_res:,.0f}. Institutional desks remain hedged via defined-risk credit spreads aligned with prevailing volatility.
    </div>
  </div>

  <!-- EXHIBIT 2: STRATEGIC BATTLEGROUND MATRIX -->
  <section class="exhibit-container avoid-break">
    <div class="exhibit-header">
      <div class="exhibit-tag">Exhibit 2</div>
      <div class="exhibit-title">Strategic Battleground Matrix &amp; Expiry Scenario Playbook</div>
    </div>

    <table class="exhibit-table tabular-nums">
      <thead>
        <tr>
          <th>Benchmark</th>
          <th>Key Pivot Level</th>
          <th>Level Classification</th>
          <th>Institutional Significance &amp; Tactical Guidance</th>
        </tr>
      </thead>
      <tbody>
        {battleground_rows}
      </tbody>
    </table>
  </section>

  <!-- REPORT FOOTER -->
  <footer class="report-footer">
    <div>
      <span class="confidential-tag">SteadyAlpha</span> — Indian Capital Markets &amp; Quantitative Analytics Practice
    </div>
    <div>
      Ref: SA-DERV-{datetime.now(IST).strftime('%Y%m%d')} | Executive Memorandum
    </div>
    <div>
      {date_display}
    </div>
  </footer>

</div>

</body>
</html>
"""
    return html


# Backwards compatibility alias
_render_mckinsey_html = _render_steadyalpha_html


def _generate_pdf_from_html(html_content: str, output_pdf_path: Path) -> Path | None:
    """Render HTML string to PDF using Playwright headless Chromium.

    Returns the written Path or None on failure. Handles file locking gracefully.
    """
    try:
        from playwright.async_api import async_playwright

        temp_html = output_pdf_path.with_suffix(".html")
        temp_html.write_text(html_content, encoding="utf-8")

        target_path = output_pdf_path
        # If the target is locked by an external viewer (e.g. Acrobat), fallback to timestamped
        try:
            with open(target_path, "a+"):
                pass
        except PermissionError:
            ts = datetime.now(IST).strftime("%H%M%S")
            target_path = output_pdf_path.with_stem(f"{output_pdf_path.stem}_{ts}")
            log.warning("[eod_report] %s locked by another process; saving to %s", output_pdf_path.name, target_path.name)

        async def _render():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(f"file:///{temp_html.resolve().as_posix()}", wait_until="networkidle")
                await page.pdf(
                    path=str(target_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "10mm", "bottom": "10mm", "left": "12mm", "right": "12mm"},
                )
                await browser.close()

        asyncio.run(_render())
        log.info("[eod_report] Successfully rendered SteadyAlpha PDF: %s (%d bytes)",
                 target_path, target_path.stat().st_size)
        return target_path
    except Exception as exc:
        log.error("[eod_report] Playwright PDF generation failed: %s", exc)
        return None


# ── Main Entry Point ────────────────────────────────────────────────────────

def generate_eod_macro_report() -> Path | None:
    """Generate daily Market Close EOD report at 16:00 IST (4:00 PM).

    Data flow:
      1. Firecrawl   → web market closing bell, sectoral movers, FII/DII flows, macro wrap
      2. DB scan     → local quantitative OI/PCR/verdict/levels data
      3. LLM         → generates concise Telegram briefing (no bot ledger)
      4. SteadyAlpha → renders executive-grade A4 HTML & PDF document with rich institutional narration
      5. Telegram    → dispatches text briefing + attaches SteadyAlpha PDF document
    """
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y%m%d")
    date_display = now_ist.strftime("%d %b %Y")

    log.info("[eod_report] Starting EOD Strategic Intelligence Report generation for %s (4:00 PM IST)", date_display)

    # ── 1. Fetch live web market wrap via Firecrawl ─────────────────────────
    fc_data = fetch_firecrawl_eod_data(date_display)
    closing_bell_text = _format_firecrawl_items(fc_data["closing_bell"]["items"], max_snippet=1200)
    fii_dii_text = _format_firecrawl_items(fc_data["fii_dii_flows"]["items"], max_snippet=800)
    news_text = _format_firecrawl_items(fc_data["macro_news"]["items"], max_snippet=800)
    fc_ok = fc_data["ok"]

    log.info("[eod_report] Firecrawl: closing_bell=%d fii_dii=%d news=%d (ok=%s)",
             fc_data["closing_bell"]["count"],
             fc_data["fii_dii_flows"]["count"],
             fc_data["macro_news"]["count"],
             fc_ok)

    # ── 2. DB scan data (OI, PCR, max pain, verdicts) ──────────────────────
    summaries = _get_today_scan_data()
    scan_text = _build_scan_text(summaries)

    # ── 3. Build LLM prompt: Live web data + Local quantitative OI ──────────
    if fc_ok:
        data_source_block = f"""═══ WEB CLOSING BELL & SECTOR WRAP (Firecrawl) ═══
{closing_bell_text}

═══ WEB FII / DII INSTITUTIONAL FLOWS (Firecrawl) ═══
{fii_dii_text}

═══ WEB MACRO NEWS & MARKET DRIVERS (Firecrawl) ═══
{news_text}

═══ LOCAL QUANTITATIVE OI & VERDICTS (Internal Bot Engine) ═══
{scan_text}"""
    else:
        log.warning("[eod_report] Firecrawl unavailable — falling back to DB scan data")
        data_source_block = f"""═══ LOCAL QUANTITATIVE OI & VERDICTS (Internal Bot Engine) ═══
{scan_text}

═══ WEB DATA (unavailable — Firecrawl returned no items) ═══
No web data available."""

    prompt = f"""Generate a CONCISE End-of-Day market report. Today: {date_display}

RULES:
- Synthesize the WEB MARKET WRAP from Firecrawl with the LOCAL QUANTITATIVE OI from the bot.
- Do NOT include internal bot trades or ledger details. Focus purely on institutional market intelligence.
- No prose paragraphs. Use pipes |, colons, and short labels.
- Abbreviate large numbers: M for millions, K for thousands (e.g. -26.4M, 56.9K).
- Each section 2-4 lines max. Total message under 1200 characters.
- Never invent numbers. If data missing, say "N/A".

{data_source_block}

═══ OUTPUT FORMAT ═══

1. index_summary: One line per index. Use the LOCAL scan spot as the authoritative close; use the web wrap only for % change and sector/market drivers. Never copy a web spot that differs from LOCAL QUANTITATIVE OI & VERDICTS:
   NIFTY Spot ±X% | DRIVER (e.g. IT gains, Energy drags) | VERDICT
   BANKNIFTY Spot ±X% | VERDICT
   SENSEX Spot ±X% | VERDICT

2. oi_derivatives_analysis: One line per index using local OI data:
   NIFTY | CEΔ-XXM PEΔ-XXM | PCR X.XX | MP±pts | 3-word signal
   BANKNIFTY | CEΔ-XXM PEΔ-XXM | PCR X.XX | MP±pts | 3-word signal
   SENSEX | CEΔ-XXM PEΔ-XXM | PCR X.XX | MP±pts | 3-word signal

3. bot_performance: State institutional market regime in 2 lines:
   Regime: DISTRIBUTION / LONG UNWINDING (or prevailing verdict) | VIX: Trend
   Capital Flows: FII/DII summary or put liquidation velocity

4. key_levels_tomorrow: One line per index:
   NIFTY Sup-XXXX Res-XXXX MP-XXXX Bias: BULLISH reason
   BANKNIFTY Sup-XXXX Res-XXXX MP-XXXX Bias: NEUTRAL reason
   Watchout: one line

5. macro_and_watchlist: 2-3 lines max. Weave in the web market wrap, FII/DII data, and key triggers:
   Wrap: Key market move driver from web in ≤12 words
   FII/DII: Institutional net flow figure or direction from web if available
   Watch: 2-3 bullet items with levels or events
"""

    report_obj: EODMarketReport | None = None
    try:
        import time
        from src.engine.llm_enrichment import _call_llm_api
        res = _call_llm_api(
            symbol="NIFTY",
            prompt=prompt,
            response_schema=EODMarketReport,
            deadline=time.time() + 90.0,
            purpose="eod_review",
        )
        if res and hasattr(res, "index_summary"):
            report_obj = res
        elif isinstance(res, dict) and "index_summary" in res:
            report_obj = EODMarketReport(**res)
    except Exception as exc:
        log.warning("[eod_report] LLM generation failed: %s", exc)

    # ── 4. Build markdown report & save ─────────────────────────────────────
    if report_obj:
        report_markdown = (
            f"## {report_obj.title}\n\n"
            f"### 📈 Index Summary\n{report_obj.index_summary}\n\n"
            f"### 📉 OI & Derivatives Analysis\n{report_obj.oi_derivatives_analysis}\n\n"
            f"### 🤖 Bot Performance & Capital Flows\n{report_obj.bot_performance}\n\n"
            f"### 🎯 Key Levels & Bias for Tomorrow\n{report_obj.key_levels_tomorrow}\n\n"
            f"### 🌐 Macro & Watchlist\n{report_obj.macro_and_watchlist}"
        )
    else:
        trade_perf = _get_today_trade_performance()
        bot_perf_fallback = _build_trade_text(trade_perf)
        report_markdown = (
            f"## Indian Market Close EOD Report — {date_display}\n\n"
            f"### 📈 Index & Quantitative OI Summary\n{scan_text}\n\n"
            f"### 🤖 Bot Performance & Capital Flows\n{bot_perf_fallback}\n\n"
            f"### 📊 Market Closing Snapshot\n{closing_bell_text}\n\n"
            f"### 📉 FII/DII Flows\n{fii_dii_text}\n\n"
            f"### 🌐 Macro News\n{news_text}"
        )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"eod_report_{today_str}.md"
    full_content = f"# Indian Market Close EOD Report — {date_display}\n\n{report_markdown}\n"
    report_path.write_text(full_content, encoding="utf-8")
    log.info("[eod_report] Markdown report saved to %s", report_path)

    # ── 5. Render SteadyAlpha HTML & PDF Document ───────────────────────────
    steadyalpha_html = _render_steadyalpha_html(date_display, summaries, fc_data, report_obj)
    pdf_path = REPORTS_DIR / f"eod_report_steadyalpha_{today_str}.pdf"
    rendered_pdf = _generate_pdf_from_html(steadyalpha_html, pdf_path)

    # ── 6. Dispatch Telegram (Text Summary + PDF Document) ──────────────────
    if report_obj:
        telegram_msg = _format_telegram_report(report_obj, date_display)
    else:
        telegram_msg = f"📊 *EOD Intelligence — {date_display}*\n\n{closing_bell_text[:1500]}"

    _send_telegram_chunked(telegram_msg)

    if rendered_pdf and rendered_pdf.exists():
        try:
            from src.alerts.telegram_dispatcher import send_document
            doc_id = send_document(
                document_path=str(rendered_pdf),
                caption=(
                    f"🏛️ *SteadyAlpha Executive Derivatives Intelligence — {date_display}*\n\n"
                    "• Full institutional memorandum with structural SCIR narrative\n"
                    "• Exhibit 1: Quantitative Option Chain & Derivatives Flow Landscape\n"
                    "• Exhibit 2: Strategic Battleground Matrix & Scenario Playbook"
                ),
                filename=f"SteadyAlpha_EOD_Intelligence_{today_str}.pdf",
            )
            log.info("[eod_report] Dispatched SteadyAlpha PDF to Telegram (doc_id=%s)", doc_id)
        except Exception as exc:
            log.warning("[eod_report] Failed to dispatch PDF document to Telegram: %s", exc)

    return report_path


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    path = generate_eod_macro_report()
    print("EOD Report generated at:", path)
