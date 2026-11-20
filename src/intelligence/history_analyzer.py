"""
Analyzes closed paper trades to discover winning/losing patterns.
No ML required - pure statistical aggregation.

v2.0 FIXES:
- IST timezone conversion for session bucketing
- Minimum 10 trades per pattern (was 3)
- Confidence bands widened to +/-20 (was +/-10)

v3.0 FIXES:
- Zero-padded numeric compare for hour matching (was string compare)
- MIN_PATTERN_TRADES as single source of truth
- Module-level singleton + 5-min in-memory cache
- Single CASE WHEN query for session bucketing
- Atomic DELETE+INSERT for DB cache persistence
"""

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import time as _time

log = logging.getLogger(__name__)

# IST offset constant (matches risk_engine.py)
IST_OFFSET = timedelta(hours=5, minutes=30)


@dataclass
class PatternInsight:
    """A discovered pattern with actionable recommendation."""

    pattern_name: str  # e.g., "BANKNIFTY Long Buildup"
    sample_size: int  # number of trades
    win_rate: float  # 0.0-1.0
    avg_pnl: float  # average PnL in rupees
    best_time: str  # e.g., "09:30-11:00"
    best_conditions: dict  # e.g., {"min_confidence": 75, "pcr_range": [1.1, 1.5]}
    recommendation: str  # actionable advice
    profit_factor: float = 0.0
    net_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    dimension: str = "setup"  # "setup", "regime", "confidence", "session"


# -- Singleton + Cache (v2.2 FIX) ------------------------------------------------------------------------
# Previously, every API call and pipeline cycle instantiated a new
# TradeHistoryAnalyzer and ran 5 full SQL aggregation queries. With 30s
# dashboard polling, that's ~2,880 unnecessary query sets per day.
#
# Fix: Module-level singleton + 5-minute in-memory cache.
# Cache is invalidated when a trade closes (via invalidate_cache()).
# The ai_pattern_insights DB table is also populated for cross-process
# cache sharing and persistence across restarts.

_analyzer: "TradeHistoryAnalyzer | None" = None
_analyzer_lock = threading.Lock()


def get_analyzer() -> "TradeHistoryAnalyzer":
    """
    Return the module-level TradeHistoryAnalyzer singleton.

    v3.0 FIX: Removed the `min_trades` parameter. As a singleton it was only
    honored on the first call and silently ignored thereafter -- a footgun.
    The single threshold now lives in MIN_PATTERN_TRADES.
    """
    global _analyzer
    if _analyzer is None:
        with _analyzer_lock:
            if _analyzer is None:  # double-checked locking
                _analyzer = TradeHistoryAnalyzer()
    return _analyzer


UNIFIED_TRADES_SQL = """
    (
        SELECT symbol, verdict_label, pnl_rupees, confidence_score, opened_at, closed_at, status, regime
        FROM paper_trades
        UNION ALL
        SELECT m.symbol,
               COALESCE(s.verdict_label, m.strategy_type, m.structure) as verdict_label,
               m.total_pnl as pnl_rupees,
               COALESCE(m.confidence_score, s.confidence, 50) as confidence_score,
               m.opened_at, m.closed_at, m.status,
               COALESCE(s.market_regime, 'NEUTRAL') as regime
        FROM multi_leg_trades m
        LEFT JOIN scan_summaries s ON m.digest_id = s.digest_id
        WHERE COALESCE(m.is_pnl_corrupted, 0) = 0
        UNION ALL
        SELECT symbol, verdict_label, pnl_rupees, confidence_score, opened_at, closed_at, status, regime
        FROM live_trades
    )
"""


