"""
Feature Coverage Validation Gate
AI_INTELLIGENCE_ROADMAP_v3.0 — Phase 0.3

Validates that closed trades have sufficient feature data before allowing
Phase 2 ML training. Prevents training on zero-filled features which would
produce a meaningless model.

Called before every training attempt. Returns False when feature coverage
is below the threshold (default 90%), causing training to skip.

v4.0: Unified 3-table check (paper_trades + live_trades + multi_leg_trades).
Multi-leg trades supply confidence_score via their own column and pcr via
scan_summaries JOIN, so coverage proxy is confidence_score IS NOT NULL.
"""
import logging

log = logging.getLogger(__name__)


def assert_feature_coverage(min_pct: float = 0.90) -> bool:
    """
    Validate that closed trades (across all 3 tables) have sufficient ML
    feature data to allow training.

    v4.0: Queries the unified UNION ALL dataset:
      - paper_trades  — full inline features (pcr, rsi_1h, price_change_pct)
      - live_trades   — same schema as paper_trades
      - multi_leg_trades — confidence_score inline; pcr via scan_summaries JOIN

    Coverage proxy for multi-leg trades: confidence_score IS NOT NULL.
    For paper/live: original pcr IS NOT NULL gate.

    Returns True when coverage >= min_pct across the unified dataset.

    Args:
        min_pct: Minimum fraction with non-NULL key feature. Default 0.90.

    Returns:
        True if coverage is sufficient, False otherwise.
    """
    try:
        from src.models.schema import get_conn

        with get_conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN has_feature = 1 THEN 1 ELSE 0 END) AS with_features
                FROM (
                    SELECT CASE WHEN pcr IS NOT NULL THEN 1 ELSE 0 END AS has_feature
                    FROM paper_trades
                    WHERE status != 'OPEN' AND closed_at IS NOT NULL
                    UNION ALL
                    SELECT CASE WHEN pcr IS NOT NULL THEN 1 ELSE 0 END AS has_feature
                    FROM live_trades
                    WHERE status != 'OPEN' AND closed_at IS NOT NULL
                    UNION ALL
                    SELECT CASE WHEN m.confidence_score IS NOT NULL THEN 1 ELSE 0 END AS has_feature
                    FROM multi_leg_trades m
                    WHERE m.status != 'OPEN'
                      AND m.closed_at IS NOT NULL
                      AND m.total_pnl IS NOT NULL
                )
            """).fetchone()

            total = row["total"] if row else 0
            with_features = row["with_features"] if row else 0

        if total == 0:
            log.info(
                "Feature coverage: no closed trades yet. "
                "Training deferred until trades accumulate."
            )
            return False

        coverage = with_features / total
        log.info(
            "Feature coverage: %d/%d = %.1f%% (threshold: %.0f%%)",
            with_features, total, coverage * 100, min_pct * 100,
        )

        if coverage < min_pct:
            log.warning(
                "Feature coverage %.1f%% < %.0f%% threshold. "
                "Training deferred until more instrumented trades close.",
                coverage * 100, min_pct * 100,
            )
            return False

        return True

    except Exception as e:
        log.error("Feature coverage check failed: %s", e)
        return False

