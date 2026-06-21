# 🧠 AI Intelligence System - Optimized Roadmap
## Building on Existing NSEBOT Architecture

**Date:** June 21, 2026  
**Status:** Ready for Implementation  
**Estimated Effort:** 10-12 weeks (incremental delivery)

---

## 📊 Executive Summary

This roadmap extends the **already functional NSEBOT** with an AI Intelligence System that learns from trade history, provides actionable coaching, and continuously self-improves. Unlike the theoretical plan, this is **practical and builds on existing code**.

### What Already Exists ✅

| Component | Location | Status |
|-----------|----------|--------|
| Intelligence Engine | `src/engine/intelligence.py` | ✅ Production-ready |
| LLM Integration | `src/engine/llm_enrichment.py` | ✅ Multi-provider (OpenRouter/Gemini/Groq) |
| Paper Trading | `src/engine/paper_trading.py` | ✅ Full lifecycle management |
| Database | `data/bot.db` | ✅ SQLite with trade history |
| Dashboard | `src/dashboard/` | ✅ Flask web UI |
| Signal Detection | `src/engine/anomaly_detector.py` | ✅ 15+ alert types |
| Risk Management | `src/engine/risk_engine.py` | ✅ Position sizing, limits |

### What We're Building 🚀

1. **Trade History Analyzer** (Weeks 1-2) - Pattern discovery & performance analytics
2. **ML Success Predictor** (Weeks 3-5) - XGBoost model predicting P(success)
3. **Behavioral Coach** (Weeks 6-7) - Overtrading/revenge trading detection
4. **Edge Decay Monitor** (Weeks 8-9) - Strategy performance tracking
5. **AI Dashboard UI** (Weeks 10-12) - Visual insights & recommendations

---

## 🎯 Design Philosophy

### Core Principles

1. **Extend, Don't Replace** - Add AI as a layer on top of existing intelligence
2. **Transparent Confidence** - Show uncertainty based on sample size
3. **Actionable Insights** - Every metric has a "what to do" recommendation
4. **Trader Control** - AI advises, trader decides (no autopilot)
5. **Incremental Value** - Each phase delivers usable features

### Integration Strategy

```
Existing Flow:
  scan → alerts → intelligence → paper_trading → database

Enhanced Flow:
  scan → alerts → intelligence → [AI ANALYZER] → paper_trading → database
                                      ↓
                              [ML MODEL] → predictions
                                      ↓
                              [LLM COACH] → narrative advice
                                      ↓
                              [DASHBOARD UI] → visual insights
```

---

## 📅 Phase 1: Trade History Analyzer (Weeks 1-2)

**Goal:** Extract patterns from existing trade data without ML

### 1.1 New Module: `src/intelligence/history_analyzer.py`

```python
"""
Analyzes closed paper trades to discover winning/losing patterns.
No ML required - pure statistical aggregation.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections import defaultdict

@dataclass
class PatternInsight:
    """A discovered pattern with actionable recommendation."""
    pattern_name: str              # e.g., "BANKNIFTY Long Buildup"
    sample_size: int               # number of trades
    win_rate: float                # 0.0-1.0
    avg_pnl: float                 # average PnL in rupees
    best_time: str                 # e.g., "09:30-11:00"
    best_conditions: dict          # e.g., {"min_confidence": 75, "pcr_range": [1.1, 1.5]}
    recommendation: str            # actionable advice
    
class TradeHistoryAnalyzer:
    """Analyzes closed trades to find patterns."""
    
    def __init__(self, min_trades: int = 30):
        self.min_trades = min_trades
    
    def analyze_all_patterns(self) -> list[PatternInsight]:
        """Discover patterns across multiple dimensions."""
        patterns = []
        
        # 1. By Symbol + Verdict
        patterns.extend(self._analyze_by_symbol_verdict())
        
        # 2. By Time of Day
        patterns.extend(self._analyze_by_session())
        
        # 3. By Confidence Range
        patterns.extend(self._analyze_by_confidence())
        
        # 4. By Setup Type
        patterns.extend(self._analyze_by_setup_type())
        
        # 5. By Market Regime
        patterns.extend(self._analyze_by_regime())
        
        return sorted(patterns, key=lambda p: p.win_rate * p.sample_size, reverse=True)
    
    def _analyze_by_symbol_verdict(self) -> list[PatternInsight]:
        """Analyze performance by symbol and verdict label."""
        from src.models.schema import get_conn
        
        with get_conn() as conn:
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
                HAVING COUNT(*) >= 3
            """).fetchall()
        
        insights = []
        for row in rows:
            recommendation = self._generate_recommendation(
                row["win_rate"], row["avg_pnl"], row["count"]
            )
            insights.append(PatternInsight(
                pattern_name=f"{row['symbol']} {row['verdict_label']}",
                sample_size=row["count"],
                win_rate=row["win_rate"],
                avg_pnl=row["avg_pnl"],
                best_time="All day",
                best_conditions={"avg_confidence": row["avg_confidence"]},
                recommendation=recommendation
            ))
        return insights
    
    def _analyze_by_session(self) -> list[PatternInsight]:
        """Analyze performance by time of day (IST sessions)."""
        sessions = {
            "Market Open (09:15-10:30)": (9, 10, 30),
            "Mid-Morning (10:30-12:00)": (10, 12, 0),
            "Post-Lunch (12:00-14:00)": (12, 14, 0),
            "Afternoon (14:00-15:00)": (14, 15, 0),
            "Closing (15:00-15:30)": (15, 15, 30),
        }
        
        from src.models.schema import get_conn
        
        insights = []
        with get_conn() as conn:
            for session_name, (start_h, end_h, end_m) in sessions.items():
                rows = conn.execute("""
                    SELECT 
                        COUNT(*) as count,
                        AVG(CASE WHEN pnl_rupees > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_rupees) as avg_pnl
                    FROM paper_trades
                    WHERE status != 'OPEN'
                      AND closed_at IS NOT NULL
                      AND strftime('%H', opened_at) >= ?
                      AND (strftime('%H', opened_at) < ? OR 
                           (strftime('%H', opened_at) = ? AND strftime('%M', opened_at) <= ?))
                """, (str(start_h), str(end_h), str(end_h), str(end_m))).fetchone()
                
                if rows and rows["count"] >= 3:
                    recommendation = self._generate_recommendation(
                        rows["win_rate"], rows["avg_pnl"], rows["count"]
                    )
                    insights.append(PatternInsight(
                        pattern_name=f"Session: {session_name}",
                        sample_size=rows["count"],
                        win_rate=rows["win_rate"],
                        avg_pnl=rows["avg_pnl"],
                        best_time=session_name,
                        best_conditions={},
                        recommendation=recommendation
                    ))
        return insights
    
    def _generate_recommendation(self, win_rate: float, avg_pnl: float, count: int) -> str:
        """Generate actionable recommendation based on performance."""
        if count < self.min_trades:
            return f"⚠️ Insufficient data ({count}/{self.min_trades} trades needed)"
        
        if win_rate >= 0.70 and avg_pnl > 1000:
            return "🟢 STRONG EDGE - Increase position size or frequency"
        elif win_rate >= 0.60 and avg_pnl > 0:
            return "🟡 MODERATE EDGE - Trade with standard size, look for confluence"
        elif win_rate >= 0.50 and avg_pnl >= 0:
            return "🟠 WEAK EDGE - Reduce size or wait for higher confidence"
        elif win_rate < 0.50:
            return "🔴 NEGATIVE EDGE - Avoid this setup until performance improves"
        else:
            return "⚪ NEUTRAL - Monitor for more data"
    
    def get_trade_dna_match(self, current_trade_context: dict) -> dict:
        """Find similar historical trades and show success probability."""
        from src.models.schema import get_conn
        
        # Extract key features from current trade
        symbol = current_trade_context.get("symbol")
        verdict = current_trade_context.get("verdict_label")
        confidence = current_trade_context.get("confidence", 0)
        opened_hour = datetime.now().hour
        
        with get_conn() as conn:
            # Find trades with similar characteristics
            similar_trades = conn.execute("""
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
                  AND strftime('%H', opened_at) BETWEEN ? AND ?
            """, (
                symbol, verdict,
                confidence - 10, confidence + 10,
                max(0, opened_hour - 1), min(23, opened_hour + 1)
            )).fetchone()
        
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
            "confidence_note": f"Based on {similar_trades['total']} similar trades"
        }
```

