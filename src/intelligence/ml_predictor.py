"""
Machine Learning model for predicting trade success probability.
Uses XGBoost with features extracted from trade context.

AI_INTELLIGENCE_ROADMAP_v3.0 — Phase 2

v2.0 FIXES:
- Feature leakage: uses opened_at, not datetime.now()
- Class imbalance: scale_pos_weight = n_neg/n_pos
- Feature ordering: explicit FEATURE_ORDER constant (no sort)
- Model versioning: only deploy if AUC improves >=2%
- Training source: UNION paper_trades + live_trades
- Event-driven retraining: edge health < 60 OR 20+ new trades

v2.1 FIXES:
- AUC gate startup blind spot: floor baseline = max(current_auc, 0.55)
- SHAP TreeExplainer cached per predictor, invalidated only on retrain
- Thread-safe trade counter with threading.Lock

v2.2 FIXES:
- FEATURE_ORDER missing 4 verdict encodings added (all 8 verdicts)
- Module-level singleton with lazy init (get_predictor())

v3.0 FIXES:
- Saved model feature-name mismatch guard on load
- Stratified train_test_split + stratified K-fold CV for deploy gate
- SHAP shap_values() shape normalization (list vs ndarray)
- net_oi_change redefined as pe - ce (directional, not total)
- Confidence level blends sample count with predicted-probability margin
- Model version uses UTC ISO timestamp (not naive local)
"""

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# ── ML dependencies (optional — graceful degradation) ──────────────────────
try:
    import shap
    import xgboost as xgb
    from sklearn.base import clone, ClassifierMixin
    from sklearn.metrics import accuracy_score, roc_auc_score
    from sklearn.model_selection import StratifiedKFold, train_test_split

    class PatchedXGBClassifier(xgb.XGBClassifier, ClassifierMixin):
        _estimator_type = "classifier"

    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    log.warning(
        "XGBoost/sklearn/shap not installed. ML predictions disabled. "
        "Install: pip install xgboost scikit-learn shap"
    )

MODEL_DIR = Path("data/models")
MODEL_PATH = MODEL_DIR / "ml_model.json"
FEATURES_PATH = MODEL_DIR / "ml_features.json"
MIN_TRADES_FOR_TRAINING = 30
MIN_TRADES_FOR_PREDICTION = 10
AUC_IMPROVEMENT_THRESHOLD = 0.02  # Only deploy if AUC improves >= 2%

# ── Explicit feature order — NEVER use sorted() at runtime ────────────────────
# v2.0 FIX: This MUST match between training and prediction.
# v2.2 FIX: Added 4 missing verdict one-hots (all 8 verdicts from VERDICT_ACTION_MAP).
# v3.0 FIX: Feature count is 25 (roadmap says ~28 approx; this is exact).
FEATURE_ORDER = [
    # Core signal features
    "confidence",
    "price_change_pct",
    "pcr",
    # OI features
    "ce_oi_change",
    "pe_oi_change",
    "net_oi_change",
    # Distance features
    "distance_to_support_pct",
    "distance_to_resistance_pct",
    "distance_to_max_pain_pct",
    # Time features (from trade record, NOT datetime.now())
    "hour_of_day",
    "day_of_week",
    "days_to_expiry",
    # Chart features
    "chart_conflict",
    "rsi_1h",
    "rsi_3h",
    # Verdict encoding (one-hot — ALL 8 verdicts from VERDICT_ACTION_MAP)
    "verdict_long_buildup",
    "verdict_short_buildup",
    "verdict_short_covering",
    "verdict_long_unwinding",
    "verdict_call_writing",
    "verdict_put_writing",
    "verdict_oi_bias_bullish",
    "verdict_oi_bias_bearish",
    # Regime features
    "regime_trending",
    "regime_rangebound",
]

IST_OFFSET = timedelta(hours=5, minutes=30)


