"""Shared verdict classification sets — single source of truth (B4 fix).

Two vocabulary layers coexist in this codebase:
  • Canonical OI/price labels  : "Long Buildup", "Short Buildup", etc.
  • LLM action labels          : "GO_LONG", "GO_SHORT", "NO_TRADE"

Both are registered here so is_bullish()/is_bearish() return correct
results regardless of which layer produced the verdict string.  All
callers (trade_decision, paper_plan, paper_trading, intelligence, …)
import only from this module — changing the sets here is sufficient.
"""

# ── OI / Price-matrix labels ───────────────────────────────────────────────
_OI_BULLISH = frozenset({
    "Long Buildup",
    "Put Writing",
    "OI Bias Bullish",
    "Short Covering",
})

_OI_BEARISH = frozenset({
    "Short Buildup",
    "Call Writing",
    "OI Bias Bearish",
    "Long Unwinding",
})

_OI_NEUTRAL = frozenset({
    "Sideways",
    "Volatility Expansion",
    "Volatility Contraction",
})

# ── LLM action labels ──────────────────────────────────────────────────────
_LLM_BULLISH = frozenset({"GO_LONG"})
_LLM_BEARISH = frozenset({"GO_SHORT"})
_LLM_NEUTRAL = frozenset({"NO_TRADE"})

# ── Public sets (union of both vocabularies) ───────────────────────────────
BULLISH_VERDICTS: frozenset[str] = _OI_BULLISH | _LLM_BULLISH
BEARISH_VERDICTS: frozenset[str] = _OI_BEARISH | _LLM_BEARISH
NEUTRAL_VERDICTS: frozenset[str] = _OI_NEUTRAL | _LLM_NEUTRAL


def is_bullish(verdict: str) -> bool:
    return str(verdict or "").strip() in BULLISH_VERDICTS


def is_bearish(verdict: str) -> bool:
    return str(verdict or "").strip() in BEARISH_VERDICTS