### 1.2 Database Schema Extension

```sql
-- Add to bot.db

-- Pattern insights cache (refreshed daily)
CREATE TABLE IF NOT EXISTS ai_pattern_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name TEXT NOT NULL,
    pattern_type TEXT NOT NULL,  -- 'symbol_verdict', 'session', 'confidence', 'regime'
    sample_size INTEGER,
    win_rate REAL,
    avg_pnl REAL,
    best_conditions TEXT,  -- JSON
    recommendation TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);

-- Trade DNA matches (for real-time similarity lookup)
CREATE INDEX IF NOT EXISTS idx_trades_similarity 
ON paper_trades(symbol, verdict_label, confidence_score, strftime('%H', opened_at));
```

### 1.3 Integration Points

**File:** `src/engine/pipeline.py` (add after intelligence generation)

```python
# After generating intelligence, analyze patterns
from src.intelligence.history_analyzer import TradeHistoryAnalyzer

analyzer = TradeHistoryAnalyzer(min_trades=20)
current_context = {
    "symbol": symbol,
    "verdict_label": intel.verdict_label,
    "confidence": intel.confidence,
}
dna_match = analyzer.get_trade_dna_match(current_context)

# Add to intelligence result
intel.trade_dna = dna_match
```

### 1.4 API Endpoints

**File:** `dashboard_server.py` (add new routes)

```python
@app.route("/api/ai/patterns")
def get_ai_patterns():
    """Get discovered trading patterns."""
    from src.intelligence.history_analyzer import TradeHistoryAnalyzer
    analyzer = TradeHistoryAnalyzer()
    patterns = analyzer.analyze_all_patterns()
    return jsonify([{
        "name": p.pattern_name,
        "win_rate": p.win_rate,
        "avg_pnl": p.avg_pnl,
        "sample_size": p.sample_size,
        "recommendation": p.recommendation
    } for p in patterns[:10]])

@app.route("/api/ai/trade-dna/<symbol>")
def get_trade_dna(symbol):
    """Get historical match for potential trade."""
    from src.intelligence.history_analyzer import TradeHistoryAnalyzer
    analyzer = TradeHistoryAnalyzer()
    context = {
        "symbol": symbol,
        "verdict_label": request.args.get("verdict"),
        "confidence": int(request.args.get("confidence", 0))
    }
    return jsonify(analyzer.get_trade_dna_match(context))
```

### 1.5 Phase 1 Deliverables

- ✅ `TradeHistoryAnalyzer` class with 5 pattern dimensions
- ✅ Database indexes for fast similarity lookup
- ✅ API endpoints for patterns and trade DNA
- ✅ Integration with existing pipeline
- ✅ Basic pattern visualization in dashboard

**Estimated Effort:** 8-12 hours  
**Impact:** Immediate value - shows what's working without ML

---

## 🤖 Phase 2: ML Success Predictor (Weeks 3-5)

**Goal:** Train XGBoost model to predict P(trade profitable)

### 2.1 New Module: `src/intelligence/ml_predictor.py`

