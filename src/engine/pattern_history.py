"""
Pattern History Module — A0 of ADR-007 v2: AI Role Redesign.

Provides `get_pattern_stats(symbol, verdict, pcr_regime) -> PatternStats` to measure
empirical performance over historical trades instead of relying on uncalibrated LLM confidence scalars.
Supports reading from `pattern_stats_rollup` table (nightly rollup) or computing live over closed trades.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from src.models.schema import get_conn

log = logging.getLogger(__name__)


@dataclass
class PatternStats:
    n_trades: int
    win_rate: float
    avg_pnl: float


def get_pattern_stats(
    symbol: str,
    verdict: Optional[str] = None,
    pcr_regime: Optional[str] = None,
) -> PatternStats:
    """
    Retrieve historical pattern statistics for empirical promotion evaluation.

    Looks up precomputed statistics in `pattern_stats_rollup` first. If missing,
    falls back to computing rolling 90-day statistics from closed paper/live trades.
    """
    if not symbol:
        return PatternStats(n_trades=0, win_rate=0.0, avg_pnl=0.0)

    verdict_key = str(verdict or "")
    pcr_key = str(pcr_regime or "")

    try:
        with get_conn() as conn:
            # 1. Check rollup table
            row = conn.execute(
                """
                SELECT n_trades, win_rate, avg_pnl
                FROM pattern_stats_rollup
                WHERE symbol = ? AND verdict_label = ? AND pcr_regime = ?
                """,
                (symbol, verdict_key, pcr_key),
            ).fetchone()

            if row is not None and row["n_trades"] is not None:
                return PatternStats(
                    n_trades=int(row["n_trades"]),
                    win_rate=float(row["win_rate"] or 0.0),
                    avg_pnl=float(row["avg_pnl"] or 0.0),
                )

            # 2. Check general symbol rollup if specific regime/verdict not rolled up
            if verdict_key or pcr_key:
                row_gen = conn.execute(
                    """
                    SELECT n_trades, win_rate, avg_pnl
                    FROM pattern_stats_rollup
                    WHERE symbol = ? AND verdict_label = ? AND pcr_regime = ?
                    """,
                    (symbol, verdict_key, ""),
                ).fetchone()
                if row_gen is not None and row_gen["n_trades"] is not None and int(row_gen["n_trades"]) > 0:
                    return PatternStats(
                        n_trades=int(row_gen["n_trades"]),
                        win_rate=float(row_gen["win_rate"] or 0.0),
                        avg_pnl=float(row_gen["avg_pnl"] or 0.0),
                    )
    except Exception as e:
        log.debug("Error querying pattern_stats_rollup: %s", e)

    # 3. Live calculation from closed trades across rolling 90 days
    # BUG-M14 FIX: Use a single UNION ALL query instead of two separate queries
    # for paper_trades and live_trades. This is more efficient and ensures
    # consistent filtering across both tables.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    pnls = []

    try:
        with get_conn() as conn:
            # BUG-M14: Single UNION ALL query for both paper and live trades
            if verdict_key:
                query = """
                    SELECT pnl_rupees FROM (
                        SELECT pnl_rupees, verdict_label, setup_type
                        FROM paper_trades
                        WHERE symbol = ? AND status != 'OPEN' AND opened_at >= ?
                        UNION ALL
                        SELECT pnl_rupees, verdict_label, setup_type
                        FROM live_trades
                        WHERE symbol = ? AND status != 'OPEN' AND opened_at >= ?
                    )
                    WHERE (verdict_label = ? OR setup_type = ?)
                """
                params: list[Any] = [symbol, cutoff, symbol, cutoff, verdict_key, verdict_key]
            else:
                query = """
                    SELECT pnl_rupees FROM (
                        SELECT pnl_rupees FROM paper_trades
                        WHERE symbol = ? AND status != 'OPEN' AND opened_at >= ?
                        UNION ALL
                        SELECT pnl_rupees FROM live_trades
                        WHERE symbol = ? AND status != 'OPEN' AND opened_at >= ?
                    )
                """
                params: list[Any] = [symbol, cutoff, symbol, cutoff]

            for r in conn.execute(query, params):
                if r["pnl_rupees"] is not None:
                    pnls.append(float(r["pnl_rupees"]))
    except Exception as e:
        log.error("Error computing live pattern stats for %s: %s", symbol, e)
        return PatternStats(n_trades=0, win_rate=0.0, avg_pnl=0.0)

    n_trades = len(pnls)
    if n_trades == 0:
        return PatternStats(n_trades=0, win_rate=0.0, avg_pnl=0.0)

    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / n_trades
    avg_pnl = sum(pnls) / n_trades

    return PatternStats(n_trades=n_trades, win_rate=win_rate, avg_pnl=avg_pnl)


def refresh_pattern_stats_rollup() -> int:
    """
    Nightly job to roll up historical trade performance by symbol/verdict/pcr_regime
    into `pattern_stats_rollup` table.
    Returns the number of rollup rows inserted/updated.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    groups: dict[tuple[str, str, str], list[float]] = {}

    try:
        with get_conn() as conn:
            # Aggregate paper trades
            for r in conn.execute(
                """
                SELECT symbol, verdict_label, pnl_rupees
                FROM paper_trades
                WHERE status != 'OPEN' AND opened_at >= ? AND pnl_rupees IS NOT NULL
                """,
                (cutoff,),
            ):
                sym = str(r["symbol"] or "")
                verdict = str(r["verdict_label"] or "")
                pnl = float(r["pnl_rupees"])
                if not sym:
                    continue
                # Specific group
                groups.setdefault((sym, verdict, ""), []).append(pnl)
                # Symbol general group
                groups.setdefault((sym, "", ""), []).append(pnl)

            # Aggregate live trades
            for r in conn.execute(
                """
                SELECT symbol, verdict_label, pnl_rupees
                FROM live_trades
                WHERE status != 'OPEN' AND opened_at >= ? AND pnl_rupees IS NOT NULL
                """,
                (cutoff,),
            ):
                sym = str(r["symbol"] or "")
                verdict = str(r["verdict_label"] or "")
                pnl = float(r["pnl_rupees"])
                if not sym:
                    continue
                groups.setdefault((sym, verdict, ""), []).append(pnl)
                groups.setdefault((sym, "", ""), []).append(pnl)

            now_str = datetime.now(timezone.utc).isoformat()
            count = 0
            for (sym, verdict, pcr), pnls in groups.items():
                n = len(pnls)
                if n == 0:
                    continue
                wr = sum(1 for p in pnls if p > 0) / n
                avg = sum(pnls) / n
                conn.execute(
                    """
                    INSERT OR REPLACE INTO pattern_stats_rollup
                    (symbol, verdict_label, pcr_regime, n_trades, win_rate, avg_pnl, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (sym, verdict, pcr, n, wr, avg, now_str),
                )
                count += 1

            log.info("Refreshed pattern_stats_rollup: %d rows updated", count)
            return count
    except Exception as e:
        log.error("Failed to refresh pattern_stats_rollup: %s", e)
        return 0
