"""
ML model retraining job.

AI_INTELLIGENCE_ROADMAP_v3.0 — Phase 2

v2.0 CHANGE: Event-driven instead of weekly-only.
Triggers when:
1. Edge health score drops below 60
2. 20+ new trades accumulated since last training
3. Weekly fallback (Sunday 2 AM IST) as safety net

v2.1 FIX: Thread-safe trade counter with threading.Lock.
APScheduler's BackgroundScheduler runs the weekly job in a background
thread while on_trade_closed() is called from the pipeline thread.
"""
import logging
import threading

log = logging.getLogger(__name__)

_retrain_lock = threading.Lock()
_trades_since_last_train = 0
_trades_since_last_train_lock = threading.Lock()
TRADES_THRESHOLD_FOR_RETRAIN = 20
EDGE_HEALTH_THRESHOLD = 60


def on_trade_closed():
    """Called after each trade closes. Increments counter (thread-safe)."""
    global _trades_since_last_train
    with _trades_since_last_train_lock:
        _trades_since_last_train += 1
        count = _trades_since_last_train

    if count >= TRADES_THRESHOLD_FOR_RETRAIN:
        log.info("Retrain triggered: %d new trades", count)
        run_training()


def on_edge_health_alert(health_score: float):
    """Called when edge decay monitor detects declining performance."""
    if health_score < EDGE_HEALTH_THRESHOLD:
        log.info(
            "Retrain triggered: edge health %.1f < %d",
            health_score, EDGE_HEALTH_THRESHOLD,
        )
        run_training()


def run_weekly_training():
    """Weekly fallback retraining (Sunday 2 AM IST)."""
    log.info("Starting weekly ML training job...")
    run_training()


def run_training():
    """
    Execute model training with rollback protection.

    v2.2 FIX: Uses singleton predictor and invalidates it after successful
    training so the next get_predictor() call loads the new model from disk.
    """
    global _trades_since_last_train

    try:
        from src.intelligence.ml_predictor import get_predictor, invalidate_predictor
    except ImportError as e:
        log.error("Cannot import ml_predictor: %s", e)
        return False

    predictor = get_predictor()  # v2.2: Use singleton
    success = predictor.train()

    if success:
        with _trades_since_last_train_lock:
            _trades_since_last_train = 0
        invalidate_predictor()  # v2.2: Force reload of new model on next use
        log.info("ML model training completed successfully")
    else:
        log.warning("ML model training failed or skipped (AUC not improved)")

    return success