```python
"""
Machine Learning model for predicting trade success probability.
Uses XGBoost with features extracted from trade context.
"""
import json
import logging
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

# ML dependencies (optional - graceful degradation)
try:
    import xgboost as xgb
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, roc_auc_score
    import shap
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    log.warning("XGBoost/sklearn not installed. ML predictions disabled.")

MODEL_PATH = Path("data/ml_model.json")
FEATURES_PATH = Path("data/ml_features.json")
MIN_TRADES_FOR_TRAINING = 30
MIN_TRADES_FOR_PREDICTION = 10

@dataclass
class MLPrediction:
    """ML model prediction for a trade."""
    success_probability: float      # 0.0-1.0
    confidence_level: str           # "LOW", "MEDIUM", "HIGH"
    top_factors: list[tuple]        # [(feature_name, impact_score)]
    model_version: str
    training_samples: int
    
class TradeSuccessPredictor:
    """Predicts probability of trade success using XGBoost."""
    
    def __init__(self):
        self.model = None
        self.feature_names = []
        self.model_version = "0.0"
        self.training_samples = 0
        self._load_model()
    
    def _load_model(self):
        """Load pre-trained model from disk."""
        if not ML_AVAILABLE or not MODEL_PATH.exists():
            return
        
        try:
            self.model = xgb.XGBClassifier()
            self.model.load_model(str(MODEL_PATH))
            
            with open(FEATURES_PATH) as f:
                meta = json.load(f)
                self.feature_names = meta["feature_names"]
                self.model_version = meta["version"]
                self.training_samples = meta["training_samples"]
            
            log.info(f"Loaded ML model v{self.model_version} ({self.training_samples} samples)")
        except Exception as e:
            log.error(f"Failed to load ML model: {e}")
            self.model = None
    
    def predict(self, trade_context: dict) -> MLPrediction | None:
        """Predict success probability for a trade."""
        if self.model is None:
            return None
        
        features = self._extract_features(trade_context)
        if features is None:
            return None
        
        # Ensure feature order matches training
        feature_vector = [features.get(name, 0) for name in self.feature_names]
        
        # Get probability
        proba = self.model.predict_proba([feature_vector])[0]
        success_prob = proba[1]  # P(profitable)
        
        # Get SHAP values for explainability
        explainer = shap.TreeExplainer(self.model)
        shap_values = explainer.shap_values([feature_vector])[0]
        
        # Top 3 factors driving prediction
        top_indices = np.argsort(np.abs(shap_values))[-3:][::-1]
        top_factors = [
            (self.feature_names[i], float(shap_values[i]))
            for i in top_indices
        ]
        
        # Confidence based on training sample size
        if self.training_samples < 50:
            confidence = "LOW"
        elif self.training_samples < 100:
            confidence = "MEDIUM"
        else:
            confidence = "HIGH"
        
        return MLPrediction(
            success_probability=float(success_prob),
            confidence_level=confidence,
            top_factors=top_factors,
            model_version=self.model_version,
            training_samples=self.training_samples
        )
    
    def _extract_features(self, ctx: dict) -> dict | None:
        """Extract numeric features from trade context."""
        try:
            features = {
                # Core signal features
                "confidence": float(ctx.get("confidence", 0)),
                "price_change_pct": float(ctx.get("price_change_pct", 0)),
                "pcr": float(ctx.get("pcr", 1.0)),
                
                # OI features
                "ce_oi_change": float(ctx.get("ce_oi_change", 0)),
                "pe_oi_change": float(ctx.get("pe_oi_change", 0)),
                "net_oi_change": float(ctx.get("ce_oi_change", 0) + ctx.get("pe_oi_change", 0)),
                
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
                
                # Time features
                "hour_of_day": datetime.now().hour,
                "day_of_week": datetime.now().weekday(),
                "days_to_expiry": int(ctx.get("days_to_expiry", 7)),
                
                # Chart features
                "chart_conflict": 1 if ctx.get("chart_conflict") else 0,
                "rsi_1h": float(ctx.get("rsi_1h", 50)),
                "rsi_3h": float(ctx.get("rsi_3h", 50)),
                
                # Verdict encoding (one-hot)
                "verdict_long_buildup": 1 if ctx.get("verdict_label") == "Long Buildup" else 0,
                "verdict_short_buildup": 1 if ctx.get("verdict_label") == "Short Buildup" else 0,
                "verdict_short_covering": 1 if ctx.get("verdict_label") == "Short Covering" else 0,
                "verdict_long_unwinding": 1 if ctx.get("verdict_label") == "Long Unwinding" else 0,
                
                # Regime features
                "regime_trending": 1 if "trending" in str(ctx.get("regime", "")).lower() else 0,
                "regime_rangebound": 1 if "range" in str(ctx.get("regime", "")).lower() else 0,
            }
            return features
        except Exception as e:
            log.error(f"Feature extraction failed: {e}")
            return None
    
    def _calc_distance_pct(self, underlying, level) -> float:
        """Calculate percentage distance to a level."""
        if not underlying or not level:
            return 0.0
        return abs(float(underlying) - float(level)) / float(underlying) * 100
    
    def train(self) -> bool:
        """Train model on historical trades."""
        if not ML_AVAILABLE:
            log.warning("ML libraries not available. Training skipped.")
            return False
        
        from src.models.schema import get_conn
        
        # Fetch closed trades
        with get_conn() as conn:
            trades = conn.execute("""
                SELECT * FROM paper_trades
                WHERE status != 'OPEN'
                  AND closed_at IS NOT NULL
                  AND pnl_rupees IS NOT NULL
            """).fetchall()
        
        if len(trades) < MIN_TRADES_FOR_TRAINING:
            log.info(f"Insufficient trades for training ({len(trades)}/{MIN_TRADES_FOR_TRAINING})")
            return False
        
        log.info(f"Training ML model on {len(trades)} trades...")
        
        # Extract features and labels
        X = []
        y = []
        
        for trade in trades:
            trade_dict = dict(trade)
            features = self._extract_features(trade_dict)
            if features is None:
                continue
            
            # Label: 1 if profitable, 0 if loss
            label = 1 if float(trade["pnl_rupees"]) > 0 else 0
            
            X.append([features.get(name, 0) for name in sorted(features.keys())])
            y.append(label)
        
        if len(X) < MIN_TRADES_FOR_TRAINING:
            log.warning(f"Insufficient valid samples ({len(X)}/{MIN_TRADES_FOR_TRAINING})")
            return False
        
        # Train/test split
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )
        
        # Train XGBoost
        self.model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            random_state=42
        )
        self.model.fit(X_train, y_train)
        
        # Evaluate
        y_pred = self.model.predict(X_test)
        accuracy = accuracy_score(y_test, y_pred)
        log.info(f"Model accuracy: {accuracy:.2%}")
        
        # Save model
        self.feature_names = sorted(features.keys())
        self.model_version = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.training_samples = len(X)
        
        self.model.save_model(str(MODEL_PATH))
        
        with open(FEATURES_PATH, "w") as f:
            json.dump({
                "feature_names": self.feature_names,
                "version": self.model_version,
                "training_samples": self.training_samples,
                "accuracy": accuracy
            }, f)
        
        log.info(f"Model saved: v{self.model_version} ({self.training_samples} samples)")
        return True
```

