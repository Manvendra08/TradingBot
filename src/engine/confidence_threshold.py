"""
Data-driven derivation of the paper-plan confidence floor.

The paper-plan confidence floor was historically a hardcoded magic number
(``MIN_PAPER_CONFIDENCE = 65`` in ``paper_plan.py``) with no empirical basis.
This module derives it from historical closed, scored trades instead.

Design
-------
* **OPTIONAL** — gated by the runtime flag ``derive_min_confidence`` (default
  ``False``). Off by default so behaviour is unchanged unless explicitly enabled.
* **GUARDED** — derivation only runs once ``>= MIN_TRADES_FOR_DERIVATION``
  (50) closed, scored trades exist. Below that the hardcoded default is used;
  this prevents overfitting a cutoff to a tiny / noisy sample.
* **METHOD** — over candidate thresholds, choose the one that *maximises* the
  win rate of the ``conf >= T`` slice (the point where the edge actually
  begins), subject to guards:
    - slice win rate ``>= WIN_RATE_FLOOR``
    - slice average PnL ``> 0``
    - slice size ``>= max(MIN_SLICE_TRADES, 20% of all scored trades)``
      (prevents overfitting to a tiny high-confidence slice)
  Tie-break: larger slice first, then higher threshold. If no candidate
  qualifies, fall back to the default.
  (A naive "lowest T meeting the bar" is wrong: when winners dominate,
  the cumulative ``>=T`` slice stays positive at low T and the bad
  low-confidence region is never excluded.)
* **PERSISTENCE** — the derived value + sample count are cached to
  ``data/models/derived_confidence.json`` so they survive restarts and are only
  recomputed when the trade count grows.

Why require BOTH win rate and avg PnL: win rate is the low-variance primary
signal; average PnL is dominated by a few lottery winners/losers (e.g. the
NG Parity bucket), so requiring both avoids picking a threshold that only looks
good on outliers.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
DEFAULT_MIN_PAPER_CONFIDENCE = 65      # fallback when derivation disabled/ungated
MIN_TRADES_FOR_DERIVATION = 50        # need this many closed, scored trades
WIN_RATE_FLOOR = 0.55                 # required win rate in the >=T slice
MIN_SLICE_TRADES = 10                 # a candidate slice must hold >= this many
CANDIDATE_THRESHOLDS = (55, 60, 65, 70, 75, 80, 85, 90)
COUNT_CACHE_TTL = 300.0               # seconds; avoid COUNT per hot-path call

DERIVED_PATH = Path("data/models/derived_confidence.json")

# Module-level cache: {threshold, n, enabled, count_ts}
_cache: dict = {"threshold": None, "n": -1, "enabled": None, "count_ts": 0.0}


def _load_history():
    from src.models.schema import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT confidence_score, pnl_rupees
            FROM paper_trades
            WHERE status != 'OPEN'
              AND closed_at IS NOT NULL
              AND pnl_rupees IS NOT NULL
              AND confidence_score IS NOT NULL
            """
        ).fetchall()
    return [(int(r["confidence_score"]), float(r["pnl_rupees"])) for r in rows]


def _count_history() -> int:
    from src.models.schema import get_conn

    with get_conn() as conn:
        return conn.execute(
            """
            SELECT COUNT(*)
            FROM paper_trades
            WHERE status != 'OPEN'
              AND closed_at IS NOT NULL
              AND pnl_rupees IS NOT NULL
              AND confidence_score IS NOT NULL
            """
        ).fetchone()[0]


def derive_min_confidence() -> tuple[float | None, int]:
    """Return ``(threshold, n_trades)``.

    ``threshold`` is ``None`` when ungated (too few trades) or when no
    candidate threshold satisfies the quality bar.
    """
    history = _load_history()
    n = len(history)
    if n < MIN_TRADES_FOR_DERIVATION:
        return None, n

    # Guard against overfitting to a tiny high-confidence slice.
    min_slice = max(MIN_SLICE_TRADES, int(0.20 * n))

    best_t = None
    best_wr = -1.0
    best_n = -1
    for t in CANDIDATE_THRESHOLDS:
        slice_ = [p for cs, p in history if cs >= t]
        s_n = len(slice_)
        if s_n < min_slice:
            continue
        wins = sum(1 for p in slice_ if p > 0)
        wr = wins / s_n
        avg = sum(slice_) / s_n
        if wr < WIN_RATE_FLOOR or avg <= 0:
            continue
        # Maximize win rate; tie-break by larger slice, then higher threshold.
        if (wr > best_wr + 1e-9) or (abs(wr - best_wr) <= 1e-9 and s_n > best_n):
            best_wr, best_n, best_t = wr, s_n, t

    return best_t, n


