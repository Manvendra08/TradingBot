"""
Autopsy Writer — ADR-007 §4 B1
Nightly trade autopsy with LLM analysis + trade_autopsies table writes.

Runs at 23:45 IST daily via job_runner. Analyzes all closed trades from today,
calls LLM for each to produce {reasons_held: bool, primary_failure: str, note: str},
and persists to trade_autopsies + docs/autopsy_YYYYMMDD.md.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytz

from src.models.schema import get_conn
from config.settings import AUTOPSY_ENABLED, AUTOPSY_TIME_IST

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")
AUTOPSY_DIR = Path("docs/autopsies")


def get_closed_trades_today() -> list[dict]:
    """Fetch all paper_trades and live_trades closed today (IST)."""
    now_ist = datetime.now(IST)
    today_start = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start.astimezone(timezone.utc).isoformat()

    trades = []
    with get_conn() as conn:
        for table in ("paper_trades", "live_trades"):
            try:
                rows = conn.execute(
                    f"""
                    SELECT id, symbol, opened_at, closed_at, entry_premium, exit_premium,
                           pnl_rupees, status, verdict_label, setup_type, reason,
                           sl_premium, target_premium
                    FROM {table}
                    WHERE status IN ('CLOSED_TARGET', 'CLOSED_SL', 'SL_HIT', 'CLOSED_MANUAL',
                                     'CLOSED_REVERSAL', 'CLOSED_EARLY', 'AI_CLOSE_EARLY')
                      AND closed_at >= ?
                    ORDER BY closed_at DESC
                    """,
                    (today_start_utc,),
                ).fetchall()
                for r in rows:
                    r_dict = dict(r)
                    r_dict["source_table"] = table
                    trades.append(r_dict)
            except Exception as e:
                log.error("Failed to fetch closed trades from %s: %s", table, e)
    return trades


def get_shadow_decisions_for_trade(trade_id: int, source_table: str) -> dict | None:
    """Fetch shadow_decisions row matching a closed trade."""
    with get_conn() as conn:
        try:
            row = conn.execute(
                """
                SELECT id, ts, engine, rule_action, rule_block_reason,
                       old_ai_would_boost, ai_bias, ai_conf, ai_veto_flag, ai_veto_reason,
                       empirical_n, empirical_winrate, empirical_avg_pnl,
                       final_action, setup_type
                FROM shadow_decisions
                WHERE symbol = (SELECT symbol FROM {table} WHERE id = ?)
                  AND ts >= (SELECT opened_at FROM {table} WHERE id = ?)
                  AND ts <= (SELECT closed_at FROM {table} WHERE id = ?)
                ORDER BY ts DESC LIMIT 1
                """.format(table=source_table),
                (trade_id, trade_id, trade_id),
            ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            log.debug("Failed to fetch shadow_decisions for trade %d: %s", trade_id, e)
            return None


def _call_llm_autopsy(trade: dict, shadow: dict | None) -> dict:
    """Call LLM for trade autopsy analysis."""
    try:
        from src.engine.llm_enrichment import _call_llm_api, LLMTradeVerdict
    except ImportError:
        return {"reasons_held": None, "primary_failure": "LLM unavailable", "note": ""}

    prompt = f"""Analyze this closed trade and determine if the decision logic held up.

TRADE:
- Symbol: {trade.get('symbol')}
- Verdict: {trade.get('verdict_label')}
- Setup Type: {trade.get('setup_type')}
- Entry: {trade.get('entry_premium')}
- Exit: {trade.get('exit_premium')}
- P&L: {trade.get('pnl_rupees')}
- Status: {trade.get('status')}
- Close Reason: {trade.get('reason')}

SHADOW DECISION (if any):
{json.dumps(shadow, indent=2, default=str) if shadow else "None"}