### 2.2 Training Scheduler

**File:** `src/scheduler/ml_training_job.py`

```python
"""
Weekly ML model retraining job.
Runs every Sunday at 2 AM IST.
"""
import logging
from datetime import datetime

log = logging.getLogger(__name__)

def run_weekly_training():
    """Scheduled job to retrain ML model."""
    log.info("Starting weekly ML training job...")
    
    from src.intelligence.ml_predictor import TradeSuccessPredictor
    
    predictor = TradeSuccessPredictor()
    success = predictor.train()
    
    if success:
        log.info("✅ ML model training completed successfully")
        # TODO: Send Telegram notification
    else:
        log.warning("⚠️ ML model training failed or skipped")
    
    return success
```

**Integration:** Add to `src/scheduler/__init__.py`

```python
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()

# Weekly ML training (Sunday 2 AM IST)
scheduler.add_job(
    run_weekly_training,
    'cron',
    day_of_week='sun',
    hour=2,
    minute=0,
    id='ml_training'
)
```

### 2.3 Integration with Pipeline

**File:** `src/engine/pipeline.py` (modify after intelligence generation)

```python
# Add ML prediction to intelligence
from src.intelligence.ml_predictor import TradeSuccessPredictor

ml_predictor = TradeSuccessPredictor()
ml_prediction = ml_predictor.predict({
    "symbol": symbol,
    "confidence": intel.confidence,
    "verdict_label": intel.verdict_label,
    "price_change_pct": scan_context.get("price_change_pct"),
    "pcr": scan_context.get("pcr"),
    "ce_oi_change": scan_context.get("ce_oi_change"),
    "pe_oi_change": scan_context.get("pe_oi_change"),
    "underlying": scan_context.get("underlying"),
    "support": scan_context.get("support"),
    "resistance": scan_context.get("resistance"),
    "max_pain": scan_context.get("max_pain"),
    "chart_conflict": intel.chart_conflict,
    "days_to_expiry": intel.days_to_expiry,
})

if ml_prediction:
    intel.ml_prediction = ml_prediction
    log.info(f"[ML] {symbol}: P(success) = {ml_prediction.success_probability:.1%} "
             f"(confidence: {ml_prediction.confidence_level})")
```

### 2.4 Phase 2 Deliverables

- ✅ `TradeSuccessPredictor` with XGBoost model
- ✅ Feature extraction from trade context
- ✅ SHAP explainability (top 3 factors)
- ✅ Weekly automated retraining
- ✅ Integration with intelligence pipeline
- ✅ API endpoint for predictions

**Estimated Effort:** 15-20 hours  
**Dependencies:** `pip install xgboost scikit-learn shap`  
**Impact:** Quantitative success probability with explainability

---

## 🧘 Phase 3: Behavioral Coach (Weeks 6-7)

**Goal:** Detect and prevent overtrading, revenge trading, FOMO

### 3.1 New Module: `src/intelligence/behavioral_coach.py`

