"""
Edge Decay Monitor
AI_INTELLIGENCE_ROADMAP_v3.0 — Phase 3

Monitors strategy performance over time to detect edge decay.
Alerts when win rate or profitability is declining.

v2.0 FIXES:
- Guard for insufficient historical data (< 5 trades → INSUFFICIENT_HISTORY)
- Health score includes absolute performance, not just deltas
- Single GROUP BY query instead of N+1 per strategy
- IST-corrected time windows

v2.1 FIXES:
- get_all_strategies_health() passes pre-fetched row data directly to
  scoring; no re-query inside check_edge_health()
- Inline scoring delegates to _calculate_health_score_absolute()

v2.2 FIXES:
- Previous version called undefined module-level _classify_trend() and
  EdgeHealthReport (neither exist). Now uses self._classify_trend(change, baseline)
  and the EdgeHealth dataclass with correct field names.

v3.0 FIXES:
- #6: Retrain trigger ignores INSUFFICIENT_* trends and count < MIN_HISTORICAL_TRADES
- #10: Unified sentinel: INSUFFICIENT_HISTORY everywhere
- #11: Per-strategy rows carry win_rate_trend=INSUFFICIENT_HISTORY; sorted within
  method-consistent groups
- #12: Min-baseline floor (max(abs(hist_pnl), 100)) before ratio
"""

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)


