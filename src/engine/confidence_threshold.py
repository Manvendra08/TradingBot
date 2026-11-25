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
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
DEFAULT_MIN_PAPER_CONFIDENCE = 55      # fallback when derivation disabled/ungated (was 65)
DEFAULT_MCX_CONFIDENCE_FLOOR = 72      # fallback MCX floor until >= 100 scored trades exist
MIN_TRADES_FOR_DERIVATION = 100       # need this many closed, scored trades before auto-derivation kicks in
WIN_RATE_FLOOR = 0.55                 # required win rate in the >=T slice
MIN_SLICE_TRADES = 10                 # a candidate slice must hold >= this many
CANDIDATE_THRESHOLDS = (55, 60, 65, 70, 75, 80, 85, 90)
MCX_CANDIDATE_THRESHOLDS = (55, 60, 65, 70, 72, 75, 80, 85)
COUNT_CACHE_TTL = 300.0               # seconds; avoid COUNT per hot-path call

DERIVED_PATH = Path("data/models/derived_confidence.json")

# Module-level cache: {threshold, n, enabled, count_ts}
_cache: dict = {"threshold": None, "n": -1, "enabled": None, "count_ts": 0.0}
_symbol_cache: dict[str, dict] = {}


def _norm_symbol(symbol: str | None) -> str | None:
    if not symbol:
        return None
    norm = str(symbol).upper().strip().split()[0]
    return norm or None


def _derived_path_for(symbol: str | None) -> Path:
    norm = _norm_symbol(symbol)
    if not norm:
        return DERIVED_PATH
    return DERIVED_PATH.parent / f"derived_confidence_{norm}.json"


def _load_history(symbol: str | None = None):
    from src.models.schema import get_conn

    norm = _norm_symbol(symbol)
    with get_conn() as conn:
        if not norm:
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

        p_rows = conn.execute(
            """
            SELECT confidence_score, pnl_rupees
            FROM paper_trades
            WHERE symbol = ?
              AND status != 'OPEN'
              AND closed_at IS NOT NULL
              AND pnl_rupees IS NOT NULL
              AND confidence_score IS NOT NULL
            """,
            (norm,),
        ).fetchall()
        ml_rows = conn.execute(
            """
            SELECT confidence_score, total_pnl
            FROM multi_leg_trades
            WHERE symbol = ?
              AND status != 'OPEN'
              AND closed_at IS NOT NULL
              AND total_pnl IS NOT NULL
              AND confidence_score IS NOT NULL
            """,
            (norm,),
        ).fetchall()
    return [
        (int(r["confidence_score"]), float(r["pnl_rupees"])) for r in p_rows
    ] + [
        (int(r["confidence_score"]), float(r["total_pnl"])) for r in ml_rows
    ]


def _count_history(symbol: str | None = None) -> int:
    from src.models.schema import get_conn

    norm = _norm_symbol(symbol)
    with get_conn() as conn:
        if not norm:
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

        p_cnt = conn.execute(
            """
            SELECT COUNT(*)
            FROM paper_trades
            WHERE symbol = ?
              AND status != 'OPEN'
              AND closed_at IS NOT NULL
              AND pnl_rupees IS NOT NULL
              AND confidence_score IS NOT NULL
            """,
            (norm,),
        ).fetchone()[0]
        ml_cnt = conn.execute(
            """
            SELECT COUNT(*)
            FROM multi_leg_trades
            WHERE symbol = ?
              AND status != 'OPEN'
              AND closed_at IS NOT NULL
              AND total_pnl IS NOT NULL
              AND confidence_score IS NOT NULL
            """,
            (norm,),
        ).fetchone()[0]
        return int(p_cnt) + int(ml_cnt)


def derive_min_confidence(
    symbol: str | None = None,
    candidate_thresholds: tuple[int, ...] | None = None,
) -> tuple[float | None, int]:
    """Return ``(threshold, n_trades)``.

    ``threshold`` is ``None`` when ungated (too few trades) or when no
    candidate threshold satisfies the quality bar.
    """
    history = _load_history(symbol=symbol)
    n = len(history)
    if n < MIN_TRADES_FOR_DERIVATION:
        return None, n

    # Guard against overfitting to a tiny high-confidence slice.
    min_slice = max(MIN_SLICE_TRADES, int(0.20 * n))
    thresholds = candidate_thresholds or CANDIDATE_THRESHOLDS

    best_t = None
    best_wr = -1.0
    best_n = -1
    for t in thresholds:
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