Respond in JSON:
{{"reasons_held": bool, "primary_failure": "string or null", "note": "3 sentences max"}}
"""
    try:
        response = _call_llm_api(prompt, model_override="gemini-2.0-flash")
        if response:
            text = getattr(response, "text", "") or str(response)
            try:
                parsed = json.loads(text.strip().strip("```json").strip("```"))
                return {
                    "reasons_held": parsed.get("reasons_held"),
                    "primary_failure": parsed.get("primary_failure"),
                    "note": parsed.get("note", "")[:500],
                }
            except json.JSONDecodeError:
                return {"reasons_held": None, "primary_failure": "JSON parse failed", "note": text[:500]}
    except Exception as e:
        log.warning("LLM autopsy call failed: %s", e)
    return {"reasons_held": None, "primary_failure": str(e)[:100], "note": ""}


def write_autopsy_report(date_str: str, autopsies: list[dict]) -> Path:
    """Write daily autopsy markdown file."""
    AUTOPSY_DIR.mkdir(parents=True, exist_ok=True)
    path = AUTOPSY_DIR / f"autopsy_{date_str}.md"

    lines = [
        f"# Trade Autopsy Report — {date_str}",
        "",
        f"**Total Closed Trades:** {len(autopsies)}",
        "",
    ]

    wins = [a for a in autopsies if a.get("pnl_rupees", 0) > 0]
    losses = [a for a in autopsies if a.get("pnl_rupees", 0) <= 0]
    win_rate = len(wins) / len(autopsies) * 100 if autopsies else 0

    lines.append(f"**Win Rate:** {win_rate:.1f}% ({len(wins)} wins, {len(losses)} losses)")
    lines.append("")

    for a in autopsies:
        pnl = a.get("pnl_rupees", 0)
        pnl_str = f"+{pnl:.0f}" if pnl > 0 else f"{pnl:.0f}"
        lines.append(f"## {a.get('symbol')} — {a.get('setup_type', 'N/A')}")
        lines.append(f"- **P&L:** {pnl_str}")
        lines.append(f"- **Status:** {a.get('status')}")
        lines.append(f"- **Reasons Held:** {a.get('reasons_held', 'N/A')}")
        if a.get("primary_failure"):
            lines.append(f"- **Primary Failure:** {a.get('primary_failure')}")
        if a.get("note"):
            lines.append(f"- **Note:** {a.get('note')}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote autopsy report: %s", path)
    return path


def run_nightly_autopsy() -> int:
    """Main entry point: run autopsy for all closed trades today."""
    if not AUTOPSY_ENABLED:
        log.info("Autopsy disabled (AUTOPSY_ENABLED=false)")
        return 0

    closed_trades = get_closed_trades_today()
    if not closed_trades:
        log.info("No closed trades today to autopsy")
        return 0

    log.info("Running autopsy for %d closed trades", len(closed_trades))

    autopsies = []
    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(IST).strftime("%Y%m%d")

    for trade in closed_trades:
        shadow = get_shadow_decisions_for_trade(trade["id"], trade["source_table"])
        analysis = _call_llm_autopsy(trade, shadow)

        autopsy_record = {
            "trade_id": trade["id"],
            "ts": now_iso,
            "symbol": trade.get("symbol"),
            "pnl_rupees": trade.get("pnl_rupees"),
            "status": trade.get("status"),
            "setup_type": trade.get("setup_type"),
            "reasons_held": analysis.get("reasons_held"),
            "primary_failure": analysis.get("primary_failure"),
            "note": analysis.get("note"),
        }
        autopsies.append(autopsy_record)

        try:
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO trade_autopsies (trade_id, ts, reasons_held, primary_failure, note, llm_model)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade["id"],
                        now_iso,
                        1 if analysis.get("reasons_held") else 0,
                        analysis.get("primary_failure"),
                        analysis.get("note"),
                        "gemini-2.0-flash",
                    ),
                )
        except Exception as e:
            log.error("Failed to insert autopsy for trade %d: %s", trade["id"], e)

    report_path = write_autopsy_report(today_str, autopsies)
    log.info("Autopsy complete: %d trades processed, report at %s", len(autopsies), report_path)
    return len(autopsies)


def run_weekly_rollup() -> dict:
    """Weekly rollup: win-rate by setup_type, empirical vs shadow, veto precision."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    rollup = {
        "by_setup_type": {},
        "empirical_boost": {"wins": 0, "losses": 0},
        "shadow_ai_boost": {"wins": 0, "losses": 0},
        "veto_flag": {"flagged": 0, "correct": 0},
    }

    with get_conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT t.setup_type, t.pnl_rupees, s.ai_veto_flag, s.old_ai_would_boost
                FROM paper_trades t
                LEFT JOIN shadow_decisions s ON t.symbol = s.symbol
                    AND s.ts >= ? AND s.ts <= t.closed_at
                WHERE t.closed_at >= ? AND t.pnl_rupees IS NOT NULL
                """,
                (cutoff, cutoff),
            ).fetchall()

            for r in rows:
                setup = r["setup_type"] or "UNKNOWN"
                pnl = float(r["pnl_rupees"] or 0)
                if setup not in rollup["by_setup_type"]:
                    rollup["by_setup_type"][setup] = {"wins": 0, "losses": 0}
                if pnl > 0:
                    rollup["by_setup_type"][setup]["wins"] += 1
                else:
                    rollup["by_setup_type"][setup]["losses"] += 1

                if setup == "EMPIRICAL_PROMOTED":
                    if pnl > 0:
                        rollup["empirical_boost"]["wins"] += 1
                    else:
                        rollup["empirical_boost"]["losses"] += 1

                if r["old_ai_would_boost"]:
                    if pnl > 0:
                        rollup["shadow_ai_boost"]["wins"] += 1
                    else:
                        rollup["shadow_ai_boost"]["losses"] += 1

                if r["ai_veto_flag"]:
                    rollup["veto_flag"]["flagged"] += 1
                    if pnl < 0:
                        rollup["veto_flag"]["correct"] += 1
        except Exception as e:
            log.error("Weekly rollup query failed: %s", e)

    return rollup


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_nightly_autopsy()