```python
"""
Behavioral coaching to prevent emotional trading decisions.
Detects patterns like overtrading, revenge trading, FOMO.
"""
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)

class BehaviorType(Enum):
    OVERTRADING = "overtrading"
    REVENGE_TRADING = "revenge_trading"
    FOMO = "fomo"
    CHASING = "chasing"
    NORMAL = "normal"

@dataclass
class BehaviorAlert:
    """Alert for detected behavioral pattern."""
    behavior_type: BehaviorType
    severity: str          # "LOW", "MEDIUM", "HIGH"
    message: str
    evidence: list[str]
    recommendation: str
    
class BehavioralCoach:
    """Detects and prevents emotional trading patterns."""
    
    def __init__(self):
        self.max_trades_per_day = 3
        self.max_trades_per_hour = 1
        self.revenge_cooldown_minutes = 30
        self.fomo_confidence_threshold = 60
    
    def check_behavior(self, symbol: str, proposed_trade: dict) -> BehaviorAlert | None:
        """Check if proposed trade shows emotional patterns."""
        alerts = []
        
        # 1. Check overtrading
        overtrading_alert = self._check_overtrading(symbol)
        if overtrading_alert:
            alerts.append(overtrading_alert)
        
        # 2. Check revenge trading
        revenge_alert = self._check_revenge_trading(symbol)
        if revenge_alert:
            alerts.append(revenge_alert)
        
        # 3. Check FOMO
        fomo_alert = self._check_fomo(proposed_trade)
        if fomo_alert:
            alerts.append(fomo_alert)
        
        # Return highest severity alert
        if alerts:
            return max(alerts, key=lambda a: {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[a.severity])
        
        return None
    
    def _check_overtrading(self, symbol: str) -> BehaviorAlert | None:
        """Detect excessive trading frequency."""
        from src.models.schema import get_conn
        
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        with get_conn() as conn:
            # Trades today
            today_trades = conn.execute("""
                SELECT COUNT(*) as count
                FROM paper_trades
                WHERE DATE(opened_at) = DATE(?)
            """, (today_start.isoformat(),)).fetchone()["count"]
            
            # Trades in last hour
            hour_ago = now - timedelta(hours=1)
            recent_trades = conn.execute("""
                SELECT COUNT(*) as count
                FROM paper_trades
                WHERE opened_at >= ?
            """, (hour_ago.isoformat(),)).fetchone()["count"]
        
        if today_trades >= self.max_trades_per_day:
            return BehaviorAlert(
                behavior_type=BehaviorType.OVERTRADING,
                severity="HIGH",
                message=f"⚠️ Overtrading detected: {today_trades} trades today (max: {self.max_trades_per_day})",
                evidence=[
                    f"{today_trades} trades opened today",
                    "Exceeds daily limit of 3 trades"
                ],
                recommendation="🛑 STOP TRADING. Review today's trades tomorrow with fresh eyes."
            )
        
        if recent_trades >= self.max_trades_per_hour:
            return BehaviorAlert(
                behavior_type=BehaviorType.OVERTRADING,
                severity="MEDIUM",
                message=f"⚠️ High frequency: {recent_trades} trades in last hour",
                evidence=[
                    f"{recent_trades} trades in 60 minutes",
                    "May indicate impulsive decisions"
                ],
                recommendation="⏸️ Take a 30-minute break before next trade."
            )
        
        return None
    
    def _check_revenge_trading(self, symbol: str) -> BehaviorAlert | None:
        """Detect revenge trading after a loss."""
        from src.models.schema import get_conn
        
        now = datetime.utcnow()
        cooldown_ago = now - timedelta(minutes=self.revenge_cooldown_minutes)
        
        with get_conn() as conn:
            # Find recent losing trade
            recent_loss = conn.execute("""
                SELECT * FROM paper_trades
                WHERE status != 'OPEN'
                  AND closed_at >= ?
                  AND pnl_rupees < 0
                ORDER BY closed_at DESC
                LIMIT 1
            """, (cooldown_ago.isoformat(),)).fetchone()
        
        if recent_loss:
            loss_amount = abs(float(recent_loss["pnl_rupees"]))
            minutes_ago = (now - datetime.fromisoformat(recent_loss["closed_at"])).total_seconds() / 60
            
            return BehaviorAlert(
                behavior_type=BehaviorType.REVENGE_TRADING,
                severity="HIGH",
                message=f"⚠️ Revenge trading risk: Lost ₹{loss_amount:,.0f} {minutes_ago:.0f} minutes ago",
                evidence=[
                    f"Lost ₹{loss_amount:,.0f} on {recent_loss['symbol']}",
                    f"Only {minutes_ago:.0f} minutes since loss",
                    "Emotional state likely compromised"
                ],
                recommendation=f"🛑 WAIT {self.revenge_cooldown_minutes - minutes_ago:.0f} more minutes. "
                              "Losses are part of trading. Don't try to 'make it back' immediately."
            )
        
        return None
    
    def _check_fomo(self, proposed_trade: dict) -> BehaviorAlert | None:
        """Detect FOMO (Fear Of Missing Out) trades."""
        confidence = proposed_trade.get("confidence", 0)
        verdict = proposed_trade.get("verdict_label", "")
        
        # Low confidence but high urgency signals
        if confidence < self.fomo_confidence_threshold:
            # Check if price moved significantly (chasing)
            price_change = abs(float(proposed_trade.get("price_change_pct", 0)))
            
            if price_change > 0.5:  # >0.5% move
                return BehaviorAlert(
                    behavior_type=BehaviorType.FOMO,
                    severity="MEDIUM",
                    message=f"⚠️ FOMO alert: Low confidence ({confidence}%) with large price move ({price_change:.2f}%)",
                    evidence=[
                        f"Confidence only {confidence}%",
                        f"Price already moved {price_change:.2f}%",
                        "You may be chasing a move"
                    ],
                    recommendation="⏸️ Wait for pullback or higher confidence setup. "
                                  "Missing a trade is better than losing money."
                )
        
        return None
    
    def get_daily_summary(self) -> dict:
        """Generate daily behavioral summary."""
        from src.models.schema import get_conn
        
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        with get_conn() as conn:
            # Today's trades
            trades = conn.execute("""
                SELECT 
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(pnl_rupees) as total_pnl,
                    AVG(confidence_score) as avg_confidence
                FROM paper_trades
                WHERE opened_at >= ?
            """, (today_start.isoformat(),)).fetchone()
            
            # Longest winning streak
            streak_rows = conn.execute("""
                SELECT 
                    pnl_rupees,
                    opened_at
                FROM paper_trades
                WHERE status != 'OPEN'
                ORDER BY opened_at DESC
                LIMIT 20
            """).fetchall()
        
        if not trades or trades["total_trades"] == 0:
            return {"message": "No trades today"}
        
        win_rate = trades["wins"] / trades["total_trades"]
        
        # Calculate current streak
        current_streak = 0
        for trade in streak_rows:
            if float(trade["pnl_rupees"]) > 0:
                current_streak += 1
            else:
                break
        
        return {
            "total_trades": trades["total_trades"],
            "win_rate": win_rate,
            "total_pnl": trades["total_pnl"],
            "avg_confidence": trades["avg_confidence"],
            "current_streak": current_streak,
            "behavioral_score": self._calculate_behavioral_score(
                trades["total_trades"], win_rate, current_streak
            )
        }
    
    def _calculate_behavioral_score(self, total_trades: int, win_rate: float, streak: int) -> float:
        """Calculate behavioral discipline score (0-100)."""
        score = 100.0
        
        # Penalize overtrading
        if total_trades > 3:
            score -= (total_trades - 3) * 15
        
        # Penalize low win rate
        if win_rate < 0.4:
            score -= 30
        elif win_rate < 0.5:
            score -= 15
        
        # Penalize long losing streaks
        if streak < -3:
            score -= 20
        elif streak < -2:
            score -= 10
        
        return max(0, min(100, score))
```

### 3.2 Integration with Trade Decision

**File:** `src/engine/paper_trading.py` (modify `run_paper_trading`)

```python
# Add behavioral check before executing trade
from src.intelligence.behavioral_coach import BehavioralCoach

coach = BehavioralCoach()
behavior_alert = coach.check_behavior(symbol, {
    "confidence": confidence,
    "verdict_label": verdict,
    "price_change_pct": scan_context.get("price_change_pct")
})

if behavior_alert and behavior_alert.severity == "HIGH":
    log.warning(f"[BEHAVIOR] {symbol}: {behavior_alert.message}")
    return {
        "action": "BLOCKED_BEHAVIOR",
        "reason": behavior_alert.message,
        "recommendation": behavior_alert.recommendation
    }
```

### 3.3 Phase 3 Deliverables

- ✅ `BehavioralCoach` with 3 behavior detectors
- ✅ Overtrading prevention (daily/hourly limits)
- ✅ Revenge trading cooldown
- ✅ FOMO detection
- ✅ Daily behavioral summary
- ✅ Integration with trade execution

**Estimated Effort:** 10-12 hours  
**Impact:** Prevents emotional losses, improves discipline

---