def _read_derived(symbol: str | None = None) -> dict:
    try:
        return json.loads(_derived_path_for(symbol).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_derived(threshold, n, symbol: str | None = None) -> None:
    path = _derived_path_for(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"threshold": threshold, "n": n}, indent=2),
        encoding="utf-8",
    )


_CACHE_LOCK = threading.Lock()


def _bucket_for(symbol: str | None) -> dict:
    norm = _norm_symbol(symbol)
    if not norm:
        return _cache
    return _symbol_cache.setdefault(
        norm, {"threshold": None, "n": -1, "enabled": None, "count_ts": 0.0}
    )


def _cached_count(symbol: str | None = None) -> int:
    with _CACHE_LOCK:
        bucket = _bucket_for(symbol)
        now = time.time()
        if now - bucket.get("count_ts", 0.0) > COUNT_CACHE_TTL or bucket.get("n", -1) < 0:
            bucket["n"] = _count_history(symbol=symbol)
            bucket["count_ts"] = now
        return bucket["n"]


def get_effective_min_confidence(
    symbol: str | None = None,
    default_floor: float | None = None,
    candidate_thresholds: tuple[int, ...] | None = None,
) -> float:
    """Return the confidence floor for ``build_paper_trade_plan`` (global or per-symbol).

    * Flag ``derive_min_confidence`` False AND ``< 100`` scored trades ->
      fallback default.
    * Flag False but ``>= 100`` scored trades -> **auto-promote**: the
      flag is flipped to ``True`` in ``runtime_config.json`` (persisted),
      then derivation runs. This lets the bot switch to data-driven gating
      automatically once enough history exists.
    * Flag True but ``< 100`` scored trades -> fallback default (avoid overfitting).
    * Otherwise derive the best qualifying threshold (cached, refreshed
      when trade count grows), falling back to default if none qualifies.
    """
    from config.runtime_config import load_runtime_config, save_runtime_config

    fallback = float(DEFAULT_MIN_PAPER_CONFIDENCE if default_floor is None else default_floor)
    config = load_runtime_config()
    enabled = bool(config.get("derive_min_confidence", False))

    n = _cached_count(symbol=symbol)
    bucket = _bucket_for(symbol)

    # Auto-promote: once enough scored trades accumulate, turn the feature on.
    if not enabled:
        if n >= MIN_TRADES_FOR_DERIVATION:
            if symbol is None:
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
            return fallback

    if n < MIN_TRADES_FOR_DERIVATION:
        return fallback

    # Reuse cache if trade count unchanged and a threshold is stored.
    if (
        bucket.get("enabled") is True
        and bucket.get("n") == n
        and bucket.get("threshold") is not None
    ):
        return float(bucket["threshold"])

    # Persisted value matches current sample size?
    derived = _read_derived(symbol=symbol)
    if derived.get("n") == n and derived.get("threshold") is not None:
        thr = float(derived["threshold"])
        bucket.update(threshold=thr, n=n, enabled=True)
        return thr

    thr, _ = derive_min_confidence(symbol=symbol, candidate_thresholds=candidate_thresholds)
    if thr is None:
        log.warning(
            "Confidence-threshold derivation (%s) found no qualifying cutoff at n=%d; "
            "using default %.0f.",
            _norm_symbol(symbol) or "global",
            n,
            fallback,
        )
        bucket.update(threshold=fallback, n=n, enabled=True)
        return fallback

    _write_derived(thr, n, symbol=symbol)
    bucket.update(threshold=float(thr), n=n, enabled=True)
    log.info(
        "Derived confidence floor (%s)=%.0f from %d historical trades.",
        _norm_symbol(symbol) or "global",
        thr,
        n,
    )
    return float(thr)


def get_effective_mcx_confidence_floor(
    symbol: str | None = None,
    default_floor: int = DEFAULT_MCX_CONFIDENCE_FLOOR,
) -> int:
    """Return derived per-symbol MCX confidence floor, falling back to ``default_floor`` (72)."""
    try:
        eff = get_effective_min_confidence(
            symbol=symbol,
            default_floor=float(default_floor),
            candidate_thresholds=MCX_CANDIDATE_THRESHOLDS,
        )
        return int(round(eff))
    except Exception as exc:
        log.debug("get_effective_mcx_confidence_floor(%s) fallback to %d: %s", symbol, default_floor, exc)
        return int(default_floor)


def refresh_derived_confidence(symbol: str | None = None) -> float:
    """Force recompute (call on trade close). Returns effective threshold."""
    with _CACHE_LOCK:
        bucket = _bucket_for(symbol)
        bucket["count_ts"] = 0.0  # invalidate count + threshold cache
        bucket["n"] = -1
    return get_effective_min_confidence(symbol=symbol)
