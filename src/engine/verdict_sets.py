"""Shared verdict classification sets — single source of truth (B4 fix)."""

BULLISH_VERDICTS = frozenset({
    "Long Buildup",
    "Put Writing",
    "OI Bias Bullish",
    "Short Covering",
})

BEARISH_VERDICTS = frozenset({
    "Short Buildup",
    "Call Writing",
    "OI Bias Bearish",
    "Long Unwinding",
})

NEUTRAL_VERDICTS = frozenset({
    "Sideways",
    "Volatility Expansion",
    "Volatility Contraction",
})


def is_bullish(verdict: str) -> bool:
    return str(verdict or "").strip() in BULLISH_VERDICTS


def is_bearish(verdict: str) -> bool:
    return str(verdict or "").strip() in BEARISH_VERDICTS