## 📉 Phase 4: Edge Decay Monitor (Weeks 8-9)

**Goal:** Track strategy performance over time, detect when edge is weakening

### 4.1 New Module: `src/intelligence/edge_monitor.py`

```python
"""
Monitors strategy performance over time to detect edge decay.
Alerts when win rate or profitability is declining.
"""
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass

log = logging.getLogger(__name__)

@dataclass
class EdgeHealth:
    """Health status of a trading strategy."""
    strategy_name: str
    current_win_rate: float
    historical_win_rate: float
    win_rate_trend: str          # "IMPROVING", "STABLE", "DECLINING"
    pnl_trend: str               # "IMPROVING", "STABLE", "DECLINING"
    health_score: float          # 0-100
    recommendation: str

class EdgeDecayMonitor:
    """Detects when trading edge is weakening."""
    
    def __init__(self):
        self.rolling_window_days = 30
        self.historical_window_days = 90
        self.decay_threshold = 0.15  # 15% decline triggers alert
    
    def check_edge_health(self, strategy_filter: dict | None = None) -> list[EdgeHealth]:
        """Check health of all strategies or filtered subset."""
        from src.models.schema import get_conn
        
        # Build WHERE clause
        where_clause = "status != 'OPEN' AND closed_at IS NOT NULL"
        params = []
        
        if strategy_filter:
            if "symbol" in strategy_filter:
                where_clause += " AND symbol = ?"
                params.append(strategy_filter["symbol"])
            if "verdict_label" in strategy_filter:
                where_clause += " AND verdict_label = ?"
                params.append(strategy_filter["verdict_label"])
        
        with get_conn() as conn:
            # Recent performance (last 30 days)
            recent_cutoff = (datetime.utcnow() - timedelta(days=self.rolling_window_days)).isoformat()
            recent = conn.execute(f"""
                SELECT 
                    COUNT(*) as count,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(pnl_rupees) as total_pnl,
                    AVG(pnl_rupees) as avg_pnl
                FROM paper_trades
                WHERE {where_clause} AND closed_at >= ?
            """, params + [recent_cutoff]).fetchone()
            
            # Historical performance (30-90 days ago)
            hist_start = (datetime.utcnow() - timedelta(days=self.historical_window_days)).isoformat()
            hist_end = recent_cutoff
            historical = conn.execute(f"""
                SELECT 
                    COUNT(*) as count,
                    SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(pnl_rupees) as total_pnl,
                    AVG(pnl_rupees) as avg_pnl
                FROM paper_trades
                WHERE {where_clause} AND closed_at >= ? AND closed_at < ?
            """, params + [hist_start, hist_end]).fetchone()
        
        if not recent or recent["count"] < 5:
            return [EdgeHealth(
                strategy_name="All Strategies",
                current_win_rate=0,
                historical_win_rate=0,
                win_rate_trend="INSUFFICIENT_DATA",
                pnl_trend="INSUFFICIENT_DATA",
                health_score=50,
                recommendation="Not enough recent trades to assess"
            )]
        
        # Calculate metrics
        current_win_rate = recent["wins"] / recent["count"] if recent["count"] > 0 else 0
        hist_win_rate = historical["wins"] / historical["count"] if historical and historical["count"] > 0 else 0
        
        # Determine trends
        win_rate_change = current_win_rate - hist_win_rate if hist_win_rate > 0 else 0
        pnl_change = (recent["avg_pnl"] - historical["avg_pnl"]) if historical else 0
        
        win_rate_trend = self._classify_trend(win_rate_change, hist_win_rate)
        pnl_trend = self._classify_trend(pnl_change, abs(historical["avg_pnl"]) if historical else 1)
        
        # Calculate health score
        health_score = self._calculate_health_score(
            current_win_rate, hist_win_rate, recent["avg_pnl"], historical["avg_pnl"] if historical else 0
        )
        
        # Generate recommendation
        recommendation = self._generate_edge_recommendation(
            current_win_rate, hist_win_rate, recent["avg_pnl"], win_rate_trend
        )
        
        strategy_name = "All Strategies"
        if strategy_filter:
            parts = []
            if "symbol" in strategy_filter:
                parts.append(strategy_filter["symbol"])
            if "verdict_label" in strategy_filter:
                parts.append(strategy_filter["verdict_label"])
            strategy_name = " ".join(parts)
        
        return [EdgeHealth(
            strategy_name=strategy_name,
            current_win_rate=current_win_rate,
            historical_win_rate=hist_win_rate,
            win_rate_trend=win_rate_trend,
            pnl_trend=pnl_trend,
            health_score=health_score,
            recommendation=recommendation
        )]
    
    def _classify_trend(self, change: float, baseline: float) -> str:
        """Classify trend as IMPROVING, STABLE, or DECLINING."""
        if baseline == 0:
            return "STABLE"
        
        change_pct = change / baseline
        
        if change_pct > 0.10:
            return "IMPROVING"
        elif change_pct < -self.decay_threshold:
            return "DECLINING"
        else:
            return "STABLE"
    
    def _calculate_health_score(self, current_wr: float, hist_wr: float, 
                                current_pnl: float, hist_pnl: float) -> float:
        """Calculate overall health score (0-100)."""
        score = 100.0
        
        # Win rate component (40 points)
        if current_wr >= 0.70:
            score += 0
        elif current_wr >= 0.60:
            score -= 10
        elif current_wr >= 0.50:
            score -= 25
        else:
            score -= 40
        
        # Win rate trend component (30 points)
        wr_change = current_wr - hist_wr if hist_wr > 0 else 0
        if wr_change < -0.15:
            score -= 30
        elif wr_change < -0.10:
            score -= 20
        elif wr_change < -0.05:
            score -= 10
        
        # PnL trend component (30 points)
        if hist_pnl != 0:
            pnl_change_pct = (current_pnl - hist_pnl) / abs(hist_pnl)
            if pnl_change_pct < -0.30:
                score -= 30
            elif pnl_change_pct < -0.20:
                score -= 20
            elif pnl_change_pct < -0.10:
                score -= 10
        
        return max(0, min(100, score))
    
    def _generate_edge_recommendation(self, current_wr: float, hist_wr: float,
                                     avg_pnl: float, trend: str) -> str:
        """Generate actionable recommendation."""
        if trend == "DECLINING":
            return ("🔴 EDGE DECAY DETECTED - Your strategy is underperforming. "
                   "Consider: (1) Reducing position size, (2) Raising confidence threshold, "
                   "(3) Pausing this strategy for 1 week to recalibrate.")
        
        if current_wr < 0.50:
            return ("🟠 BELOW BREAKEVEN - Win rate below 50%. "
                   "Review recent trades for common mistakes. "
                   "Consider pausing until you identify the issue.")
        
        if current_wr < 0.60:
            return ("🟡 MARGINAL EDGE - Win rate is acceptable but not strong. "
                   "Look for higher confidence setups or better confluence.")
        
        if current_wr >= 0.70 and avg_pnl > 1000:
            return ("🟢 STRONG EDGE - Your strategy is performing well. "
                   "Continue with current parameters. Consider slight size increase.")
        
        return ("⚪ STABLE - Strategy is performing as expected. "
               "Monitor for changes over next 2 weeks.")
    
    def get_all_strategies_health(self) -> list[EdgeHealth]:
        """Check health of all strategy combinations."""
        from src.models.schema import get_conn
        
        # Get unique strategy combinations
        with get_conn() as conn:
            strategies = conn.execute("""
                SELECT DISTINCT symbol, verdict_label
                FROM paper_trades
                WHERE status != 'OPEN' AND closed_at IS NOT NULL
                GROUP BY symbol, verdict_label
                HAVING COUNT(*) >= 10
            """).fetchall()
        
        health_reports = []
        
        # Overall health
        health_reports.extend(self.check_edge_health())
        
        # Per-strategy health
        for strategy in strategies:
            health = self.check_edge_health({
                "symbol": strategy["symbol"],
                "verdict_label": strategy["verdict_label"]
            })
            health_reports.extend(health)
        
        return sorted(health_reports, key=lambda h: h.health_score)
```