def _read_derived() -> dict:
    try:
        return json.loads(DERIVED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_derived(threshold, n) -> None:
    DERIVED_PATH.parent.mkdir(parents=True, exist_ok=True)
    DERIVED_PATH.write_text(
        json.dumps({"threshold": threshold, "n": n}, indent=2),
        encoding="utf-8",
    )


def _cached_count() -> int:
    now = time.time()
    if now - _cache.get("count_ts", 0.0) > COUNT_CACHE_TTL or _cache.get("n", -1) < 0:
        _cache["n"] = _count_history()
        _cache["count_ts"] = now
    return _cache["n"]


def get_effective_min_confidence() -> float:
    """Return the confidence floor for ``build_paper_trade_plan``.

    * Flag ``derive_min_confidence`` False AND ``< 50`` scored trades ->
      hardcoded default.
    * Flag False but ``>= 50`` scored trades -> **auto-promote**: the
      flag is flipped to ``True`` in ``runtime_config.json`` (persisted),
      then derivation runs. This lets the bot switch to data-driven gating
      automatically once enough history exists.
    * Flag True but ``< 50`` scored trades -> default (avoid overfitting).
    * Otherwise derive the best qualifying threshold (cached, refreshed
      when trade count grows), falling back to default if none qualifies.
    """
    from config.runtime_config import load_runtime_config, save_runtime_config

    config = load_runtime_config()
    enabled = bool(config.get("derive_min_confidence", False))

    n = _cached_count()

    # Auto-promote: once enough scored trades accumulate, turn the feature on.
    if not enabled:
        if n >= MIN_TRADES_FOR_DERIVATION:
            config["derive_min_confidence"] = True
            try:
                save_runtime_config(config)
                log.info(
                    "Auto-enabled derive_min_confidence at n=%d scored trades.", n
                )
            except Exception as e:
                log.warning("Auto-enable derive_min_confidence failed: %s", e)
            enabled = True
        else:
            return float(DEFAULT_MIN_PAPER_CONFIDENCE)

    if n < MIN_TRADES_FOR_DERIVATION:
        return float(DEFAULT_MIN_PAPER_CONFIDENCE)

    # Reuse cache if trade count unchanged and a threshold is stored.
    if (
        _cache.get("enabled") is True
        and _cache.get("n") == n
        and _cache.get("threshold") is not None
    ):
        return float(_cache["threshold"])

    # Persisted value matches current sample size?
    derived = _read_derived()
    if derived.get("n") == n and derived.get("threshold") is not None:
        thr = float(derived["threshold"])
        _cache.update(threshold=thr, n=n, enabled=True)
        return thr

    thr, _ = derive_min_confidence()
    if thr is None:
        log.warning(
            "Confidence-threshold derivation found no qualifying cutoff at n=%d; "
            "using default %.0f.",
            n,
            DEFAULT_MIN_PAPER_CONFIDENCE,
        )
        _cache.update(
            threshold=float(DEFAULT_MIN_PAPER_CONFIDENCE), n=n, enabled=True
        )
        return float(DEFAULT_MIN_PAPER_CONFIDENCE)

    _write_derived(thr, n)
    _cache.update(threshold=float(thr), n=n, enabled=True)
    log.info("Derived MIN_PAPER_CONFIDENCE=%.0f from %d historical trades.", thr, n)
    return float(thr)


def refresh_derived_confidence() -> float:
    """Force recompute (call on trade close). Returns effective threshold."""
    _cache["count_ts"] = 0.0  # invalidate count + threshold cache
    _cache["n"] = -1
    return get_effective_min_confidence()
