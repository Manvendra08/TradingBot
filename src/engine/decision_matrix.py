"""
Pipeline Decision Matrix (PDM) v1.0
====================================
Consolidates all signal inputs produced during a pipeline scan cycle into a
single, auditable GO / NO-GO decision with a composite confidence score.

Signal dimensions scored
------------------------
1. OI Anomalies      — count + severity of detected anomalies
2. PCR Bias          — Put-Call Ratio directional pressure
3. Chart Sentiment   — 1h + 3h OHLC sentiment from chart_fetcher
4. Market Regime     — regime_detector output (BULLISH / BEARISH / CHOPPY / UNKNOWN)
5. ML Prediction     — P(success) from ml_predictor singleton
6. LLM Verdict       — AI action + confidence from llm_enrichment
7. Trade DNA         — historical win-rate for similar setups

Scoring
-------
Each dimension returns a score in [-1, +1]:
  +1 = strongly supports GO_LONG
  -1 = strongly supports GO_SHORT
   0 = neutral / no signal

A weighted composite is computed; absolute value gives overall signal strength.
Direction is derived from sign of composite.

Gating
------
A trade gate blocks execution when composite strength < GATE_THRESHOLD (0.40)
or when the regime is CHOPPY/UNKNOWN and strength < CHOPPY_GATE_THRESHOLD (0.55).

Integration
-----------
Call evaluate() after LLM verdict and ML prediction are resolved inside
_process_symbol_inner().  Inject result into scan_context and intel dict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ── Configurable thresholds ───────────────────────────────────────────────────
GATE_THRESHOLD = 0.40          # minimum composite strength to pass gate
CHOPPY_GATE_THRESHOLD = 0.55   # stricter gate when regime is CHOPPY / UNKNOWN

# ── Signal weights (must sum to 1.0) ─────────────────────────────────────────
WEIGHTS: dict[str, float] = {
    "oi_anomalies":   0.20,
    "pcr_bias":       0.15,
    "chart_sentiment":0.15,
    "market_regime":  0.10,
    "ml_prediction":  0.20,
    "llm_verdict":    0.15,
    "trade_dna":      0.05,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-6, "PDM weights must sum to 1.0"


@dataclass
class SignalScore:
    name: str
    raw_score: float           # [-1, +1]
    weight: float
    weighted_score: float
    detail: str = ""


@dataclass
class DecisionResult:
    direction: str             # "LONG" | "SHORT" | "NEUTRAL"
    composite_score: float     # weighted sum [-1, +1]
    strength: float            # abs(composite_score) [0, 1]
    confidence_band: str       # "LOW" | "MEDIUM" | "HIGH"
    gate_pass: bool            # True = proceed, False = hold
    gate_reason: str           # human-readable gate explanation
    signals: list[SignalScore] = field(default_factory=list)
    telegram_block: str = ""   # pre-formatted Telegram section


# ── Individual scorers ────────────────────────────────────────────────────────

def _score_oi_anomalies(new_alerts: list[dict]) -> tuple[float, str]:
    """
    Score based on count and severity of new (non-deduped) anomalies.
    Multiple HIGH-severity bearish/bullish OI anomalies push score ± 1.
    """
    if not new_alerts:
        return 0.0, "no anomalies"

    _sev_w = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.2}
    _dir_w = {"BULLISH": +1.0, "BEARISH": -1.0, "NEUTRAL": 0.0}

    total_weight = 0.0
    directional_sum = 0.0
    for a in new_alerts:
        sev = str(a.get("severity") or "LOW").upper()
        bias = str(a.get("bias") or a.get("direction") or "NEUTRAL").upper()
        w = _sev_w.get(sev, 0.2)
        d = _dir_w.get(bias, 0.0)
        total_weight += w
        directional_sum += w * d

    if total_weight == 0:
        return 0.0, "no weighted anomalies"

    raw = max(-1.0, min(1.0, directional_sum / total_weight))
    detail = f"{len(new_alerts)} anomaly(s), net_dir={raw:+.2f}"
    return raw, detail


def _score_pcr(scan_context: dict) -> tuple[float, str]:
    """
    PCR < 0.7  → bullish pressure (+)
    PCR 0.7–1.3 → neutral
    PCR > 1.3  → bearish pressure (−)
    """
    pcr = scan_context.get("pcr")
    if pcr is None:
        return 0.0, "pcr unavailable"
    try:
        pcr = float(pcr)
    except (TypeError, ValueError):
        return 0.0, "pcr parse error"

    if pcr < 0.7:
        score = min(1.0, (0.7 - pcr) / 0.5)   # scale 0.7→0 maps to 0→+1
        detail = f"PCR={pcr:.2f} (bullish pressure)"
        return round(score, 3), detail
    elif pcr > 1.3:
        score = -min(1.0, (pcr - 1.3) / 0.7)  # scale 1.3→2.0 maps to 0→-1
        detail = f"PCR={pcr:.2f} (bearish pressure)"
        return round(score, 3), detail
    else:
        return 0.0, f"PCR={pcr:.2f} (neutral zone)"


def _score_chart(scan_context: dict) -> tuple[float, str]:
    """
    Blend 1h and 3h chart sentiments.
    BULLISH=+1, BEARISH=-1, NEUTRAL=0.
    Weights: 3h carries more structural weight (0.6) vs 1h (0.4).
    """
    chart = scan_context.get("chart_indicators") or {}
    # chart_indicators is keyed by base_symbol; grab first available symbol
    symbol_data: dict = {}
    for v in chart.values():
        if isinstance(v, dict):
            symbol_data = v
            break

    _sent_val = {"BULLISH": 1.0, "BEARISH": -1.0, "NEUTRAL": 0.0}
    tf_weights = {"1h": 0.4, "3h": 0.6}
    composite = 0.0
    parts = []
    total_w = 0.0
    for tf, tw in tf_weights.items():
        tf_data = symbol_data.get(tf)
        if not tf_data:
            continue
        sent = str(tf_data.get("sentiment") or "NEUTRAL").upper()
        val = _sent_val.get(sent, 0.0)
        composite += tw * val
        total_w += tw
        parts.append(f"{tf}:{sent}")

    if total_w == 0:
        return 0.0, "chart data unavailable"

    raw = max(-1.0, min(1.0, composite / total_w))
    detail = " | ".join(parts) + f" → {raw:+.2f}"
    return round(raw, 3), detail


def _score_regime(scan_context: dict) -> tuple[float, str]:
    """
    Market regime from regime_detector or scan_context['market_regime'].
    BULLISH=+0.8, BEARISH=-0.8, TRENDING_UP=+0.5, TRENDING_DOWN=-0.5,
    CHOPPY/UNKNOWN = 0 (but triggers stricter gate downstream).
    """
    regime = (
        scan_context.get("market_regime")
        or scan_context.get("regime")
        or ""
    )
    regime = str(regime).upper()

    _regime_map = {
        "BULLISH":       +0.8,
        "TRENDING_UP":   +0.5,
        "SIDEWAYS_UP":   +0.3,
        "NEUTRAL":        0.0,
        "SIDEWAYS":       0.0,
        "CHOPPY":         0.0,
        "UNKNOWN":        0.0,
        "SIDEWAYS_DOWN": -0.3,
        "TRENDING_DOWN": -0.5,
        "BEARISH":       -0.8,
    }
    score = 0.0
    for key, val in _regime_map.items():
        if key in regime:
            score = val
            break

    return round(score, 3), f"regime={regime or 'UNKNOWN'}"


def _score_ml(intel: dict | None) -> tuple[float, str]:
    """
    ML predictor P(success) → score.
    p > 0.65 → positive (directional from verdict_label)
    p < 0.40 → negative signal
    0.40–0.65 → weak/neutral
    """
    if not intel:
        return 0.0, "intel unavailable"

    ml = intel.get("ml_prediction")
    if not ml:
        return 0.0, "ml_prediction unavailable"

    prob = float(ml.get("success_probability") or 0.0)
    label = str(intel.get("verdict_label") or "NEUTRAL").upper()
    _label_dir = {"BULLISH": +1, "BEARISH": -1, "NEUTRAL": 0}
    direction = _label_dir.get(label, 0) or 1  # default +1 if label unknown

    if prob >= 0.65:
        score = direction * min(1.0, (prob - 0.65) / 0.25 + 0.5)
    elif prob <= 0.40:
        score = -direction * min(1.0, (0.40 - prob) / 0.30 + 0.2)
    else:
        score = direction * (prob - 0.525) / 0.125 * 0.3  # weak linear in neutral band

    detail = f"P(success)={prob:.0%}, label={label}, score={score:+.2f}"
    return round(max(-1.0, min(1.0, score)), 3), detail


def _score_llm(llm_verdict: Any) -> tuple[float, str]:
    """
    LLM verdict action + confidence → score.
    GO_LONG at 90% confidence → +1
    NO_TRADE → 0
    GO_SHORT at 90% confidence → -1
    """
    if llm_verdict is None:
        return 0.0, "llm_verdict unavailable"

    def _gv(key, default=None):
        if isinstance(llm_verdict, dict):
            return llm_verdict.get(key, default)
        return getattr(llm_verdict, key, default)

    action = str(_gv("action") or "NO_TRADE").upper()
    confidence = float(_gv("confidence") or 0)
    conf_norm = confidence / 100.0  # 0–1

    _action_dir = {"GO_LONG": +1, "GO_SHORT": -1, "NO_TRADE": 0}
    direction = _action_dir.get(action, 0)
    score = direction * conf_norm
    detail = f"LLM:{action} conf={confidence:.0f}% → {score:+.2f}"
    return round(max(-1.0, min(1.0, score)), 3), detail


def _score_trade_dna(intel: dict | None) -> tuple[float, str]:
    """
    Historical win-rate from Trade DNA match.
    WR > 0.65 → modest positive boost
    WR < 0.40 → modest negative
    """
    if not intel:
        return 0.0, "intel unavailable"

    dna = intel.get("trade_dna")
    if not dna or not dna.get("match_found"):
        return 0.0, "no DNA match"

    wr = float(dna.get("historical_win_rate") or 0.0)
    n = int(dna.get("similar_trades") or 0)
    if n < 5:
        return 0.0, f"DNA match found but n={n} (too few trades)"

    if wr >= 0.65:
        score = min(1.0, (wr - 0.65) / 0.25 + 0.3)
    elif wr <= 0.40:
        score = -min(1.0, (0.40 - wr) / 0.30 + 0.2)
    else:
        score = (wr - 0.525) / 0.125 * 0.2

    detail = f"DNA WR={wr:.0%} n={n} → {score:+.2f}"
    return round(max(-1.0, min(1.0, score)), 3), detail


# ── Main evaluator ────────────────────────────────────────────────────────────

def evaluate(
    symbol: str,
    new_alerts: list[dict],
    scan_context: dict,
    intel: dict | None,
    llm_verdict: Any,
) -> DecisionResult:
    """
    Run all signal scorers, apply weights, compute composite, and determine
    gate pass/fail.

    Returns a DecisionResult with full breakdown and a Telegram-formatted block.
    """
    scorers = [
        ("oi_anomalies",    lambda: _score_oi_anomalies(new_alerts)),
        ("pcr_bias",        lambda: _score_pcr(scan_context)),
        ("chart_sentiment", lambda: _score_chart(scan_context)),
        ("market_regime",   lambda: _score_regime(scan_context)),
        ("ml_prediction",   lambda: _score_ml(intel)),
        ("llm_verdict",     lambda: _score_llm(llm_verdict)),
        ("trade_dna",       lambda: _score_trade_dna(intel)),
    ]

    signals: list[SignalScore] = []
    composite = 0.0

    for name, scorer in scorers:
        w = WEIGHTS[name]
        try:
            raw, detail = scorer()
        except Exception as exc:
            log.debug("PDM scorer '%s' failed: %s", name, exc)
            raw, detail = 0.0, f"scorer error: {exc}"

        weighted = w * raw
        composite += weighted
        signals.append(SignalScore(
            name=name,
            raw_score=round(raw, 3),
            weight=w,
            weighted_score=round(weighted, 4),
            detail=detail,
        ))

    composite = round(max(-1.0, min(1.0, composite)), 4)
    strength = round(abs(composite), 4)

    # Direction
    if composite > 0.05:
        direction = "LONG"
    elif composite < -0.05:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    # Confidence band
    if strength >= 0.65:
        confidence_band = "HIGH"
    elif strength >= 0.40:
        confidence_band = "MEDIUM"
    else:
        confidence_band = "LOW"

    # Gate check
    regime = str(
        scan_context.get("market_regime") or scan_context.get("regime") or ""
    ).upper()
    is_choppy = any(r in regime for r in ("CHOPPY", "UNKNOWN", ""))
    effective_threshold = CHOPPY_GATE_THRESHOLD if is_choppy else GATE_THRESHOLD

    gate_pass = strength >= effective_threshold
    if gate_pass:
        gate_reason = f"strength={strength:.2f} ≥ threshold={effective_threshold:.2f} → PASS"
    else:
        gate_reason = (
            f"strength={strength:.2f} < threshold={effective_threshold:.2f} "
            f"({'choppy/unknown regime' if is_choppy else 'standard gate'}) → HOLD"
        )

    log.info(
        "[PDM] %s | dir=%s strength=%.2f band=%s gate=%s | %s",
        symbol, direction, strength, confidence_band,
        "PASS" if gate_pass else "HOLD", gate_reason,
    )

    # ── Telegram block ────────────────────────────────────────────────────────
    gate_icon = "✅" if gate_pass else "🚫"
    dir_icon = {"LONG": "🟢", "SHORT": "🔴", "NEUTRAL": "⚪"}.get(direction, "❓")
    band_icon = {"HIGH": "🔥", "MEDIUM": "🟡", "LOW": "🔵"}.get(confidence_band, "❓")

    bar_len = 10
    filled = round(strength * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)

    lines = [
        f"\n📊 *Decision Matrix* {gate_icon}",
        f"{dir_icon} Direction: *{direction}* | {band_icon} Band: *{confidence_band}*",
        f"Composite: `[{bar}]` {strength:.0%}",
        f"Gate: _{gate_reason}_",
        "\n_Signal breakdown:_",
    ]

    _name_label = {
        "oi_anomalies":    "OI Anomalies",
        "pcr_bias":        "PCR Bias",
        "chart_sentiment": "Chart",
        "market_regime":   "Regime",
        "ml_prediction":   "ML Pred",
        "llm_verdict":     "LLM",
        "trade_dna":       "Trade DNA",
    }
    for s in signals:
        icon = "🟢" if s.raw_score > 0.1 else ("🔴" if s.raw_score < -0.1 else "⚪")
        label = _name_label.get(s.name, s.name)
        lines.append(f"  {icon} {label}: `{s.raw_score:+.2f}` — _{s.detail}_")

    telegram_block = "\n".join(lines)

    return DecisionResult(
        direction=direction,
        composite_score=composite,
        strength=strength,
        confidence_band=confidence_band,
        gate_pass=gate_pass,
        gate_reason=gate_reason,
        signals=signals,
        telegram_block=telegram_block,
    )