### 4.2 Integration with Dashboard

**File:** `dashboard_server.py` (add endpoint)

```python
@app.route("/api/ai/edge-health")
def get_edge_health():
    """Get edge health for all strategies."""
    from src.intelligence.edge_monitor import EdgeDecayMonitor
    monitor = EdgeDecayMonitor()
    health = monitor.get_all_strategies_health()
    return jsonify([{
        "strategy": h.strategy_name,
        "win_rate": h.current_win_rate,
        "historical_win_rate": h.historical_win_rate,
        "win_rate_trend": h.win_rate_trend,
        "pnl_trend": h.pnl_trend,
        "health_score": h.health_score,
        "recommendation": h.recommendation
    } for h in health])
```

### 4.3 Phase 4 Deliverables

- ✅ `EdgeDecayMonitor` with health scoring
- ✅ Win rate and PnL trend detection
- ✅ Edge decay alerts
- ✅ Strategy-specific health reports
- ✅ Dashboard integration

**Estimated Effort:** 8-10 hours  
**Impact:** Prevents continued use of failing strategies

---

## 🎨 Phase 5: AI Dashboard UI (Weeks 10-12)

**Goal:** Visual interface for all AI insights

### 5.1 New Dashboard Components

**File:** `src/dashboard/ai_insights.html`

```html
<!-- AI Insights Tab -->
<div class="ai-insights-container">
    <!-- Trade DNA Match -->
    <div class="dna-match-card">
        <h3>🧬 Trade DNA Match</h3>
        <div id="dna-match-content">
            <!-- Populated dynamically -->
        </div>
    </div>
    
    <!-- Pattern Discovery -->
    <div class="patterns-card">
        <h3>📊 Top Patterns</h3>
        <div id="patterns-list">
            <!-- Populated dynamically -->
        </div>
    </div>
    
    <!-- ML Prediction -->
    <div class="ml-prediction-card">
        <h3>🤖 ML Prediction</h3>
        <div id="ml-prediction-content">
            <!-- Populated dynamically -->
        </div>
    </div>
    
    <!-- Behavioral Coach -->
    <div class="behavior-card">
        <h3>🧘 Behavioral Coach</h3>
        <div id="behavior-content">
            <!-- Populated dynamically -->
        </div>
    </div>
    
    <!-- Edge Health -->
    <div class="edge-health-card">
        <h3>📉 Edge Health Monitor</h3>
        <div id="edge-health-content">
            <!-- Populated dynamically -->
        </div>
    </div>
</div>
```

### 5.2 JavaScript Integration

**File:** `src/dashboard/theme.js` (add AI functions)

