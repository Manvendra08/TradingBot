"""
Feature Coverage Validation Gate
AI_INTELLIGENCE_ROADMAP_v3.0 — Phase 0.3

Validates that closed trades have sufficient feature data before allowing
Phase 2 ML training. Prevents training on zero-filled features which would
produce a meaningless model.

Called before every training attempt. Returns False when feature coverage
is below the threshold (default 90%), causing training to skip.
"""
import logging

log = logging.getLogger(__name__)


def assert_feature_coverage(min_pct: float = 0.90) -> bool:
    """
    Validate that closed paper trades have sufficient ML feature data.

    Returns True when feature coverage >= min_pct, allowing training to proceed.
    Returns False when coverage is below threshold, blocking training.

    Args:
        min_pct: Minimum fraction of closed trades with non-NULL features.
                 Default 0.90 (90%).

    Returns:
        True if coverage is sufficient, False otherwise.
    """
    try:
        from src.models.schema import get_conn

        with get_conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN pcr IS NOT NULL
                              AND rsi_1h IS NOT NULL
                              AND price_change_pct IS NOT NULL
                         THEN 1 ELSE 0 END) AS with_features
                FROM paper_trades
                WHERE status != 'OPEN'
                  AND closed_at IS NOT NULL
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
