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


class TradeHistoryAnalyzer:
    """Analyzes closed trades to find patterns."""

    MIN_PATTERN_TRADES = 10  # v2.0: raised from 3 -- 3 trades is noise.
    # v3.0: SINGLE SOURCE OF TRUTH. Used by both the
    # HAVING clause AND _generate_recommendation, so a
    # pattern can never surface yet be labelled
    # "insufficient" (the old min_trades=30 conflict).
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
                    patterns.append(
                        PatternInsight(
                            pattern_name=row["pattern_name"],
                            sample_size=row["sample_size"],
                            win_rate=row["win_rate"],
                            avg_pnl=row["avg_pnl"],
                            best_time="All day",
                            best_conditions=json.loads(row["best_conditions"])
                            if row["best_conditions"]
                            else {},
                            recommendation=row["recommendation"],
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
                # v3.0 FIX: Atomic refresh. Previously DELETE then loop-INSERT
                # ran un-transactioned -- a crash mid-loop wiped the cache table
                # and left it empty. Wrap both in one transaction so the table
                # is never observed empty.
                conn.execute("BEGIN")
                try:
                    conn.execute("DELETE FROM ai_pattern_insights")
                    for p in patterns:
                        conn.execute(
                            """
                            INSERT INTO ai_pattern_insights
                            (pattern_name, pattern_type, sample_size, win_rate, avg_pnl,
                             best_conditions, recommendation, discovered_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                            (
                                p.pattern_name,
                                "auto",
                                p.sample_size,
                                p.win_rate,
                                p.avg_pnl,
                                json.dumps(p.best_conditions),
                                p.recommendation,
                            ),
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
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

        return sorted(patterns, key=lambda p: p.win_rate * p.sample_size, reverse=True)

    def _analyze_by_symbol_verdict(self, symbol: str = None) -> list[PatternInsight]:
        """Analyze performance by symbol and verdict label."""
        from src.models.schema import get_conn

        with get_conn() as conn:
            if symbol:
                rows = conn.execute("""
                    SELECT
                        symbol,
                        verdict_label,
                        COUNT(*) as count,
                        AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_rupees) as avg_pnl,
                        AVG(confidence_score) as avg_confidence
                    FROM paper_trades
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                      AND symbol = ?
                    GROUP BY symbol, verdict_label
                    HAVING COUNT(*) >= ?
                """, (symbol, self.MIN_PATTERN_TRADES)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT
                        symbol,
                        verdict_label,
                        COUNT(*) as count,
                        AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_rupees) as avg_pnl,
                        AVG(confidence_score) as avg_confidence
                    FROM paper_trades
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                    GROUP BY symbol, verdict_label
                    HAVING COUNT(*) >= ?
                """, (self.MIN_PATTERN_TRADES,)).fetchall()

        insights = []
        for row in rows:
            recommendation = self._generate_recommendation(
                row["win_rate"], row["avg_pnl"], row["count"]
            )
            insights.append(
                PatternInsight(
                    pattern_name=f"{row['symbol']} {row['verdict_label']}",
                    sample_size=row["count"],
                    win_rate=row["win_rate"],
                    avg_pnl=row["avg_pnl"],
                    best_time="All day",
                    best_conditions={"avg_confidence": row["avg_confidence"]},
                    recommendation=recommendation,
                )
            )
        return insights

    def _analyze_by_session(self, symbol: str = None) -> list[PatternInsight]:
        """
        Analyze performance by time of day (IST sessions).

        v2.0 FIX: Database stores UTC timestamps. Must convert to IST
        before bucketing. A 9:15 AM IST trade is 3:45 UTC.

        v2.1 FIX: Previous hour/minute string comparison caused overlapping
        sessions (e.g., 15:00 matched both Afternoon and Closing). Now uses
        total-minutes-since-midnight comparison for mutually exclusive buckets.
        Sessions are defined as start_minutes to end_minutes (inclusive start,
        exclusive end) - so no trade can belong to two sessions.

        v2.2 FIX: Runs a single CASE WHEN query instead of 5 separate queries
        in a loop. Each trade is bucketed exactly once (or into NULL if outside
        all sessions), and aggregation is computed in one pass.
        """
        from src.models.schema import get_conn

        if symbol:
            query = """
                SELECT
                    session,
                    COUNT(*) as count,
                    AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                    AVG(pnl_rupees) as avg_pnl
                FROM (
                    SELECT
                        pnl_rupees,
                        CASE
                            WHEN total_min >= 555  AND total_min < 630  THEN 'Market Open (09:15-10:30)'
                            WHEN total_min >= 630  AND total_min < 720  THEN 'Mid-Morning (10:30-12:00)'
                            WHEN total_min >= 720  AND total_min < 840  THEN 'Post-Lunch (12:00-14:00)'
                            WHEN total_min >= 840  AND total_min < 900  THEN 'Afternoon (14:00-15:00)'
                            WHEN total_min >= 900  AND total_min < 930  THEN 'Closing (15:00-15:30)'
                            ELSE NULL
                        END as session
                    FROM (
                        SELECT
                            pnl_rupees,
                            CAST(strftime('%H', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) * 60
                            + CAST(strftime('%M', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) as total_min
                        FROM paper_trades
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
            query = """
                SELECT
                    session,
                    COUNT(*) as count,
                    AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                    AVG(pnl_rupees) as avg_pnl
                FROM (
                    SELECT
                        pnl_rupees,
                        CASE
                            WHEN total_min >= 555  AND total_min < 630  THEN 'Market Open (09:15-10:30)'
                            WHEN total_min >= 630  AND total_min < 720  THEN 'Mid-Morning (10:30-12:00)'
                            WHEN total_min >= 720  AND total_min < 840  THEN 'Post-Lunch (12:00-14:00)'
                            WHEN total_min >= 840  AND total_min < 900  THEN 'Afternoon (14:00-15:00)'
                            WHEN total_min >= 900  AND total_min < 930  THEN 'Closing (15:00-15:30)'
                            ELSE NULL
                        END as session
                    FROM (
                        SELECT
                            pnl_rupees,
                            CAST(strftime('%H', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) * 60
                            + CAST(strftime('%M', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER) as total_min
                        FROM paper_trades
                        WHERE status != 'OPEN'
                          AND closed_at IS NOT NULL
                    )
                )
                WHERE session IS NOT NULL
                GROUP BY session
                HAVING COUNT(*) >= ?
            """

        # Session names in display order (for sorting results)
        SESSION_ORDER = [
            "Market Open (09:15-10:30)",
            "Mid-Morning (10:30-12:00)",
            "Post-Lunch (12:00-14:00)",
            "Afternoon (14:00-15:00)",
            "Closing (15:00-15:30)",
        ]

        insights = []
        with get_conn() as conn:
            if symbol:
                rows = conn.execute(query, (symbol, self.MIN_PATTERN_TRADES)).fetchall()
            else:
                rows = conn.execute(query, (self.MIN_PATTERN_TRADES,)).fetchall()

        # Build lookup for O(1) access by session name
        row_by_session = {row["session"]: row for row in rows}

        for session_name in SESSION_ORDER:
            row = row_by_session.get(session_name)
            if row:
                recommendation = self._generate_recommendation(
                    row["win_rate"], row["avg_pnl"], row["count"]
                )
                insights.append(
                    PatternInsight(
                        pattern_name=f"Session: {session_name}",
                        sample_size=row["count"],
                        win_rate=row["win_rate"],
                        avg_pnl=row["avg_pnl"],
                        best_time=session_name,
                        best_conditions={},
                        recommendation=recommendation,
                    )
                )
        return insights

    def _analyze_by_confidence(self, symbol: str = None) -> list[PatternInsight]:
        """Analyze performance by confidence score range."""
        from src.models.schema import get_conn

        # Define confidence ranges
        confidence_ranges = [
            ("Low Confidence (60-70)", 60, 70),
            ("Medium Confidence (70-80)", 70, 80),
            ("High Confidence (80-90)", 80, 90),
            ("Very High Confidence (90+)", 90, 100),
        ]

        insights = []
        with get_conn() as conn:
            for range_name, min_conf, max_conf in confidence_ranges:
                if symbol:
                    row = conn.execute(
                        """
                        SELECT
                            COUNT(*) as count,
                            AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                            AVG(pnl_rupees) as avg_pnl
                        FROM paper_trades
                        WHERE status != 'OPEN'
                          AND closed_at IS NOT NULL
                          AND confidence_score >= ? AND confidence_score < ?
                          AND symbol = ?
                    """,
                        (min_conf, max_conf, symbol),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT
                            COUNT(*) as count,
                            AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                            AVG(pnl_rupees) as avg_pnl
                        FROM paper_trades
                        WHERE status != 'OPEN'
                          AND closed_at IS NOT NULL
                          AND confidence_score >= ? AND confidence_score < ?
                    """,
                        (min_conf, max_conf),
                    ).fetchone()

                if row and row["count"] >= self.MIN_PATTERN_TRADES:
                    recommendation = self._generate_recommendation(
                        row["win_rate"], row["avg_pnl"], row["count"]
                    )
                    insights.append(
                        PatternInsight(
                            pattern_name=f"Confidence: {range_name}",
                            sample_size=row["count"],
                            win_rate=row["win_rate"],
                            avg_pnl=row["avg_pnl"],
                            best_time="All day",
                            best_conditions={"confidence_range": [min_conf, max_conf]},
                            recommendation=recommendation,
                        )
                    )
        return insights

    def _analyze_by_setup_type(self, symbol: str = None) -> list[PatternInsight]:
        """Analyze performance by verdict/setup type."""
        from src.models.schema import get_conn

        with get_conn() as conn:
            if symbol:
                rows = conn.execute(
                    """
                    SELECT
                        verdict_label,
                        COUNT(*) as count,
                        AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_rupees) as avg_pnl
                    FROM paper_trades
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                      AND symbol = ?
                    GROUP BY verdict_label
                    HAVING COUNT(*) >= ?
                """,
                    (symbol, self.MIN_PATTERN_TRADES),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        verdict_label,
                        COUNT(*) as count,
                        AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_rupees) as avg_pnl
                    FROM paper_trades
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                    GROUP BY verdict_label
                    HAVING COUNT(*) >= ?
                """,
                    (self.MIN_PATTERN_TRADES,),
                ).fetchall()

        insights = []
        for row in rows:
            recommendation = self._generate_recommendation(
                row["win_rate"], row["avg_pnl"], row["count"]
            )
            insights.append(
                PatternInsight(
                    pattern_name=f"Setup: {row['verdict_label']}",
                    sample_size=row["count"],
                    win_rate=row["win_rate"],
                    avg_pnl=row["avg_pnl"],
                    best_time="All day",
                    best_conditions={},
                    recommendation=recommendation,
                )
            )
        return insights

    def _analyze_by_regime(self, symbol: str = None) -> list[PatternInsight]:
        """Analyze performance by market regime."""
        from src.models.schema import get_conn

        with get_conn() as conn:
            if symbol:
                rows = conn.execute(
                    """
                    SELECT
                        regime,
                        COUNT(*) as count,
                        AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_rupees) as avg_pnl
                    FROM paper_trades
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
                    """
                    SELECT
                        regime,
                        COUNT(*) as count,
                        AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_rupees) as avg_pnl
                    FROM paper_trades
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
            recommendation = self._generate_recommendation(
                row["win_rate"], row["avg_pnl"], row["count"]
            )
            insights.append(
                PatternInsight(
                    pattern_name=f"Regime: {row['regime']}",
                    sample_size=row["count"],
                    win_rate=row["win_rate"],
                    avg_pnl=row["avg_pnl"],
                    best_time="All day",
                    best_conditions={},
                    recommendation=recommendation,
                )
            )
        return insights

    def _generate_recommendation(
        self, win_rate: float, avg_pnl: float, count: int
    ) -> str:
        """Generate actionable recommendation based on performance."""
        # v3.0 FIX: Gate on the same constant as the HAVING clause. Previously
        # this used self.min_trades (30) while HAVING used MIN_PATTERN_TRADES
        # (10), so 10-29-trade patterns surfaced then displayed "insufficient".
        if count < self.MIN_PATTERN_TRADES:
            return (
                f"[!] Insufficient data ({count}/{self.MIN_PATTERN_TRADES} trades needed)"
            )

        if win_rate >= 0.70 and avg_pnl > 1000:
            return "[GREEN] STRONG EDGE - Increase position size or frequency"
        elif win_rate >= 0.60 and avg_pnl > 0:
            return "[YELLOW] MODERATE EDGE - Trade with standard size, look for confluence"
        elif win_rate >= 0.50 and avg_pnl >= 0:
            return "[ORANGE] WEAK EDGE - Reduce size or wait for higher confidence"
        elif win_rate < 0.50:
            return "[RED] NEGATIVE EDGE - Avoid this setup until performance improves"
        else:
            return "[WHITE] NEUTRAL - Monitor for more data"

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
            # v3.0 FIX: The old query compared strftime('%H') (zero-padded
            # text like "09") against str(ist_hour-1) ("8"). Lexically
            # "09" >= "8" is FALSE, so the entire 09:00-15:00 IST session was
            # silently dropped. Cast BOTH sides to INTEGER so the comparison
            # is numeric, not lexicographic.
            similar_trades = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    AVG(pnl_rupees) as avg_pnl,
                    AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees ELSE 0 END) as avg_win,
                    AVG(CASE WHEN pnl_rupees <= 0 THEN pnl_rupees ELSE 0 END) as avg_loss
                FROM paper_trades
                WHERE status != 'OPEN'
                  AND symbol = ?
                  AND verdict_label = ?
                  AND confidence_score BETWEEN ? AND ?
                  AND CAST(strftime('%H', datetime(opened_at, '+5 hours', '+30 minutes')) AS INTEGER)
                      BETWEEN ? AND ?
            """,
                (
                    symbol,
                    verdict,
                    max(0, confidence - 20),
                    min(100, confidence + 20),
                    max(0, ist_hour - 1),
                    min(23, ist_hour + 1),  # v3.0: ints, not str()
                ),
            ).fetchone()

        if not similar_trades or similar_trades["total"] == 0:
            return {"match_found": False, "message": "No similar historical trades"}

        win_rate = similar_trades["wins"] / similar_trades["total"]

        return {
            "match_found": True,
            "similar_trades": similar_trades["total"],
            "historical_win_rate": win_rate,
            "avg_pnl": similar_trades["avg_pnl"],
            "avg_win": similar_trades["avg_win"],
            "avg_loss": similar_trades["avg_loss"],
            "confidence_note": f"Based on {similar_trades['total']} similar trades (+/-20 confidence band)",
        }