@dataclass
class MLPrediction:
    """ML model prediction for a trade."""

    success_probability: float  # 0.0-1.0
    confidence_level: str  # "LOW", "MEDIUM", "HIGH"
    top_factors: list  # [(feature_name, impact_score)]
    model_version: str
    training_samples: int


# ── Singleton (v2.2 FIX) ──────────────────────────────────────────────────
# Module-level singleton with lazy init. Loaded once, reused forever.
# After retraining, invalidate_predictor() forces reload from disk.

_predictor: "TradeSuccessPredictor | None" = None
_predictor_lock = threading.Lock()


def get_predictor() -> "TradeSuccessPredictor":
    """Return the module-level TradeSuccessPredictor singleton."""
    global _predictor
    if _predictor is None:
        with _predictor_lock:
            if _predictor is None:  # double-checked locking
                _predictor = TradeSuccessPredictor()
    return _predictor


def invalidate_predictor():
    """v2.2: Call after retraining to force reload from disk."""
    global _predictor
    with _predictor_lock:
        _predictor = None


class TradeSuccessPredictor:
    """Predicts probability of trade success using XGBoost."""

    def __init__(self):
        self.model = None
        self.feature_names = list(FEATURE_ORDER)
        self.model_version = "0.0"
        self.training_samples = 0
        self.current_auc = 0.0
        self._shap_explainer = None  # v2.1: Cached SHAP explainer
        self._needs_retrain = False  # v3.0: Set when stale model discarded
        self._force_shadow = False   # ADR-007: True if AUC/samples below threshold
        self._load_model()

    def _load_model(self):
        """Load pre-trained model from disk."""
        if not ML_AVAILABLE or not MODEL_PATH.exists():
            return

        try:
            self.model = PatchedXGBClassifier()
            self.model.load_model(str(MODEL_PATH))

            with open(FEATURES_PATH) as f:
                meta = json.load(f)
                saved_features = meta["feature_names"]
                self.model_version = meta["version"]
                self.training_samples = meta["training_samples"]
                self.current_auc = meta.get("auc", 0.0)

            # v3.0 FIX: Guard against FEATURE_ORDER drift. A model trained under
            # an old feature set, loaded and fed a NEW vector, would either
            # throw on shape or silently miscolumn every feature.
            if list(saved_features) != list(FEATURE_ORDER):
                log.error(
                    "Model feature set mismatch: saved=%d features, "
                    "current FEATURE_ORDER=%d. Discarding stale model — "
                    "retrain required.",
                    len(saved_features),
                    len(FEATURE_ORDER),
                )
                self.model = None
                self._shap_explainer = None
                self._needs_retrain = True
                return

            self.feature_names = saved_features

            # v2.1 FIX: Cache SHAP explainer — init costs ~50-200ms per call.
            # Wrap in try/except: some xgboost model artifacts fail TreeExplainer
            # even after _estimator_type correction (e.g. native Booster format).
            try:
                self._shap_explainer = shap.TreeExplainer(self.model)
            except Exception as e:
                log.warning("SHAP TreeExplainer init failed (deferred): %s", e)
                self._shap_explainer = None

            log.info(
                "Loaded ML model v%s (%d samples, AUC=%.3f)",
                self.model_version,
                self.training_samples,
                self.current_auc,
            )

            # ADR-007 §3 A2: AUC guard — force shadow if model quality insufficient
            if self.current_auc < 0.55 or self.training_samples < 300:
                log.warning(
                    "ML model below quality threshold (AUC=%.3f, samples=%d). "
                    "Forcing shadow mode regardless of ML_PREDICTOR_MODE setting.",
                    self.current_auc,
                    self.training_samples,
                )
                self._force_shadow = True
            else:
                self._force_shadow = False
        except Exception as e:
            log.error("Failed to load ML model: %s", e)
            self.model = None
            self._shap_explainer = None

    def _invalidate_shap_cache(self):
        """v2.1: Call after retraining to force explainer rebuild."""
        self._shap_explainer = None

    def _get_shap_explainer(self) -> "shap.TreeExplainer | None":
        """v2.1: Lazy-init cached SHAP explainer."""
        if self._shap_explainer is None and self.model is not None:
            try:
                self._shap_explainer = shap.TreeExplainer(self.model)
            except Exception as e:
                log.warning("SHAP TreeExplainer lazy init failed: %s", e)
                self._shap_explainer = None
        return self._shap_explainer

    def predict(self, trade_context: dict) -> "MLPrediction | None":
        """Predict success probability for a trade."""
        if self.model is None:
            return None

        features = self._extract_features(trade_context)
        if features is None:
            return None

        # v2.0 FIX: Use explicit FEATURE_ORDER, not sorted()
        feature_vector = [features.get(name, 0) for name in FEATURE_ORDER]

        # Get probability
        proba = self.model.predict_proba([feature_vector])[0]
        success_prob = proba[1]  # P(profitable)

        # v2.1 FIX: Use cached SHAP explainer (saves ~50-200ms per call).
        # v3.0 FIX: Normalize SHAP return shape. TreeExplainer.shap_values()
        # returns an ndarray (n, features) on newer SHAP for binary models, but
        # a list [class0_arr, class1_arr] on older versions.
        explainer = self._get_shap_explainer()
        if explainer is None:
            # SHAP unavailable — return prediction without factor breakdown
            top_factors = []
        else:
            try:
                raw = explainer.shap_values([feature_vector])
                if isinstance(raw, list):
                    shap_values = np.asarray(raw[-1])[0]
                else:
                    shap_values = np.asarray(raw)[0]

                top_indices = np.argsort(np.abs(shap_values))[-3:][::-1]
                top_factors = [
                    (FEATURE_ORDER[i], float(shap_values[i])) for i in top_indices
                ]
            except Exception as e:
                log.debug("SHAP explanation failed: %s", e)
                top_factors = []

        # v3.0 FIX: Confidence level blends TWO signals, not just sample count.
        # margin = |p - 0.5| measures how decisive THIS prediction is.
        margin = abs(success_prob - 0.5)  # 0 (coin-flip) … 0.5 (certain)
        if self.training_samples < 50 or margin < 0.10:
            confidence = "LOW"
        elif self.training_samples < 100 or margin < 0.20:
            confidence = "MEDIUM"
        else:
            confidence = "HIGH"

        prediction = MLPrediction(
            success_probability=float(success_prob),
            confidence_level=confidence,
            top_factors=top_factors,
            model_version=self.model_version,
            training_samples=self.training_samples,
        )

        # ADR-007 §3 A2: Write to shadow_predictions if in shadow mode
        self._write_shadow_prediction(prediction, trade_context, features)

        return prediction

    def _is_shadow_mode(self) -> bool:
        """ADR-007 §3 A2: Check if ML predictor is in shadow mode."""
        if self._force_shadow:
            return True
        try:
            from config.runtime_config import load_runtime_config
            rconf = load_runtime_config()
            mode = rconf.get("ml_predictor_mode", "shadow")
            return mode == "shadow"
        except Exception:
            return True  # Default to shadow on error

    def _write_shadow_prediction(
        self, prediction: "MLPrediction", trade_context: dict, features: dict
    ) -> None:
        """ADR-007 §3 A2: Write prediction to shadow_predictions table in shadow mode."""
        if not self._is_shadow_mode():
            return
        try:
            from datetime import datetime, timezone
            from src.models.schema import get_conn

            now_iso = datetime.now(timezone.utc).isoformat()
            symbol = trade_context.get("symbol", "")
            features_json = json.dumps(features, default=str)

            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO shadow_predictions
                    (ts, symbol, model_version, p_success, features_json, decision_id, outcome)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        now_iso,
                        symbol,
                        prediction.model_version,
                        prediction.success_probability,
                        features_json,
                    ),
                )
        except Exception as e:
            log.debug("Failed to write shadow prediction: %s", e)

    def _extract_features(self, ctx: dict) -> "dict | None":
        """
        Extract numeric features from trade context.

        v2.0 FIX: Time features use opened_at from trade record,
        NOT datetime.now(). Using current time during training would
        cause feature leakage.
        """
        try:
            # v2.0 FIX: Extract time from trade record, not current time
            opened_at = ctx.get("opened_at")
            if opened_at:
                trade_time = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                ist_time = trade_time + IST_OFFSET
                hour_of_day = ist_time.hour
                day_of_week = ist_time.weekday()
            else:
                # Fallback for live predictions (no opened_at yet)
                now_ist = datetime.now(timezone.utc) + IST_OFFSET
                hour_of_day = now_ist.hour
                day_of_week = now_ist.weekday()

            features = {
                # Core signal features
                "confidence": float(ctx.get("confidence", 0)),
                "price_change_pct": float(ctx.get("price_change_pct") or 0),
                "pcr": float(ctx.get("pcr") or 1.0),
                # OI features
                "ce_oi_change": float(ctx.get("ce_oi_change") or 0),
                "pe_oi_change": float(ctx.get("pe_oi_change") or 0),
                # v3.0 FIX: "net" implies direction. Redefined as pe - ce:
                # positive = put-writing / bullish OI bias
                # negative = call-writing / bearish OI bias
                "net_oi_change": (
                    float(ctx.get("pe_oi_change") or 0)
                    - float(ctx.get("ce_oi_change") or 0)
                ),
                # Distance features
                "distance_to_support_pct": self._calc_distance_pct(
                    ctx.get("underlying"), ctx.get("support")
                ),
                "distance_to_resistance_pct": self._calc_distance_pct(
                    ctx.get("underlying"), ctx.get("resistance")
                ),
                "distance_to_max_pain_pct": self._calc_distance_pct(
                    ctx.get("underlying"), ctx.get("max_pain")
                ),
                # Time features (v2.0: from trade record)
                "hour_of_day": hour_of_day,
                "day_of_week": day_of_week,
                "days_to_expiry": int(ctx.get("days_to_expiry") or 7),
                # Chart features
                "chart_conflict": 1 if ctx.get("chart_conflict") else 0,
                "rsi_1h": float(ctx.get("rsi_1h") or 50),
                "rsi_3h": float(ctx.get("rsi_3h") or 50),
                # Verdict encoding (one-hot — ALL 8 verdicts)
                "verdict_long_buildup": (
                    1 if ctx.get("verdict_label") == "Long Buildup" else 0
                ),
                "verdict_short_buildup": (
                    1 if ctx.get("verdict_label") == "Short Buildup" else 0
                ),
                "verdict_short_covering": (
                    1 if ctx.get("verdict_label") == "Short Covering" else 0
                ),
                "verdict_long_unwinding": (
                    1 if ctx.get("verdict_label") == "Long Unwinding" else 0
                ),
                "verdict_call_writing": (
                    1 if ctx.get("verdict_label") == "Call Writing" else 0
                ),
                "verdict_put_writing": (
                    1 if ctx.get("verdict_label") == "Put Writing" else 0
                ),
                "verdict_oi_bias_bullish": (
                    1 if ctx.get("verdict_label") == "OI Bias Bullish" else 0
                ),
                "verdict_oi_bias_bearish": (
                    1 if ctx.get("verdict_label") == "OI Bias Bearish" else 0
                ),
                # Regime features
                "regime_trending": (
                    1 if "trending" in str(ctx.get("regime", "")).lower() else 0
                ),
                "regime_rangebound": (
                    1 if "range" in str(ctx.get("regime", "")).lower() else 0
                ),
            }
            return features
        except Exception as e:
            log.error("Feature extraction failed: %s", e)
            return None

    def _calc_distance_pct(self, underlying, level) -> float:
        """Calculate percentage distance to a level."""
        if not underlying or not level:
            return 0.0
        try:
            return abs(float(underlying) - float(level)) / float(underlying) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    def train(self) -> bool:
        """
        Train model on historical trades.

        v2.0 FIXES:
        - Uses UNION of paper_trades + live_trades
        - Handles class imbalance with scale_pos_weight
        - Uses FEATURE_ORDER (not sorted()) for consistent ordering
        - Version comparison: only deploy if AUC improves >= 2%

        v3.0 FIXES:
        - Phase 0 feature coverage gate
        - Stratified train_test_split (stratify=y)
        - Stratified K-fold CV for deploy gate (holdout too noisy at n~30)
        - Model version uses UTC ISO timestamp
        """
        if not ML_AVAILABLE:
            log.warning("ML libraries not available. Training skipped.")
            return False

        # v3.0 FIX: Phase 0 gate. Refuse to train if feature coverage is low.
        from src.intelligence.feature_coverage import assert_feature_coverage

        if not assert_feature_coverage(min_pct=0.90):
            log.warning("Feature coverage below threshold. Training skipped.")
            return False

        from src.models.schema import get_conn

        # v2.0 FIX: Fetch from BOTH paper_trades and live_trades (v3.0: explicit columns to guard UNION ALL)
        with get_conn() as conn:
            trades = conn.execute("""
                SELECT opened_at, closed_at, symbol, verdict_label, option_type, strike,
                       entry_underlying, exit_underlying, sl_underlying, target_underlying,
                       pnl_points, pnl_rupees, status, reason, digest_id, entry_premium,
                       exit_premium, sl_premium, target_premium, lots, trade_status,
                       setup_type, decision_reason, confidence_score, entry_quality_score,
                       trend_alignment_score, regime_score, signal_key, pyramid_level,
                       max_favorable_r, side, expiry, price_change_pct, pcr, ce_oi_change,
                       pe_oi_change, underlying, support, resistance, max_pain,
                       days_to_expiry, chart_conflict, rsi_1h, rsi_3h, regime,
                       'paper' as source
                FROM paper_trades
                WHERE status != 'OPEN'
                  AND closed_at IS NOT NULL
                  AND pnl_rupees IS NOT NULL
                UNION ALL
                SELECT opened_at, closed_at, symbol, verdict_label, option_type, strike,
                       entry_underlying, exit_underlying, sl_underlying, target_underlying,
                       pnl_points, pnl_rupees, status, reason, digest_id, entry_premium,
                       exit_premium, sl_premium, target_premium, lots, trade_status,
                       setup_type, decision_reason, confidence_score, entry_quality_score,
                       trend_alignment_score, regime_score, signal_key, pyramid_level,
                       max_favorable_r, side, expiry, price_change_pct, pcr, ce_oi_change,
                       pe_oi_change, underlying, support, resistance, max_pain,
                       days_to_expiry, chart_conflict, rsi_1h, rsi_3h, regime,
                       'live' as source
                FROM live_trades
                WHERE status != 'OPEN'
                  AND closed_at IS NOT NULL
                  AND pnl_rupees IS NOT NULL
            """).fetchall()

        if len(trades) < MIN_TRADES_FOR_TRAINING:
            log.info(
                "Insufficient trades for training (%d/%d)",
                len(trades),
                MIN_TRADES_FOR_TRAINING,
            )
            return False

        log.info("Training ML model on %d trades...", len(trades))

        # Extract features and labels
        X = []
        y = []

        for trade in trades:
            trade_dict = dict(trade)
            features = self._extract_features(trade_dict)
            if features is None:
                continue

            label = 1 if float(trade["pnl_rupees"]) > 0 else 0

            # v2.0 FIX: Use FEATURE_ORDER, not sorted(features.keys())
            X.append([features.get(name, 0) for name in FEATURE_ORDER])
            y.append(label)

        if len(X) < MIN_TRADES_FOR_TRAINING:
            log.warning(
                "Insufficient valid samples (%d/%d)",
                len(X),
                MIN_TRADES_FOR_TRAINING,
            )
            return False

        # v2.0 FIX: Handle class imbalance
        n_pos = sum(y)
        n_neg = len(y) - n_pos
        scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0
        log.info(
            "Class balance: %d positive, %d negative (scale_pos_weight=%.2f)",
            n_pos,
            n_neg,
            scale_pos_weight,
        )

        # v3.0 FIX: Stratified split (stratify=y).
        X_arr, y_arr = np.asarray(X), np.asarray(y)

        # Guard: need at least 2 samples per class for stratified split
        unique, counts = np.unique(y_arr, return_counts=True)
        min_class_count = int(counts.min()) if len(counts) > 1 else 0

        if min_class_count < 2:
            log.warning(
                "Cannot stratify: minority class has only %d samples. "
                "Training skipped until more balanced data.",
                min_class_count,
            )
            return False

        X_train, X_test, y_train, y_test = train_test_split(
            X_arr,
            y_arr,
            test_size=0.2,
            random_state=42,
            stratify=y_arr,
        )

        # Train XGBoost with class imbalance handling
        new_model = PatchedXGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            scale_pos_weight=scale_pos_weight,
            random_state=42,
            eval_metric="logloss",
        )
        new_model.fit(X_train, y_train)

        # Evaluate on holdout (reported)
        y_pred_proba = new_model.predict_proba(X_test)[:, 1]
        holdout_auc = (
            roc_auc_score(y_test, y_pred_proba) if len(set(y_test)) > 1 else 0.5
        )
        accuracy = accuracy_score(y_test, new_model.predict(X_test))

        # v3.0 FIX: GATE on cross-validated AUC, not the holdout.
        # A 6-sample holdout at n~30 is pure noise.
        n_splits = min(5, n_pos, n_neg)
        if n_splits >= 2:
            skf = StratifiedKFold(
                n_splits=n_splits,
                shuffle=True,
                random_state=42,
            )
            cv_aucs = []
            for tr_idx, va_idx in skf.split(X_arr, y_arr):
                m = clone(new_model)
                m.fit(X_arr[tr_idx], y_arr[tr_idx])
                if len(set(y_arr[va_idx])) > 1:
                    cv_aucs.append(
                        roc_auc_score(
                            y_arr[va_idx],
                            m.predict_proba(X_arr[va_idx])[:, 1],
                        )
                    )
            new_auc = float(np.mean(cv_aucs)) if cv_aucs else holdout_auc
        else:
            new_auc = holdout_auc

        log.info(
            "New model: holdout_acc=%.2f%%, holdout_AUC=%.3f, CV_AUC=%.3f",
            accuracy * 100,
            holdout_auc,
            new_auc,
        )

        # v2.1 FIX: AUC floor to prevent startup blind spot. (v3.0: allow initial deploy if current model is None)
        AUC_FLOOR = 0.55
        effective_baseline = max(self.current_auc, AUC_FLOOR)

        if self.model is not None and new_auc < effective_baseline + AUC_IMPROVEMENT_THRESHOLD:
            log.warning(
                "New model AUC (%.3f) not >=%.2f better than baseline "
                "(%.3f, current=%.3f). Keeping old model.",
                new_auc,
                AUC_IMPROVEMENT_THRESHOLD,
                effective_baseline,
                self.current_auc,
            )
            return False

        # Save model with versioning
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self.model = new_model
        self.feature_names = list(FEATURE_ORDER)
        # v3.0 FIX: UTC ISO timestamp (not naive local)
        self.model_version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        self.training_samples = len(X)
        self.current_auc = new_auc

        # v2.1 FIX: Invalidate cached SHAP explainer after retrain
        self._invalidate_shap_cache()

        self.model.save_model(str(MODEL_PATH))

        with open(FEATURES_PATH, "w") as f:
            json.dump(
                {
                    "feature_names": self.feature_names,
                    "version": self.model_version,
                    "training_samples": self.training_samples,
                    "accuracy": accuracy,
                    "auc": new_auc,
                    "scale_pos_weight": scale_pos_weight,
                },
                f,
                indent=2,
            )

        log.info(
            "Model deployed: v%s (%d samples, AUC=%.3f)",
            self.model_version,
            self.training_samples,
            new_auc,
        )
        return True