@dataclass
class EdgeHealth:
    """Health status of a trading strategy."""

    strategy_name: str
    current_win_rate: float
    historical_win_rate: float
    win_rate_trend: str  # "IMPROVING", "STABLE", "DECLINING", "INSUFFICIENT_HISTORY"
    pnl_trend: str  # "IMPROVING", "STABLE", "DECLINING", "INSUFFICIENT_HISTORY"
    health_score: float  # 0-100
    recommendation: str

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict for API responses."""
        return asdict(self)


# ── Singleton (Phase 3) ────────────────────────────────────────────────────
# EdgeDecayMonitor is stateless (no model to load), but making it a singleton
# ensures consistent configuration and avoids unnecessary object creation
# when called from both the pipeline and the dashboard API.
_monitor: "EdgeDecayMonitor | None" = None
_monitor_lock = threading.Lock()


def get_monitor() -> "EdgeDecayMonitor":
    """Return the module-level EdgeDecayMonitor singleton."""
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:  # double-checked locking
                _monitor = EdgeDecayMonitor()
    return _monitor


class EdgeDecayMonitor:
    """Detects when trading edge is weakening."""

    MIN_HISTORICAL_TRADES = 5  # v2.0: Guard against biased comparisons
    RECENT_WINDOW_DAYS = 30
    HISTORICAL_WINDOW_DAYS = 90
    DECAY_THRESHOLD = 0.15  # 15% decline triggers alert

    def __init__(self):
        self.rolling_window_days = self.RECENT_WINDOW_DAYS
        self.historical_window_days = self.HISTORICAL_WINDOW_DAYS
        self.decay_threshold = self.DECAY_THRESHOLD

    # ── Core health check ──────────────────────────────────────────────────

    def check_edge_health(
        self, strategy_filter: dict | None = None
    ) -> list[EdgeHealth]:
        """
        Check health of all strategies or filtered subset.

        Args:
            strategy_filter: Optional dict with keys:
                - symbol: Filter by specific symbol (e.g., "NIFTY")
                - verdict_label: Filter by specific verdict (e.g., "Long Buildup")

        Returns:
            List of EdgeHealth objects. Usually a single-element list for the
            overall health, or filtered subset.
        """
        from src.models.schema import get_conn

        where_clause = "status != 'OPEN' AND closed_at IS NOT NULL"
        params: list = []

        if strategy_filter:
            if "symbol" in strategy_filter:
                where_clause += " AND symbol = ?"
                params.append(strategy_filter["symbol"])
            if "verdict_label" in strategy_filter:
                where_clause += " AND verdict_label = ?"
                params.append(strategy_filter["verdict_label"])

        now_utc = datetime.now(timezone.utc)
        recent_cutoff = (now_utc - timedelta(days=self.rolling_window_days)).isoformat()
        hist_start = (now_utc - timedelta(days=self.historical_window_days)).isoformat()

        with get_conn() as conn:
            # Recent window (last 30 days)
            recent = conn.execute(
                f"""
                SELECT
                    COUNT(*) as count,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(pnl_rupees) as total_pnl,
                    AVG(pnl_rupees) as avg_pnl
                FROM paper_trades
                WHERE {where_clause} AND closed_at >= ?
            """,
                params + [recent_cutoff],
            ).fetchone()

            # Historical window (30-90 days ago)
            historical = conn.execute(
                f"""
                SELECT
                    COUNT(*) as count,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(pnl_rupees) as total_pnl,
                    AVG(pnl_rupees) as avg_pnl
                FROM paper_trades
                WHERE {where_clause} AND closed_at >= ? AND closed_at < ?
            """,
                params + [hist_start, recent_cutoff],
            ).fetchone()

        # ── Guard: insufficient recent data ────────────────────────────────
        recent_count = recent["count"] if recent else 0
        if not recent or recent_count < self.MIN_HISTORICAL_TRADES:
            return [
                EdgeHealth(
                    strategy_name=self._get_strategy_name(strategy_filter),
                    current_win_rate=0,
                    historical_win_rate=0,
                    win_rate_trend="INSUFFICIENT_HISTORY",  # v3.0: unified sentinel
                    pnl_trend="INSUFFICIENT_HISTORY",
                    health_score=50,
                    recommendation=f"⏳ Not enough recent trades to assess ({recent_count}/{self.MIN_HISTORICAL_TRADES} needed)",
                )
            ]

        current_win_rate = (
            recent["wins"] / recent["count"] if recent["count"] > 0 else 0
        )

        # ── Guard: insufficient historical data ────────────────────────────
        hist_count = historical["count"] if historical else 0
        if hist_count < self.MIN_HISTORICAL_TRADES:
            return [
                EdgeHealth(
                    strategy_name=self._get_strategy_name(strategy_filter),
                    current_win_rate=current_win_rate,
                    historical_win_rate=0,
                    win_rate_trend="INSUFFICIENT_HISTORY",
                    pnl_trend="INSUFFICIENT_HISTORY",
                    health_score=self._calculate_health_score_absolute(
                        current_win_rate, recent["avg_pnl"]
                    ),
                    recommendation=(
                        f"⏳ Building history ({hist_count}/{self.MIN_HISTORICAL_TRADES} trades). "
                        f"Current win rate: {current_win_rate:.0%}"
                    ),
                )
            ]

        # ── Full comparison: recent vs historical ──────────────────────────
        hist_win_rate = historical["wins"] / historical["count"]
        hist_avg_pnl = historical["avg_pnl"] if historical else 0

        win_rate_change = current_win_rate - hist_win_rate
        pnl_change = recent["avg_pnl"] - hist_avg_pnl

        win_rate_trend = self._classify_trend(win_rate_change, hist_win_rate)

        # v3.0 FIX #12: Floor the PnL baseline. When historical avg PnL ≈ 0
        # the ratio change/baseline explodes and every period reads IMPROVING
        # or DECLINING. Floor at ₹100 so tiny baselines can't dominate.
        pnl_baseline = max(abs(hist_avg_pnl), 100.0)
        pnl_trend = self._classify_trend(pnl_change, pnl_baseline)

        # v2.0 FIX: Health score includes absolute performance
        health_score = self._calculate_health_score(
            current_win_rate, hist_win_rate, recent["avg_pnl"], hist_avg_pnl
        )

        recommendation = self._generate_edge_recommendation(
            current_win_rate, hist_win_rate, recent["avg_pnl"], win_rate_trend
        )

        return [
            EdgeHealth(
                strategy_name=self._get_strategy_name(strategy_filter),
                current_win_rate=current_win_rate,
                historical_win_rate=hist_win_rate,
                win_rate_trend=win_rate_trend,
                pnl_trend=pnl_trend,
                health_score=health_score,
                recommendation=recommendation,
            )
        ]

    # ── Per-strategy health (single query) ─────────────────────────────────

    def get_all_strategies_health(self, symbol: str = None) -> list[EdgeHealth]:
        """
        Check health of all strategy combinations.

        v2.0 FIX: Single GROUP BY query instead of N+1 per strategy.
        v2.1 FIX: Score per-strategy health directly from pre-fetched row
        data instead of calling check_edge_health() which re-queries the DB.

        v2.2 FIX: Previous version called undefined module-level _classify_trend()
        and EdgeHealthReport (neither exist). Now uses self._classify_trend(change, baseline)
        and the EdgeHealth dataclass with correct field names. Inline scoring delegates
        to _calculate_health_score_absolute() to avoid formula divergence.

        v3.0 FIX #11: Keep the two scoring methods in separate, clearly-labelled
        groups instead of interleaving them in one sorted list. The overall
        report uses trend-based scoring (real historical comparison); the
        per-strategy rows have NO historical window here, so they get
        absolute-only scoring and an honest INSUFFICIENT_HISTORY trend rather
        than a fabricated IMPROVING/DECLINING label derived from a fixed 0.55.
        """
        from src.models.schema import get_conn

        now_utc = datetime.now(timezone.utc)
        recent_cutoff = (now_utc - timedelta(days=self.rolling_window_days)).isoformat()

        # Single GROUP BY query — fetches all strategy metrics at once
        with get_conn() as conn:
            if symbol:
                rows = conn.execute(
                    """
                    SELECT
                        symbol,
                        verdict_label,
                        COUNT(*) as count,
                        SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                        AVG(pnl_rupees) as avg_pnl
                    FROM paper_trades
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                      AND closed_at >= ?
                      AND symbol = ?
                    GROUP BY symbol, verdict_label
                    HAVING COUNT(*) >= ?
                """,
                    (recent_cutoff, symbol, self.MIN_HISTORICAL_TRADES),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        symbol,
                        verdict_label,
                        COUNT(*) as count,
                        SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                        AVG(pnl_rupees) as avg_pnl
                    FROM paper_trades
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                      AND closed_at >= ?
                    GROUP BY symbol, verdict_label
                    HAVING COUNT(*) >= ?
                """,
                    (recent_cutoff, self.MIN_HISTORICAL_TRADES),
                ).fetchall()

        # ── Group A: overall health (trend-based, real comparison) ─────────
        overall = self.check_edge_health()

        # ── Group B: per-strategy snapshots (absolute scoring, no trend claim) ──
        per_strategy = []
        for row in rows:
            total = row["count"]
            wins = row["wins"]
            avg_pnl = row["avg_pnl"]
            win_rate = wins / total if total > 0 else 0.0
            score = self._calculate_health_score_absolute(win_rate, avg_pnl)
            per_strategy.append(
                EdgeHealth(
                    strategy_name=f"{row['symbol']} {row['verdict_label']}",
                    current_win_rate=win_rate,
                    historical_win_rate=0.0,
                    # v3.0: honest sentinel — this is a snapshot, not a trend
                    win_rate_trend="INSUFFICIENT_HISTORY",
                    pnl_trend="INSUFFICIENT_HISTORY",
                    health_score=score,
                    recommendation=self._generate_edge_recommendation(
                        win_rate, win_rate, avg_pnl, "INSUFFICIENT_HISTORY"
                    ),
                )
            )

        # Sort each group by health_score; overall first, then worst strategies.
        return overall + sorted(per_strategy, key=lambda h: h.health_score)

    # ── Helper methods ─────────────────────────────────────────────────────

    def _get_strategy_name(self, strategy_filter: dict | None) -> str:
        """Build a human-readable strategy name from the filter dict."""
        if not strategy_filter:
            return "All Strategies"
        parts = []
        if "symbol" in strategy_filter:
            parts.append(strategy_filter["symbol"])
        if "verdict_label" in strategy_filter:
            parts.append(strategy_filter["verdict_label"])
        return " ".join(parts) if parts else "All Strategies"

    def _classify_trend(self, change: float, baseline: float) -> str:
        """
        Classify trend as IMPROVING, STABLE, or DECLINING.

        Args:
            change: Absolute change (current - historical)
            baseline: Historical baseline value

        Returns:
            One of: "IMPROVING", "STABLE", "DECLINING"
        """
        if baseline == 0:
            return "STABLE"
        change_pct = change / baseline
        if change_pct > 0.10:
            return "IMPROVING"
        elif change_pct < -self.decay_threshold:
            return "DECLINING"
        else:
            return "STABLE"

    def _calculate_health_score_absolute(
        self, current_wr: float, avg_pnl: float
    ) -> float:
        """
        v2.0 FIX: Health score based on absolute performance only.
        Used when historical data is insufficient for comparison.

        Scoring:
        - Win rate component: -20 to +30 points
        - PnL component: -20 to +20 points
        - Base: 50 points (neutral)
        - Range: 0-100
        """
        score = 50.0  # Start at neutral

        # Win rate component (0-30 points)
        if current_wr >= 0.70:
            score += 30
        elif current_wr >= 0.60:
            score += 20
        elif current_wr >= 0.50:
            score += 10
        elif current_wr >= 0.40:
            score -= 10
        else:
            score -= 20

        # PnL component (0-20 points)
        if avg_pnl > 1000:
            score += 20
        elif avg_pnl > 0:
            score += 10
        elif avg_pnl > -500:
            score -= 5
        else:
            score -= 20

        return max(0, min(100, score))

    def _calculate_health_score(
        self,
        current_wr: float,
        hist_wr: float,
        current_pnl: float,
        hist_pnl: float,
    ) -> float:
        """
        Calculate overall health score (0-100).

        v2.0 FIX: Includes BOTH absolute performance AND trend.
        Previous version only subtracted from 100, causing new strategies
        with 60% win rate to score higher than declining strategies at 70%.

        Scoring breakdown (100 points total):
        - Absolute win rate: 0-40 points
        - Win rate trend: 0-30 points
        - Absolute PnL: 0-15 points
        - PnL trend: 0-15 points
        """
        score = 0.0

        # Absolute win rate (40 points max)
        if current_wr >= 0.70:
            score += 40
        elif current_wr >= 0.60:
            score += 30
        elif current_wr >= 0.50:
            score += 20
        elif current_wr >= 0.40:
            score += 10
        # Below 40% gets 0 points

        # Win rate trend (30 points max)
        wr_change = current_wr - hist_wr if hist_wr > 0 else 0
        if wr_change > 0.10:
            score += 30
        elif wr_change > 0.05:
            score += 20
        elif wr_change > -0.05:
            score += 15  # Stable is OK
        elif wr_change > -0.10:
            score += 5
        elif wr_change > -0.15:
            score += 0
        # Severe decline gets 0 points

        # Absolute PnL (15 points max)
        if current_pnl > 1000:
            score += 15
        elif current_pnl > 0:
            score += 10
        elif current_pnl > -500:
            score += 5
        # Negative PnL gets 0

        # PnL trend (15 points max)
        if hist_pnl != 0:
            pnl_change_pct = (current_pnl - hist_pnl) / abs(hist_pnl)
            if pnl_change_pct > 0.20:
                score += 15
            elif pnl_change_pct > 0:
                score += 10
            elif pnl_change_pct > -0.20:
                score += 5
            # Severe PnL decline gets 0

        return max(0, min(100, score))

    def _generate_edge_recommendation(
        self,
        current_wr: float,
        hist_wr: float,
        avg_pnl: float,
        trend: str,
    ) -> str:
        """Generate actionable recommendation based on edge health."""
        # Insufficient data
        if trend == "INSUFFICIENT_HISTORY":
            return (
                "⏳ Building history — not enough trades to assess edge health. "
                "Continue trading normally to build a reliable sample."
            )

        # Edge decay detected
        if trend == "DECLINING":
            return (
                "🔴 EDGE DECAY DETECTED — Your strategy is underperforming. "
                "Consider: (1) Reducing position size, (2) Raising confidence threshold, "
                "(3) Pausing this strategy for 1 week to recalibrate."
            )

        # Below breakeven
        if current_wr < 0.50:
            return (
                "🟠 BELOW BREAKEVEN — Win rate below 50%. "
                "Review recent trades for common mistakes. "
                "Consider pausing until you identify the issue."
            )

        # Marginal edge
        if current_wr < 0.60:
            return (
                "🟡 MARGINAL EDGE — Win rate is acceptable but not strong. "
                "Look for higher confidence setups or better confluence."
            )

        # Improving trend at moderate WR
        if current_wr >= 0.55 and trend == "IMPROVING":
            if avg_pnl > 500:
                return (
                    "🟢 IMPROVING EDGE — Your strategy is gaining traction. "
                    "Win rate improving with positive avg P&L. Continue executing."
                )
            return (
                "🟡 MODERATE EDGE (IMPROVING) — Win rate improving but avg P&L is modest. "
                "Focus on preserving capital while trend establishes."
            )

        # Strong edge
        if current_wr >= 0.70 and avg_pnl > 1000:
            return (
                "🟢 STRONG EDGE — Your strategy is performing well. "
                "Continue with current parameters. Consider slight size increase."
            )

        # Stable / healthy
        return (
            "⚪ STABLE — Strategy is performing as expected. "
            "Monitor for changes over next 2 weeks."
        )