class TradeHistoryAnalyzer:
    """Analyzes closed trades to find patterns."""

    MIN_PATTERN_TRADES = 5  # Statistical baseline across active trades
    CACHE_TTL_SECONDS = 300  # v2.2: 5-minute cache TTL

    def __init__(self):
        # v3.0: min_trades param removed -- see MIN_PATTERN_TRADES.
        # v2.2: In-memory pattern cache
        self._patterns_cache: list[PatternInsight] | None = None
        self._patterns_cache_ts: float = 0.0

    def invalidate_cache(self):
        """v2.2: Call after a trade closes to force re-computation."""
        self._patterns_cache = None
        self._patterns_cache_ts = 0.0

    def get_cached_patterns(self, symbol: str = None) -> list[PatternInsight]:
        """
        v2.2: Return cached patterns if fresh (< TTL), else recompute.
        Persists to ai_pattern_insights table for cross-process sharing.
        Reads from DB table if in-memory cache is empty before computing.
        """
        now = _time()
        need_refresh = (
            self._patterns_cache is None
            or (now - self._patterns_cache_ts) >= self.CACHE_TTL_SECONDS
        )

        if need_refresh:
            # Load from DB first if in-memory cache is empty (e.g. on restart)
            if self._patterns_cache is None:
                db_patterns = self._load_patterns_from_db()
                if db_patterns:
                    self._patterns_cache = db_patterns
                    self._patterns_cache_ts = now

            # If still empty or expired, recompute
            if self._patterns_cache is None or (now - self._patterns_cache_ts) >= self.CACHE_TTL_SECONDS:
                patterns = self.analyze_all_patterns()
                self._patterns_cache = patterns
                self._patterns_cache_ts = now
                # Persist to DB cache table (best-effort -- non-blocking)
                self._persist_patterns_to_db(patterns)

        patterns = self._patterns_cache or []
        if symbol:
            symbol_upper = symbol.upper().strip()
            patterns = [p for p in patterns if p.pattern_name.upper().startswith(symbol_upper)]
        return patterns

    def _load_patterns_from_db(self, symbol: str = None) -> list[PatternInsight]:
        """Read patterns from ai_pattern_insights table for cache population."""
        try:
            from src.models.schema import get_conn

            with get_conn() as conn:
                if symbol:
                    rows = conn.execute(
                        """
                        SELECT pattern_name, sample_size, win_rate, avg_pnl,
                               best_conditions, recommendation
                        FROM ai_pattern_insights
                        WHERE pattern_name LIKE ?
                        ORDER BY win_rate * sample_size DESC
                    """,
                        (f"{symbol} %",),
                    ).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT pattern_name, sample_size, win_rate, avg_pnl,
                               best_conditions, recommendation
                        FROM ai_pattern_insights
                        ORDER BY win_rate * sample_size DESC
                    """).fetchall()

                patterns = []
                for row in rows:
                    conds = json.loads(row["best_conditions"]) if row["best_conditions"] else {}
                    patterns.append(
                        PatternInsight(
                            pattern_name=row["pattern_name"],
                            sample_size=row["sample_size"],
                            win_rate=row["win_rate"],
                            avg_pnl=row["avg_pnl"],
                            best_time=conds.get("best_time", "All day"),
                            best_conditions=conds,
                            recommendation=row["recommendation"],
                            profit_factor=conds.get("profit_factor", 0.0),
                            net_pnl=conds.get("net_pnl", 0.0),
                            avg_win=conds.get("avg_win", 0.0),
                            avg_loss=conds.get("avg_loss", 0.0),
                            dimension=conds.get("dimension", "setup"),
                        )
                    )
                return patterns
        except Exception as e:
            log.warning(f"Failed to load patterns from DB cache: {e}")
            return []

    def _persist_patterns_to_db(self, patterns: list[PatternInsight]):
        """Write patterns to ai_pattern_insights table for cross-process sharing."""
        try:
            from src.models.schema import get_conn

            with get_conn() as conn:
                conn.execute("DELETE FROM ai_pattern_insights")
                for p in patterns:
                    conds = dict(p.best_conditions or {})
                    conds.update({
                        "profit_factor": p.profit_factor,
                        "net_pnl": p.net_pnl,
                        "avg_win": p.avg_win,
                        "avg_loss": p.avg_loss,
                        "dimension": p.dimension,
                        "best_time": p.best_time,
                    })
                    conn.execute(
                        """
                        INSERT INTO ai_pattern_insights
                        (pattern_name, pattern_type, sample_size, win_rate, avg_pnl,
                         best_conditions, recommendation, discovered_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                        (
                            p.pattern_name,
                            p.dimension,
                            p.sample_size,
                            p.win_rate,
                            p.avg_pnl,
                            json.dumps(conds),
                            p.recommendation,
                        ),
                    )
        except Exception as e:
            log.warning(f"Failed to persist patterns to DB cache: {e}")

    def analyze_all_patterns(self, symbol: str = None) -> list[PatternInsight]:
        """Discover patterns across multiple dimensions."""
        patterns = []

        # 1. By Symbol + Verdict
        patterns.extend(self._analyze_by_symbol_verdict(symbol))

        # 2. By Time of Day (IST-corrected)
        patterns.extend(self._analyze_by_session(symbol))

        # 3. By Confidence Range
        patterns.extend(self._analyze_by_confidence(symbol))

        # 4. By Setup Type
        patterns.extend(self._analyze_by_setup_type(symbol))

        # 5. By Market Regime
        patterns.extend(self._analyze_by_regime(symbol))

        # Sort patterns by Net PnL (expectancy * volume) descending
        return sorted(patterns, key=lambda p: (p.net_pnl, p.profit_factor), reverse=True)

    def _analyze_by_symbol_verdict(self, symbol: str = None) -> list[PatternInsight]:
        """Analyze performance by symbol and verdict label."""
        from src.models.schema import get_conn

        filter_clause = "status != 'OPEN' AND closed_at IS NOT NULL AND verdict_label NOT IN ('Low Conviction', 'NEUTRAL', 'UNKNOWN', 'N/A', 'None', '')"
        with get_conn() as conn:
            if symbol:
                rows = conn.execute(f"""
                    SELECT
                        symbol,
                        verdict_label,
                        COUNT(*) as count,
                        AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_rupees) as avg_pnl,
                        SUM(pnl_rupees) as net_pnl,
                        SUM(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as gross_profit,
                        ABS(SUM(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE 0 END)) as gross_loss,
                        COALESCE(AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE NULL END), 0) as avg_win,
                        COALESCE(ABS(AVG(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE NULL END)), 0) as avg_loss,
                        AVG(confidence_score) as avg_confidence
                    FROM {UNIFIED_TRADES_SQL}
                    WHERE {filter_clause}
                      AND symbol = ?
                    GROUP BY symbol, verdict_label
                    HAVING COUNT(*) >= ?
                """, (symbol, self.MIN_PATTERN_TRADES)).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT
                        symbol,
                        verdict_label,
                        COUNT(*) as count,
                        AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_rupees) as avg_pnl,
                        SUM(pnl_rupees) as net_pnl,
                        SUM(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as gross_profit,
                        ABS(SUM(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE 0 END)) as gross_loss,
                        COALESCE(AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE NULL END), 0) as avg_win,
                        COALESCE(ABS(AVG(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE NULL END)), 0) as avg_loss,
                        AVG(confidence_score) as avg_confidence
                    FROM {UNIFIED_TRADES_SQL}
                    WHERE {filter_clause}
                    GROUP BY symbol, verdict_label
                    HAVING COUNT(*) >= ?
                """, (self.MIN_PATTERN_TRADES,)).fetchall()

        insights = []
        for row in rows:
            gross_p = float(row["gross_profit"] or 0.0)
            gross_l = float(row["gross_loss"] or 0.0)
            pf = round(gross_p / gross_l, 2) if gross_l > 0 else (99.0 if gross_p > 0 else 1.0)
            net_p = float(row["net_pnl"] or 0.0)
            avg_w = float(row["avg_win"] or 0.0)
            avg_l = float(row["avg_loss"] or 0.0)

            rec = self._generate_recommendation(
                win_rate=row["win_rate"],
                avg_pnl=row["avg_pnl"],
                count=row["count"],
                profit_factor=pf,
                net_pnl=net_p,
                avg_win=avg_w,
                avg_loss=avg_l,
            )
            insights.append(
                PatternInsight(
                    pattern_name=f"{row['symbol']} {row['verdict_label']}",
                    sample_size=row["count"],
                    win_rate=row["win_rate"],
                    avg_pnl=row["avg_pnl"],
                    best_time="All day",
                    best_conditions={"avg_confidence": row["avg_confidence"]},
                    recommendation=rec,
                    profit_factor=pf,
                    net_pnl=net_p,
                    avg_win=avg_w,
                    avg_loss=avg_l,
                    dimension="setup",
                )
            )
        return insights

    def _analyze_by_session(self, symbol: str = None) -> list[PatternInsight]:
        """
        Analyze performance by time of day (IST sessions).
        Classifies by Expectancy and Profit Factor, eliminating win-rate myopia.
        """
        from src.models.schema import get_conn

        quant_cols = """
            COUNT(*) as count,
            AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
            AVG(pnl_rupees) as avg_pnl,
            SUM(pnl_rupees) as net_pnl,
            SUM(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as gross_profit,
            ABS(SUM(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE 0 END)) as gross_loss,
            COALESCE(AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE NULL END), 0) as avg_win,
            COALESCE(ABS(AVG(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE NULL END)), 0) as avg_loss
        """

        if symbol:
            query = f"""
                SELECT
                    session,
                    {quant_cols}
                FROM (
                    SELECT
                        pnl_rupees,
                        CASE
                            WHEN total_min >= 555  AND total_min < 630  THEN 'Market Open (09:15-10:30)'
                            WHEN total_min >= 630  AND total_min < 720  THEN 'Mid-Morning (10:30-12:00)'
                            WHEN total_min >= 720  AND total_min < 840  THEN 'Post-Lunch (12:00-14:00)'
                            WHEN total_min >= 840  AND total_min < 900  THEN 'Afternoon (14:00-15:00)'
                            WHEN total_min >= 900  AND total_min < 940  THEN 'Closing (15:00-15:40)'
                            WHEN total_min >= 940  AND total_min < 1410 THEN 'Evening/MCX (15:40-23:30)'
                            ELSE NULL
                        END as session
                    FROM (
                        SELECT
                            pnl_rupees,
                            CAST(strftime('%H', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) * 60
                            + CAST(strftime('%M', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) as total_min
                        FROM {UNIFIED_TRADES_SQL}
                        WHERE status != 'OPEN'
                          AND closed_at IS NOT NULL
                          AND symbol = ?
                    )
                )
                WHERE session IS NOT NULL
                GROUP BY session
                HAVING COUNT(*) >= ?
            """
        else:
            query = f"""
                SELECT
                    session,
                    {quant_cols}
                FROM (
                    SELECT
                        pnl_rupees,
                        CASE
                            WHEN total_min >= 555  AND total_min < 630  THEN 'Market Open (09:15-10:30)'
                            WHEN total_min >= 630  AND total_min < 720  THEN 'Mid-Morning (10:30-12:00)'
                            WHEN total_min >= 720  AND total_min < 840  THEN 'Post-Lunch (12:00-14:00)'
                            WHEN total_min >= 840  AND total_min < 900  THEN 'Afternoon (14:00-15:00)'
                            WHEN total_min >= 900  AND total_min < 940  THEN 'Closing (15:00-15:40)'
                            WHEN total_min >= 940  AND total_min < 1410 THEN 'Evening/MCX (15:40-23:30)'
                            ELSE NULL
                        END as session
                    FROM (
                        SELECT
                            pnl_rupees,
                            CAST(strftime('%H', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) * 60
                            + CAST(strftime('%M', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) as total_min
                        FROM {UNIFIED_TRADES_SQL}
                        WHERE status != 'OPEN'
                          AND closed_at IS NOT NULL
                    )
                )
                WHERE session IS NOT NULL
                GROUP BY session
                HAVING COUNT(*) >= ?
            """

        SESSION_ORDER = [
            "Market Open (09:15-10:30)",
            "Mid-Morning (10:30-12:00)",
            "Post-Lunch (12:00-14:00)",
            "Afternoon (14:00-15:00)",
            "Closing (15:00-15:40)",
            "Evening/MCX (15:40-23:30)",
        ]

        insights = []
        with get_conn() as conn:
            if symbol:
                rows = conn.execute(query, (symbol, self.MIN_PATTERN_TRADES)).fetchall()
            else:
                rows = conn.execute(query, (self.MIN_PATTERN_TRADES,)).fetchall()

        row_by_session = {row["session"]: row for row in rows}

        for session_name in SESSION_ORDER:
            row = row_by_session.get(session_name)
            if row:
                gross_p = float(row["gross_profit"] or 0.0)
                gross_l = float(row["gross_loss"] or 0.0)
                pf = round(gross_p / gross_l, 2) if gross_l > 0 else (99.0 if gross_p > 0 else 1.0)
                net_p = float(row["net_pnl"] or 0.0)
                avg_w = float(row["avg_win"] or 0.0)
                avg_l = float(row["avg_loss"] or 0.0)

                rec = self._generate_recommendation(
                    win_rate=row["win_rate"],
                    avg_pnl=row["avg_pnl"],
                    count=row["count"],
                    profit_factor=pf,
                    net_pnl=net_p,
                    avg_win=avg_w,
                    avg_loss=avg_l,
                )
                insights.append(
                    PatternInsight(
                        pattern_name=f"Session: {session_name}",
                        sample_size=row["count"],
                        win_rate=row["win_rate"],
                        avg_pnl=row["avg_pnl"],
                        best_time="",  # Avoid redundant self-reference in UI
                        best_conditions={},
                        recommendation=rec,
                        profit_factor=pf,
                        net_pnl=net_p,
                        avg_win=avg_w,
                        avg_loss=avg_l,
                        dimension="session",
                    )
                )
        return insights

    def _analyze_by_confidence(self, symbol: str = None) -> list[PatternInsight]:
        """Analyze performance by confidence score range."""
        from src.models.schema import get_conn

        confidence_ranges = [
            ("Low Confidence (60-70)", 60, 70),
            ("Medium Confidence (70-80)", 70, 80),
            ("High Confidence (80-90)", 80, 90),
            ("Very High Confidence (90+)", 90, 100),
        ]

        insights = []
        quant_cols = """
            COUNT(*) as count,
            AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
            AVG(pnl_rupees) as avg_pnl,
            SUM(pnl_rupees) as net_pnl,
            SUM(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as gross_profit,
            ABS(SUM(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE 0 END)) as gross_loss,
            COALESCE(AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE NULL END), 0) as avg_win,
            COALESCE(ABS(AVG(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE NULL END)), 0) as avg_loss
        """

        with get_conn() as conn:
            for range_name, min_conf, max_conf in confidence_ranges:
                if symbol:
                    row = conn.execute(
                        f"""
                        SELECT {quant_cols}
                        FROM {UNIFIED_TRADES_SQL}
                        WHERE status != 'OPEN'
                          AND closed_at IS NOT NULL
                          AND confidence_score >= ? AND confidence_score < ?
                          AND symbol = ?
                    """,
                        (min_conf, max_conf, symbol),
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"""
                        SELECT {quant_cols}
                        FROM {UNIFIED_TRADES_SQL}
                        WHERE status != 'OPEN'
                          AND closed_at IS NOT NULL
                          AND confidence_score >= ? AND confidence_score < ?
                    """,
                        (min_conf, max_conf),
                    ).fetchone()

                if row and row["count"] >= self.MIN_PATTERN_TRADES:
                    gross_p = float(row["gross_profit"] or 0.0)
                    gross_l = float(row["gross_loss"] or 0.0)
                    pf = round(gross_p / gross_l, 2) if gross_l > 0 else (99.0 if gross_p > 0 else 1.0)
                    net_p = float(row["net_pnl"] or 0.0)
                    avg_w = float(row["avg_win"] or 0.0)
                    avg_l = float(row["avg_loss"] or 0.0)

                    rec = self._generate_recommendation(
                        win_rate=row["win_rate"],
                        avg_pnl=row["avg_pnl"],
                        count=row["count"],
                        profit_factor=pf,
                        net_pnl=net_p,
                        avg_win=avg_w,
                        avg_loss=avg_l,
                    )
                    insights.append(
                        PatternInsight(
                            pattern_name=f"Confidence: {range_name}",
                            sample_size=row["count"],
                            win_rate=row["win_rate"],
                            avg_pnl=row["avg_pnl"],
                            best_time="",
                            best_conditions={"confidence_range": [min_conf, max_conf]},
                            recommendation=rec,
                            profit_factor=pf,
                            net_pnl=net_p,
                            avg_win=avg_w,
                            avg_loss=avg_l,
                            dimension="confidence",
                        )
                    )
        return insights

    def _analyze_by_setup_type(self, symbol: str = None) -> list[PatternInsight]:
        """Analyze performance by verdict/setup type."""
        from src.models.schema import get_conn

        filter_clause = "status != 'OPEN' AND closed_at IS NOT NULL AND verdict_label NOT IN ('Low Conviction', 'NEUTRAL', 'UNKNOWN', 'N/A', 'None', '')"
        quant_cols = """
            verdict_label,
            COUNT(*) as count,
            AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
            AVG(pnl_rupees) as avg_pnl,
            SUM(pnl_rupees) as net_pnl,
            SUM(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as gross_profit,
            ABS(SUM(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE 0 END)) as gross_loss,
            COALESCE(AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE NULL END), 0) as avg_win,
            COALESCE(ABS(AVG(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE NULL END)), 0) as avg_loss
        """

        with get_conn() as conn:
            if symbol:
                rows = conn.execute(
                    f"""
                    SELECT {quant_cols}
                    FROM {UNIFIED_TRADES_SQL}
                    WHERE {filter_clause}
                      AND symbol = ?
                    GROUP BY verdict_label
                    HAVING COUNT(*) >= ?
                """,
                    (symbol, self.MIN_PATTERN_TRADES),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT {quant_cols}
                    FROM {UNIFIED_TRADES_SQL}
                    WHERE {filter_clause}
                    GROUP BY verdict_label
                    HAVING COUNT(*) >= ?
                """,
                    (self.MIN_PATTERN_TRADES,),
                ).fetchall()

        insights = []
        for row in rows:
            gross_p = float(row["gross_profit"] or 0.0)
            gross_l = float(row["gross_loss"] or 0.0)
            pf = round(gross_p / gross_l, 2) if gross_l > 0 else (99.0 if gross_p > 0 else 1.0)
            net_p = float(row["net_pnl"] or 0.0)
            avg_w = float(row["avg_win"] or 0.0)
            avg_l = float(row["avg_loss"] or 0.0)

            rec = self._generate_recommendation(
                win_rate=row["win_rate"],
                avg_pnl=row["avg_pnl"],
                count=row["count"],
                profit_factor=pf,
                net_pnl=net_p,
                avg_win=avg_w,
                avg_loss=avg_l,
            )
            insights.append(
                PatternInsight(
                    pattern_name=f"Setup: {row['verdict_label']}",
                    sample_size=row["count"],
                    win_rate=row["win_rate"],
                    avg_pnl=row["avg_pnl"],
                    best_time="All day",
                    best_conditions={},
                    recommendation=rec,
                    profit_factor=pf,
                    net_pnl=net_p,
                    avg_win=avg_w,
                    avg_loss=avg_l,
                    dimension="setup",
                )
            )
        return insights

    def _analyze_by_regime(self, symbol: str = None) -> list[PatternInsight]:
        """Analyze performance by market regime."""
        from src.models.schema import get_conn

        quant_cols = """
            regime,
            COUNT(*) as count,
            AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
            AVG(pnl_rupees) as avg_pnl,
            SUM(pnl_rupees) as net_pnl,
            SUM(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as gross_profit,
            ABS(SUM(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE 0 END)) as gross_loss,
            COALESCE(AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE NULL END), 0) as avg_win,
            COALESCE(ABS(AVG(CASE WHEN pnl_rupees < 0 THEN pnl_rupees ELSE NULL END)), 0) as avg_loss
        """

        with get_conn() as conn:
            if symbol:
                rows = conn.execute(
                    f"""
                    SELECT {quant_cols}
                    FROM {UNIFIED_TRADES_SQL}
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                      AND regime IS NOT NULL
                      AND symbol = ?
                    GROUP BY regime
                    HAVING COUNT(*) >= ?
                """,
                    (symbol, self.MIN_PATTERN_TRADES),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT {quant_cols}
                    FROM {UNIFIED_TRADES_SQL}
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                      AND regime IS NOT NULL
                    GROUP BY regime
                    HAVING COUNT(*) >= ?
                """,
                    (self.MIN_PATTERN_TRADES,),
                ).fetchall()

        insights = []
        for row in rows:
            gross_p = float(row["gross_profit"] or 0.0)
            gross_l = float(row["gross_loss"] or 0.0)
            pf = round(gross_p / gross_l, 2) if gross_l > 0 else (99.0 if gross_p > 0 else 1.0)
            net_p = float(row["net_pnl"] or 0.0)
            avg_w = float(row["avg_win"] or 0.0)
            avg_l = float(row["avg_loss"] or 0.0)

            rec = self._generate_recommendation(
                win_rate=row["win_rate"],
                avg_pnl=row["avg_pnl"],
                count=row["count"],
                profit_factor=pf,
                net_pnl=net_p,
                avg_win=avg_w,
                avg_loss=avg_l,
            )
            insights.append(
                PatternInsight(
                    pattern_name=f"Regime: {row['regime']}",
                    sample_size=row["count"],
                    win_rate=row["win_rate"],
                    avg_pnl=row["avg_pnl"],
                    best_time="All day",
                    best_conditions={},
                    recommendation=rec,
                    profit_factor=pf,
                    net_pnl=net_p,
                    avg_win=avg_w,
                    avg_loss=avg_l,
                    dimension="regime",
                )
            )
        return insights

    def _generate_recommendation(
        self,
        win_rate: float,
        avg_pnl: float,
        count: int,
        profit_factor: float = 0.0,
        net_pnl: float = 0.0,
        avg_win: float = 0.0,
        avg_loss: float = 0.0,
    ) -> str:
        """Generate decision-grade quant recommendation based on Expectancy & Profit Factor."""
        if count < self.MIN_PATTERN_TRADES:
            return f"[!] Insufficient data ({count}/{self.MIN_PATTERN_TRADES} trades needed)"

        pnl_str = f"+₹{net_pnl:,.0f}" if net_pnl >= 0 else f"-₹{abs(net_pnl):,.0f}"
        metrics = f"Net: {pnl_str} · PF: {profit_factor:.2f} · W/L: ₹{avg_win:,.0f}/₹{avg_loss:,.0f}"

        # Decision-grade quant classification:
        # Strong Edge: Positive Expectancy (avg_pnl >= 1000 and PF >= 1.4) or high-asymmetric payoff (PF >= 2.0)
        if (avg_pnl >= 1000 and profit_factor >= 1.4) or (profit_factor >= 2.0 and avg_pnl > 300):
            return f"[GREEN] STRONG EDGE · {metrics}"
        elif avg_pnl > 0 and profit_factor >= 1.15:
            return f"[YELLOW] MODERATE EDGE · {metrics}"
        elif abs(avg_pnl) <= 500 or (0.9 <= profit_factor < 1.15):
            return f"[WHITE] MARGINAL EDGE · {metrics}"
        elif avg_pnl < 0 or profit_factor < 0.9:
            return f"[RED] NEGATIVE EDGE · {metrics}"
        else:
            return f"[WHITE] NEUTRAL · {metrics}"

    def get_trade_dna_match(self, current_trade_context: dict) -> dict:
        """
        Find similar historical trades and show success probability.

        v2.0 FIXES:
        - Confidence band widened to +/-20 (was +/-10) for early-stage bots
        - IST timezone conversion for hour matching
        - Uses opened_at from trade record, not datetime.now()

        v3.0 FIX: Zero-padded numeric compare for hour matching
        """
        from src.models.schema import get_conn

        symbol = current_trade_context.get("symbol")
        verdict = current_trade_context.get("verdict_label")
        confidence = current_trade_context.get("confidence", 0)
        # v2.0 FIX: Use IST hour from context, not datetime.now()
        ist_hour = current_trade_context.get(
            "ist_hour", (datetime.now(timezone.utc) + IST_OFFSET).hour
        )

        with get_conn() as conn:
            # Flexible verdict matching expression
            v_match = "(UPPER(REPLACE(verdict_label, ' ', '_')) = UPPER(REPLACE(?, ' ', '_')) OR UPPER(verdict_label) = UPPER(?))"

            similar_trades = None
            note = ""

            if verdict:
                # Tier 1: symbol + verdict + confidence (+/-20) + time (+/-1h)
                similar_trades = conn.execute(
                    f"""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                        AVG(pnl_rupees) as avg_pnl,
                        AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as avg_win,
                        AVG(CASE WHEN pnl_rupees <= 0 THEN pnl_rupees ELSE 0 END) as avg_loss
                    FROM {UNIFIED_TRADES_SQL}
                    WHERE status != 'OPEN'
                      AND symbol = ?
                      AND {v_match}
                      AND confidence_score BETWEEN ? AND ?
                      AND CAST(strftime('%H', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER)
                          BETWEEN ? AND ?
                """,
                    (
                        symbol, verdict, verdict,
                        max(0, confidence - 20), min(100, confidence + 20),
                        max(0, ist_hour - 1), min(23, ist_hour + 1),
                    ),
                ).fetchone()

                if similar_trades and similar_trades["total"] > 0:
                    note = f"Based on {similar_trades['total']} similar trades (+/-20 confidence band, +/-1h time)"

                # Tier 2 fallback: symbol + verdict + confidence (+/-25, any hour)
                if not similar_trades or similar_trades["total"] == 0:
                    similar_trades = conn.execute(
                        f"""
                        SELECT
                            COUNT(*) as total,
                            SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                            AVG(pnl_rupees) as avg_pnl,
                            AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as avg_win,
                            AVG(CASE WHEN pnl_rupees <= 0 THEN pnl_rupees ELSE 0 END) as avg_loss
                        FROM {UNIFIED_TRADES_SQL}
                        WHERE status != 'OPEN'
                          AND symbol = ?
                          AND {v_match}
                          AND confidence_score BETWEEN ? AND ?
                    """,
                        (
                            symbol, verdict, verdict,
                            max(0, confidence - 25), min(100, confidence + 25),
                        ),
                    ).fetchone()
                    if similar_trades and similar_trades["total"] > 0:
                        note = f"Based on {similar_trades['total']} trades for {symbol} ({verdict}) (+/-25 confidence band)"

                # Tier 3 fallback: symbol + verdict (all hours/confidences)
                if not similar_trades or similar_trades["total"] == 0:
                    similar_trades = conn.execute(
                        f"""
                        SELECT
                            COUNT(*) as total,
                            SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                            AVG(pnl_rupees) as avg_pnl,
                            AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as avg_win,
                            AVG(CASE WHEN pnl_rupees <= 0 THEN pnl_rupees ELSE 0 END) as avg_loss
                        FROM {UNIFIED_TRADES_SQL}
                        WHERE status != 'OPEN'
                          AND symbol = ?
                          AND {v_match}
                    """,
                        (symbol, verdict, verdict),
                    ).fetchone()
                    if similar_trades and similar_trades["total"] > 0:
                        note = f"Based on all {similar_trades['total']} historical trades for {symbol} ({verdict})"

            # Tier 4 fallback: all historical trades for symbol
            if not similar_trades or similar_trades["total"] == 0:
                similar_trades = conn.execute(
                    f"""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                        AVG(pnl_rupees) as avg_pnl,
                        AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as avg_win,
                        AVG(CASE WHEN pnl_rupees <= 0 THEN pnl_rupees ELSE 0 END) as avg_loss
                    FROM {UNIFIED_TRADES_SQL}
                    WHERE status != 'OPEN'
                      AND symbol = ?
                """,
                    (symbol,),
                ).fetchone()
                if similar_trades and similar_trades["total"] > 0:
                    note = f"Based on all {similar_trades['total']} historical trades for {symbol}"

        if not similar_trades or similar_trades["total"] == 0:
            return {"match_found": False, "message": "No similar historical trades"}

        win_rate = (similar_trades["wins"] or 0) / similar_trades["total"]

        return {
            "match_found": True,
            "similar_trades": similar_trades["total"],
            "historical_win_rate": win_rate,
            "avg_pnl": similar_trades["avg_pnl"] or 0,
            "avg_win": similar_trades["avg_win"] or 0,
            "avg_loss": similar_trades["avg_loss"] or 0,
            "confidence_note": note,
        }

    def get_confidence_calibration_curve(
        self, days: int = 90, symbol: str | None = None
    ) -> dict:
        """
        Compute calibration curve: engine confidence bucket vs realized win rate.
        Phase 5 requirement: evaluates whether higher engine/LLM confidence correlates
        with higher realized win rates.

        Buckets:
          - '<50%'
          - '50-59%'
          - '60-69%'
          - '70-79%'
          - '80-89%'
          - '90-100%'
        """
        from src.models.schema import get_conn
        now_utc = datetime.now(timezone.utc)
        cutoff = (now_utc - timedelta(days=days)).isoformat()

        where_clauses = ["status != 'OPEN'", "closed_at IS NOT NULL", "closed_at >= ?"]
        params = [cutoff]
        if symbol:
            where_clauses.append("symbol = ?")
            params.append(symbol)

        where_sql = " AND ".join(where_clauses)

        query = f"""
            SELECT
                confidence_score,
                pnl_rupees
            FROM {UNIFIED_TRADES_SQL}
            WHERE {where_sql}
        """

        buckets = {
            "<50%": {"range": (0, 49), "mid": 0.25, "trades": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0},
            "50-59%": {"range": (50, 59), "mid": 0.55, "trades": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0},
            "60-69%": {"range": (60, 69), "mid": 0.65, "trades": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0},
            "70-79%": {"range": (70, 79), "mid": 0.75, "trades": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0},
            "80-89%": {"range": (80, 89), "mid": 0.85, "trades": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0},
            "90-100%": {"range": (90, 100), "mid": 0.95, "trades": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0},
        }

        with get_conn(read_only=True) as conn:
            rows = conn.execute(query, params).fetchall()

        for r in rows:
            conf = float(r["confidence_score"] or 0)
            pnl = float(r["pnl_rupees"] or 0)

            if conf < 50:
                target_b = buckets["<50%"]
            elif 50 <= conf <= 59:
                target_b = buckets["50-59%"]
            elif 60 <= conf <= 69:
                target_b = buckets["60-69%"]
            elif 70 <= conf <= 79:
                target_b = buckets["70-79%"]
            elif 80 <= conf <= 89:
                target_b = buckets["80-89%"]
            else:
                target_b = buckets["90-100%"]

            target_b["trades"] += 1
            target_b["pnl"] += pnl
            if pnl > 0:
                target_b["wins"] += 1
                target_b["gross_win"] += pnl
            else:
                target_b["gross_loss"] += abs(pnl)

        results = []
        total_trades = sum(b["trades"] for b in buckets.values())
        brier_score_sum = 0.0

        for name, b in buckets.items():
            cnt = b["trades"]
            wr = round(b["wins"] / cnt, 4) if cnt > 0 else 0.0
            avg_pnl = round(b["pnl"] / cnt, 2) if cnt > 0 else 0.0
            pf = round(b["gross_win"] / b["gross_loss"], 2) if b["gross_loss"] > 0 else (99.0 if b["gross_win"] > 0 else 0.0)
            gap = round(wr - b["mid"], 4) if cnt > 0 else 0.0

            if cnt > 0:
                brier_score_sum += b["wins"] * ((b["mid"] - 1.0) ** 2) + (cnt - b["wins"]) * (b["mid"] ** 2)

            results.append({
                "bucket": name,
                "confidence_midpoint": b["mid"],
                "trades": cnt,
                "wins": b["wins"],
                "win_rate": wr,
                "avg_pnl": avg_pnl,
                "total_pnl": round(b["pnl"], 2),
                "profit_factor": pf,
                "calibration_gap": gap,
                "is_well_calibrated": abs(gap) <= 0.10 if cnt >= 5 else None,
            })

        overall_brier = round(brier_score_sum / total_trades, 4) if total_trades > 0 else None

        return {
            "period_days": days,
            "symbol": symbol or "ALL",
            "total_trades": total_trades,
            "brier_score": overall_brier,
            "calibration_curve": results,
        }


def get_confidence_calibration_curve(days: int = 90, symbol: str | None = None) -> dict:
    """Convenience module function for calibration curve."""
    return get_analyzer().get_confidence_calibration_curve(days=days, symbol=symbol)