```javascript
// AI Insights Module
const AIInsights = {
    async loadDNA(symbol, verdict, confidence) {
        const response = await fetch(`/api/ai/trade-dna/${symbol}?verdict=${verdict}&confidence=${confidence}`);
        const data = await response.json();
        this.renderDNA(data);
    },
    
    renderDNA(data) {
        const container = document.getElementById('dna-match-content');
        if (!data.match_found) {
            container.innerHTML = '<p>No similar historical trades found</p>';
            return;
        }
        
        const winRate = (data.historical_win_rate * 100).toFixed(1);
        const winRateClass = data.historical_win_rate >= 0.6 ? 'success' : 
                            data.historical_win_rate >= 0.5 ? 'warning' : 'danger';
        
        container.innerHTML = `
            <div class="dna-stats">
                <div class="stat">
                    <span class="label">Similar Trades:</span>
                    <span class="value">${data.similar_trades}</span>
                </div>
                <div class="stat">
                    <span class="label">Historical Win Rate:</span>
                    <span class="value ${winRateClass}">${winRate}%</span>
                </div>
                <div class="stat">
                    <span class="label">Avg PnL:</span>
                    <span class="value">₹${data.avg_pnl.toFixed(0)}</span>
                </div>
            </div>
            <p class="confidence-note">${data.confidence_note}</p>
        `;
    },
    
    async loadPatterns() {
        const response = await fetch('/api/ai/patterns');
        const patterns = await response.json();
        this.renderPatterns(patterns);
    },
    
    renderPatterns(patterns) {
        const container = document.getElementById('patterns-list');
        container.innerHTML = patterns.slice(0, 5).map(p => `
            <div class="pattern-item">
                <div class="pattern-header">
                    <span class="pattern-name">${p.name}</span>
                    <span class="win-rate ${p.win_rate >= 0.6 ? 'success' : 'warning'}">
                        ${(p.win_rate * 100).toFixed(0)}%
                    </span>
                </div>
                <div class="pattern-details">
                    <span>${p.sample_size} trades</span>
                    <span>Avg PnL: ₹${p.avg_pnl.toFixed(0)}</span>
                </div>
                <div class="recommendation">${p.recommendation}</div>
            </div>
        `).join('');
    },
    
    async loadMLPrediction(symbol) {
        const response = await fetch(`/api/ai/ml-prediction/${symbol}`);
        const data = await response.json();
        this.renderMLPrediction(data);
    },
    
    renderMLPrediction(data) {
        const container = document.getElementById('ml-prediction-content');
        if (!data.available) {
            container.innerHTML = '<p>ML model not trained yet (needs 30+ trades)</p>';
            return;
        }
        
        const prob = (data.success_probability * 100).toFixed(1);
        const probClass = data.success_probability >= 0.6 ? 'success' : 
                         data.success_probability >= 0.5 ? 'warning' : 'danger';
        
        container.innerHTML = `
            <div class="ml-prediction">
                <div class="probability ${probClass}">${prob}%</div>
                <div class="confidence-level">Confidence: ${data.confidence_level}</div>
                <div class="top-factors">
                    <h4>Top Factors:</h4>
                    <ul>
                        ${data.top_factors.map(([name, impact]) => 
                            `<li>${name}: ${impact > 0 ? '+' : ''}${impact.toFixed(2)}</li>`
                        ).join('')}
                    </ul>
                </div>
            </div>
        `;
    },
    
    async loadEdgeHealth() {
        const response = await fetch('/api/ai/edge-health');
        const health = await response.json();
        this.renderEdgeHealth(health);
    },
    
    renderEdgeHealth(health) {
        const container = document.getElementById('edge-health-content');
        container.innerHTML = health.slice(0, 5).map(h => `
            <div class="health-item">
                <div class="health-header">
                    <span class="strategy-name">${h.strategy}</span>
                    <span class="health-score ${h.health_score >= 70 ? 'success' : h.health_score >= 50 ? 'warning' : 'danger'}">
                        ${h.health_score}/100
                    </span>
                </div>
                <div class="health-trends">
                    <span>Win Rate: ${(h.win_rate * 100).toFixed(0)}%</span>
                    <span class="trend ${h.win_rate_trend.toLowerCase()}">${h.win_rate_trend}</span>
                </div>
                <div class="recommendation">${h.recommendation}</div>
            </div>
        `).join('');
    }
};

// Auto-load AI insights when tab is opened
document.addEventListener('DOMContentLoaded', () => {
    const aiTab = document.querySelector('[data-tab="ai-insights"]');
    if (aiTab) {
        aiTab.addEventListener('click', () => {
            AIInsights.loadPatterns();
            AIInsights.loadEdgeHealth();
        });
    }
});
```

### 5.3 Phase 5 Deliverables

- ✅ AI Insights tab in dashboard
- ✅ Trade DNA visualization
- ✅ Pattern discovery heatmap
- ✅ ML prediction display with SHAP factors
- ✅ Behavioral coach alerts
- ✅ Edge health monitor
- ✅ Real-time updates via WebSocket

**Estimated Effort:** 20-25 hours  
**Impact:** Unified view of all AI insights

---

## 📊 Implementation Summary

| Phase | Deliverables | Effort | Impact | Dependencies |
|-------|-------------|--------|--------|--------------|
| **Phase 1** | History Analyzer | 8-12h | ⭐⭐⭐⭐ | None |
| **Phase 2** | ML Predictor | 15-20h | ⭐⭐⭐⭐⭐ | xgboost, sklearn, shap |
| **Phase 3** | Behavioral Coach | 10-12h | ⭐⭐⭐⭐ | None |
| **Phase 4** | Edge Monitor | 8-10h | ⭐⭐⭐ | None |
| **Phase 5** | Dashboard UI | 20-25h | ⭐⭐⭐⭐⭐ | All previous phases |

**Total Estimated Effort:** 61-79 hours (~10-12 weeks part-time)

---

## 🚀 Quick Start Guide

### Immediate Actions (This Week)

1. **Install ML dependencies:**
   ```bash
   pip install xgboost scikit-learn shap
   ```

2. **Create intelligence directory:**
   ```bash
   mkdir -p src/intelligence
   ```

3. **Start with Phase 1:**
   - Implement `TradeHistoryAnalyzer`
   - Add API endpoints
   - Test with existing trade data

4. **Train initial ML model (after 30 trades):**
   ```python
   from src.intelligence.ml_predictor import TradeSuccessPredictor
   predictor = TradeSuccessPredictor()
   predictor.train()
   ```

### Success Metrics

- **Phase 1:** Discover 5+ actionable patterns
- **Phase 2:** ML model accuracy > 65%
- **Phase 3:** Reduce overtrading incidents by 50%
- **Phase 4:** Detect edge decay 2+ weeks before major losses
- **Phase 5:** 80% dashboard adoption by users

---

## 🎯 Key Differentiators from Original Plan

1. **Builds on existing code** - No rewrites, only extensions
2. **Leverages LLM integration** - Uses `llm_enrichment.py` for narrative advice
3. **Incremental delivery** - Each phase provides immediate value
4. **Practical ML** - XGBoost (proven, fast) vs theoretical approaches
5. **Behavioral focus** - Prevents emotional losses (huge ROI)
6. **Edge monitoring** - Detects strategy decay before catastrophic losses
7. **Existing database** - Uses current schema, minimal migrations

---

## 📝 Next Steps

1. **Review this roadmap** and prioritize phases
2. **Start Phase 1** (History Analyzer) - no dependencies, immediate value
3. **Accumulate 30+ closed trades** for ML training
4. **Install ML dependencies** when ready for Phase 2
5. **Iterate based on results** - adjust thresholds, add features

---

**Document Version:** 1.0  
**Last Updated:** June 21, 2026  
**Status:** Ready for Implementation
